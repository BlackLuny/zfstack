#!/usr/bin/env python3
"""Paired summaries from issue23 loader.jsonl. Diagnostic/failure slots stay out of means."""
import collections
import json
import math
import pathlib
import random
import statistics
import os

ROOT = pathlib.Path(__file__).resolve().parent
EVID = pathlib.Path(os.environ.get("ZF48_EVIDENCE_DIR", "/tmp/zf48"))


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def pctile(x, q):
    if not x:
        return None
    x = sorted(x)
    return x[max(0, math.ceil(q * len(x)) - 1)]


def jain(values):
    values = [v for v in values if v > 0]
    if not values:
        return None
    s = sum(values)
    return (s * s) / (len(values) * sum(v * v for v in values))


def cpu_stat(text):
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        k, v = line.split()
        out[k] = int(v)
    return out


def metrics(log):
    out = {}
    for direction in ["down", "up"]:
        bulk = [r for r in log if r.get("type") == "bulk" and r.get("direction") == direction]
        byte = sum(r["verified_bytes"] for r in bulk)
        wall = sum(r["window_wall_sec"] for r in bulk)
        cpu = sum(r["proxy_cpu_sec"] for r in bulk)
        out[direction + ".cpu_s_GB"] = cpu / (byte / 1e9) if byte else None
        out[direction + ".goodput_Mbps"] = byte * 8 / wall / 1e6 if wall else None
        out[direction + ".cpu_cores"] = cpu / wall if wall else None
        mixed = []
        jains = []
        for r in bulk:
            m = r.get("mixed_rr")
            if m:
                mixed += [
                    p["rtt_us"]
                    for p in m["raw_samples"]
                    if m["bulk_begin_monotonic_ns"] <= p["begin_monotonic_ns"] < m["bulk_end_monotonic_ns"]
                ]
            rates = []
            for flow in r.get("per_flow") or []:
                w = flow.get("flow_wall_sec") or 0
                b = flow.get("verified_bytes") or 0
                if w > 0:
                    rates.append(b / w)
            if rates:
                jains.append(jain(rates))
        out[direction + ".mixed_max_us"] = max(mixed) if mixed else None
        out[direction + ".mixed_p99_us"] = pctile(mixed, 0.99) if len(mixed) >= 100 else None
        out[direction + ".mixed_p50_us"] = pctile(mixed, 0.50) if mixed else None
        out[direction + ".mixed_n"] = len(mixed)
        out[direction + ".jain_fairness"] = statistics.median(jains) if jains else None
    for r in log:
        if r.get("type") == "rr":
            prefix = f'rr{r["size"]}.phase{r["phase"]}'
            out[prefix + ".p99_us"] = r["latency"]["p99_us"]
            out[prefix + ".p50_us"] = r["latency"]["p50_us"]
            out[prefix + ".n"] = r["latency"]["n"]
    quiet = [r for r in log if r.get("type") == "quiet"]
    if quiet:
        out["quiet.cpu_sec"] = sum(r["proxy_cpu_sec"] for r in quiet)
    return out


def interval(values, seed):
    rng = random.Random(seed)
    estimates = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(10000))
    return [pctile(estimates, 0.025), pctile(estimates, 0.975)]


