#!/usr/bin/env python3
"""Audit each fixed-arrival cohort and summarize run-level paired changes."""
import argparse
from collections import defaultdict
import json
import statistics
from pathlib import Path

from analyze import load, verify_arrivals, cgroup_delta
from confirm import interval
from rtt_matrix import ARMS, BASE

KEYS = ["mbps", "stack_cpu_s_per_gb", "stack_cpu_s_per_s", "stack_link_cpu_s_per_gb",
        "stack_link_cpu_s_per_s", "process_cpu_s_per_gb", "process_cpu_s_per_s",
        "rss_hwm_kib", "user_equal_mean_ms", "worst_user_p99_ms",
        "heavy_jain", "max_observed_no_read_sec"]


def progress_stats(rows, count):
    if not rows or len(rows) < 2 or any(len(r["bytes"]) != count for r in rows):
        raise ValueError("missing flow observations")
    size = [b - a for a, b in zip(rows[0]["bytes"], rows[-1]["bytes"])]
    gaps, max_gaps = [0.] * count, [0.] * count
    for a, b in zip(rows, rows[1:]):
        dt = (b["elapsed_ms"] - a["elapsed_ms"]) / 1000
        if dt < 0 or any(y < x for x, y in zip(a["bytes"], b["bytes"])):
            raise ValueError("nonmonotonic progress")
        for i, (x, y) in enumerate(zip(a["bytes"], b["bytes"])):
            gaps[i] = gaps[i] + dt if x == y else 0
            max_gaps[i] = max(max_gaps[i], gaps[i])
    return dict(heavy_flow_window_bytes=size, heavy_zero_window_flows=sum(n == 0 for n in size),
                heavy_jain=sum(size)**2 / (len(size) * sum(n*n for n in size)) if any(size) else 0,
                max_observed_no_read_sec=max(max_gaps), per_flow_max_observed_no_read_sec=max_gaps)


def summarize(path):
    start, end = load(path / "start.json"), load(path / "end.json")
    out = dict(name=path.name, **start["row"], valid=False,
               throttled_usec=cgroup_delta(start, end, "throttled_usec"),
               nr_throttled=cgroup_delta(start, end, "nr_throttled"))
    try:
        r, resources = load(path / "stdout.json"), load(path / "resources.json")
    except (OSError, ValueError) as e:
        return dict(out, errors=[str(e)])
    errors = list(r["errors"])
    if end["returncode"] != 0 or not r["ok"] or not end["binary_unchanged"]:
        errors.append("process or identity failed")
    env = start["env"]
    expected = dict(min_prefetch_bytes=int(env["ZFBENCH_MIN_PREFETCH"]) if "ZFBENCH_MIN_PREFETCH" in env else None,
                    prefetch_ms=int(env["ZFBENCH_PREFETCH_MS"]) if "ZFBENCH_PREFETCH_MS" in env else None,
                    quantum_bytes=int(env["ZFBENCH_QUANTUM"]) if "ZFBENCH_QUANTUM" in env else None,
                    round_cap_bytes=int(env["ZFBENCH_ROUND_CAP"]) if "ZFBENCH_ROUND_CAP" in env else None,
                    connect_parallel=32, heavy_progress=True)
    if "retry_budget_writers" in r["trial_tuning"]:
        expected["retry_budget_writers"] = env.get("ZFBENCH_RETRY_BUDGET_WRITERS", "1") == "1"
    tx = dict(max_snd_buf_bytes=None, prefetch_max_bytes=int(env["ZFBENCH_PREFETCH_MAX"]) if "ZFBENCH_PREFETCH_MAX" in env else None)
    if r["trial_tuning"] != expected or r["tx_tuning"] != tx or r["config"]["sock_buf_kb"] != 16384:
        errors.append("configuration does not match plan")
    discipline = "peer-drr-pushout" if env["ZFBENCH_QUEUE_DISCIPLINE"] == "peer" else "fifo-service"
    if any(r["link"][k]["queue_discipline"] != discipline for k in ("up", "down")):
        errors.append("queue discipline identity mismatch")
    cpu, result = r["cpu"], r["results"]
    gb, wall = cpu["window_effective_bytes"] / 1e9, cpu["window_wall_sec"]
    if gb <= 0 or wall <= 0:
        return dict(out, errors=errors + ["no measured work"])
    stack = cpu["stack_thread_window_sec"]
    link = cpu["link_up_window_sec"] + cpu["link_down_window_sec"]
    out.update(mbps=result["heavy"]["goodput_mbps_window"], stack_cpu_s_per_gb=stack / gb,
               stack_cpu_s_per_s=stack / wall, stack_link_cpu_s_per_gb=(stack + link) / gb,
               stack_link_cpu_s_per_s=(stack + link) / wall,
               process_cpu_s_per_gb=cpu["process_window_sec"] / gb,
               process_cpu_s_per_s=cpu["process_window_sec"] / wall, rss_hwm_kib=resources["max_rss_kib"],
               link_late_mean_us={k:r["link"][k]["timing_late_mean_us"] for k in ("up", "down")},
               bottleneck_drops={k:r["link"][k]["bottleneck_drops"] for k in ("up", "down")},
               wall_sec=wall)
    try:
        out.update(progress_stats(result["heavy"]["progress_250ms"], out["heavy"]))
        streams = load(path / "arrivals.json")
        if out["lights"]:
            users, faults = verify_arrivals(streams, result, expected_loaded=out["measure"] * 25)
            errors.extend(faults)
            out["users"] = users
            means, tails = [u["mean_ms"] for u in users], [u["p99_ms"] for u in users]
            out["user_equal_mean_ms"] = statistics.mean(means) if len(means) == 3 and all(x is not None for x in means) else None
            out["worst_user_p99_ms"] = max(tails) if len(tails) == 3 and all(x is not None for x in tails) else None
            out["idle_user_mean_ms"] = statistics.mean([statistics.mean(u["samples"]["raw_ms"]) for u in result["baseline_users"]])
            out["generator_max_lag_ms"] = max((a["started_ns"] - a["planned_ns"]) / 1e6 for s in streams for a in s["arrivals"] if a["started_ns"] is not None)
            out["planned_requests"] = sum(s["count"] for s in streams)
            out["missing_requests"] = sum(a["outcome"] != "completed" for s in streams for a in s["arrivals"])
        elif streams or result["loaded_users"] or result["baseline_users"]:
            errors.append("unexpected light traffic in peak guard")
    except (OSError, ValueError, KeyError, TypeError) as e:
        errors.append(f"observation validation: {e}")
    return dict(out, valid=not errors, errors=sorted(set(errors)))


