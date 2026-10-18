#!/usr/bin/env python3
"""Describe all retained PacketPool pairs; never grant performance qualification.

One JSONL row per child invocation, matching run_probes.py: phase (AA/AB),
warmup (bool), case, size, pair, arm (A1/A2/B), iterations, index, returncode,
cpu_sec, cpu_user_sec, cpu_system_sec, process_wall_ns, and probe (parsed JSON).
CPU input is in seconds; reported CPU values are converted to nanoseconds.
Extra runner metadata stays in the raw file and does not affect arithmetic.
"""

import argparse
import json
import math
from pathlib import Path
import random
import statistics


CASES = ("cache0", "cache32", "overflow33")
SIZES = (64, 1500, 65536)
PAIR_COUNTS = {"AA": 4, "AB": 12}


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def describe(ratios):
    if not ratios:
        return None
    rng = random.Random(23)
    boot = [
        statistics.median(rng.choices(ratios, k=len(ratios)))
        for _ in range(10000)
    ]
    median = statistics.median(ratios)
    interval = [percentile(boot, 0.025), percentile(boot, 0.975)]
    return {
        "paired_ratios": ratios,
        "median_ratio": median,
        "median_change_pct": (median - 1) * 100,
        "observed_ratio_range": [min(ratios), max(ratios)],
        "descriptive_bootstrap_95pct_ratio": interval,
        "descriptive_bootstrap_95pct_change_pct": [(x - 1) * 100 for x in interval],
        "bootstrap_seed": 23,
        "bootstrap_resamples": 10000,
    }