def main():
    records = [json.loads(p.read_text()) for p in sorted((EVID / "records").glob("*.json"))]
    summary = {
        "scope": "Linux TUN + whole proxy process CPU from /proc ticks; not core-only, not WireGuard, not qa-perf.yml. Intervals are descriptive.",
        "records": len(records),
        "failures": [],
        "performance": [],
        "outcomes": [],
    }
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for record in records:
        result = record.get("result")
        path = EVID / "runs" / record["name"]
        log = rows(path / "loader.jsonl") if (path / "loader.jsonl").exists() else []
        summary["outcomes"].append({
            "name": record["name"],
            "group": record["group"],
            "started": bool(result),
            "ok": bool(result and result.get("ok")),
            "cleanup_ok": bool(result and result.get("cleanup_ok")),
            "elapsed_sec": record.get("elapsed"),
            "diagnostic": bool(record.get("diagnostic")),
        })
        if record.get("diagnostic") or record["group"] == "smoke":
            continue
        if not result or not result.get("ok") or record.get("exit_code"):
            summary["failures"].append({
                "name": record["name"],
                "errors": (result or {}).get("errors") or [str(record.get("stderr", ""))[-500:]],
                "started": bool(result),
                "cleanup_ok": bool(result and result.get("cleanup_ok")),
            })
            continue
        meta = json.loads((path / "final_metadata.json").read_text()) if (path / "final_metadata.json").exists() else json.loads((path / "metadata.json").read_text())
        if not result.get("eligible_for_performance") or meta["case_config"].get("diagnostic"):
            continue
        record = dict(record)
        record["metrics"] = metrics(log)
        record["case_config"] = meta["case_config"]
        record["hashes"] = {k: meta[k] for k in ["binary_sha256", "loader_sha256", "runner_sha256"] if k in meta}
        groups[record["group"]][record["pair"]].append(record)
    for group, pairs in sorted(groups.items()):
        item = {
            "group": group,
            "complete_pairs": len(pairs),
            "metrics": {},
            "throttled_usec": 0,
        }
        measurements = collections.defaultdict(lambda: [[], []])
        for pair, recs in sorted(pairs.items()):
            recs = sorted(recs, key=lambda r: r["position"])
            if len(recs) != 2:
                item.setdefault("incomplete", []).append({"pair": pair, "n": len(recs)})
                continue
            if group == "aa":
                recs_sorted = recs
            else:
                recs_sorted = sorted(recs, key=lambda r: r["variant"] != "baseline")
            for rec in recs:
                before = cpu_stat(rec["before"].get("cpu_stat") or "")
                after = cpu_stat(rec["after"].get("cpu_stat") or "")
                item["throttled_usec"] += after.get("throttled_usec", 0) - before.get("throttled_usec", 0)
            for key in recs_sorted[0]["metrics"]:
                a, b = recs_sorted[0]["metrics"][key], recs_sorted[1]["metrics"][key]
                if a is None or b is None:
                    continue
                measurements[key][0].append(a)
                measurements[key][1].append(b)
        for key, (a, b) in measurements.items():
            vals = [100 * (y / x - 1) for x, y in zip(a, b) if x not in (0, None)]
            item["metrics"][key] = {
                "first_or_baseline_median": statistics.median(a) if a else None,
                "second_or_candidate_median": statistics.median(b) if b else None,
                "n": len(a),
                "paired_median_percent": statistics.median(vals) if vals else None,
                "descriptive_ci95": interval(vals, 20261016) if len(vals) >= 6 else None,
                "paired_values_percent": vals,
            }
        summary["performance"].append(item)
    (ROOT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (EVID / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("Records", len(records), "failures", len(summary["failures"]))
    keys = [
        "down.cpu_s_GB", "up.cpu_s_GB",
        "down.goodput_Mbps", "up.goodput_Mbps",
        "down.mixed_p99_us", "up.mixed_p99_us",
        "rr64.phase1.p99_us",
        "down.jain_fairness", "up.jain_fairness",
    ]
    for item in summary["performance"]:
        print(item["group"], "pairs", item["complete_pairs"], "throttle_us", item["throttled_usec"])
        for key in keys:
            m = item["metrics"].get(key)
            if not m:
                print(" ", key, "MISSING")
                continue
            print(
                f"  {key} n={m['n']} median%={None if m['paired_median_percent'] is None else round(m['paired_median_percent'], 3)}",
                "ci", m["descriptive_ci95"],
                "A", m["first_or_baseline_median"], "B", m["second_or_candidate_median"],
            )


if __name__ == "__main__":
    main()
