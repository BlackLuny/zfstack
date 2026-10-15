"""Adversarial synthetic evidence tests; these do not qualify a real runner."""
import copy
import math
from pathlib import Path
import tempfile
import unittest

from calibrate import calibrate
from perf_gate import calibration_scope, digest, evaluate, read_json, student_t_quantile, validate_plan


def plan(blocks=20, method="block_t", comparison="AB"):
    return {"schema_version": 1, "runner_id": "fixture-node", "profile": "release", "case_id": "arbitrary-C16",
            "case_version": 1, "mode": "enforce", "comparison": comparison,
            "config": {"light_conns": 3, "churn": 0, "udp": True, "runtime": 4, "mtu": 1420},
            "binary_sha256": {"A": "a" * 64, "B": "b" * 64},
            "build_manifest_hash": "c" * 64, "environment_hash": "d" * 64,
            "blocks": [{"block_id": f"block-{i}", "order": "ABBA" if i % 2 == 0 else "BAAB"} for i in range(blocks)],
            "metrics": [{"id": "cost", "direction": "lower", "unit": "CPU-sec/GiB", "budget_fraction": .03}],
            "alpha": .05, "method": method, "bootstrap_iterations": 2000, "seed": 17,
            "min_blocks": 2, "max_abs_lag1": .95, "calibration_id": "fixture-only"}


def calibrated(p):
    cal = {"validated": True, "source": "measured-held-out", "scope_hash": calibration_scope(p),
           "calibration_id": p["calibration_id"], "errors": []}
    p["calibration_hash"] = digest(cal)
    return cal


def rows(p, factor=1.0, run_prefix="fixture"):
    result = []
    for block_index, block in enumerate(p["blocks"]):
        for position, arm in enumerate(block["order"]):
            index = block_index * 4 + position
            result.append({"run_id": f"{run_prefix}:{index}", "case_id": p["case_id"],
                           "block_id": block["block_id"], "position": position, "arm": arm,
                           "plan_hash": digest(p), "config_hash": digest(p["config"]),
                           "binary_sha256": p["binary_sha256"][arm], "build_manifest_hash": p["build_manifest_hash"],
                           "environment_hash": p["environment_hash"],
                           "validity": {"functional": "VALID", "performance": "VALID"}, "functional": {"status": "PASS"},
                           "execution": {"status": "completed", "exit_code": 0, "cleanup": "PASS", "clock": "CLOCK_MONOTONIC",
                                         "start_ns": index * 100 + 1, "end_ns": index * 100 + 99},
                           "metrics": {m["id"]: 100 * (factor if arm == "B" else 1) for m in p["metrics"]}})
    return result


