from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from qa.common import ROOT, digest, file_hash, read_json, write_json
from qa.evaluate import evaluate
from qa.plan import make_plan
from qa.run import commands, finish_fuzz, prepare_fuzz
from qa.tests.test_identity_process import environment_fixture, manifest_fixture


class SafetyEvaluationTests(unittest.TestCase):
    """Synthetic complete evidence: the independent gate must require its hashes."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = self.root / "policy"
        self.policy.mkdir()
        self.sha = "1" * 40
        case = next(c for c in read_json(ROOT / "qa/policy/cases.json")["cases"]
                    if c["case_id"] == "QA-fuzz-wire_parser")
        write_json(self.policy / "cases.json", {"schema_version": 1, "cases": [case]})
        write_json(self.policy / "required.json", {"schema_version": 1, "toolchain": "1.94.1",
                   "suites": {"safety": [case["case_id"]]}, "jobs": {"safety": ["safety"]}})
        write_json(self.policy / "known-failures.json", {"schema_version": 1, "exemptions": []})
        self.plan = make_plan(self.policy, "safety", self.sha)
        self.case = self.plan["cases"][0]
        self.output = self.root / "evidence"
        self.output.mkdir()
        shutil.copytree(ROOT / "fuzz/corpus", self.root / "fuzz/corpus")
        write_json(self.output / "safety-capability.json", {
            "host": "x86_64-unknown-linux-gnu", "target": "x86_64-unknown-linux-gnu", "errors": []})
        seeds = prepare_fuzz(self.case, self.root, self.output)
        finish_fuzz(self.root, self.output, seeds)
        (self.output / "command-0.log").write_text("Done 3 runs in 1 second(s)\n")
        write_json(self.output / "execution.json", {"commands": [{
            "argv": commands(self.case, self.case["toolchain"], self.root, self.output)[0],
            "status": "completed", "exit_code": 0, "cleanup_status": "PASS"}]})
        build = manifest_fixture(toolchain=self.case["toolchain"], profile="dev",
                                 manifest_path="fuzz/Cargo.toml", package="zfstack-fuzz")
        build["rustc"] = build["rustc"].replace("1.94.1", "1.99.0-nightly")
        environment = environment_fixture()
        write_json(self.output / "build-manifest.json", build)
        write_json(self.output / "environment.json", environment)
        self.result = {"schema_version": 2, "run_id": "synthetic-fuzz", "block_id": "functional", "arm": "B",
            "case_id": self.case["case_id"], "case_version": self.case["case_version"],
            "case_instance_id": self.case["case_instance_id"], "tested_sha": self.sha, "plan_hash": digest(self.plan),
            "build_manifest_hash": digest(build), "environment_hash": digest(environment),
            "config_hash": digest(self.case["config"]), "validity": {"functional": "VALID", "performance": "UNKNOWN", "reasons": []},
            "functional": {"status": "PASS", "assertions": [{"id": "execution.contracts", "status": "PASS",
                "kind": "execution", "detail": "synthetic completed fuzz input"}]},
            "performance": {"status": "NOT_APPLICABLE", "metrics": {}, "analysis_hash": None},
            "attribution": "UNATTRIBUTED", "execution": {"status": "completed", "exit_code": 0,
                "start_ns": 100, "end_ns": 200, "clock": "monotonic", "cleanup": "PASS"},
            "artifacts": [{"path": name, "sha256": file_hash(self.output / name),
                "size_bytes": (self.output / name).stat().st_size} for name in (
                    "execution.json", "command-0.log", "fuzz-seeds.json", "fuzz-artifacts.json", "safety-capability.json")],
            "observations": {}}

    def gate(self, result=None):
        path = self.output / "result.json"
        write_json(path, self.result if result is None else result)
        return evaluate(self.plan, [path], self.policy, expected_sha=self.sha)

    def test_complete_fuzz_evidence_passes_independent_gate(self):
        self.assertEqual(self.gate()["status"], "PASS")

    def test_present_but_unhashed_safety_proof_cannot_pass(self):
        for name in ("fuzz-seeds.json", "fuzz-artifacts.json", "safety-capability.json"):
            with self.subTest(name=name):
                result = copy.deepcopy(self.result)
                result["artifacts"] = [a for a in result["artifacts"] if a["path"] != name]
                self.assertTrue((self.output / name).is_file())
                report = self.gate(result)
                self.assertEqual(report["status"], "BLOCKED")
                self.assertTrue(any("missing hashed safety artifacts" in reason for reason in report["blocked"]))

    def test_tampered_safety_proof_is_bound_to_inventory(self):
        for name in ("fuzz-seeds.json", "fuzz-artifacts.json", "safety-capability.json"):
            with self.subTest(name=name):
                path = self.output / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                report = self.gate()
                self.assertEqual(report["status"], "BLOCKED")
                self.assertTrue(any("artifact missing/tampered" in reason for reason in report["blocked"]))
                path.write_bytes(original)

    def test_observed_failure_survives_missing_safety_proof(self):
        self.result["functional"]["status"] = "FAIL"
        self.result["functional"]["assertions"][0]["status"] = "FAIL"
        self.result["artifacts"] = [a for a in self.result["artifacts"] if a["path"] != "fuzz-seeds.json"]
        report = self.gate()
        self.assertEqual(report["status"], "FAIL")
        self.assertTrue(report["failures"])
        self.assertTrue(report["blocked"])


if __name__ == "__main__":
    unittest.main()