def changes(pairs):
    output = {}
    for k in KEYS:
        values = [100 * (b[k] / a[k] - 1) for a, b in pairs if a.get(k) is not None and b.get(k) is not None and a[k] > 0]
        if values:
            output[k] = interval(values) if len(values) >= 6 else dict(paired_pct=values, median_pct=statistics.median(values))
    return output


def analyze(out):
    plan = load(out / "plan.json")
    rows = [summarize(p) for p in sorted(out.glob("[0-9][0-9][0-9]-*")) if (p / "end.json").exists()]
    groups = defaultdict(list)
    for r in rows:
        groups[r["cc"], r["rtt"], r["heavy"]].append(r)
    comparisons = []
    for (cc, rtt, heavy), group in sorted(groups.items()):
        candidates = [BASE] if plan["phase"] == "aa" else sorted({r["arm"] for r in group} - {BASE})
        for arm in candidates:
            pairs = []
            for block in sorted({r["block"] for r in group}):
                aa = [r for r in group if r["block"] == block and r["arm"] == BASE]
                bb = [r for r in group if r["block"] == block and r["arm"] == arm]
                if plan["phase"] == "aa" and len(aa) == 2:
                    pairs.append((aa[0], aa[1]))
                elif len(aa) == len(bb) == 1:
                    pairs.append((aa[0], bb[0]))
            comparisons.append(dict(cc=cc, rtt=rtt, heavy=heavy, arm=arm, pairs=len(pairs),
                                    valid=bool(pairs) and all(a["valid"] and b["valid"] for a, b in pairs),
                                    changes_descriptive=changes(pairs)))
    return dict(phase=plan["phase"], planned=len(plan["rows"]), completed=len(rows), valid=sum(r["valid"] for r in rows),
                all_valid=len(rows) == len(plan["rows"]) and all(r["valid"] for r in rows),
                rows=rows, comparisons=comparisons,
                scope="Same-host controlled bottleneck, run-level paired estimates. No real-WAN ingress or global-optimum claim.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    result = json.dumps(analyze(args.out), indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        with args.output.open("x") as f:
            f.write(result)
    else:
        print(result, end="")
