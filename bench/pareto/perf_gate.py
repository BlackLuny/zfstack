#!/usr/bin/env python3
"""Frozen ABBA/BAAB performance analysis. Only complete blocks are units.

Default: mean block log-ratio with Student t simultaneous intervals. An optional
predeclared paired whole-block bootstrap resamples the same block indices for
all metrics. Both require independent, representative blocks; calibration is
empirical evidence for this runner/profile, not proof of those assumptions.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys


METHODS = ("block_t", "paired_block_bootstrap")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def decode_json(text):
    def reject(value):
        raise ValueError(f"nonfinite JSON constant: {value}")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    value = json.loads(text, parse_constant=reject, object_pairs_hook=unique)
    canonical(value)  # Also rejects exponent overflow (1e999).
    return value


MAX_INPUT_BYTES = 64 * 1024 * 1024


def bounded_text(path):
    path = Path(path)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("input exceeds size limit")
    with path.open() as stream:
        data = stream.read(MAX_INPUT_BYTES + 1)
    if len(data.encode()) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds size limit")
    return data


def read_json(path):
    return decode_json(bounded_text(path))


def read_rows(path):
    data = bounded_text(path)
    if data.lstrip().startswith("["):
        return decode_json(data)
    return [decode_json(line) for line in data.splitlines() if line.strip()]


def finite(value, positive=False):
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _beta_fraction(a, b, x):
    # Lentz continued fraction for the regularized incomplete beta function.
    tiny, qab, qap, qam = 1e-300, a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1 / d
    h = d
    for m in range(1, 1001):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d, c = 1 + aa * d, 1 + aa / c
        if abs(d) < tiny: d = tiny
        if abs(c) < tiny: c = tiny
        d = 1 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d, c = 1 + aa * d, 1 + aa / c
        if abs(d) < tiny: d = tiny
        if abs(c) < tiny: c = tiny
        d = 1 / d
        change = d * c
        h *= change
        if abs(change - 1) < 1e-13:
            return h
    raise ValueError("Student t beta calculation did not converge")


def _beta(a, b, x):
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    factor = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                      + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return factor * _beta_fraction(a, b, x) / a
    return 1 - factor * _beta_fraction(b, a, 1 - x) / b


def student_t_quantile(probability, degrees):
    if not .5 < probability < 1 or degrees < 1:
        raise ValueError("invalid t quantile arguments")
    def cdf(t):
        return 1 - .5 * _beta(degrees / 2, .5, degrees / (degrees + t * t))
    low, high = 0.0, 1.0
    while cdf(high) < probability:
        high *= 2
    for _ in range(80):
        mid = (low + high) / 2
        if cdf(mid) < probability: low = mid
        else: high = mid
    return (low + high) / 2


def percentile(values, probability):
    values = sorted(values)
    position = (len(values) - 1) * probability
    lo, hi = math.floor(position), math.ceil(position)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def calibration_scope(plan):
    fields = ("runner_id", "profile", "case_id", "case_version", "config", "environment_hash",
              "metrics", "alpha", "method", "min_blocks", "max_abs_lag1", "max_abs_skew", "bootstrap_iterations")
    return digest({**{key: plan.get(key) for key in fields}, "block_count": len(plan.get("blocks", []))})


def validate_plan(plan):
    errors = []
    if not isinstance(plan, dict): return ["plan must be an object"]
    if plan.get("schema_version") != 1: errors.append("unsupported performance plan version")
    for key in ("runner_id", "profile", "case_id"):
        if not isinstance(plan.get(key), str) or not plan[key]: errors.append(f"missing {key}")
    if type(plan.get("case_version")) is not int or plan["case_version"] < 1: errors.append("case_version must be a positive integer")
    for key in ("build_manifest_hash", "environment_hash"):
        if not sha(plan.get(key)): errors.append(f"invalid {key}")
    if not isinstance(plan.get("config"), dict) or not plan["config"]: errors.append("missing complete effective config")
    if plan.get("mode") not in ("shadow", "enforce"): errors.append("mode must be shadow or enforce")
    if plan.get("comparison") not in ("AB", "AA"): errors.append("comparison must be AB or AA")
    if plan.get("method") not in METHODS: errors.append("method must be predeclared")
    if not finite(plan.get("alpha"), True) or plan["alpha"] >= .5: errors.append("invalid family alpha")
    if type(plan.get("seed")) is not int: errors.append("seed must be an integer")
    if type(plan.get("min_blocks")) is not int or plan["min_blocks"] < 2: errors.append("min_blocks must be at least 2")
    if not finite(plan.get("max_abs_lag1")) or plan["max_abs_lag1"] > 1: errors.append("invalid predeclared lag-1 threshold")
    if not finite(plan.get("max_abs_skew", 2.0), True): errors.append("invalid predeclared skew threshold")
    if plan.get("method") == "paired_block_bootstrap":
        if type(plan.get("bootstrap_iterations")) is not int or plan["bootstrap_iterations"] < 1000:
            errors.append("bootstrap_iterations must be at least 1000")
    blocks = plan.get("blocks", [])
    if not isinstance(blocks, list) or len(blocks) < max(2, plan.get("min_blocks", 2) if type(plan.get("min_blocks")) is int else 2):
        errors.append("insufficient frozen blocks")
    elif len(blocks) > 1024:
        errors.append("block count exceeds analysis resource budget")
    elif any(not isinstance(b, dict) or not isinstance(b.get("block_id"), str) or not b["block_id"] or b.get("order") not in ("ABBA", "BAAB") for b in blocks):
        errors.append("every block needs a unique ID and ABBA/BAAB order")
    elif len({b["block_id"] for b in blocks}) != len(blocks): errors.append("duplicate frozen block ID")
    binaries = plan.get("binary_sha256", {})
    if not isinstance(binaries, dict) or set(binaries) != {"A", "B"} or not all(sha(v) for v in binaries.values()):
        errors.append("missing immutable per-arm binary hashes")
    elif plan.get("comparison") == "AB" and binaries["A"] == binaries["B"]:
        errors.append("identical binary hashes require explicit AA comparison")
    metrics = plan.get("metrics", [])
    if not isinstance(metrics, list) or not metrics:
        errors.append("primary metric family must be nonempty")
    else:
        ids = []
        for metric in metrics:
            if not isinstance(metric, dict):
                errors.append("metric must be an object"); continue
            ids.append(metric.get("id"))
            if not isinstance(metric.get("id"), str) or not metric["id"]: errors.append("invalid metric ID")
            if metric.get("direction") not in ("lower", "higher"): errors.append("invalid metric direction")
            if not isinstance(metric.get("unit"), str) or not metric["unit"]: errors.append("missing metric unit")
            if "absolute_slo" in metric:
                slo = metric["absolute_slo"]
                bound = "max" if metric.get("direction") == "lower" else "min"
                if (not isinstance(slo, dict) or set(slo) != {"scope", bound}
                        or slo.get("scope") != "observed-per-run" or not finite(slo.get(bound))):
                    errors.append("absolute_slo requires observed-per-run scope and a finite directional max/min bound")
            scale = metric.get("scale", "log")
            if scale not in ("log", "absolute"): errors.append("invalid metric scale")
            budget = metric.get("budget_absolute" if scale == "absolute" else "budget_fraction")
            if not finite(budget): errors.append("missing finite nonnegative metric budget")
            elif scale == "log" and metric.get("direction") == "higher" and budget >= 1: errors.append("throughput drop budget must be < 1")
        if len(set(str(i) for i in ids)) != len(ids): errors.append("duplicate primary metric ID")
    if isinstance(metrics, list) and len(metrics) > 256:
        errors.append("metric count exceeds analysis resource budget")
    if (plan.get("method") == "paired_block_bootstrap" and isinstance(blocks, list) and isinstance(metrics, list)
            and type(plan.get("bootstrap_iterations")) is int
            and plan["bootstrap_iterations"] * len(blocks) * len(metrics) > 20_000_000):
        errors.append("bootstrap plan exceeds fixed analysis resource budget")
    try: canonical(plan)
    except (ValueError, TypeError): errors.append("plan contains noncanonical/nonfinite data")
    return errors


def _status(row, key):
    value = row.get(key)
    return value.get("status") if isinstance(value, dict) else value


def _measurements(row):
    return row.get("metrics", row.get("performance", {}).get("metrics", {}))


def block_values(plan, rows):
    errors, candidate_failures = [], []
    planned = {(block["block_id"], pos): arm for block in plan["blocks"] for pos, arm in enumerate(block["order"])}
    seen, intervals = {}, []
    expected_hash = digest(plan)
    for index, row in enumerate(rows):
        label = f"record {index}"
        if not isinstance(row, dict):
            errors.append(f"{label}: not an object"); continue
        key = (row.get("block_id"), row.get("position"))
        arm = row.get("arm")
        # A real candidate failure must survive invalid timestamps/identity too.
        if arm == "B" and _status(row, "functional") == "FAIL":
            candidate_failures.append(f"{label}: candidate functional failure")
        if not isinstance(key[0], str) or type(key[1]) is not int or not isinstance(arm, str):
            errors.append(f"{label}: malformed block/position/arm")
            continue
        if key not in planned or arm != planned.get(key):
            errors.append(f"{label}: unexpected block/position/arm")
        if key in seen: errors.append(f"{label}: duplicate block slot")
        seen[key] = row
        for field, wanted in (("plan_hash", expected_hash), ("config_hash", digest(plan["config"])),
                              ("build_manifest_hash", plan["build_manifest_hash"]),
                              ("environment_hash", plan["environment_hash"]),
                              ("binary_sha256", plan["binary_sha256"].get(arm)),
                              ("case_id", plan["case_id"])):
            if row.get(field) != wanted: errors.append(f"{label}: changed or missing {field}")
        validity = row.get("validity", {})
        if not isinstance(validity, dict) or validity.get("performance") != "VALID" or validity.get("functional") != "VALID":
            errors.append(f"{label}: evidence is not valid for both scopes")
        if _status(row, "functional") != "PASS": errors.append(f"{label}: functional result is not PASS")
        execution = row.get("execution", {})
        if execution.get("status") != "completed" or execution.get("exit_code") != 0:
            errors.append(f"{label}: process did not complete successfully")
        start, end = execution.get("start_ns"), execution.get("end_ns")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start or execution.get("clock") != "CLOCK_MONOTONIC":
            errors.append(f"{label}: missing or invalid monotonic execution interval")
        else:
            intervals.append((key, start, end))
        if execution.get("cleanup") != "PASS": errors.append(f"{label}: cleanup unverified")
        metrics = _measurements(row)
        if not isinstance(metrics, dict):
            errors.append(f"{label}: missing metrics"); continue
        for metric in plan["metrics"]:
            if not finite(metrics.get(metric["id"]), metric.get("scale", "log") == "log"):
                errors.append(f"{label}: invalid/zero/missing {metric['id']}")
    missing = set(planned) - set(seen)
    if missing: errors.append(f"incomplete frozen blocks: {sorted(missing)}")
    ordered = [(b["block_id"], p) for b in plan["blocks"] for p in range(4)]
    if [key for key, _, _ in intervals] != ordered:
        errors.append("records are not in the frozen chronological order")
    if any(a[2] > b[1] for a, b in zip(intervals, intervals[1:])):
        errors.append("overlapping executions in one physical performance domain")
    values = {m["id"]: [] for m in plan["metrics"]}
    if errors: return values, errors, candidate_failures
    for block in plan["blocks"]:
        for metric in plan["metrics"]:
            arms = {arm: [] for arm in "AB"}
            for position, arm in enumerate(block["order"]):
                value = _measurements(seen[(block["block_id"], position)])[metric["id"]]
                arms[arm].append(math.log(value) if metric.get("scale", "log") == "log" else value)
            z = statistics.mean(arms["B"]) - statistics.mean(arms["A"])
            values[metric["id"]].append(z if metric["direction"] == "lower" else -z)
    return values, errors, candidate_failures


def intervals(plan, values):
    count = len(plan["blocks"])
    per_metric_alpha = plan["alpha"] / len(plan["metrics"])
    draws = None
    if plan["method"] == "paired_block_bootstrap":
        rng = random.Random(plan["seed"])
        draws = [[rng.randrange(count) for _ in range(count)] for _ in range(plan["bootstrap_iterations"])]
    critical = student_t_quantile(1 - per_metric_alpha / 2, count - 1)
    summaries, diagnostics = {}, []
    if draws is not None and plan["bootstrap_iterations"] * per_metric_alpha / 2 < 10:
        diagnostics.append("bootstrap tail resolution is insufficient for the primary metric family")
    for metric in plan["metrics"]:
        v = values[metric["id"]]
        mean, sd = statistics.mean(v), statistics.stdev(v)
        if draws is None:
            half = critical * sd / math.sqrt(count)
            low, high = mean - half, mean + half
        else:
            boot = [statistics.mean(v[i] for i in draw) for draw in draws]
            low, high = percentile(boot, per_metric_alpha / 2), percentile(boot, 1 - per_metric_alpha / 2)
        centered = [x - mean for x in v]
        denom = sum(x * x for x in centered)
        lag1 = sum(a * b for a, b in zip(centered, centered[1:])) / denom if denom else 0.0
        skew = sum(x ** 3 for x in centered) / count / (sd ** 3) if sd else 0.0
        if count >= 8 and abs(skew) > plan.get("max_abs_skew", 2.0):
            diagnostics.append(f"{metric['id']}: block distribution skew exceeds frozen threshold")
        if count >= 8 and abs(lag1) > plan["max_abs_lag1"]:
            diagnostics.append(f"{metric['id']}: lag-1 dependence exceeds frozen threshold")
        if metric.get("scale", "log") == "absolute": budget = metric["budget_absolute"]
        elif metric["direction"] == "lower": budget = math.log1p(metric["budget_fraction"])
        else: budget = -math.log1p(-metric["budget_fraction"])
        status = "PASS" if high <= budget else "FAIL" if low > budget else "INCONCLUSIVE"
        summaries[metric["id"]] = {"unit": metric["unit"], "direction": metric["direction"],
                                   "scale": metric.get("scale", "log"), "blocks": count, "block_effects": v,
                                   "mean_degradation": mean, "interval": [low, high], "budget": budget,
                                   "per_metric_alpha": per_metric_alpha, "lag1": lag1, "sample_skew": skew, "status": status}
    return summaries, diagnostics


def absolute_slo(plan, rows):
    """Observed candidate-run bounds, independent of relative inferential evidence.

    Only identity-bound candidate slots can establish a violation. Missing,
    duplicate, malformed or incomplete slots prevent PASS. Environment validity
    does not erase an already observed absolute bound violation.
    """
    metrics = [metric for metric in plan["metrics"] if "absolute_slo" in metric]
    if not metrics:
        return None
    expected = {(block["block_id"], position) for block in plan["blocks"]
                for position, arm in enumerate(block["order"]) if arm == "B"}
    seen, invalid, assertions = set(), [], []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("arm") != "B":
            continue
        key = (row.get("block_id"), row.get("position"))
        identity = (("plan_hash", digest(plan)), ("config_hash", digest(plan["config"])),
                    ("build_manifest_hash", plan["build_manifest_hash"]), ("environment_hash", plan["environment_hash"]),
                    ("binary_sha256", plan["binary_sha256"]["B"]), ("case_id", plan["case_id"]))
        if (not isinstance(key[0], str) or type(key[1]) is not int or key not in expected
                or any(row.get(field) != value for field, value in identity)):
            invalid.append(f"candidate record {index}: identity does not match frozen SLO population")
            continue
        duplicate = key in seen
        seen.add(key)
        if duplicate:
            invalid.append(f"candidate record {index}: duplicate SLO population slot")
        measurements = _measurements(row)
        for metric in metrics:
            value = measurements.get(metric["id"]) if isinstance(measurements, dict) else None
            slo = metric["absolute_slo"]
            bound = "max" if metric["direction"] == "lower" else "min"
            valid = finite(value)
            violated = valid and (value > slo[bound] if bound == "max" else value < slo[bound])
            complete = (valid and not duplicate and _status(row, "functional") == "PASS"
                        and isinstance(row.get("validity"), dict) and row["validity"].get("functional") == "VALID"
                        and isinstance(row.get("execution"), dict) and row["execution"].get("status") == "completed"
                        and type(row["execution"].get("exit_code")) is int and row["execution"]["exit_code"] == 0
                        and row["execution"].get("cleanup") == "PASS")
            assertions.append({"id": f"absolute_slo.{metric['id']}.{key[0]}.{key[1]}",
                "status": "FAIL" if violated else "PASS" if complete else "NOT_RUN", "kind": "service",
                "detail": f"observed candidate value {value!r} {metric['unit']}; {bound} {slo[bound]}"})
    if seen != expected:
        invalid.append("missing frozen candidate slots for absolute SLO")
    statuses = [entry["status"] for entry in assertions]
    status = "FAIL" if "FAIL" in statuses else "NOT_RUN" if invalid or "NOT_RUN" in statuses or not statuses else "PASS"
    return {"status": status, "assertions": assertions, "errors": invalid,
            "scope": "Observed per candidate run only; not a tail/population probability guarantee."}


def evaluate(plan, rows, calibration=None):
    errors = validate_plan(plan)
    result = {"schema_version": 1, "plan_hash": None, "functional": "NOT_RUN",
              "validity": "INVALID", "performance": "INCONCLUSIVE", "decision": "BLOCKED",
              "metrics": {}, "errors": errors, "calibrated": False,
              "assumptions": ["Independent representative complete blocks, fixed sample endpoint and primary metric family.",
                              "Student t requires approximately normal block effects; bootstrap requires representative blocks.",
                              "Bonferroni simultaneous intervals do not repair dependent data or optional stopping."]}
    if errors: return result
    result["plan_hash"] = digest(plan)
    values, errors, failures = block_values(plan, rows)
    slo = absolute_slo(plan, rows)
    if slo is not None:
        result["absolute_slo"] = slo
        if slo["status"] == "FAIL":
            failures.append("candidate observed-per-run absolute SLO violation")
        elif slo["status"] != "PASS":
            errors.append("candidate absolute SLO evidence is incomplete")
    candidates = [row for row in rows if isinstance(row, dict) and row.get("arm") == "B"]
    expected_b = {(block["block_id"], pos) for block in plan["blocks"] for pos, arm in enumerate(block["order"]) if arm == "B"}
    candidate_slots = [(row.get("block_id"), row.get("position")) for row in candidates]
    candidate_complete = (len(candidates) == len(expected_b) and all(isinstance(key[0], str) and type(key[1]) is int for key in candidate_slots)
                          and set(candidate_slots) == expected_b
                          and all(_status(row, "functional") == "PASS" and isinstance(row.get("validity"), dict)
                                  and row["validity"].get("functional") == "VALID"
                                  and all(row.get(field) == wanted for field, wanted in
                                          (("plan_hash", digest(plan)), ("config_hash", digest(plan["config"])),
                                           ("binary_sha256", plan["binary_sha256"]["B"]),
                                           ("case_id", plan["case_id"]), ("build_manifest_hash", plan["build_manifest_hash"])))
                                  for row in candidates))
    result["functional"] = "FAIL" if failures else "PASS" if candidate_complete else "NOT_RUN"
    result["errors"] = errors + failures
    if failures: result["decision"] = "FAIL"
    if errors or failures: return result
    try:
        summaries, diagnostics = intervals(plan, values)
    except (ValueError, OverflowError) as error:
        result["errors"].append(f"statistics not representable: {error}")
        return result
    result.update(metrics=summaries, method=plan["method"], family_alpha=plan["alpha"], validity="VALID")
    result["errors"].extend(diagnostics)
    cal_ok = (isinstance(calibration, dict) and calibration.get("validated") is True
              and calibration.get("source") == "measured-held-out"
              and calibration.get("scope_hash") == calibration_scope(plan)
              and calibration.get("calibration_id") == plan.get("calibration_id")
              and sha(plan.get("calibration_hash")) and digest(calibration) == plan["calibration_hash"])
    result["calibrated"] = cal_ok
    statuses = [m["status"] for m in summaries.values()]
    statistical = "FAIL" if "FAIL" in statuses else "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "PASS"
    result["statistical_status"] = statistical
    if diagnostics:
        result["errors"].append("dependence diagnostic failed; no post-hoc method switching")
    if not cal_ok:
        result["errors"].append("no matching validated and hash-bound calibration; shadow evidence only")
    if cal_ok and not diagnostics:
        result["performance"] = statistical
    result["decision"] = ("SHADOW" if plan["mode"] == "shadow" else
                          "PASS" if result["performance"] == "PASS" else
                          "FAIL" if result["performance"] == "FAIL" else "BLOCKED")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--rows", type=Path, required=True)
    p.add_argument("--calibration", type=Path)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    try:
        report = evaluate(read_json(a.plan), read_rows(a.rows), read_json(a.calibration) if a.calibration else None)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        report = {"validity": "INVALID", "performance": "INCONCLUSIVE", "decision": "BLOCKED", "errors": [str(error)]}
    a.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: report.get(key) for key in ("decision", "performance", "errors")}, indent=2))
    return 1 if report["decision"] == "FAIL" else 0 if report["decision"] in ("PASS", "SHADOW") else 2


if __name__ == "__main__":
    raise SystemExit(main())
