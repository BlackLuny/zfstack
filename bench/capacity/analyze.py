#!/usr/bin/env python3
"""Recompute screening summaries; failed arrivals never become valid latency wins."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import statistics


def load(path):
    return json.loads(Path(path).read_text())


def quantile(xs, p):
    return sorted(xs)[math.ceil(len(xs) * p) - 1] if xs else None


def request_stats(observations):
    completed = [r for r in observations if r["outcome"] == "completed"]
    latencies = [(r["completed_ns"] - r["planned_ns"]) / 1e6 for r in completed]
    complete = bool(observations) and len(completed) == len(observations)
    return dict(planned=len(observations), completed=len(completed), missing=len(observations) - len(completed),
                mean_ms=statistics.mean(latencies) if complete else None,
                p50_ms=quantile(latencies, .50) if complete else None,
                p95_ms=quantile(latencies, .95) if complete else None,
                p99_ms=quantile(latencies, .99) if complete and len(latencies) >= 100 else None,
                max_observed_ms=max(latencies, default=None),
                late_completed=sum(t > 3000 for t in latencies),
                observed_mean_ms=statistics.mean(latencies) if latencies else None)


def verify_arrivals(streams, results, expected_loaded=250, expected_baseline=50):
    errors, users = [], []
    loaded = results.get("loaded_users", [])
    if len(streams) != 6 or len(loaded) != 3:
        errors.append("expected three baseline and three loaded streams")
    for s in streams:
        rows = s["arrivals"]
        if len(rows) != s["count"] or s["hz"] != 25 or s["period_ns"] != 40_000_000:
            errors.append("arrival plan counts or period mismatch")
        if s["count"] not in (expected_baseline, expected_loaded):
            errors.append("unexpected arrival cohort size")
        for i, r in enumerate(rows):
            if r["seq"] != i or r["planned_ns"] != 5_000_000 + i * 40_000_000:
                errors.append("sequence or planned time mismatch")
            t = r.get("started_ns")
            if t is None or t < r["planned_ns"] or t - r["planned_ns"] > 20_000_000:
                errors.append("invalid generator timing")
            if r["outcome"] != "completed":
                errors.append("unfinished scheduled request")
            elif r["completed_ns"] < t or r["completed_ns"] > r["planned_ns"] + 3_000_000_000:
                errors.append("invalid completion time or deadline exceeded")
        if s["count"] == expected_loaded:
            peer = int(s["local"].split(":")[0].split(".")[2])
            u = dict(peer=peer, **request_stats(rows))
            users.append(u)
            matching = [x for x in loaded if x["peer"] == peer]
            observed = sorted((r["completed_ns"] - r["planned_ns"]) / 1e6 for r in rows if r["outcome"] == "completed")
            raw = sorted(matching[0]["samples"].get("raw_ms", [])) if len(matching) == 1 else []
            if len(raw) != len(observed) or any(abs(a-b) > 1e-6 for a, b in zip(raw, observed)):
                errors.append("raw RR samples disagree with independent arrival ledger")
    if len({u["peer"] for u in users}) != 3:
        errors.append("missing or duplicate loaded user")
    return users, sorted(set(errors))


def cgroup_delta(start, end, field):
    def parse(x):
        return dict(line.split() for line in x["cgroup"]["cpu.stat"].splitlines())
    return int(parse(end).get(field, 0)) - int(parse(start).get(field, 0))


def summarize_run(path, expected_loaded=250):
    start, end = load(path / "start.json"), load(path / "end.json")
    out = dict(name=path.name, **start["row"], returncode=end["returncode"], valid=False,
               throttled_usec=cgroup_delta(start, end, "throttled_usec"),
               nr_throttled=cgroup_delta(start, end, "nr_throttled"))
    try:
        r = load(path / "stdout.json")
        resources = load(path / "resources.json")
    except (OSError, ValueError) as e:
        return dict(out, errors=[str(e)])
    errors = list(r.get("errors", []))
    if end["returncode"] != 0 or not r.get("ok") or not end["binary_unchanged"]:
        errors.append("process or binary identity check failed")
    requested = dict(max_snd_buf_bytes=int(start["env"]["ZFBENCH_MAX_SND_BUF"]) if "ZFBENCH_MAX_SND_BUF" in start["env"] else None,
                     prefetch_max_bytes=int(start["env"]["ZFBENCH_PREFETCH_MAX"]) if "ZFBENCH_PREFETCH_MAX" in start["env"] else None)
    if r.get("tx_tuning") != requested:
        errors.append("result configuration differs from frozen arm")
    rr = r["results"]
    cpu = r["cpu"]
    size = cpu["window_effective_bytes"] / 1e9
    wall = cpu["window_wall_sec"]
    if not size > 0 or not wall > 0:
        return dict(out, errors=errors + ["nonpositive work or measurement window"])
    if r.get("mode") == "client":
        cost = r["proxy"]["cpu_window_sec"]
        out.update(mbps=rr["goodput_mbps_window"], service_cpu_s_per_gb=cost / size,
                   service_cpu_s_per_s=cost / wall, rss_hwm_kib=r["proxy"]["hwm_kb"],
                   zero_progress_seconds=[f["zero_secs"] for f in rr["per_flow"]])
    else:
        cost = cpu["stack_thread_window_sec"]
        out.update(mbps=rr["heavy"]["goodput_mbps_window"], service_cpu_s_per_gb=cost / size,
                   service_cpu_s_per_s=cost / wall, process_cpu_s_per_gb=cpu["process_window_sec"] / size,
                   rss_hwm_kib=resources["max_rss_kib"],
                   bottleneck_drops={k:r["link"][k]["bottleneck_drops"] for k in ("up", "down")})
        try:
            users, faults = verify_arrivals(load(path / "arrivals.json"), rr, expected_loaded=expected_loaded)
            errors.extend(faults)
            out["users"] = users
            means = [u["mean_ms"] for u in users]
            tails = [u["p99_ms"] for u in users]
            out["user_equal_mean_ms"] = statistics.mean(means) if len(means) == 3 and all(x is not None for x in means) else None
            out["worst_user_p99_ms"] = max(tails) if len(tails) == 3 and all(x is not None for x in tails) else None
        except (OSError, ValueError, KeyError, TypeError) as e:
            errors.append(f"arrival validation: {e}")
    return dict(out, valid=not errors, errors=errors)


def analyze(root):
    plan = load(root / "plan.json")
    rows = [summarize_run(p) for p in sorted(root.glob("[0-9][0-9][0-9]-*")) if (p / "end.json").exists()]
    groups = defaultdict(list)
    for row in rows:
        groups[row["case"], row["block"]].append(row)
    comparisons = []
    keys = ["mbps", "service_cpu_s_per_gb", "service_cpu_s_per_s", "rss_hwm_kib", "user_equal_mean_ms", "worst_user_p99_ms"]
    for (case, block), group in sorted(groups.items()):
        group.sort(key=lambda r:r["position"])
        complete = len(group) == (2 if block == "AA" else 4)
        valid = complete and all(r["valid"] for r in group)
        pairs = [(group[0], group[1])] if block == "AA" and complete else [(group[0], group[1]), (group[3], group[2])] if complete else []
        changes = {}
        for key in keys:
            values = [100*(b[key]/a[key]-1) for a,b in pairs if a.get(key) is not None and b.get(key) is not None and a[key] > 0]
            if values:
                changes[key] = dict(paired_pct=values, median_pct=statistics.median(values))
        reject = valid and block != "AA" and changes.get("mbps", {}).get("median_pct", 0) < -3
        comparisons.append(dict(case=case, block=block, valid=valid, changes_descriptive=changes,
                                decision="reject_universal_default_throughput" if reject else "inconclusive"))
    return dict(planned=len(plan["rows"]), completed=len(rows), valid=sum(r["valid"] for r in rows),
                scope="Exploratory local TUN screening; two pairs/cell, no confidence intervals or production non-regression claim",
                rows=rows, comparisons=comparisons)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    result = analyze(a.root)
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if a.output:
        with a.output.open("x") as f:
            f.write(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
