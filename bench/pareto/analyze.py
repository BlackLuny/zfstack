#!/usr/bin/env python3
"""Analyze fixed-work pareto_ab.py JSONL records without running any benchmarks.

Usage:
  python3 analyze_pareto.py /path/to/run-directory
  python3 analyze_pareto.py /path/to/runs.jsonl --output /path/to/summary.json

Warmups never enter statistics. Failed, malformed, unpaired, or duplicate runs
never enter paired comparisons. A rejected measured run makes the exit code 2,
even if other pairs are usable. --allow-partial permits missing cases/pairs only;
it does not forgive failed measurements. Rates are recomputed from real work
wall time and received bytes, never from the synthetic protocol clock.

Bootstrap intervals describe median paired changes in this fixed workload on
this machine. They do not establish population/network performance guarantees.
Only the Python standard library is needed.
"""

import argparse
from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys


VARIANTS = ("baseline", "candidate")
WORKLOAD_FIELDS = (
    "case", "iterations", "flows", "peers", "payload_bytes", "server_ingress",
    "pacing", "timing_scope", "cpu_clock", "real_network_io",
    "virtual_clock_used_for_rate",
)
EXACT_FIELDS = (
    "operations", "effective_bytes", "allocation_requested_bytes",
    "checksum_u64", "expected_checksum_u64", "connections_checked",
)
PROTOCOL_FIELDS = (
    "wire_packets", "wire_bytes", "driver_rounds_including_cleanup",
    "virtual_work_ns", "checksum_u64", "expected_checksum_u64",
)
ENV_FIELDS = ("throttled_usec", "nr_throttled", "nr_periods", "usage_usec")


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def reject_nonfinite(value):
    raise ValueError(f"nonfinite JSON constant: {value}")


def read_json(path):
    return json.loads(path.read_text(), parse_constant=reject_nonfinite)


def compact_stat(values):
    values = [v for v in values if v is not None and math.isfinite(v)]
    if not values:
        return {"n": 0, "median": None, "min": None, "max": None}
    return {
        "n": len(values), "median": statistics.median(values),
        "min": min(values), "max": max(values),
    }


