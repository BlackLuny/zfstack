#!/usr/bin/env python3
"""Historical small/normal/delayed dossier comparison (not the QA perf gate).

Unrecorded configuration and old CPU approximations remain descriptive. Formal
comparison uses export_case.py and bench/pareto/perf_gate.py. Candidate failures
remain failures even when the measurement is contaminated.
"""
import argparse
import json
from pathlib import Path
import statistics

from evidence import aligned_metrics, configuration, load_case, number
from netem import evidence_errors as qdisc_evidence_errors


def measure(path):
    result, raw, analysis = load_case(path)
    errors, evidence_errors = list(result.get("errors", [])), []
    if result.get("ok") is not True and not errors:
        errors.append("run marked unsuccessful or missing completion marker")
    if result.get("request_timeout_sec") != 3:
        errors.append("comparison requires the original 3s request timeout")
    for peer in range(1, 4):
        client = raw.get(f"client-{peer}.json", {})
        errors.extend(client.get("errors", []))
        for phase in ("baseline", "loaded", "recovery"):
            if not any(s.get("phase") == phase for s in client.get("samples", [])):
                errors.append(f"peer {peer} missing {phase} progress")
        for sample in client.get("samples", []):
            if not number(sample.get("latency_ms")):
                evidence_errors.append(f"peer {peer} invalid latency observation")
            elif sample["latency_ms"] >= 3000:
                errors.append(f"peer {peer} exceeded the original 3s deadline")
    delay = result.get("uplink_delay_ms")
    if not number(delay):
        evidence_errors.append("invalid configured uplink delay")
    else:
        evidence_errors.extend(qdisc_evidence_errors(result.get("qdisc"), delay))
    for log in ("light-capture.log", "target-capture.log"):
        if "0 packets dropped by kernel" not in raw.get(log, ""):
            evidence_errors.append(f"{log}: capture loss or missing capture statistics")
    if result.get("performance_contaminated") is True:
        evidence_errors.append("performance contaminated by concurrent compiler / external work")
    flows = raw.get("client-0.json", {}).get("flows", [])
    if not flows:
        evidence_errors.append("missing heavy flow evidence")
    for flow in flows:
        if not number(flow.get("bytes"), positive=True, integer=True) or flow.get("errors"):
            evidence_errors.append("heavy flow failed integrity or made no progress")
    metrics, precision_errors = aligned_metrics(result)
    full = result.get("worker", {}).get("loaded", {})
    # Preserve historical descriptive values, with their approximation labelled.
    rate, cpu, seconds = result.get("heavy_mbps"), full.get("worker_cpu_sec"), full.get("sample_window_sec")
    if metrics["heavy_mbps"] is None:
        if number(rate, positive=True) and number(cpu) and number(seconds, positive=True):
            cores = cpu / seconds
            metrics.update(heavy_mbps=rate, worker_cpu_cores=cores,
                           worker_cpu_sec_per_gib=cores * 8 * 1024 ** 3 / (rate * 1e6))
        else:
            evidence_errors.append("zero, missing or invalid throughput / CPU measurement")
    config, complete = configuration(result, raw)
    binary = raw.get("worker.sha256", "").split()
    performance_errors = list(evidence_errors) + precision_errors
    if not complete:
        performance_errors.append("historical configuration manifest is incomplete")
    if errors:
        performance_errors.append("functional failure changes completed workload")
    return {"case": path.name, "ok": not errors and not evidence_errors,
            "errors": sorted(set(errors + evidence_errors)), "functional_errors": sorted(set(errors)),
            "evidence_errors": sorted(set(evidence_errors)), "performance_errors": sorted(set(performance_errors)),
            "dimensions": config, "config_complete": complete, **metrics,
            "same_port_loaded_n": analysis.get("loaded_n"),
            "same_port_loaded_max_ms": analysis.get("loaded_max_ms"),
            "same_port_loaded_p99_ms": analysis.get("loaded_p99_ms"),
            "requests_retransmitted": analysis.get("requests_retransmitted"),
            "worker_sha256": binary[0] if binary else None}


