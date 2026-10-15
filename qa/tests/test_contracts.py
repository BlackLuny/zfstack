from __future__ import annotations

import copy
import datetime as dt
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from qa.archive import archive
from qa.baseline import promote, validate_factorial
from qa.common import EvidenceError, case_identity, digest, file_hash, read_json, safe_path, write_json
from qa.evaluate import evaluate
from qa.lease import Lease
from qa.plan import check_trusted_plan, make_plan
from qa.render_report import render
from qa.run import _functional_evidence, commands, run_child
from qa.validate import validate_exemption, validate_plan, validate_result
from qa.tests.test_identity_process import manifest_fixture, environment_fixture

SHA = "1" * 40
HASH = "2" * 64


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = self.root / "policy"
        self.policy.mkdir()
        case = {"case_id": "C06-only-release", "case_version": 1, "layer": "A0", "profile": "release",
                "runner": "adapter-qa", "toolchain": "1.94.1", "features": ["tokio", "test-peer"], "config": {"request_bytes": 1}, "timeout_seconds": 10,
                "performance": {"mode": "not_applicable", "metrics": []}}
        write_json(self.policy / "cases.json", {"schema_version": 1, "cases": [case]})
        write_json(self.policy / "required.json", {"schema_version": 1, "toolchain": "1.94.1",
                   "suites": {"pr": [case["case_id"]]}, "jobs": {"pr": ["test"]}})
        write_json(self.policy / "known-failures.json", {"schema_version": 1, "exemptions": []})
        self.plan = make_plan(self.policy, "pr", SHA)
        self.case = self.plan["cases"][0]
        self.output = self.root / "evidence"
        self.output.mkdir()
        build = manifest_fixture(features=self.case["features"])
        environment = environment_fixture()
        write_json(self.output / "build-manifest.json", build)
        write_json(self.output / "environment.json", environment)
        log = self.output / "command-0.log"
        log.write_text("test result: ok. 1 passed; 0 failed\n")
        lifecycle = self.output / "execution.json"
        write_json(lifecycle, {"commands": [{"argv": ["cargo", "test"], "status": "completed", "exit_code": 0, "cleanup_status": "PASS"}]})
        self.result = {"schema_version": 2, "run_id": "fixture", "block_id": "functional", "arm": "B",
                       "case_id": self.case["case_id"], "case_version": 1, "case_instance_id": self.case["case_instance_id"],
                       "tested_sha": SHA, "plan_hash": digest(self.plan), "build_manifest_hash": digest(build),
                       "environment_hash": digest(environment), "config_hash": digest(self.case["config"]),
                       "validity": {"functional": "VALID", "performance": "UNKNOWN", "reasons": []},
                       "functional": {"status": "PASS", "assertions": [{"id": "only_release", "status": "PASS",
                                      "kind": "liveness", "detail": "unique release woke registered writer"}]},
                       "performance": {"status": "NOT_APPLICABLE", "metrics": {}, "analysis_hash": None},
                       "attribution": "UNATTRIBUTED", "execution": {"status": "completed", "exit_code": 0,
                       "start_ns": 100, "end_ns": 200, "clock": "monotonic", "cleanup": "PASS"},
                       "artifacts": [{"path": log.name, "size_bytes": log.stat().st_size, "sha256": file_hash(log)},
                                     {"path": lifecycle.name, "size_bytes": lifecycle.stat().st_size, "sha256": file_hash(lifecycle)}],
                       "observations": {}}

    def gate(self, result=None, **kwargs):
        path = self.output / "result.json"
        write_json(path, self.result if result is None else result)
        return evaluate(self.plan, [path], self.policy, expected_sha=SHA, **kwargs)

    def set_performance(self, mode):
        data = read_json(self.policy / "cases.json")
        data["cases"][0]["performance"] = {"mode": mode, "metrics": ["cpu"], "calibration_id": HASH, "experiment_plan_hash": HASH}
        write_json(self.policy / "cases.json", data)
        self.plan = make_plan(self.policy, "pr", SHA)
        self.result["plan_hash"] = digest(self.plan)
        self.result["performance"]["status"] = "INCONCLUSIVE"

    def test_pass(self):
        self.assertEqual(self.gate()["status"], "PASS")

    def test_rehashed_zero_tests_cannot_claim_pass(self):
        log = self.output / "command-0.log"
        log.write_text("test result: ok. 0 passed; 0 failed; 500 filtered out\n")
        self.result["artifacts"][0].update(size_bytes=log.stat().st_size, sha256=file_hash(log))
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_missing_child_execution_records_cannot_claim_pass(self):
        self.result["artifacts"] = self.result["artifacts"][:1]
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_no_samples_never_pass(self):
        result = evaluate(self.plan, [], self.policy, expected_sha=SHA)
        self.assertEqual(result["status"], "BLOCKED")

    def test_wrong_sha(self):
        self.result["tested_sha"] = "3" * 40
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_wrong_plan(self):
        self.result["plan_hash"] = HASH
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_wrong_config(self):
        self.result["config_hash"] = HASH
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_failure_survives_contamination(self):
        self.result["functional"]["status"] = "FAIL"
        self.result["functional"]["assertions"][0]["status"] = "FAIL"
        self.result["validity"]["performance"] = "INVALID"
        self.result["validity"]["functional"] = "UNKNOWN"
        self.result["validity"]["reasons"] = ["compiler contamination"]
        self.assertEqual(self.gate()["status"], "FAIL")

    def test_preexisting_does_not_waive(self):
        self.result["functional"]["status"] = "FAIL"
        self.result["attribution"] = "PREEXISTING"
        self.assertEqual(self.gate()["status"], "FAIL")

    def test_shadow_is_explicit(self):
        self.set_performance("shadow")
        report = self.gate()
        self.assertEqual(report["status"], "PASS")
        self.assertIn("SHADOW", report["notes"][0])

    def test_enforced_inconclusive_blocks(self):
        self.set_performance("enforce")
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_command_log_is_not_calibrated_analysis(self):
        self.set_performance("enforce")
        self.result["validity"]["performance"] = "VALID"
        self.result["performance"] = {"status": "PASS", "metrics": {"cpu": None},
                                      "analysis_hash": self.result["artifacts"][0]["sha256"]}
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_runner_cannot_declare_na(self):
        self.set_performance("shadow")
        self.result["performance"]["status"] = "NOT_APPLICABLE"
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_artifact_tamper(self):
        (self.output / "command-0.log").write_text("fabricated PASS")
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_missing_manifest(self):
        (self.output / "environment.json").unlink()
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_empty_artifact_inventory(self):
        self.result["artifacts"] = []
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_duplicate_results_do_not_rerun_to_green(self):
        self.gate()
        path = self.output / "result.json"
        self.assertEqual(evaluate(self.plan, [path, path], self.policy, expected_sha=SHA)["status"], "BLOCKED")

    def test_ci_missing_jobs(self):
        self.assertEqual(self.gate(ci=True)["status"], "BLOCKED")

    def test_ci_skipped_cancelled_failed(self):
        for status in ("skipped", "cancelled", "failure", "neutral", "timed_out"):
            with self.subTest(status=status):
                self.assertEqual(self.gate(ci=True, jobs={"test": {"result": status}})["status"], "BLOCKED")

    def test_ci_success(self):
        self.assertEqual(self.gate(ci=True, jobs={"test": {"result": "success"}})["status"], "PASS")

    def test_cleaning_failed(self):
        self.result["execution"]["cleanup"] = "FAIL"
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_empty_success_assertions(self):
        self.result["functional"]["assertions"] = []
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_pass_with_nonzero_exit(self):
        self.result["execution"]["exit_code"] = 1
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_negative_time(self):
        self.result["execution"]["end_ns"] = 99
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_case_identity_includes_features_and_size(self):
        for field, value in (("profile", "production"), ("config", {"request_bytes": 2}), ("config", {"features": ["tokio"]})):
            case = copy.deepcopy(self.case)
            case[field] = value
            self.assertNotEqual(case_identity(case), self.case["case_instance_id"])

    def test_optional_service_contract_changes_identity(self):
        contract = {"schema_version": 1, "peer_ids": ["client-1"], "phases": ["loaded"],
                    "min_samples_per_peer_phase": 3, "max_latency_ms": 250, "deadline_ms": 250,
                    "max_deadline_miss_rate": 0}
        case = copy.deepcopy(self.case)
        case["service_contract"] = contract
        self.assertNotEqual(case_identity(case), self.case["case_instance_id"])
        case["case_instance_id"] = case_identity(case)
        plan = copy.deepcopy(self.plan)
        plan["cases"] = [case]
        with self.assertRaisesRegex(EvidenceError, "requires an L2 runner"):
            validate_plan(plan)

    def test_candidate_policy_loosen_blocked(self):
        self.plan["cases"][0]["requiredness"] = "OPTIONAL"
        with self.assertRaises(EvidenceError):
            check_trusted_plan(self.plan, self.policy)

    def test_empty_plan_rejected(self):
        self.plan["cases"] = []
        with self.assertRaises(EvidenceError):
            validate_plan(self.plan)

    def test_placeholder_rejected(self):
        self.plan["cases"][0]["timeout_seconds"] = "required_at_plan_build"
        with self.assertRaises(EvidenceError):
            validate_plan(self.plan)

    def test_arbitrary_shell_runner_rejected(self):
        self.plan["cases"][0]["runner"] = "sh -c payload"
        with self.assertRaises(EvidenceError):
            validate_plan(self.plan)

    def test_html_is_escaped(self):
        report = self.gate()
        report["notes"] = ["<script>alert(1)</script>"]
        self.assertNotIn("<script>", render(report))

    def test_unsafe_artifact_path(self):
        self.result["artifacts"][0]["path"] = "../secrets"
        self.assertEqual(self.gate()["status"], "BLOCKED")

    def test_exemption_expiry_and_scope(self):
        e = {"id": "waiver", "case_instance_id": self.case["case_instance_id"], "assertion_id": "only_release",
             "expires": "2001-01-01T00:00:00Z", "owner": "maintainer", "approval": "review-1", "reason": "bounded SLO only"}
        with self.assertRaises(EvidenceError):
            validate_exemption(e, self.case)
        e["expires"] = "2999-01-01T00:00:00Z"
        self.assertEqual(validate_exemption(e, self.case)["id"], "waiver")
        e["case_instance_id"] = HASH
        with self.assertRaises(EvidenceError):
            validate_exemption(e, self.case)


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_json_nonfinite_duplicate_truncated(self):
        for text in ('{"x":NaN}', '{"x":Infinity}', '{"x":1,"x":2}', '{"x":'):
            path = self.root / "bad.json"
            path.write_text(text)
            with self.assertRaises(EvidenceError):
                read_json(path)

    def test_exclusive_evidence(self):
        path = self.root / "result.json"
        write_json(path, {"failed": True}, exclusive=True)
        with self.assertRaises(FileExistsError):
            write_json(path, {"failed": False}, exclusive=True)
        self.assertTrue(read_json(path)["failed"])

    def test_lease_no_stale_takeover(self):
        with Lease(self.root, "physical-domain", "run-A", "generation-1"):
            with self.assertRaises(EvidenceError):
                with Lease(self.root, "physical-domain", "run-B", "generation-2"):
                    pass
        self.assertFalse(list(self.root.glob("*.json")))

    def test_lease_lost_does_not_delete_foreign_owner(self):
        lease = Lease(self.root, "domain", "run-A", "generation-1")
        lease.__enter__()
        foreign = {"owner": "new-owner"}
        write_json(lease.path, foreign)
        with self.assertRaises(EvidenceError):
            lease.__exit__(None, None, None)
        self.assertEqual(read_json(lease.path), foreign)
        self.assertTrue(lease.path.with_suffix(".quarantine").exists())

    def test_child_nonzero(self):
        r = run_child([sys.executable, "-c", "raise SystemExit(7)"], self.root, self.root / "command-0.log", 5, os.environ.copy())
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["exit_code"], 7)

    def test_child_watchdog(self):
        r = run_child([sys.executable, "-c", "import time; time.sleep(30)"], self.root, self.root / "command-0.log", .1, os.environ.copy())
        self.assertEqual(r["status"], "timeout")
        self.assertLess(r["end_ns"] - r["start_ns"], 10_000_000_000)

    def test_zero_tests_is_not_run(self):
        (self.root / "command-0.log").write_text("test result: ok. 0 passed; 0 failed; 500 filtered out\n")
        status, _ = _functional_evidence({"runner": "adapter-qa"}, self.root, [{"status": "completed", "exit_code": 0}])
        self.assertEqual(status, "NOT_RUN")
        (self.root / "command-0.log").write_text("Ran 4 tests in 0.01s\n\nOK (skipped=4)\n")
        status, _ = _functional_evidence({"runner": "python-qa"}, self.root, [{"status": "completed", "exit_code": 0}])
        self.assertEqual(status, "NOT_RUN")

    def test_l0_bare_ok_is_not_evidence(self):
        write_json(self.root / "harness.json", {"ok": True})
        case = {"runner": "l0", "config": {"scenario": "matrix", "app_drive": "event-only", "seed": 1,
                                             "expected_scenarios": ["quota"]}}
        status, _ = _functional_evidence(case, self.root, [{"status": "completed", "exit_code": 0}])
        self.assertEqual(status, "NOT_RUN")

    def test_child_lifecycle_failure_survives_exit_zero(self):
        status, _ = _functional_evidence({"runner": "feature-audit"}, self.root,
                                         [{"status": "failed", "exit_code": 0}])
        self.assertEqual(status, "FAIL")

    def test_test_util_on_production_tokio_graph_is_a_failure(self):
        case = {"runner": "feature-audit", "config": {}, "toolchain": "1.94.1"}
        records = [{"argv": argv, "status": "completed", "exit_code": 0}
                   for argv in commands(case, "1.94.1", self.root, self.root)]
        (self.root / "command-0.log").write_text("zfstack v0.0.1\nbytes v1.0.0\n")
        (self.root / "command-1.log").write_text('zfstack v0.0.1\ntokio v1.0.0\ntokio feature "net"\n')
        self.assertEqual(_functional_evidence(case, self.root, records)[0], "PASS")
        with (self.root / "command-1.log").open("a") as stream:
            stream.write('tokio feature "test-util"\n')
        self.assertEqual(_functional_evidence(case, self.root, records)[0], "FAIL")
        self.assertEqual(_functional_evidence(case, self.root, records[:1])[0], "NOT_RUN")

    def test_archive_excludes_keys_and_worker_config(self):
        evidence = self.root / "run"
        evidence.mkdir()
        (evidence / "result.json").write_text("{}")
        (evidence / "private.key").write_text("secret")
        (evidence / "config.json").write_text("secret")
        path = self.root / "artifact.tar.gz"
        archive(evidence, path)
        with tarfile.open(path) as file:
            self.assertEqual(file.getnames(), ["result.json"])

    def test_archive_budget(self):
        (self.root / "result.json").write_text("large")
        with self.assertRaises(EvidenceError):
            archive(self.root, self.root / "out.tar.gz", limit_bytes=1)

    @unittest.skipIf(os.name == "nt", "symlinks require a separately granted Windows privilege")
    def test_symlink_artifact_rejected(self):
        (self.root / "result.json").symlink_to(self.root / "other")
        with self.assertRaises(EvidenceError):
            safe_path(self.root, "result.json")

    def test_unbuilt_factorial_and_wrong_path_block(self):
        with self.assertRaises(EvidenceError):
            validate_factorial({}, "private_tun")
        cells = {name: {"build_status": "PASS", "functional": "PASS", "path": "direct", "path_packets": 1}
                 for name in ("Z0S0", "Z0S1", "Z1S0", "Z1S1")}
        with self.assertRaises(EvidenceError):
            validate_factorial(cells, "private_tun")

    def test_no_promotion_from_pr_or_shadow(self):
        with self.assertRaises(EvidenceError):
            promote({"suite": "pr"}, {"status": "PASS"}, generation="g1", release_anchor=SHA,
                    calibration_hashes=[HASH], expires="2999-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
