"""Hosted nightly GitHub conclusion; evaluate summaries stay fail-closed."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from qa.hosted_nightly_gate import hosted_nightly_decision, is_expected_l2_skip_block, l2_case_ids, l2_enabled_value, main

PLAN = {
    "suite": "nightly",
    "cases": [
        {"case_id": "C11-l1", "runner": "l1"},
        {"case_id": "C16-l2", "runner": "l2"},
        {"case_id": "C16-l2-relay", "runner": "l2"},
        {"case_id": "C16-l2-queue-control", "runner": "l2-queue-control"},
    ],
}
SKIPPED_JOBS = {
    "extended": {"result": "success"},
    "l1": {"result": "success"},
    "l2": {"result": "skipped"},
}
SUCCESS_JOBS = {**SKIPPED_JOBS, "l2": {"result": "success"}}
L2_BLOCKS = [
    "job l2: skipped",
    "C16-l2: required case NOT_RUN",
    "C16-l2-relay: required case NOT_RUN",
    "C16-l2-queue-control: required case NOT_RUN",
]


def summary(*, status="BLOCKED", failures=None, blocked=None):
    return {"status": status, "failures": [] if failures is None else failures,
            "blocked": list(L2_BLOCKS) if blocked is None else blocked}


class HostedNightlyGateTests(unittest.TestCase):
    def test_l2_enabled_flag_is_exact(self):
        self.assertTrue(l2_enabled_value("true"))
        for raw in ("", "True", "TRUE", "false", "1"):
            self.assertFalse(l2_enabled_value(raw))

    def test_l2_cases_come_from_plan_runners(self):
        self.assertEqual(l2_case_ids(PLAN), {"C16-l2", "C16-l2-relay", "C16-l2-queue-control"})

    def test_skip_blocks_are_tied_to_plan_l2_cases(self):
        cases = l2_case_ids(PLAN)
        for block in L2_BLOCKS + ["C16-l2: functional evidence NOT_RUN/UNKNOWN"]:
            self.assertTrue(is_expected_l2_skip_block(block, cases))
        for block in ("job extended: failure", "C11-l1: required case NOT_RUN",
                      "C16-l2: execution/cleanup incomplete (failed, PASS)", ""):
            self.assertFalse(is_expected_l2_skip_block(block, cases))

    def test_unprovisioned_l2_skip_is_green_for_github(self):
        code, message = hosted_nightly_decision(summary(), PLAN, SKIPPED_JOBS, l2_enabled=False)
        self.assertEqual(code, 0)
        self.assertIn("L2 not certified", message)

    def test_unprovisioned_pass_status_is_also_green(self):
        code, _ = hosted_nightly_decision(summary(status="PASS", blocked=[]), PLAN, SKIPPED_JOBS, l2_enabled=False)
        self.assertEqual(code, 0)

    def test_hosted_failure_is_not_hidden(self):
        blocked = L2_BLOCKS + ["job extended: failure"]
        code, message = hosted_nightly_decision(summary(blocked=blocked), PLAN, SKIPPED_JOBS, l2_enabled=False)
        self.assertEqual(code, 2)
        self.assertIn("job extended: failure", message)

    def test_functional_failure_stays_fail(self):
        code, message = hosted_nightly_decision(
            summary(status="FAIL", failures=["C20-soak: functional FAIL (soak); attribution=UNATTRIBUTED"], blocked=[]),
            PLAN, SKIPPED_JOBS, l2_enabled=False)
        self.assertEqual(code, 1)
        self.assertIn("functional failures", message)

    def test_l2_job_failure_is_not_a_skip(self):
        jobs = {**SKIPPED_JOBS, "l2": {"result": "failure"}}
        code, message = hosted_nightly_decision(summary(blocked=["job l2: failure"]), PLAN, jobs, l2_enabled=False)
        self.assertEqual(code, 2)
        self.assertIn("expected job l2 skipped", message)

    def test_enabled_l2_requires_full_pass(self):
        code, message = hosted_nightly_decision(summary(), PLAN, SKIPPED_JOBS, l2_enabled=True)
        self.assertEqual(code, 2)
        self.assertIn("L2 enabled", message)
        code, message = hosted_nightly_decision(summary(status="PASS", blocked=[]), PLAN, SUCCESS_JOBS, l2_enabled=True)
        self.assertEqual(code, 0)
        self.assertEqual(message, "full nightly passed")

    def test_enabled_l2_rejects_blocked_or_failed_job(self):
        code, _ = hosted_nightly_decision(summary(status="PASS", blocked=["note"]), PLAN, SUCCESS_JOBS, l2_enabled=True)
        self.assertEqual(code, 2)
        jobs = {**SUCCESS_JOBS, "l2": {"result": "failure"}}
        code, _ = hosted_nightly_decision(summary(status="PASS", blocked=[]), PLAN, jobs, l2_enabled=True)
        self.assertEqual(code, 2)

    def test_invalid_inputs_are_blocked(self):
        self.assertEqual(hosted_nightly_decision("x", PLAN, SKIPPED_JOBS, l2_enabled=False)[0], 2)
        self.assertEqual(hosted_nightly_decision(summary(), {"cases": "no"}, SKIPPED_JOBS, l2_enabled=False)[0], 2)
        self.assertEqual(hosted_nightly_decision({"status": "BLOCKED"}, PLAN, SKIPPED_JOBS, l2_enabled=False)[0], 2)
        empty = {"suite": "nightly", "cases": [{"case_id": "C11-l1", "runner": "l1"}]}
        self.assertEqual(hosted_nightly_decision(summary(), empty, SKIPPED_JOBS, l2_enabled=False)[0], 2)

    def test_cli_matches_library_and_rejects_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "summary.json").write_text(json.dumps(summary()))
            (root / "plan.json").write_text(json.dumps(PLAN))
            (root / "jobs.json").write_text(json.dumps(SKIPPED_JOBS))
            self.assertEqual(main(["--summary", str(root / "summary.json"), "--plan", str(root / "plan.json"),
                                   "--jobs", str(root / "jobs.json"), "--l2-enabled", ""]), 0)
            (root / "jobs.json").write_text("{")
            self.assertEqual(main(["--summary", str(root / "summary.json"), "--plan", str(root / "plan.json"),
                                   "--jobs", str(root / "jobs.json")]), 2)
            script = Path(__file__).resolve().parents[1] / "hosted_nightly_gate.py"
            (root / "jobs.json").write_text(json.dumps(SKIPPED_JOBS))
            completed = subprocess.run([sys.executable, str(script), "--summary", str(root / "summary.json"),
                                        "--plan", str(root / "plan.json"), "--jobs", str(root / "jobs.json")],
                                       check=False, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0)
            self.assertIn("L2 not certified", completed.stdout)


if __name__ == "__main__":
    unittest.main()