def compare(root, throughput_drop=None, cpu_rise=None, aa=False):
    profiles, errors = {}, []
    for value in (throughput_drop, cpu_rise):
        if value is not None and not number(value):
            raise ValueError("regression limits must be finite and nonnegative")
    for profile in ("small", "normal", "delayed"):
        arms = {}
        for source in ("base", "candidate"):
            rows = []
            for repeat in "ab":
                path = root / f"{profile}-{source}-{repeat}"
                try:
                    rows.append(measure(path))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    errors.append(f"{path.name}: malformed or missing evidence: {error}")
            arms[source] = rows
        base, candidate = arms["base"], arms["candidate"]
        if not base or not candidate:
            errors.append(f"{profile}: both arms require complete evidence")
            profiles[profile] = {**arms, "metrics": {}, "performance": "INCONCLUSIVE"}
            continue
        if any(row["dimensions"] != base[0]["dimensions"] for row in base + candidate):
            errors.append(f"{profile}: workload dimensions / full configuration changed between binaries")
        for row in base + candidate:
            if row["evidence_errors"]:
                errors.append(f"{row['case']}: invalid comparison evidence {row['evidence_errors']}")
            if (throughput_drop is not None or cpu_rise is not None) and row["performance_errors"]:
                errors.append(f"{row['case']}: cannot apply performance limits: {row['performance_errors']}")
        for row in candidate:
            if not row["ok"]:
                errors.append(f"{row['case']}: {row['errors']}")
            if row["requests_retransmitted"]:
                errors.append(f"{row['case']}: same-port light requests retransmitted")
        metrics = {}
        for metric in ("heavy_mbps", "worker_cpu_cores", "worker_cpu_sec_per_gib", "worker_peak_rss_mib"):
            values = [row[metric] for row in base + candidate]
            before = statistics.mean(row[metric] for row in base) if all(number(v) for v in values) else None
            after = statistics.mean(row[metric] for row in candidate) if before is not None else None
            metrics[metric] = {"base_mean": before, "candidate_mean": after,
                               "change_percent": (after / before - 1) * 100 if before else None}
        tp, cp = metrics["heavy_mbps"]["change_percent"], metrics["worker_cpu_sec_per_gib"]["change_percent"]
        if throughput_drop is not None and (tp is None or tp < -throughput_drop):
            errors.append(f"{profile}: throughput drop exceeds {throughput_drop}% or is undefined")
        if cpu_rise is not None and (cp is None or cp > cpu_rise):
            errors.append(f"{profile}: CPU per GiB rise exceeds {cpu_rise}% or is undefined")
        profiles[profile] = {**arms, "metrics": metrics, "performance": "INCONCLUSIVE"}
    hashes = {source: {row["worker_sha256"] for p in profiles.values() for row in p[source]}
              for source in ("base", "candidate")}
    if (any(len(values) != 1 or None in values for values in hashes.values())
            or (not aa and hashes["base"] == hashes["candidate"])):
        errors.append("each source must use one immutable, distinct binary across all cases (same hash requires --aa)")
    return {"ok": not errors, "errors": errors, "profiles": profiles, "mode": "historical-AA" if aa else "historical-AB",
            "performance": "INCONCLUSIVE", "note": "Historical dossier checks are not a calibrated statistical performance gate.",
            "limits": {"throughput_drop_percent": throughput_drop, "cpu_per_gib_rise_percent": cpu_rise}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--max-throughput-drop", type=float)
    parser.add_argument("--max-cpu-rise", type=float)
    parser.add_argument("--aa", action="store_true")
    args = parser.parse_args()
    result = compare(args.root, args.max_throughput_drop, args.max_cpu_rise, args.aa)
    (args.root / "comparison.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"ok": result["ok"], "errors": result["errors"]}, indent=2))
    return int(not result["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
