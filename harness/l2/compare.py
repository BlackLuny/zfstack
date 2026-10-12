#!/usr/bin/env python3
"""Compare sanitized L2 ABBA evidence; fail on candidate correctness regressions.

CPU is the whole worker. Performance limits are optional test thresholds, not
product defaults. Failed requests stay failures even if all completed samples
have small latencies. Run the pcap analyzer before this checker.
"""
import argparse
import gzip
import json
from pathlib import Path
import statistics


def measure(path):
    result = json.loads((path / "result.json").read_text())
    raw = json.loads(gzip.decompress((path / "raw.json.gz").read_bytes()))
    analysis = json.loads((path / "analysis.json").read_text())
    loaded = result["worker"]["loaded"]
    cores = loaded["worker_cpu_sec"] / loaded["sample_window_sec"]
    rate = result["heavy_mbps"]
    errors = list(result["errors"])
    if not result["ok"] and not errors:
        errors.append("run marked unsuccessful")
    if result["request_timeout_sec"] != 3:
        errors.append("comparison requires the original 3s request timeout")
    for peer in range(1, 4):
        client = raw[f"client-{peer}.json"]
        errors.extend(client["errors"])
        for phase in ("baseline", "loaded", "recovery"):
            if not any(s["phase"] == phase for s in client["samples"]):
                errors.append(f"peer {peer} missing {phase} progress")
        if any(s["latency_ms"] >= 3000 for s in client["samples"]):
            errors.append(f"peer {peer} exceeded the original 3s deadline")
    evidence_errors = []
    for log in ("light-capture.log", "target-capture.log"):
        if "0 packets dropped by kernel" not in raw[log]:
            evidence_errors.append(f"{log}: capture loss or missing capture statistics")
    for flow in raw["client-0.json"]["flows"]:
        if not flow["bytes"] or flow["errors"]:
            evidence_errors.append("heavy flow failed integrity or made no progress")
    errors.extend(evidence_errors)
    return {
        "case": path.name, "ok": not errors, "errors": sorted(set(errors)),
        "evidence_errors": sorted(set(evidence_errors)),
        "dimensions": [result["memory_mb"], result["uplink_delay_ms"],
                       result["phases_sec"], len(raw["client-0.json"]["flows"])],
        "heavy_mbps": rate, "worker_cpu_cores": cores,
        "worker_cpu_sec_per_gib": cores * (8 * 1024 ** 3) / (rate * 1e6),
        "worker_peak_rss_mib": loaded["rss_peak_kib"] / 1024,
        "same_port_loaded_n": analysis["loaded_n"],
        "same_port_loaded_max_ms": analysis["loaded_max_ms"],
        "same_port_loaded_p99_ms": analysis["loaded_p99_ms"],
        "requests_retransmitted": analysis["requests_retransmitted"],
        "worker_sha256": raw["worker.sha256"].split()[0],
    }


def compare(root, throughput_drop=None, cpu_rise=None):
    profiles, errors = {}, []
    for profile in ("small", "normal", "delayed"):
        base = [measure(root / f"{profile}-base-{repeat}") for repeat in "ab"]
        candidate = [measure(root / f"{profile}-candidate-{repeat}") for repeat in "ab"]
        if any(row["dimensions"] != base[0]["dimensions"] for row in base + candidate):
            errors.append(f"{profile}: workload dimensions changed between binaries")
        for row in base + candidate:
            if row["evidence_errors"]:
                errors.append(f"{row['case']}: invalid comparison evidence {row['evidence_errors']}")
        for row in candidate:
            if not row["ok"]:
                errors.append(f"{row['case']}: {row['errors']}")
            if row["requests_retransmitted"]:
                errors.append(f"{row['case']}: same-port light requests retransmitted")
        metrics = {}
        for metric in ("heavy_mbps", "worker_cpu_cores", "worker_cpu_sec_per_gib", "worker_peak_rss_mib"):
            before = statistics.mean(row[metric] for row in base)
            after = statistics.mean(row[metric] for row in candidate)
            metrics[metric] = {"base_mean": before, "candidate_mean": after,
                               "change_percent": (after / before - 1) * 100}
        if throughput_drop is not None and metrics["heavy_mbps"]["change_percent"] < -throughput_drop:
            errors.append(f"{profile}: throughput drop exceeds {throughput_drop}%")
        if cpu_rise is not None and metrics["worker_cpu_sec_per_gib"]["change_percent"] > cpu_rise:
            errors.append(f"{profile}: CPU per GiB rise exceeds {cpu_rise}%")
        profiles[profile] = {"base": base, "candidate": candidate, "metrics": metrics}
    hashes = {source: {row["worker_sha256"] for p in profiles.values() for row in p[source]}
              for source in ("base", "candidate")}
    if any(len(values) != 1 for values in hashes.values()) or hashes["base"] == hashes["candidate"]:
        errors.append("each source must use one immutable, distinct binary across all cases")
    return {"ok": not errors, "errors": errors, "profiles": profiles,
            "limits": {"throughput_drop_percent": throughput_drop,
                       "cpu_per_gib_rise_percent": cpu_rise}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--max-throughput-drop", type=float)
    parser.add_argument("--max-cpu-rise", type=float)
    args = parser.parse_args()
    for value in (args.max_throughput_drop, args.max_cpu_rise):
        if value is not None and value < 0:
            parser.error("regression limits must be nonnegative")
    result = compare(args.root, args.max_throughput_drop, args.max_cpu_rise)
    (args.root / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"ok": result["ok"], "errors": result["errors"],
                      "metrics": {p: x["metrics"] for p, x in result["profiles"].items()}}, indent=2))
    return int(not result["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
