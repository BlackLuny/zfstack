"""Synthetic contract fixtures; no fixture constitutes measured calibration."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import time
import sys
import types
import tarfile
import unittest
from unittest.mock import patch

from qa.common import EvidenceError, canonical, digest, file_hash, read_json, write_json
from qa.evaluate import evaluate
from qa.archive import archive, stage
from qa.perf_run import _copy_calibration, _verify_staged_calibration, run_performance_case
from qa.performance import engine, tool, verify_performance, read_experiment_rows, inspect_rows
from qa.plan import make_plan
from qa.tests.test_identity_process import environment_fixture, manifest_fixture, SHA
from qa.validate import validate_result


class PerformanceIntegration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.policy = self.root / "policy"; self.policy.mkdir()
        self.binary = self.root / "pareto_probe"; self.binary.write_bytes(b"synthetic fixture, never executable")
        self.manifest = manifest_fixture()
        self.manifest["binaries"] = [{"name": self.binary.name, "sha256": file_hash(self.binary), "size_bytes": self.binary.stat().st_size}]
        self.environment = environment_fixture()
        self.environment.update(kernel="fixture-kernel", cpuinfo="model name\t: fixture CPU\n")
        self.perf_environment = {"kernel": "fixture-kernel", "cpu_model": "fixture CPU", "affinity": [0], "governors": {},
                                "cgroup_limits": [{"path": "/sys/fs/cgroup", "cpu.max": "max 100000",
                                                   "cpuset.cpus.effective": "0", "memory.max": "max"}]}
        self.measurement = {"tool": "zfstack-pareto-probe", "schema_version": 2, "ok": True,
                            "case": "budget", "iterations": 1, "flows": 1, "peers": 1, "payload_bytes": 1,
                            "work_wall_ns": 200, "work_cpu_ns": 100, "operations": 1, "effective_bytes": 0,
                            "allocation_requested_bytes": 1, "checksum_u64": 0, "expected_checksum_u64": 0,
                            "final_reserved_bytes": 0, "all_budget_leases_released": True,
                            "virtual_clock_used_for_rate": False, "real_network_io": False}
        self.definition = {"case_id": "QA-performance", "case_version": 1, "layer": "P0", "profile": "release",
                  "runner": "external-performance", "features": [], "toolchain": "1.94.1", "timeout_seconds": 10,
                  "performance": {"mode": "shadow", "metrics": ["cpu_primary"]},
                  "config": {"args": ["budget", "--iterations", "1", "--flows", "1", "--payload", "1"],
                             "timeout_sec": 2, "max_output_bytes": 1048576, "sample_interval_sec": .1,
                             "comparison": "AA", "blocks": 2}}
        stable = {key: self.definition["config"][key] for key in ("args", "timeout_sec", "max_output_bytes", "sample_interval_sec")}
        self.experiment = {"schema_version": 1, "runner_id": "fixture", "profile": "release", "case_id": "QA-performance",
                "case_version": 1, "mode": "shadow", "comparison": "AA", "binary_sha256": dict.fromkeys("AB", file_hash(self.binary)),
                "build_manifest_hash": digest(self.manifest), "environment_hash": digest(self.perf_environment),
                "config": {**stable, "cpu": 0, "expected_measurement": {key: value for key, value in self.measurement.items()
                                                                       if key in (*tool("analyze").WORKLOAD_FIELDS, *tool("analyze").EXACT_FIELDS)}},
                "blocks": [{"block_id": "first", "order": "ABBA"}, {"block_id": "second", "order": "BAAB"}],
                "metrics": [{"id": "cpu_primary", "direction": "lower", "unit": "CPU-ns/operation", "budget_fraction": .03}],
                "alpha": .05, "method": "block_t", "min_blocks": 2, "max_abs_lag1": .5, "max_abs_skew": 2.0, "seed": 17}
        self.node = {"isolated": True, "kind": "resettable-test-node", "physical_domain": "fixture-domain", "generation": "fixture-generation",
                     "baseline_binary": str(self.binary), "candidate_binary": str(self.binary)}
        self.refresh()

    def refresh(self):
        for key, name, value in (("build_manifest", "input-build.json", self.manifest), ("performance_plan", "input-plan.json", self.experiment),
                                 ("performance_environment", "input-environment.json", self.perf_environment)):
            path = self.root / name; write_json(path, value); self.node[key] = str(path)
        self.node["expected_plan_hash"] = digest(self.experiment)
        write_json(self.policy / "cases.json", {"schema_version": 1, "cases": [self.definition]})
        write_json(self.policy / "required.json", {"schema_version": 1, "toolchain": "1.94.1", "suites": {"perf": ["QA-performance"]}})
        write_json(self.policy / "known-failures.json", {"schema_version": 1, "exemptions": []})
        self.plan = make_plan(self.policy, "perf", SHA)
        self.case = self.plan["cases"][0]

    def fake_runner(self, argv, root, output, timeout, env):
        self.assertIn("--expected-plan-hash", argv)
        self.assertNotIn("cargo", argv)
        runs = Path(argv[argv.index("--outdir") + 1]); runs.mkdir()
        plan = read_json(argv[argv.index("--plan") + 1])
        write_json(runs / "plan.json", plan)
        write_json(runs / "environment.json", self.perf_environment)
        Path(output).write_text("synthetic fixed runner fixture\n")
        start, rows = time.monotonic_ns(), []
        for block in plan["blocks"]:
            for position, arm in enumerate(block["order"]):
                index = len(rows)
                measurement = copy.deepcopy(self.measurement)
                if getattr(self, "raw_failure", False) and arm == "B": measurement["ok"] = False
                raw = canonical(measurement).decode() + "\n"
                log = runs / f"probe-{index // 4}-{position}.log"; log.write_text(raw)
                execution = {"status": "completed", "exit_code": 0, "start_ns": start + index * 100 + 1,
                             "end_ns": start + index * 100 + 99, "clock": "CLOCK_MONOTONIC", "cleanup": "PASS"}
                if getattr(self, "bad_cleanup", False) and index == 1: execution["cleanup"] = "UNKNOWN"
                parsed, errors = tool("analyze").validate_row({"variant": "baseline" if arm == "A" else "candidate", "pair": 0,
                            "exit_code": 0, "binary_sha256": plan["binary_sha256"][arm], "measurements": [measurement]}, 0, {},
                            {"args": plan["config"]["args"]})
                sample = {**self.perf_environment, "pid": 42, "at_ns": start, "phase": "running", "compilers": [],
                          "cgroup": {"path": "/", "ancestors": copy.deepcopy(self.perf_environment["cgroup_limits"])}}
                if getattr(self, "compiler", False): sample["compilers"] = [{"pid": 5, "name": "rustc"}]
                if getattr(self, "unknown_cgroup", False): sample["cgroup"] = {"path": None, "error": "cgroup v2 unavailable"}
                if getattr(self, "cpu_noise", False): sample["affinity"] = [1]
                invalid = bool(errors or any(getattr(self, flag, False) for flag in ("compiler", "unknown_cgroup", "cpu_noise"))
                               or execution["cleanup"] != "PASS")
                rows.append({"run_id": f"{digest(plan)}:{block['block_id']}:{position}", "case_id": plan["case_id"], "case_version": 1,
                            "block_id": block["block_id"], "position": position, "arm": arm, "plan_hash": digest(plan),
                            "config_hash": digest(plan["config"]), "build_manifest_hash": plan["build_manifest_hash"],
                            "environment_hash": plan["environment_hash"], "binary_sha256": plan["binary_sha256"][arm],
                            "functional": {"status": "FAIL" if measurement["ok"] is False else "PASS"},
                            "validity": {"functional": "VALID", "performance": "INVALID" if invalid else "VALID"},
                            "execution": execution, "metrics": parsed["metrics"] if parsed else {},
                            "observations": {"measurements": [measurement], "stdout": raw, "stderr": "", "environment_samples": [sample],
                                  "process_lifecycle": {**{key: execution[key] for key in ("status", "exit_code", "start_ns", "end_ns")},
                                        "cleanup_status": execution["cleanup"], "cleanup_scope": "linux-subreaper", "cleanup_limits": []},
                                  "output_artifact": {"path": log.name, "size_bytes": log.stat().st_size, "sha256": file_hash(log),
                                                      "limit_bytes": 1048576, "streams": "stdout+stderr"}}})
        if getattr(self, "incomplete", False): rows.pop()
        if getattr(self, "forged_metric", False): rows[1]["metrics"]["cpu_primary"] = .001
        with (runs / "runs.jsonl").open("w") as stream:
            for row in rows: stream.write(canonical(row).decode() + "\n")
            if getattr(self, "truncated", False): stream.write('{"unfinished":')
        return {"status": "completed", "exit_code": 0, "start_ns": start, "end_ns": time.monotonic_ns(),
                "cleanup_status": "PASS", "cleanup_scope": "linux-subreaper", "cleanup_limits": [], "argv": argv}

    def run_fixture(self, output="evidence", node=None):
        self.output = self.root / output
        with patch("qa.perf_run.collect_environment", return_value=self.environment), patch("qa.perf_run.capability_errors", return_value=[]), \
             patch("qa.perf_run.platform_id", return_value="linux-x86_64"), patch("qa.perf_run.source_identity", return_value=self.manifest["source"]), \
             patch("qa.perf_run.run_child", side_effect=self.fake_runner) as runner:
            result = run_performance_case(self.plan, self.case, self.root, self.output, self.node if node is None else node,
                                          self.root / "leases", self.policy)
        validate_result(result)
        return result, runner

    def test_shadow_runs_fixed_experiment_and_is_independently_recomputed(self):
        result, runner = self.run_fixture()
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(result["functional"]["status"], "PASS", result["validity"])
        self.assertEqual(result["performance"]["status"], "INCONCLUSIVE")
        self.assertNotEqual(result["environment_hash"], result["observations"]["performance_environment_hash"])
        self.assertNotEqual(result["config_hash"], result["observations"]["performance_config_hash"])
        self.assertEqual(verify_performance(result, self.case, self.output)["decision"], "SHADOW")
        self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "PASS")
        self.assertEqual(read_json(self.output / "build-manifest.json"), self.manifest)

    def enable_absolute_slo(self, bound=99):
        self.experiment["metrics"][0]["absolute_slo"] = {"scope": "observed-per-run", "max": bound}
        self.definition["performance"]["absolute_slos"] = {"cpu_primary": copy.deepcopy(self.experiment["metrics"][0]["absolute_slo"])}
        self.refresh()

    def test_absolute_slo_fail_survives_clean_raw_audit_and_shadow_mode(self):
        self.enable_absolute_slo()
        result, _ = self.run_fixture()
        self.assertEqual(read_json(self.output / "perf-audit.json")["functional"], "PASS")
        actual = verify_performance(result, self.case, self.output)
        self.assertEqual((actual["functional"], actual["absolute_slo"]["status"]), ("FAIL", "FAIL"))
        self.assertEqual(result["functional"]["status"], "FAIL")
        self.assertEqual(result["performance"]["status"], "INCONCLUSIVE")
        self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "FAIL")

    def test_frozen_absolute_slo_boundary_passes_independent_raw_recomputation(self):
        self.enable_absolute_slo(bound=100)
        result, _ = self.run_fixture()
        self.assertEqual(result["functional"]["status"], "PASS")
        self.assertEqual(verify_performance(result, self.case, self.output)["absolute_slo"]["status"], "PASS")
        self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "PASS")

    def test_absolute_slo_fail_survives_performance_contamination_and_missing_slots(self):
        self.enable_absolute_slo()
        self.compiler = True
        for incomplete in (False, True):
            self.incomplete = incomplete
            result, _ = self.run_fixture(output="slo-incomplete" if incomplete else "slo-contaminated")
            self.assertEqual(result["functional"]["status"], "FAIL")
            self.assertEqual(result["validity"]["performance"], "INVALID")
            self.assertEqual(read_json(self.output / "perf-analysis.json")["absolute_slo"]["status"], "FAIL")
            if not incomplete:
                self.assertEqual(read_json(self.output / "perf-audit.json")["functional"], "PASS")
                self.assertEqual(verify_performance(result, self.case, self.output)["functional"], "FAIL")
            self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "FAIL")

    def test_absolute_slo_failure_cannot_be_washed_out_of_outer_envelope(self):
        self.enable_absolute_slo()
        result, _ = self.run_fixture()
        result["functional"]["status"] = "PASS"
        for assertion in result["functional"]["assertions"]:
            assertion["status"] = "PASS"
        with self.assertRaisesRegex(EvidenceError, "functional status differs"):
            verify_performance(result, self.case, self.output)
        write_json(self.output / "result.json", result)
        report = evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)
        self.assertNotEqual(report["status"], "PASS")

    def test_forged_below_slo_metrics_cannot_replace_raw_probe_values(self):
        self.enable_absolute_slo()
        result, _ = self.run_fixture()
        rows, errors = read_experiment_rows(self.output / "perf-rows.jsonl")
        self.assertFalse(errors)
        for row in rows:
            if row["arm"] == "B":
                row["metrics"]["cpu_primary"] = 1
        with (self.output / "perf-rows.jsonl").open("w") as stream:
            for row in rows:
                stream.write(canonical(row).decode() + "\n")
        forged_analysis = engine().evaluate(self.experiment, rows)
        self.assertEqual(forged_analysis["absolute_slo"]["status"], "PASS")
        write_json(self.output / "perf-analysis.json", forged_analysis)
        result["functional"]["status"] = "PASS"
        for assertion in result["functional"]["assertions"]:
            assertion["status"] = "PASS"
        result["performance"].update(status=forged_analysis["performance"], metrics=forged_analysis["metrics"],
                                     analysis_hash=file_hash(self.output / "perf-analysis.json"))
        for artifact in result["artifacts"]:
            path = self.output / artifact["path"]
            artifact.update(sha256=file_hash(path), size_bytes=path.stat().st_size)
        write_json(self.output / "result.json", result)
        with self.assertRaisesRegex(EvidenceError, "raw performance audit"):
            verify_performance(result, self.case, self.output)
        self.assertNotEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "PASS")

    def test_shadow_absolute_slo_cannot_be_removed_or_relaxed_from_frozen_policy(self):
        self.enable_absolute_slo()
        self.assertNotIn("experiment_plan_hash", self.case["performance"])  # Dynamic full plan hash is unnecessary here.
        for bound in (None, 1000):
            with self.subTest(bound=bound):
                result, _ = self.run_fixture(output=f"altered-slo-{bound}")
                altered = copy.deepcopy(self.experiment)
                if bound is None:
                    del altered["metrics"][0]["absolute_slo"]
                else:
                    altered["metrics"][0]["absolute_slo"]["max"] = bound
                rows, errors = read_experiment_rows(self.output / "perf-rows.jsonl")
                self.assertFalse(errors)
                for row in rows:
                    row.update(plan_hash=digest(altered), run_id=f"{digest(altered)}:{row['block_id']}:{row['position']}")
                for name in ("perf-plan.json", "runs/plan.json"):
                    write_json(self.output / name, altered)
                for name in ("perf-rows.jsonl", "runs/runs.jsonl"):
                    with (self.output / name).open("w") as stream:
                        for row in rows:
                            stream.write(canonical(row).decode() + "\n")
                analysis = engine().evaluate(altered, rows)
                audit = inspect_rows(altered, rows, self.output, self.perf_environment)
                self.assertEqual((analysis["functional"], audit["functional"]), ("PASS", "PASS"))
                write_json(self.output / "perf-analysis.json", analysis)
                write_json(self.output / "perf-audit.json", audit)
                result["observations"]["performance_plan_hash"] = digest(altered)
                result["functional"]["status"] = "PASS"
                for assertion in result["functional"]["assertions"]:
                    assertion["status"] = "PASS"
                result["performance"].update(status=analysis["performance"], metrics=analysis["metrics"],
                                             analysis_hash=file_hash(self.output / "perf-analysis.json"))
                result["validity"]["performance"] = analysis["validity"]
                for artifact in result["artifacts"]:
                    path = self.output / artifact["path"]
                    artifact.update(sha256=file_hash(path), size_bytes=path.stat().st_size)
                write_json(self.output / "result.json", result)
                report = evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)
                self.assertNotEqual(report["status"], "PASS", "artifact must not remove a trusted shadow functional SLO")

    def test_absolute_slo_only_in_artifact_is_not_a_trusted_product_contract(self):
        self.experiment["metrics"][0]["absolute_slo"] = {"scope": "observed-per-run", "max": 99}
        self.refresh()
        self.assertNotIn("absolute_slos", self.case["performance"])
        result, runner = self.run_fixture()
        self.assertFalse(runner.called)
        self.assertEqual(result["functional"]["status"], "NOT_RUN")
        self.assertIn("trusted policy", str(result["validity"]["reasons"]))

    def test_missing_node_inputs_always_emit_complete_not_run_envelope(self):
        result, runner = self.run_fixture(node={})
        self.assertFalse(runner.called)
        self.assertEqual(result["execution"]["status"], "capability_missing")
        self.assertEqual(result["functional"]["status"], "NOT_RUN")
        self.assertTrue((self.output / "result.json").is_file())

    def test_changed_static_config_version_hash_and_candidate_are_rejected_before_execution(self):
        for index, mutate in enumerate((lambda: self.experiment["config"].update(timeout_sec=3),
                                       lambda: self.experiment.update(case_version=2),
                                       lambda: self.manifest["source"].update(sha="3" * 40),
                                       lambda: self.experiment["binary_sha256"].update(B="f" * 64))):
            old_plan, old_manifest = copy.deepcopy(self.experiment), copy.deepcopy(self.manifest)
            mutate(); self.refresh()
            result, runner = self.run_fixture(output=f"rejected-{index}")
            self.assertFalse(runner.called)
            self.assertEqual(result["functional"]["status"], "NOT_RUN")
            self.experiment, self.manifest = old_plan, old_manifest
        self.refresh(); self.node["expected_plan_hash"] = "f" * 64
        result, runner = self.run_fixture(output="wrong-expected")
        self.assertFalse(runner.called)
        self.assertEqual(result["functional"]["status"], "NOT_RUN")

    def test_missing_block_and_forged_metric_cannot_be_functional_pass(self):
        for flag in ("incomplete", "forged_metric"):
            setattr(self, flag, True)
            result, _ = self.run_fixture(output=flag)
            self.assertEqual(result["functional"]["status"], "NOT_RUN")
            with self.assertRaises(EvidenceError): verify_performance(result, self.case, self.output)
            setattr(self, flag, False)

    def test_failure_survives_incomplete_truncated_and_contaminated_companion_data(self):
        self.raw_failure = self.incomplete = self.truncated = self.compiler = True
        result, _ = self.run_fixture()
        self.assertEqual(result["functional"]["status"], "FAIL")
        self.assertEqual(result["performance"]["status"], "INCONCLUSIVE")
        self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "FAIL")

    def test_compiler_contamination_keeps_functional_but_invalidates_performance(self):
        self.compiler = True
        result, _ = self.run_fixture()
        self.assertEqual(result["functional"]["status"], "PASS")
        self.assertEqual(result["validity"]["performance"], "INVALID")
        self.assertEqual(verify_performance(result, self.case, self.output)["validity"], "INVALID")
        self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "PASS")

    def test_cpu_and_unknown_cgroup_only_affect_shadow_performance(self):
        for flag in ("unknown_cgroup", "cpu_noise"):
            setattr(self, flag, True)
            result, _ = self.run_fixture(output=flag)
            self.assertEqual((result["functional"]["status"], result["validity"]["functional"]), ("PASS", "VALID"))
            self.assertEqual(result["validity"]["performance"], "INVALID")
            self.assertEqual(verify_performance(result, self.case, self.output)["validity"], "INVALID")
            self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "PASS")
            self.raw_failure = True
            failed, _ = self.run_fixture(output=flag + "-functional-failure")
            self.assertEqual(failed["functional"]["status"], "FAIL")
            self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "FAIL")
            self.raw_failure = False
            setattr(self, flag, False)

    def test_stage_and_archive_preserve_complete_performance_proof(self):
        result, _ = self.run_fixture(output="source/QA-performance")
        for relative in ("private.key", "config.json", "unrelated.log", "runs/probe-9999-0.log", "elsewhere/runner.log"):
            path = self.output / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("secret must not upload")
        source = self.output.parent
        (source / "runner.log").write_text("unrelated runner output")
        uploaded = self.root / "upload"
        stage(source, uploaded)
        copied = uploaded / "QA-performance"
        for artifact in result["artifacts"]:
            self.assertEqual(file_hash(copied / artifact["path"]), artifact["sha256"])
        self.assertFalse((uploaded / "runner.log").exists())
        for relative in ("private.key", "config.json", "unrelated.log", "runs/probe-9999-0.log", "elsewhere/runner.log"):
            self.assertFalse((copied / relative).exists())
        self.assertEqual(verify_performance(read_json(copied / "result.json"), self.case, copied)["decision"], "SHADOW")
        self.assertEqual(evaluate(self.plan, [copied / "result.json"], self.policy, expected_sha=SHA)["status"], "PASS")
        tar_path = self.root / "performance.tar.gz"
        entries = archive(source, tar_path)
        expected = {"QA-performance/" + artifact["path"] for artifact in result["artifacts"]} | {"QA-performance/result.json"}
        self.assertEqual({entry["path"] for entry in entries}, expected)
        restored = self.root / "restored"; restored.mkdir()
        with tarfile.open(tar_path) as bundle:
            bundle.extractall(restored, filter="data")
        self.assertEqual(verify_performance(read_json(restored / "QA-performance/result.json"), self.case,
                                           restored / "QA-performance")["decision"], "SHADOW")

    def calibration_input(self):
        source = self.root / "controls"; source.mkdir()
        trial = {"trial_id": "fixture-0", "kind": "run-AA", "plan_file": "calibration/fixture-0/perf-plan.json",
                 "rows_file": "calibration/fixture-0/perf-rows.jsonl", "provenance_file": "calibration/fixture-0/calibration-provenance.json"}
        write_json(source / "calibration-spec.json", {"trials": [trial]})
        write_json(source / "calibration.json", {"validated": False, "purpose": "layout test only"})
        for key in ("plan_file", "rows_file", "provenance_file"):
            path = source / trial[key]; path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, {"synthetic": True})
        return source, trial

    def test_spec_calibration_tree_roundtrip_and_unreferenced_files_excluded(self):
        result, _ = self.run_fixture()
        source, trial = self.calibration_input()
        _copy_calibration(str(source), self.output)
        for path in self.output.rglob("*"):
            name = path.relative_to(self.output).as_posix()
            if path.is_file() and name not in {a["path"] for a in result["artifacts"]} and name != "result.json":
                result["artifacts"].append({"path": name, "sha256": file_hash(path), "size_bytes": path.stat().st_size})
        write_json(self.output / "result.json", result)
        orphan = self.output / "calibration/unreferenced/perf-plan.json"
        write_json(orphan, {"secret": "not referenced, must not upload"})
        (self.output / "calibration/fixture-0/private.key").write_text("private")
        staged = self.root / "controls-upload"; stage(self.output, staged)
        for key in ("plan_file", "rows_file", "provenance_file"):
            self.assertEqual(file_hash(staged / trial[key]), file_hash(self.output / trial[key]))
        self.assertFalse((staged / "calibration/unreferenced/perf-plan.json").exists())
        self.assertFalse((staged / "calibration/fixture-0/private.key").exists())
        names = {entry["path"] for entry in archive(self.output, self.root / "controls.tar.gz")}
        self.assertTrue({trial[key] for key in ("plan_file", "rows_file", "provenance_file")}.issubset(names))
        (self.output / trial["rows_file"]).unlink()
        with self.assertRaisesRegex(EvidenceError, "required by spec is missing"):
            stage(self.output, self.root / "incomplete-upload")

    def test_nonstandard_calibration_paths_and_unsupported_declared_logs_fail_explicitly(self):
        result, _ = self.run_fixture()
        source, trial = self.calibration_input()
        trial["rows_file"] = "private.key"
        write_json(source / "calibration-spec.json", {"trials": [trial]})
        (source / "private.key").write_text("private")
        with self.assertRaisesRegex(EvidenceError, "unsupported calibration rows_file"):
            _copy_calibration(str(source), self.output)
        with self.assertRaisesRegex(EvidenceError, "unsupported calibration rows_file"):
            stage(source, self.root / "unsupported-control-upload")
        log = self.output / "unknown.log"; log.write_text("not a supported probe log")
        result["artifacts"].append({"path": log.name, "size_bytes": log.stat().st_size, "sha256": file_hash(log)})
        write_json(self.output / "result.json", result)
        with self.assertRaisesRegex(EvidenceError, "outside upload allowlist"):
            archive(self.output, self.root / "unsupported-log.tar.gz")

    @unittest.skipIf(os.name == "nt", "symlink creation requires separate Windows privilege")
    def test_allowed_probe_filename_cannot_upload_a_symlink_target(self):
        self.run_fixture()
        secret = self.root / "private.key"; secret.write_text("private")
        log = self.output / "runs/probe-0-0.log"; log.unlink(); log.symlink_to(secret)
        with self.assertRaises(EvidenceError): stage(self.output, self.root / "symlink-upload")

    def test_cleanup_failure_cannot_pass(self):
        self.bad_cleanup = True
        result, _ = self.run_fixture()
        self.assertEqual(result["functional"]["status"], "NOT_RUN")
        self.assertEqual(result["execution"]["cleanup"], "UNKNOWN")
        self.assertEqual(evaluate(self.plan, [self.output / "result.json"], self.policy, expected_sha=SHA)["status"], "BLOCKED")

    def test_reporter_rejects_wrong_environment_association_and_rehashed_raw_tamper(self):
        result, _ = self.run_fixture()
        changed = copy.deepcopy(result)
        changed["observations"]["performance_environment_hash"] = changed["environment_hash"]
        with self.assertRaises(EvidenceError): verify_performance(changed, self.case, self.output)
        log = self.output / "runs/probe-0-1.log"
        value = read_json(log); value["work_cpu_ns"] = 1; write_json(log, value)
        for artifact in result["artifacts"]:
            path = self.output / artifact["path"]
            artifact.update(sha256=file_hash(path), size_bytes=path.stat().st_size)
        with self.assertRaises(EvidenceError): verify_performance(result, self.case, self.output)

    def test_calibration_verifier_receives_a_sealed_temporary_artifact_view(self):
        result, _ = self.run_fixture()
        inspected = []
        def verify(directory, case, policy_dir, *, generation=None):
            inspected.append(Path(directory))
            self.assertNotEqual(Path(directory), self.output)
            sealed = read_json(Path(directory) / "result.json")
            self.assertEqual(sealed, result)
            self.assertEqual(case, self.case)
            self.assertEqual(generation, self.node["generation"])
            for artifact in sealed["artifacts"]:
                self.assertEqual(file_hash(Path(directory) / artifact["path"]), artifact["sha256"])
            raise EvidenceError("synthetic calibration must not qualify")
        with patch.dict(sys.modules, {"qa.release": types.SimpleNamespace(verify_calibration=verify)}):
            with self.assertRaises(EvidenceError):
                _verify_staged_calibration(self.output, result, self.case, self.policy, self.node["generation"])
        self.assertEqual(read_json(self.output / "result.json"), result)
        self.assertTrue(inspected)
        self.assertFalse(inspected[0].exists())

    def test_fake_validated_calibration_is_not_published_as_pass(self):
        self.experiment["mode"] = "enforce"
        self.experiment["calibration_id"] = "e" * 64
        calibration = {"validated": True, "source": "measured-held-out", "calibration_id": "e" * 64,
                       "scope_hash": engine().calibration_scope(self.experiment)}
        self.experiment["calibration_hash"] = digest(calibration)
        self.definition["performance"].update(mode="enforce", calibration_id="e" * 64, experiment_plan_hash=digest(self.experiment))
        source = self.root / "calibration-input"; source.mkdir()
        write_json(source / "calibration.json", calibration)
        write_json(source / "calibration-spec.json", {"trials": []})
        self.node["calibration_directory"] = str(source)
        self.refresh()
        with patch("qa.perf_run._verify_staged_calibration", side_effect=EvidenceError("no trusted held-out trials")) as verified:
            result, _ = self.run_fixture()
        self.assertTrue(verified.called)
        self.assertEqual(result["performance"]["status"], "INCONCLUSIVE")
        self.assertEqual(result["validity"]["performance"], "INVALID")
        self.assertIn("calibration verification", str(result["validity"]["reasons"]))
        self.definition["performance"]["experiment_plan_hash"] = "f" * 64
        self.refresh()
        result, runner = self.run_fixture(output="enforce-unbound")
        self.assertFalse(runner.called)


if __name__ == "__main__":
    unittest.main()
