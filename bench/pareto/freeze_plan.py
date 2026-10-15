#!/usr/bin/env python3
"""Prepare a shadow plan from real immutable inputs, never qualify a runner.

The reference probe run is exploratory and never enters confirmation samples.
Its workload counters, not its timing metrics, become the fixed-work contract.
"""
import argparse
import hashlib
from pathlib import Path
import random

from analyze import EXACT_FIELDS, WORKLOAD_FIELDS, validate_row
from perf_gate import canonical, digest, read_json, validate_plan


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def freeze(*, baseline, candidate, reference, environment, build_manifest, runner_id,
           profile, case_id, case_version, comparison, trial_id, cpu, blocks, seed,
           timeout, budget, probe_args):
    binaries = {"A": file_hash(baseline), "B": file_hash(candidate)}
    parsed, errors = validate_row({"variant": "baseline", "pair": 0, "exit_code": 0,
                                   "binary_sha256": binaries["A"], "measurements": [reference]}, 0, {},
                                  {"name": case_id, "args": probe_args})
    if errors: raise ValueError("reference is not valid fixed-work evidence: " + "; ".join(errors))
    if build_manifest.get("profile") != profile:
        raise ValueError("profile must match the actual build manifest")
    if environment.get("affinity") != [cpu]:
        raise ValueError("environment preflight must actually observe the selected CPU affinity")
    rng = random.Random(seed)
    plan = {"schema_version": 1, "runner_id": runner_id, "profile": profile,
            "case_id": case_id, "case_version": case_version, "mode": "shadow", "comparison": comparison,
            "binary_sha256": binaries, "build_manifest_hash": digest(build_manifest), "environment_hash": digest(environment),
            "reference_measurement_hash": digest(reference),
            "config": {"args": probe_args, "cpu": cpu, "timeout_sec": timeout,
                       "max_output_bytes": 1024 * 1024, "sample_interval_sec": .1,
                       "expected_measurement": {key: reference[key] for key in (*WORKLOAD_FIELDS, *EXACT_FIELDS)
                                                if key in reference and reference[key] is not None}},
            "blocks": [{"block_id": f"{trial_id}-{i}", "order": rng.choice(("ABBA", "BAAB"))} for i in range(blocks)],
            "metrics": [{"id": "cpu_primary", "direction": "lower", "unit": "CPU-sec/decimal-GB" if reference["effective_bytes"] else "CPU-ns/operation",
                         "budget_fraction": budget}],
            "alpha": .05, "method": "block_t", "min_blocks": 2,
            "max_abs_lag1": .5, "max_abs_skew": 2.0, "seed": seed}
    errors = validate_plan(plan)
    if errors: raise ValueError("invalid frozen plan: " + "; ".join(errors))
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--build-manifest", type=Path, required=True)
    parser.add_argument("--runner-id", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--case-version", type=int, default=1)
    parser.add_argument("--comparison", choices=("AA", "AB"), required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--cpu", type=int, required=True)
    parser.add_argument("--blocks", type=int, default=20)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--budget", type=float, required=True, help="exploratory relative budget, not a validated product threshold")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("probe_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    values = vars(args).copy(); output = values.pop("output")
    if values["probe_args"][:1] == ["--"]: values["probe_args"] = values["probe_args"][1:]
    if not values["probe_args"]: parser.error("probe arguments must follow --")
    for name in ("reference", "environment", "build_manifest"):
        values[name] = read_json(values[name])
    try: plan = freeze(**values)
    except (ValueError, TypeError, KeyError, OSError) as error: parser.error(str(error))
    with output.open("xb") as stream:
        stream.write(canonical(plan) + b"\n")
    print(digest(plan))


if __name__ == "__main__":
    main()
