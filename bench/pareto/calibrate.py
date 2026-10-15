#!/usr/bin/env python3
"""Validate a runner/profile against measured A/A and held-out controls.

The spec freezes acceptance criteria before collecting controls. Input datasets
are real runner records, never candidate records multiplied by a synthetic
factor. No calibration is shipped as validated. Keep the report and every
hashed input; an enforcement plan must bind the complete report's hash.
"""
import argparse
import json
import math
from pathlib import Path
import sys

from perf_gate import (block_values, calibration_scope, canonical, digest, finite,
                       intervals, read_json, read_rows, validate_plan)
from perf_gate import sha


def wilson(successes, total, z=1.959963984540054):
    if not total: return [0.0, 1.0]
    p, zz = successes / total, z * z
    center = (p + zz / (2 * total)) / (1 + zz / total)
    half = z * math.sqrt(p * (1 - p) / total + zz / (4 * total ** 2)) / (1 + zz / total)
    return [max(0.0, center - half), min(1.0, center + half)]


def calibrate(spec, datasets):
    """datasets maps frozen trial IDs to {plan, rows, provenance?}."""
    errors, trials, used_ids = [], [], set()
    target = spec.get("target_plan", {})
    errors.extend(validate_plan(target))
    for key in ("min_training_blocks", "min_null_trials", "min_delta_trials"):
        if type(spec.get(key)) is not int or spec[key] < (20 if key == "min_training_blocks" else 1):
            errors.append(f"invalid frozen {key}")
    for key in ("max_false_positive_rate", "min_detection_rate"):
        if not finite(spec.get(key)) or not 0 < spec[key] < 1:
            errors.append(f"invalid frozen {key}")
    if spec.get("schema_version") != 1 or not isinstance(spec.get("calibration_id"), str) or not spec["calibration_id"]:
        errors.append("invalid calibration identity/version")
    schedule = spec.get("trials")
    if not isinstance(schedule, list) or not schedule:
        errors.append("calibration requires a frozen training / held-out trial schedule")
    if errors:
        return {"schema_version": 1, "validated": False, "source": "measured-held-out", "errors": errors}
    scope = calibration_scope(target)
    expected = [item.get("trial_id") for item in schedule]
    if len(set(expected)) != len(expected) or set(expected) != set(datasets):
        errors.append("trial set differs from frozen calibration schedule")
    null_flags, detection_flags, training_blocks = [], [], {"run-AA": 0, "build-AA": 0}
    inputs = []
    metric_detection = {metric["id"]: [] for metric in target["metrics"]}
    for item in schedule:
        trial_id, kind = item.get("trial_id"), item.get("kind")
        data = datasets.get(trial_id)
        if not isinstance(data, dict):
            errors.append(f"{trial_id}: missing dataset"); continue
        plan, rows = data.get("plan"), data.get("rows")
        bad = validate_plan(plan)
        if bad:
            errors.extend(f"{trial_id}: {e}" for e in bad); continue
        if calibration_scope(plan) != scope:
            errors.append(f"{trial_id}: runner/profile/config/statistical policy changed")
        if not isinstance(rows, list):
            errors.append(f"{trial_id}: rows are missing"); continue
        ids = [row.get("run_id") for row in rows if isinstance(row, dict)]
        if len(ids) != len(rows) or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            errors.append(f"{trial_id}: unique run IDs are required")
        if used_ids.intersection(ids): errors.append(f"{trial_id}: reused training/held-out run IDs")
        used_ids.update(ids)
        values, bad, functional = block_values(plan, rows)
        if bad or functional:
            errors.extend(f"{trial_id}: {e}" for e in bad + functional); continue
        summaries, diagnostics = intervals(plan, values)
        if diagnostics:
            errors.extend(f"{trial_id}: {e}" for e in diagnostics)
        inputs.append({"trial_id": trial_id, "plan_hash": digest(plan), "records_hash": digest(rows),
                       "provenance_hash": digest(data.get("provenance"))})
        outcome = {"trial_id": trial_id, "kind": kind, "metrics": summaries}
        if kind in training_blocks:
            if plan["comparison"] != "AA": errors.append(f"{trial_id}: training must be explicit AA")
            same = plan["binary_sha256"]["A"] == plan["binary_sha256"]["B"]
            if kind == "run-AA" and not same: errors.append(f"{trial_id}: run-AA needs one identical binary")
            provenance = data.get("provenance", {})
            if kind == "build-AA":
                hashes = provenance.get("independent_build_manifest_hashes")
                invocations = provenance.get("independent_build_invocation_ids")
                if (provenance.get("same_source_verified") is not True
                        or not isinstance(hashes, list) or len(hashes) != 2 or not all(sha(h) for h in hashes)
                        or not isinstance(invocations, list) or len(invocations) != 2
                        or not all(isinstance(value, str) and value for value in invocations)
                        or invocations[0] == invocations[1]):
                    errors.append(f"{trial_id}: missing independent same-source build provenance")
            training_blocks[kind] += len(plan["blocks"])
        elif kind == "held-out-null":
            if plan["comparison"] != "AA": errors.append(f"{trial_id}: null must be explicit AA")
            false_positive = any(m["interval"][0] > 0 or m["interval"][1] < 0 for m in summaries.values())
            null_flags.append(false_positive)
            outcome["false_positive"] = false_positive
        elif kind == "held-out-delta":
            delta = item.get("known_delta", {})
            provenance = data.get("provenance", {})
            if (provenance.get("kind") not in ("controlled-work", "controlled-delay", "known-regression-binary")
                    or not provenance.get("control_artifact_sha256") or provenance.get("synthetic") is not False):
                errors.append(f"{trial_id}: requires measured known-delta control provenance, not synthetic rescaling")
            if not delta or any(name not in summaries or not finite(value, True) for name, value in delta.items()):
                errors.append(f"{trial_id}: missing positive known-delta primary metric set")
                detected = False
            else:
                # Delta controls must exceed the actual product budget. A tiny
                # detectable effect does not certify regression-gate power.
                detected = True
                for name, value in delta.items():
                    metric = next(m for m in plan["metrics"] if m["id"] == name)
                    budget = metric.get("budget_absolute" if metric.get("scale", "log") == "absolute" else "budget_fraction")
                    if value <= budget:
                        errors.append(f"{trial_id}: known delta must exceed {name}'s budget")
                    metric_detection[name].append(summaries[name]["status"] == "FAIL")
                    detected &= summaries[name]["status"] == "FAIL"
            detection_flags.append(detected)
            outcome["detected"] = detected
        else:
            errors.append(f"{trial_id}: unknown trial kind")
        trials.append(outcome)
    for kind, count in training_blocks.items():
        if count < spec["min_training_blocks"]: errors.append(f"insufficient {kind} training blocks")
    if len(null_flags) < spec["min_null_trials"]: errors.append("insufficient held-out null trials")
    if len(detection_flags) < spec["min_delta_trials"]: errors.append("insufficient held-out delta trials")
    metric_power = {name: {"trials": len(flags), "detected": sum(flags), "interval": wilson(sum(flags), len(flags))}
                    for name, flags in metric_detection.items()}
    for name, power in metric_power.items():
        if power["trials"] < spec["min_delta_trials"]:
            errors.append(f"{name}: insufficient held-out delta trials")
        if power["interval"][0] < spec["min_detection_rate"]:
            errors.append(f"{name}: held-out detection lower bound is below policy")
    fpr = wilson(sum(null_flags), len(null_flags))
    power = wilson(sum(detection_flags), len(detection_flags))
    if fpr[1] > spec["max_false_positive_rate"]:
        errors.append("held-out null false-positive upper bound exceeds policy")
    if power[0] < spec["min_detection_rate"]:
        errors.append("held-out known-delta detection lower bound is below policy")
    return {"schema_version": 1, "calibration_id": spec["calibration_id"], "scope_hash": scope,
            "source": "measured-held-out", "validated": not errors, "spec_hash": digest(spec),
            "errors": errors, "training_blocks": training_blocks, "trials": trials, "inputs": inputs,
            "null_trials": len(null_flags), "false_positives": sum(null_flags), "false_positive_95_interval": fpr,
            "metric_detection": metric_power, "delta_trials": len(detection_flags), "detected": sum(detection_flags), "detection_95_interval": power,
            "limitations": ["Wilson binomial bounds assume independent held-out trials.",
                            "Control provenance and immutable build manifests require trusted orchestrator verification.",
                            "No synthetic-data tests certify an actual runner; environment changes require new calibration."]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--spec", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    try:
        spec = read_json(a.spec)
        datasets = {}
        for trial in spec["trials"]:
            datasets[trial["trial_id"]] = {"plan": read_json(a.spec.parent / trial["plan_file"]),
                                            "rows": read_rows(a.spec.parent / trial["rows_file"]),
                                            "provenance": read_json(a.spec.parent / trial["provenance_file"]) if trial.get("provenance_file") else {}}
        result = calibrate(spec, datasets)
    except (OSError, ValueError, KeyError, TypeError) as error:
        result = {"validated": False, "errors": [str(error)]}
    a.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"validated": result["validated"], "errors": result["errors"]}, indent=2))
    return 0 if result["validated"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