def positive_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def validate(row):
    errors = []
    if row.get("timed_out") is True or "error" in row or "validation_error" in row:
        errors.append("runner recorded a timeout, execution error, or validation error")
    if type(row.get("returncode")) is not int or row["returncode"] != 0:
        errors.append("returncode is not integer zero")
    if not positive_number(row.get("cpu_sec")):
        errors.append("cpu_sec must be positive finite seconds")
    components = [row.get("cpu_user_sec"), row.get("cpu_system_sec")]
    if not all(type(value) in (int, float) and (value == 0 or positive_number(value)) for value in components):
        errors.append("user/system CPU seconds must be finite and nonnegative")
    elif positive_number(row.get("cpu_sec")) and not math.isclose(
        sum(components), row["cpu_sec"], rel_tol=1e-9, abs_tol=1e-9
    ):
        errors.append("cpu_sec differs from cpu_user_sec + cpu_system_sec")
    if not positive_number(row.get("process_wall_ns")):
        errors.append("process_wall_ns must be positive finite nanoseconds")
    if type(row.get("index")) is not int or row["index"] < 0:
        errors.append("index must be a nonnegative integer")
    phase = row.get("phase")
    if phase not in ("AA", "AB"):
        errors.append("phase must be AA or AB")
    if type(row.get("warmup")) is not bool:
        errors.append("warmup must be a boolean")
    warmup = row.get("warmup") is True
    arm = row.get("arm")
    allowed_arms = ("A1", "A2") if phase == "AA" else (
        ("B",) if warmup else ("A1", "B")
    )
    if arm not in allowed_arms:
        errors.append("unexpected arm for this phase/warmup")
    pair = row.get("pair")
    if type(pair) is not int:
        errors.append("pair must be an integer")
    elif warmup and pair != -1:
        errors.append("warmup pair must be -1")
    elif not warmup and phase in ("AA", "AB") and not 0 <= pair < PAIR_COUNTS[phase]:
        errors.append("formal pair is outside the planned range")
    probe = row.get("probe")
    if not isinstance(probe, dict):
        return errors + ["missing object probe"]
    case, size = row.get("case"), row.get("size")
    if case not in CASES or size not in SIZES:
        return errors + ["unexpected case or size"]
    iterations = (304 if warmup else 30304) if case == "overflow33" else (
        10000 if warmup else 1000000
    )
    if row.get("iterations") != iterations:
        errors.append("runner iterations differ from the planned workload")
    per_iteration = 33 if case == "overflow33" else 1
    packets = iterations * per_iteration
    cached = 0 if case == "cache0" else 32
    packet_checksum = sum((23 + 37 * index) % 256 for index in range(64))
    expected = {
        "schema": "zfstack-issue23-packetpool-v1",
        "status": "PASS",
        "case": case,
        "buf_size": size,
        "iterations": iterations,
        "seed": 23,
        "packets_per_iteration": per_iteration,
        "packets": packets,
        "bytes_per_packet": 64,
        "bytes": packets * 64,
        "capacity_bytes": packets * size,
        "checksum": packet_checksum * packets,
        "checksum_expected": packet_checksum * packets,
        "priming_packets": cached,
        "priming_checksum": packet_checksum * cached,
        "max_cached": cached,
        "budget_high": 64 * 1024 * 1024,
        "cached": cached,
        "in_use": 0,
        "reserved": cached * size,
        "peer_reserved": cached * size,
        "port_reserved": cached * size,
        "trimmed": cached,
        "post_trim_cached": 0,
        "post_trim_in_use": 0,
        "final_reserved": 0,
        "final_peer_reserved": 0,
        "final_port_reserved": 0,
    }
    for key, value in expected.items():
        if type(probe.get(key)) is not type(value) or probe[key] != value:
            errors.append(f"probe.{key}: expected {value!r}, got {probe.get(key)!r}")
    if not positive_number(probe.get("loop_wall_ns")):
        errors.append("probe.loop_wall_ns must be a positive finite number")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    args = parser.parse_args()
    rows, malformed = [], []
    for line_number, line in enumerate(args.raw.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
        except (ValueError, TypeError) as error:
            malformed.append({"line": line_number, "error": str(error)})
            continue
        errors = validate(row)
        rows.append({"line": line_number, "row": row, "errors": errors})

    report = {
        "scope": "shared-host public PacketPool diagnostic; no performance qualification",
        "raw": str(args.raw),
        "rows": len(rows),
        "malformed_rows": malformed,
        "invalid_rows": [
            {"line": item["line"], "errors": item["errors"]}
            for item in rows if item["errors"]
        ],
        "cpu_metric": "cpu_sec * 1_000_000_000 (nanoseconds)",
        "cpu_scope": "whole child process including setup, validation, cleanup and output",
        "warmup_rows": sum(item["row"].get("warmup") is True for item in rows),
        "warmups": [],
        "cells": [],
    }
    for arm in ("A1", "A2", "B"):
        phase = "AB" if arm == "B" else "AA"
        for case in CASES:
            for size in SIZES:
                matching = [
                    item for item in rows
                    if item["row"].get("warmup") is True
                    and item["row"].get("phase") == phase
                    and item["row"].get("arm") == arm
                    and item["row"].get("case") == case
                    and item["row"].get("size") == size
                ]
                report["warmups"].append({
                    "phase": phase, "arm": arm, "case": case, "size": size,
                    "lines": [item["line"] for item in matching],
                    "complete": len(matching) == 1 and not matching[0]["errors"],
                })
    for phase, expected_pairs in PAIR_COUNTS.items():
        for case in CASES:
            for size in SIZES:
                matching = [
                    item for item in rows
                    if item["row"].get("phase") == phase
                    and item["row"].get("warmup") is False
                    and item["row"].get("case") == case
                    and item["row"].get("size") == size
                ]
                grouped = {}
                for item in matching:
                    # Include invalid pair identifiers without silently merging
                    # them with a real ID or losing their invalid-row record.
                    key = json.dumps(item["row"].get("pair"), sort_keys=True)
                    grouped.setdefault(key, []).append(item)
                comparator = "A2" if phase == "AA" else "B"
                pairs, cpu_ratios, wall_ratios = [], [], []
                for items in grouped.values():
                    arms_in_order = [item["row"].get("arm") for item in items]
                    pair = {"pair": items[0]["row"].get("pair"), "lines": [item["line"] for item in items], "order": arms_in_order}
                    complete = (
                        len(items) == 2
                        and not any(item["errors"] for item in items)
                        and set(arms_in_order) == {"A1", comparator}
                    )
                    pair["complete"] = complete
                    if complete:
                        arms = {item["row"]["arm"]: item["row"] for item in items}
                        a, b = arms["A1"], arms[comparator]
                        cpu_ns = {arm: row["cpu_sec"] * 1_000_000_000 for arm, row in arms.items()}
                        cpu = cpu_ns[comparator] / cpu_ns["A1"]
                        wall = b["probe"]["loop_wall_ns"] / a["probe"]["loop_wall_ns"]
                        pair["process_cpu_ns"] = cpu_ns
                        pair["process_cpu_ns_per_packet"] = {
                            arm: cpu_ns[arm] / row["probe"]["packets"] for arm, row in arms.items()
                        }
                        pair["process_cpu_ratio"] = cpu
                        pair["loop_wall_ratio"] = wall
                        cpu_ratios.append(cpu)
                        wall_ratios.append(wall)
                    pairs.append(pair)
                report["cells"].append({
                    "phase": phase,
                    "case": case,
                    "size": size,
                    "ratio": f"{comparator}/A1",
                    "expected_pairs": expected_pairs,
                    "observed_pair_ids": len(pairs),
                    "complete_pairs": len(cpu_ratios),
                    "complete_planned_window": (
                        len(matching) == expected_pairs * 2
                        and len(pairs) == expected_pairs
                        and len(cpu_ratios) == expected_pairs
                    ),
                    "pairs": pairs,
                    "process_cpu": describe(cpu_ratios),
                    "loop_wall": describe(wall_ratios),
                })
    formal_count = sum(item["row"].get("warmup") is False for item in rows)
    report["formal_rows"] = formal_count
    report["complete_warmups"] = (
        report["warmup_rows"] == 27 and all(warmup["complete"] for warmup in report["warmups"])
    )
    report["complete_execution_indices"] = [item["row"].get("index") for item in rows] == list(range(315))
    report["complete_planned_window"] = (
        not malformed
        and not report["invalid_rows"]
        and formal_count == 288
        and report["complete_warmups"]
        and report["complete_execution_indices"]
        and all(cell["complete_planned_window"] for cell in report["cells"])
    )
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