def percentile(sorted_values, probability):
    position = (len(sorted_values) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    weight = position - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def paired_change(baseline, candidate, draws=None):
    values = [100.0 * (c / b - 1.0) for b, c in zip(baseline, candidate) if b is not None and c is not None and b > 0]
    result = compact_stat(values)
    result["values"] = values
    result["definition"] = "100 * (candidate / baseline - 1), paired before aggregation"
    result["bootstrap95_descriptive_ci"] = None
    if draws is not None and len(values) >= 6 and len(values) == len(baseline):
        medians = sorted(statistics.median(values[index] for index in draw) for draw in draws)
        result["bootstrap95_descriptive_ci"] = [percentile(medians, 0.025), percentile(medians, 0.975)]
        result["bootstrap_replicates"] = len(draws)
    else:
        result["bootstrap_note"] = "Not computed for this metric, fewer than six complete pairs, or a zero/missing baseline."
    return result


def metric_summary(pairs, name, unit, draws=None, improvement=None):
    baseline = [p["baseline"]["metrics"].get(name) for p in pairs]
    candidate = [p["candidate"]["metrics"].get(name) for p in pairs]
    bstats, cstats = compact_stat(baseline), compact_stat(candidate)
    ratio_of_medians = None
    if bstats["median"] is not None and bstats["median"] > 0 and cstats["median"] is not None:
        ratio_of_medians = 100.0 * (cstats["median"] / bstats["median"] - 1.0)
    return {
        "unit": unit, "baseline": bstats, "candidate": cstats,
        "paired_change_pct": paired_change(baseline, candidate, draws),
        "ratio_of_medians_change_pct": ratio_of_medians,
        "improvement_direction": improvement,
    }


def parse_cpu_stat(value):
    result = {}
    if not isinstance(value, str):
        return result
    for line in value.splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                result[fields[0]] = int(fields[1])
            except ValueError:
                pass
    return result


def environment_delta(row):
    before = row.get("before") or {}
    after = row.get("after") or {}
    b = parse_cpu_stat(before.get("cpu_stat"))
    a = parse_cpu_stat(after.get("cpu_stat"))
    delta, reset = {}, []
    for key in ENV_FIELDS:
        if key not in b or key not in a:
            delta[key] = None
        elif a[key] < b[key]:
            delta[key] = None
            reset.append(key)
        else:
            delta[key] = a[key] - b[key]
    delta["counter_resets"] = reset
    delta["cpu_max_before"] = before.get("cpu_max")
    delta["cpu_max_after"] = after.get("cpu_max")
    return delta


def validate_row(row, number, expected_hashes, expected_case):
    errors = []
    if row.get("variant") not in VARIANTS:
        errors.append("unknown variant")
    if not integer(row.get("pair")):
        errors.append("pair must be a nonnegative integer")
    if type(row.get("exit_code")) is not int or row["exit_code"] != 0:
        errors.append(f"exit_code={row.get('exit_code')!r}")
    measurements = row.get("measurements")
    if not isinstance(measurements, list) or len(measurements) != 1 or not isinstance(measurements[0], dict):
        errors.append("expected exactly one measurement object")
        return None, errors
    m = measurements[0]
    if m.get("ok") is not True:
        errors.append(f"measurement ok is not true: {m.get('error', '')}")
    if m.get("tool") != "zfstack-pareto-probe":
        errors.append("unexpected measurement tool")
    for key in ("work_wall_ns", "work_cpu_ns", "operations"):
        if not integer(m.get(key), 1):
            errors.append(f"{key} must be a positive integer")
    for key in ("effective_bytes", "allocation_requested_bytes", "checksum_u64", "expected_checksum_u64", "final_reserved_bytes"):
        if not integer(m.get(key)):
            errors.append(f"{key} must be a nonnegative integer")
    if m.get("final_reserved_bytes") != 0 or m.get("all_budget_leases_released") is not True:
        errors.append("final backing release assertion failed/missing")
    if m.get("checksum_u64") != m.get("expected_checksum_u64"):
        errors.append("actual checksum differs from expected checksum")
    if m.get("virtual_clock_used_for_rate") is not False:
        errors.append("virtual-clock rate exclusion assertion missing/false")
    if m.get("real_network_io") is not False:
        errors.append("unexpected network-I/O scope for this rootless harness")
    if expected_case and m.get("case") != expected_case["args"][0]:
        errors.append("measurement case differs from configured command")
    if expected_case:
        command_args = expected_case["args"]
        for flag, field in (("--iterations", "iterations"), ("--flows", "flows"), ("--peers", "peers"), ("--payload", "payload_bytes")):
            if flag in command_args:
                value = int(command_args[command_args.index(flag) + 1])
                if m.get(field) != value:
                    errors.append(f"measurement {field} differs from configured command")
        if command_args[0].startswith("pump-"):
            if m.get("pacing") is not ("--no-pacing" not in command_args):
                errors.append("measurement pacing differs from configured command")
            expected_ingress = "owned_verify" if "--owned-server" in command_args else "borrowed_verify"
            if m.get("server_ingress") != expected_ingress:
                errors.append("measurement server ingress differs from configured command")
    actual_hash = row.get("binary_sha256")
    if not isinstance(actual_hash, str) or len(actual_hash) != 64 or any(c not in "0123456789abcdef" for c in actual_hash):
        errors.append("missing/malformed binary SHA256")
    expected_hash = expected_hashes.get(row.get("variant"))
    if expected_hash and actual_hash != expected_hash:
        errors.append("binary SHA256 differs from metadata")
    if errors:
        return None, errors
    effective = m["effective_bytes"]
    cpu_ns = m["work_cpu_ns"]
    wall_ns = m["work_wall_ns"]
    if effective:
        # ns / bytes is numerically equal to CPU seconds / decimal GB.
        cpu_value = cpu_ns / effective
        rate = effective * 8.0 / wall_ns  # Gbit/s; denominator is REAL wall ns.
    else:
        cpu_value = cpu_ns / m["operations"]
        rate = None
    env = environment_delta(row)
    metrics = {
        "cpu_primary": cpu_value,
        "cpu_ns_per_operation": cpu_ns / m["operations"],
        "work_wall_Gbit_per_sec": rate,
        "work_wall_cpu_ratio": wall_ns / cpu_ns,
        "work_cpu_wall_ratio": cpu_ns / wall_ns,
        "work_cpu_sec": cpu_ns / 1e9,
        "work_wall_sec": wall_ns / 1e9,
        "peak_rss_bytes": m.get("process_lifetime_peak_rss_bytes"),
        "sampled_peak_reserved_bytes": m.get("sampled_peak_reserved_bytes"),
        "retained_reserved_bytes_before_drop": m.get("retained_reserved_bytes_before_drop"),
        "throttled_usec": env["throttled_usec"],
        "nr_throttled": env["nr_throttled"],
        "nr_periods": env["nr_periods"],
        "cgroup_usage_usec": env["usage_usec"],
    }
    for key, value in metrics.items():
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            errors.append(f"invalid derived/source metric {key}")
    if errors:
        return None, errors
    return {
        "line": number, "pair": row["pair"], "variant": row["variant"],
        "position": row.get("position"), "binary_sha256": actual_hash,
        "measurement": m, "metrics": metrics, "environment": env,
    }, []


def env_summary(pairs, field):
    result = metric_summary(pairs, field, "microseconds" if field == "throttled_usec" else "count")
    differences = []
    for pair in pairs:
        b = pair["baseline"]["metrics"].get(field)
        c = pair["candidate"]["metrics"].get(field)
        if b is not None and c is not None:
            differences.append(c - b)
    result["paired_candidate_minus_baseline"] = {**compact_stat(differences), "values": differences}
    return result


def case_summary(name, pairs, bootstrap, seed):
    example = pairs[0]["baseline"]["measurement"]
    byte_case = example["effective_bytes"] > 0
    cpu_unit = "CPU seconds / effective GB" if byte_case else "CPU ns / operation"
    workload = {key: example.get(key) for key in WORKLOAD_FIELDS}
    protocol_mismatches = []
    for pair in pairs:
        bm, cm = pair["baseline"]["measurement"], pair["candidate"]["measurement"]
        for field in PROTOCOL_FIELDS:
            if bm.get(field) != cm.get(field):
                protocol_mismatches.append({"pair": pair["pair"], "field": field, "baseline": bm.get(field), "candidate": cm.get(field)})
    across_runs = {}
    for field in PROTOCOL_FIELDS + EXACT_FIELDS:
        if field in across_runs:
            continue
        values = [p[v]["measurement"].get(field) for p in pairs for v in VARIANTS]
        unique = sorted(set(values), key=lambda x: (x is None, str(x)))
        across_runs[field] = {"constant_across_all_runs": len(unique) <= 1, "distinct_values": unique}
    draws = None
    case_seed = seed ^ int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big")
    if len(pairs) >= 6 and bootstrap:
        rng = random.Random(case_seed)
        draws = [[rng.randrange(len(pairs)) for _ in pairs] for _ in range(bootstrap)]
    warnings = []
    if protocol_mismatches:
        warnings.append("Protocol work counters differ within pairs; CPU differences may include a changed workload. Do not attribute them solely to implementation speed.")
    if any(not value["constant_across_all_runs"] for value in across_runs.values()):
        warnings.append("At least one protocol/work counter varies across repetitions; inspect consistency details.")
    if any((p[v]["environment"].get("throttled_usec") or 0) > 0 for p in pairs for v in VARIANTS):
        warnings.append("Cgroup CPU throttling was observed. Its counters include the whole cgroup during subprocess execution, not only this benchmark's work loop.")
    if any(p[v]["environment"]["throttled_usec"] is None for p in pairs for v in VARIANTS):
        warnings.append("Cgroup throttling counters unavailable or reset for at least one run.")
    if len(pairs) < 6:
        warnings.append("Fewer than six complete pairs: no bootstrap interval reported.")
    if example["case"].startswith("pump-"):
        rate_scope = "two-Shard packet processing, materialization, handshake and application validation; no real network throughput"
    elif byte_case:
        rate_scope = "RX buffer processing and application validation microbenchmark; no real network throughput"
    else:
        rate_scope = "not applicable: metadata operations do not deliver application bytes"
    pair_rows = []
    for pair in pairs:
        row = {"pair": pair["pair"], "order": pair["order"]}
        for variant in VARIANTS:
            run = pair[variant]
            row[variant] = {
                "source_line": run["line"], "metrics": run["metrics"],
                "environment": run["environment"],
                "work_counters": {key: run["measurement"].get(key) for key in dict.fromkeys(EXACT_FIELDS + PROTOCOL_FIELDS)},
            }
        row["cpu_change_pct"] = 100 * (row["candidate"]["metrics"]["cpu_primary"] / row["baseline"]["metrics"]["cpu_primary"] - 1)
        if byte_case:
            row["wall_processing_rate_change_pct"] = 100 * (row["candidate"]["metrics"]["work_wall_Gbit_per_sec"] / row["baseline"]["metrics"]["work_wall_Gbit_per_sec"] - 1)
        pair_rows.append(row)
    return {
        "name": name, "n_pairs": len(pairs), "pair_ids": [p["pair"] for p in pairs],
        "workload": workload, "rate_scope": rate_scope,
        "cpu": metric_summary(pairs, "cpu_primary", cpu_unit, draws, "negative percent change is less CPU per unit of work"),
        "cpu_ns_per_operation": metric_summary(pairs, "cpu_ns_per_operation", "CPU ns / operation"),
        "wall_processing_rate": metric_summary(pairs, "work_wall_Gbit_per_sec", "Gbit/s using actual work wall time", draws, "positive percent change is faster processing"),
        "work_wall_cpu_ratio": metric_summary(pairs, "work_wall_cpu_ratio", "actual work wall seconds / process CPU seconds"),
        "memory": {
            "process_lifetime_peak_rss_bytes": metric_summary(pairs, "peak_rss_bytes", "bytes"),
            "sampled_peak_reserved_bytes": metric_summary(pairs, "sampled_peak_reserved_bytes", "bytes; sampled lower bound"),
            "retained_reserved_bytes_before_drop": metric_summary(pairs, "retained_reserved_bytes_before_drop", "bytes"),
        },
        "environment": {
            "throttled_usec": env_summary(pairs, "throttled_usec"),
            "nr_throttled": env_summary(pairs, "nr_throttled"),
            "nr_periods": env_summary(pairs, "nr_periods"),
        },
        "consistency": {
            "all_pair_protocol_counters_equal": not protocol_mismatches,
            "pair_protocol_mismatches": protocol_mismatches,
            "across_all_runs": across_runs,
            "all_accepted_runs_ok_checksums_match_and_final_reserved_zero": True,
        },
        "bootstrap_seed": case_seed, "warnings": warnings, "pairs": pair_rows,
    }


def fmt(value, digits=2):
    return "-" if value is None else f"{value:.{digits}f}"


def direction(metric, scale=1.0, digits=2):
    b = metric["baseline"]["median"]
    c = metric["candidate"]["median"]
    return f"{fmt(None if b is None else b / scale, digits)} -> {fmt(None if c is None else c / scale, digits)}"


def terminal_table(headers, rows):
    widths = [max(len(header), *(len(str(row[i])) for row in rows)) for i, header in enumerate(headers)]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    for row in rows:
        print("  ".join(str(value).ljust(width) for value, width in zip(row, widths)))


def print_summary(summary):
    validation = summary["validation"]
    state = "OK" if validation["ok"] and validation["complete"] else "PARTIAL" if validation["ok"] else "INVALID"
    print(f"Validation: {state}")
    print(f"Measured pairs accepted: {sum(c['n_pairs'] for c in summary['cases'])}; warmup records ignored: {validation['warmup_records_ignored']}; rejected records: {len(validation['rejected_records'])}; excluded pairs: {len(validation['excluded_pairs'])}")
    print("CPU change is candidate/baseline - 1 (negative is better). Rates below are packet/buffer processing using real wall time, not network throughput.")
    rows = []
    for case in summary["cases"]:
        cpu = case["cpu"]
        delta = cpu["paired_change_pct"]
        interval = delta["bootstrap95_descriptive_ci"]
        unit = "s/GB" if case["pairs"][0]["baseline"]["work_counters"]["effective_bytes"] else "ns/op"
        rows.append([
            case["name"], case["n_pairs"], f"{direction(cpu, digits=3)} {unit}",
            f"{fmt(delta['median'])}% [{fmt(delta['min'])}, {fmt(delta['max'])}]",
            "-" if interval is None else f"[{fmt(interval[0])}, {fmt(interval[1])}]",
            direction(case["wall_processing_rate"], digits=3),
            fmt(case["wall_processing_rate"]["paired_change_pct"]["median"]),
        ])
    if rows:
        print()
        terminal_table(["case", "pairs", "CPU B -> C", "paired CPU delta% [range]", "95% descriptive CI", "wall processing Gbit/s B -> C", "rate delta%"], rows)
        diagnostics = []
        for case in summary["cases"]:
            diagnostics.append([
                case["name"], direction(case["memory"]["process_lifetime_peak_rss_bytes"], 1 << 20),
                direction(case["memory"]["sampled_peak_reserved_bytes"], 1024),
                direction(case["work_wall_cpu_ratio"], digits=3),
                direction(case["environment"]["throttled_usec"], digits=0),
                direction(case["environment"]["nr_throttled"], digits=0),
                "equal" if case["consistency"]["all_pair_protocol_counters_equal"] else "DIFFER",
            ])
        print()
        terminal_table(["case", "peak RSS MiB B -> C", "charged peak KiB B -> C", "wall/CPU B -> C", "throttled us B -> C", "nr_throttled B -> C", "pair work"], diagnostics)
    for item in validation["rejected_records"]:
        print(f"REJECT line {item['line']}: {item.get('case')} / pair {item.get('pair')} / {item.get('variant')}: {'; '.join(item['reasons'])}")
    for item in validation["excluded_pairs"]:
        print(f"EXCLUDE {item['case']} / pair {item['pair']}: {'; '.join(item['reasons'])}")
    for warning in summary["warnings"]:
        print(f"NOTE: {warning}")
    print("Bootstrap intervals are descriptive for these paired runs, this machine, and this fixed workload; they do not guarantee general stack or network performance.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="runs.jsonl or its containing directory")
    parser.add_argument("--output", type=Path, help="defaults to summary.json beside runs.jsonl")
    parser.add_argument("--cases", type=Path, help="case definitions; metadata.json cases take precedence when this option is absent")
    parser.add_argument("--expected-pairs", type=int, help="defaults to metadata argv.pairs when available")
    parser.add_argument("--bootstrap", type=int, default=20000, help="resamples per main metric; zero disables intervals")
    parser.add_argument("--seed", type=int, default=20261009)
    parser.add_argument("--allow-partial", action="store_true", help="permit missing cases/pairs, never failed or duplicate measured records")
    args = parser.parse_args()
    if args.bootstrap < 0 or (args.expected_pairs is not None and args.expected_pairs < 1):
        parser.error("bootstrap must be nonnegative and expected-pairs positive")
    input_path = args.input / "runs.jsonl" if args.input.is_dir() else args.input
    output_path = args.output or input_path.parent / "summary.json"
    raw = input_path.read_bytes()
    meta_path = input_path.parent / "metadata.json"
    metadata = read_json(meta_path) if meta_path.exists() else {}
    if args.cases:
        definitions = read_json(args.cases)
    else:
        definitions = metadata.get("cases", [])
        fallback = Path(__file__).with_name("pareto_cases.json")
        if not definitions and fallback.exists():
            definitions = read_json(fallback)
    expected = {case["name"]: case for case in definitions}
    expected_pairs = args.expected_pairs if args.expected_pairs is not None else metadata.get("argv", {}).get("pairs")
    expected_hashes = metadata.get("binary_sha256", {})
    groups = defaultdict(list)
    rejected = []
    invalid_keys = set()
    warmups = 0
    hashes_seen = defaultdict(set)
    for number, line in enumerate(raw.decode().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, parse_constant=reject_nonfinite)
            if not isinstance(row, dict):
                raise ValueError("record is not an object")
        except (ValueError, TypeError) as error:
            rejected.append({"line": number, "reasons": [f"invalid JSON record: {error}"]})
            continue
        pair = row.get("pair")
        if row.get("warmup") is True or (type(pair) is int and pair < 0):
            warmups += 1
            continue
        name = row.get("case")
        if not isinstance(name, str) or not name:
            rejected.append({"line": number, "pair": pair, "variant": row.get("variant"), "reasons": ["missing case name"]})
            continue
        key = (name, pair) if integer(pair) else None
        run, errors = validate_row(row, number, expected_hashes, expected.get(name))
        if errors:
            rejected.append({"line": number, "case": name, "pair": pair, "variant": row.get("variant"), "reasons": errors})
            if key:
                invalid_keys.add(key)
        else:
            hashes_seen[run["variant"]].add(run["binary_sha256"])
            groups[key].append(run)
    excluded, incomplete, valid_cases = [], [], defaultdict(list)
    for key in sorted(set(groups) | invalid_keys):
        name, pair = key
        runs = groups.get(key, [])
        variants = {variant: [r for r in runs if r["variant"] == variant] for variant in VARIANTS}
        reasons = []
        if key in invalid_keys:
            reasons.append("pair contains a rejected run")
        duplicates = [variant for variant, items in variants.items() if len(items) > 1]
        missing = [variant for variant, items in variants.items() if not items]
        if duplicates:
            reasons.append(f"duplicate variant(s): {', '.join(duplicates)}")
        if missing:
            reasons.append(f"missing variant(s): {', '.join(missing)}")
        if reasons:
            issue = {"case": name, "pair": pair, "reasons": reasons}
            excluded.append(issue)
            if missing and not duplicates and key not in invalid_keys:
                incomplete.append(issue)
            continue
        b, c = variants["baseline"][0], variants["candidate"][0]
        for field in WORKLOAD_FIELDS + EXACT_FIELDS:
            if b["measurement"].get(field) != c["measurement"].get(field):
                reasons.append(f"unequal workload/{field}: {b['measurement'].get(field)!r} vs {c['measurement'].get(field)!r}")
        if {b["position"], c["position"]} != {0, 1}:
            reasons.append("pair does not contain positions 0 and 1")
        if reasons:
            excluded.append({"case": name, "pair": pair, "reasons": reasons})
            continue
        order = [r["variant"] for r in sorted((b, c), key=lambda r: r["position"])]
        valid_cases[name].append({"pair": pair, "order": order, "baseline": b, "candidate": c})
    warnings = []
    binary_error = any(len(hashes) != 1 for hashes in hashes_seen.values())
    if binary_error:
        warnings.append("A variant's executable hash changed within these data. Results are invalid as one A/B experiment.")
    missing_cases = [name for name in expected if not valid_cases.get(name)]
    missing_pairs = {}
    if expected_pairs is not None:
        for name in dict.fromkeys([*expected, *valid_cases]):
            found = {p["pair"] for p in valid_cases.get(name, [])}
            wanted = set(range(expected_pairs))
            if found != wanted:
                missing_pairs[name] = {"missing": sorted(wanted - found), "unexpected": sorted(found - wanted)}
    if missing_cases:
        warnings.append("Cases without complete accepted pairs: " + ", ".join(missing_cases))
    if missing_pairs:
        warnings.append("The accepted pair set differs from the configured repetition count; inspect validation.missing_or_unexpected_pairs.")
    order = list(dict.fromkeys([*expected, *valid_cases]))
    cases = [case_summary(name, sorted(valid_cases[name], key=lambda p: p["pair"]), args.bootstrap, args.seed) for name in order if valid_cases.get(name)]
    hard_excluded = [x for x in excluded if x not in incomplete]
    complete = not (missing_cases or missing_pairs or incomplete)
    validation_ok = not (rejected or hard_excluded or binary_error) and (complete or args.allow_partial) and bool(cases)
    summary = {
        "tool": "analyze-pareto", "schema_version": 1,
        "input": str(input_path.resolve()), "input_sha256": hashlib.sha256(raw).hexdigest(),
        "binary_sha256": {variant: sorted(values) for variant, values in hashes_seen.items()},
        "metadata": metadata,
        "validation": {
            "ok": validation_ok, "complete": complete, "allow_partial": args.allow_partial,
            "warmup_records_ignored": warmups, "rejected_records": rejected,
            "excluded_pairs": excluded, "missing_cases": missing_cases,
            "missing_or_unexpected_pairs": missing_pairs,
        },
        "methods": {
            "cpu_primary": "work_cpu_ns/effective_bytes for byte cases (CPU seconds/decimal GB); otherwise work_cpu_ns/operations",
            "wall_processing_rate": "effective_bytes*8/work_wall_ns in Gbit/s; actual process work wall only",
            "paired_percent_change": "100*(candidate/baseline-1) for each matched (case,pair), then median and range",
            "bootstrap": f"fixed-seed nonparametric resampling of whole paired percent changes, median statistic, percentile 95% interval, {args.bootstrap} replicates, only when n>=6",
            "bootstrap_limit": "Describes this machine and fixed workload; not a population, protocol-correctness, or network-performance guarantee.",
            "memory": "VmHWM process lifetime peak includes setup/cleanup; charged peak is a sample lower bound; retained-before-drop is not a leak when final_reserved_bytes is zero.",
            "environment": "cgroup counter deltas span the subprocess, include other cgroup processes, and are contextual rather than CPU attributed to the benchmark",
            "virtual_time": "reported solely for workload consistency; never a real-throughput denominator",
        },
        "warnings": warnings,
        "cases": cases,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.name + ".tmp")
    temporary.write_text(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(output_path)
    print_summary(summary)
    print(f"Summary: {output_path.resolve()}")
    return 0 if validation_ok else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"Analysis failed: {error}", file=sys.stderr)
        sys.exit(2)