class StatisticalGate(unittest.TestCase):
    def test_case_version_is_a_positive_integer(self):
        for value in ("1", 0, -1, True, None, 1.0):
            p = plan(); p["case_version"] = value
            self.assertIn("case_version must be a positive integer", validate_plan(p))
        self.assertEqual(validate_plan(plan()), [])

    def test_student_t_quantiles_match_reference_values(self):
        self.assertAlmostEqual(student_t_quantile(.975, 1), 12.706204736, places=7)
        self.assertAlmostEqual(student_t_quantile(.975, 10), 2.228138852, places=8)
        self.assertAlmostEqual(student_t_quantile(.995, 30), 2.749995654, places=8)

    def test_pass_fail_inconclusive_and_calibration_binding(self):
        p = plan(); cal = calibrated(p)
        self.assertEqual(evaluate(p, rows(p), cal)["performance"], "PASS")
        self.assertEqual(evaluate(p, rows(p, 1.10), cal)["performance"], "FAIL")
        samples = rows(p)
        for i, row in enumerate(samples):
            if row["arm"] == "B": row["metrics"]["cost"] *= 1.03 + (.3 if (i // 4) % 2 else -.2)
        self.assertEqual(evaluate(p, samples, cal)["performance"], "INCONCLUSIVE")
        self.assertEqual(evaluate(p, rows(p, 1.10))["performance"], "INCONCLUSIVE")
        bad = copy.deepcopy(cal); bad["scope_hash"] = "e" * 64
        self.assertFalse(evaluate(p, rows(p), bad)["calibrated"])

    def test_cost_and_throughput_budgets_have_distinct_transforms(self):
        p = plan(); p["metrics"].append({"id": "goodput", "direction": "higher", "unit": "Mbit/s", "budget_fraction": .03})
        cal = calibrated(p); report = evaluate(p, rows(p), cal)
        self.assertAlmostEqual(report["metrics"]["cost"]["budget"], math.log(1.03))
        self.assertAlmostEqual(report["metrics"]["goodput"]["budget"], -math.log(.97))
        self.assertEqual(report["metrics"]["goodput"]["per_metric_alpha"], .025)
        samples = rows(p)
        for row in samples:
            if row["arm"] == "B": row["metrics"]["goodput"] = 90
        self.assertEqual(evaluate(p, samples, cal)["performance"], "FAIL")

    def test_bootstrap_is_fixed_seed_paired_whole_block(self):
        p = plan(method="paired_block_bootstrap"); cal = calibrated(p); sample = rows(p)
        for row in sample:
            if row["arm"] == "B": row["metrics"]["cost"] *= 1 + int(row["block_id"].split("-")[1]) * .001
        first, second = evaluate(p, sample, cal), evaluate(p, sample, cal)
        self.assertEqual(first, second)
        self.assertEqual(len(first["metrics"]["cost"]["block_effects"]), 20)
        self.assertEqual(first["metrics"]["cost"]["blocks"], 20)

    def test_incomplete_duplicate_overlap_and_changed_order_are_invalid(self):
        p = plan(); cal = calibrated(p)
        mutations = [lambda r: r.pop(), lambda r: r.append(copy.deepcopy(r[0])),
                     lambda r: r[1]["execution"].update(start_ns=2), lambda r: r.reverse()]
        for mutate in mutations:
            sample = rows(p); mutate(sample)
            with self.subTest(mutate=mutate):
                report = evaluate(p, sample, cal)
                self.assertEqual(report["validity"], "INVALID")
                self.assertEqual(report["performance"], "INCONCLUSIVE")

    def test_failure_cannot_be_hidden_by_invalid_or_missing_other_arm(self):
        p = plan(); cal = calibrated(p); sample = rows(p)
        candidate = next(r for r in sample if r["arm"] == "B")
        candidate["functional"]["status"] = "FAIL"
        candidate["validity"]["performance"] = "INVALID"
        sample.pop()
        report = evaluate(p, sample, cal)
        self.assertEqual((report["decision"], report["functional"]), ("FAIL", "FAIL"))
        self.assertEqual(evaluate(p, [], cal)["decision"], "BLOCKED")

    def test_all_config_and_plan_changes_are_rejected(self):
        p = plan(); cal = calibrated(p); sample = rows(p)
        for key, changed in (("light_conns", 4), ("churn", 1), ("udp", False), ("runtime", 8), ("mtu", 1500)):
            altered = copy.deepcopy(p); altered["config"][key] = changed
            self.assertEqual(evaluate(altered, sample, cal)["validity"], "INVALID")
        altered = copy.deepcopy(p); altered["metrics"][0]["budget_fraction"] = .50
        self.assertEqual(evaluate(altered, sample, cal)["validity"], "INVALID")

    def test_nonfinite_negative_missing_and_zero_metrics_are_not_passes(self):
        p = plan(); cal = calibrated(p)
        for value in (float("nan"), float("inf"), -1, 0, None, True, "1"):
            sample = rows(p); sample[0]["metrics"]["cost"] = value
            self.assertEqual(evaluate(p, sample, cal)["validity"], "INVALID")
        with tempfile.TemporaryDirectory() as tmp:
            for text in ('{"x":NaN}', '{"x":1e999}', '{"x":1,"x":2}'):
                path = Path(tmp) / "bad.json"; path.write_text(text)
                with self.assertRaises(ValueError): read_json(path)

    def test_explicit_aa_allows_same_binary_only_there(self):
        p = plan(comparison="AA"); p["binary_sha256"]["B"] = p["binary_sha256"]["A"]
        cal = calibrated(p)
        self.assertEqual(evaluate(p, rows(p), cal)["performance"], "PASS")
        p["comparison"] = "AB"
        self.assertEqual(evaluate(p, rows(p), cal)["validity"], "INVALID")

    def test_predeclared_absolute_metric_handles_zero_baseline(self):
        p = plan(); p["metrics"] = [{"id": "zero", "direction": "lower", "scale": "absolute", "unit": "bytes", "budget_absolute": 1}]
        cal = calibrated(p); sample = rows(p)
        for row in sample: row["metrics"]["zero"] = 0
        self.assertEqual(evaluate(p, sample, cal)["performance"], "PASS")


class ObservedAbsoluteSLO(unittest.TestCase):
    def fixture(self, bound=100, direction="lower"):
        p = plan()
        p["metrics"][0].update(direction=direction, absolute_slo={"scope": "observed-per-run", "max" if direction == "lower" else "min": bound})
        return p, calibrated(p)

    def test_fixed_candidate_value_bound_is_distinct_from_relative_degradation(self):
        p, cal = self.fixture(99)
        report = evaluate(p, rows(p), cal)  # A=B=100: relative non-inferiority alone passes.
        self.assertEqual((report["functional"], report["decision"], report["absolute_slo"]["status"]), ("FAIL", "FAIL", "FAIL"))
        p, cal = self.fixture(100)
        self.assertEqual(evaluate(p, rows(p), cal)["absolute_slo"]["status"], "PASS")
        self.assertEqual(evaluate(p, rows(p), cal)["decision"], "PASS")

    def test_lower_cost_max_and_higher_throughput_min_are_directional(self):
        p, cal = self.fixture(101, "higher")
        self.assertEqual(evaluate(p, rows(p), cal)["functional"], "FAIL")
        p, cal = self.fixture(100, "higher")
        self.assertEqual(evaluate(p, rows(p), cal)["decision"], "PASS")

    def test_single_violation_cannot_be_averaged_away_or_hidden_by_missing_data(self):
        p, cal = self.fixture(105)
        sample = rows(p)
        next(row for row in sample if row["arm"] == "B")["metrics"]["cost"] = 106
        for row in sample: row["validity"]["performance"] = "INVALID"
        sample.pop()  # Missing evidence and contamination cannot erase observed service failure.
        report = evaluate(p, sample, cal)
        self.assertEqual((report["functional"], report["decision"]), ("FAIL", "FAIL"))
        self.assertEqual(report["validity"], "INVALID")
        self.assertIn("not a tail/population", report["absolute_slo"]["scope"])

    def test_unrelated_arm_wrong_identity_and_missing_values_cannot_prove_candidate_bound(self):
        p, cal = self.fixture(105)
        sample = rows(p)
        for row in sample:
            if row["arm"] == "A": row["metrics"]["cost"] = 1000
        self.assertEqual(evaluate(p, sample, cal)["absolute_slo"]["status"], "PASS")
        row = next(row for row in sample if row["arm"] == "B")
        row.update(binary_sha256="e" * 64); row["metrics"]["cost"] = 1000
        self.assertEqual(evaluate(p, sample, cal)["absolute_slo"]["status"], "NOT_RUN")
        sample = rows(p); sample.pop()
        self.assertEqual(evaluate(p, sample, cal)["absolute_slo"]["status"], "NOT_RUN")
        sample = rows(p); next(row for row in sample if row["arm"] == "B")["metrics"].clear()
        self.assertEqual(evaluate(p, sample, cal)["absolute_slo"]["status"], "NOT_RUN")

    def test_absolute_slo_schema_is_strict_and_bound_into_plan_and_calibration(self):
        p, cal = self.fixture()
        for slo in ({"scope": "population-p99", "max": 1}, {"scope": "observed-per-run", "min": 1},
                    {"scope": "observed-per-run", "max": 1, "min": 0}, {"scope": "observed-per-run", "max": True},
                    {"scope": "observed-per-run", "max": float("inf")}, {"scope": "observed-per-run", "max": -1}):
            changed = copy.deepcopy(p); changed["metrics"][0]["absolute_slo"] = slo
            with self.subTest(slo=slo): self.assertTrue(validate_plan(changed))
        changed = copy.deepcopy(p); changed["metrics"][0]["absolute_slo"]["max"] = 200
        self.assertNotEqual(calibration_scope(changed), calibration_scope(p))
        self.assertEqual(evaluate(changed, rows(p), cal)["decision"], "BLOCKED")
        self.assertNotIn("absolute_slo", evaluate(plan(), rows(plan())))


class CalibrationTests(unittest.TestCase):
    def fixture(self):
        target = plan()
        spec = {"schema_version": 1, "calibration_id": "fixture-calibration", "target_plan": target,
                "min_training_blocks": 20, "min_null_trials": 80, "min_delta_trials": 20,
                "max_false_positive_rate": .05, "min_detection_rate": .80, "trials": []}
        datasets = {}
        kinds = ["run-AA", "build-AA"] + ["held-out-null"] * 80 + ["held-out-delta"] * 20
        for i, kind in enumerate(kinds):
            name = f"synthetic-test-{i}"
            trial = {"trial_id": name, "kind": kind}
            p = copy.deepcopy(target)
            p["comparison"] = "AB" if kind == "held-out-delta" else "AA"
            if kind in ("run-AA", "held-out-null"): p["binary_sha256"]["B"] = p["binary_sha256"]["A"]
            provenance = {"same_source_verified": True, "independent_build_manifest_hashes": ["c" * 64, "d" * 64],
                          "independent_build_invocation_ids": ["fixture-build-1", "fixture-build-2"]}
            if kind == "held-out-delta":
                trial["known_delta"] = {"cost": .15}
                provenance = {"kind": "controlled-work", "control_artifact_sha256": "f" * 64, "synthetic": False}
            spec["trials"].append(trial)
            datasets[name] = {"plan": p, "rows": rows(p, 1.15 if kind == "held-out-delta" else 1, name), "provenance": provenance}
        return spec, datasets

    def test_calibration_requires_two_aa_levels_heldout_power_and_false_positive_bounds(self):
        spec, data = self.fixture()
        report = calibrate(spec, data)
        self.assertTrue(report["validated"], report["errors"])
        self.assertLess(report["false_positive_95_interval"][1], .05)
        self.assertGreater(report["detection_95_interval"][0], .8)
        # These synthetic unit-test records are deliberately never persisted as
        # a real node qualification artifact.
        for name, item in data.items():
            if name.endswith("101"):
                item["provenance"]["synthetic"] = True
        self.assertFalse(calibrate(spec, data)["validated"])

    def test_reusing_training_as_heldout_is_rejected(self):
        spec, data = self.fixture()
        data["synthetic-test-2"]["rows"] = copy.deepcopy(data["synthetic-test-0"]["rows"])
        result = calibrate(spec, data)
        self.assertFalse(result["validated"])
        self.assertIn("reused training/held-out", str(result["errors"]))

    def test_reproducible_independent_builds_can_have_identical_hashes(self):
        spec, data = self.fixture()
        trial = data["synthetic-test-1"]
        trial["plan"]["binary_sha256"]["B"] = trial["plan"]["binary_sha256"]["A"]
        trial["rows"] = rows(trial["plan"], run_prefix="synthetic-test-1")
        trial["provenance"]["independent_build_manifest_hashes"] = ["c" * 64, "c" * 64]
        self.assertTrue(calibrate(spec, data)["validated"])
        trial["provenance"]["independent_build_invocation_ids"] = ["same-invocation", "same-invocation"]
        self.assertFalse(calibrate(spec, data)["validated"])

    def test_undetected_delta_and_false_positives_never_qualify(self):
        spec, data = self.fixture()
        for trial in spec["trials"]:
            d = data[trial["trial_id"]]
            factor = 1.10 if trial["kind"] == "held-out-null" else 1
            d["rows"] = rows(d["plan"], factor, trial["trial_id"])
        report = calibrate(spec, data)
        self.assertFalse(report["validated"])
        self.assertIn("false-positive", str(report["errors"]))
        self.assertIn("detection lower", str(report["errors"]))


if __name__ == "__main__":
    unittest.main()
