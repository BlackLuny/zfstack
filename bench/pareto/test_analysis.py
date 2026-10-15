"""Consumption regressions use synthetic JSON only, never benchmark binaries."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import analyze
import run_ab


def measurement(version, case="tx-alloc"):
    row = {
        "tool": "zfstack-pareto-probe", "schema_version": version, "ok": True,
        "case": case, "iterations": 1, "flows": 1, "peers": 1, "payload_bytes": 1,
        "work_wall_ns": 10, "work_cpu_ns": 5, "operations": 1,
        "effective_bytes": 1, "allocation_requested_bytes": 1,
        "checksum_u64": 0, "expected_checksum_u64": 0,
        "final_reserved_bytes": 0, "all_budget_leases_released": True,
        "virtual_clock_used_for_rate": False, "real_network_io": False,
        "sampled_peak_reserved_bytes": 65 if version == 1 else None,
        "peak_reserved_is_sampled_lower_bound": True if version == 1 else None,
    }
    if version == 2:
        row.update(warmup_reserved_bytes=2336, warmup_reserved_scope=
                   "ledger_after_warmup_push_before_consume_excludes_transient_reservations")
    return row


def accepted(version, variant, case="tx-alloc"):
    row = {"variant": variant, "pair": 0, "exit_code": 0,
           "binary_sha256": "a" * 64, "position": 0 if variant == "baseline" else 1,
           "measurements": [measurement(version, case)]}
    result, errors = analyze.validate_row(row, 1, {}, None)
    if errors:
        raise AssertionError(errors)
    return result


class ConsumptionTests(unittest.TestCase):
    def test_legacy_and_v2_tx_metrics_do_not_mix_estimate_and_snapshot(self):
        baseline, candidate = accepted(1, "baseline"), accepted(2, "candidate")
        pair = {"pair": 0, "order": ["baseline", "candidate"],
                "baseline": baseline, "candidate": candidate}
        summary = analyze.case_summary("tx", [pair], 0, 1)
        peak = summary["memory"]["sampled_peak_reserved_bytes"]
        self.assertEqual(peak["baseline"]["n"], 0)
        self.assertEqual(peak["candidate"]["n"], 0)
        snapshot = summary["memory"]["warmup_reserved_bytes"]
        self.assertEqual(snapshot["baseline"]["n"], 0)
        self.assertEqual(snapshot["candidate"]["median"], 2336)
        self.assertEqual(snapshot["paired_change_pct"]["values"], [])
        self.assertIsNone(snapshot["ratio_of_medians_change_pct"])
        self.assertIn("not an allocator/RSS peak", " ".join(summary["warnings"]))

    def test_verification_methods_must_match_within_a_pair(self):
        for baseline_method, expected_exit in [(None, 2), ("outlined_verify_v1", 2), ("outlined_cacheline_verify_v1", 2), ("outlined_page_verify_v1", 2), ("byte_compare_sum_v1", 2), ("portable_full_scan_v1", 0)]:
            with self.subTest(baseline_method=baseline_method), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                cases = root / "cases.json"
                cases.write_text(json.dumps([{"name": "tx", "args": ["tx-alloc", "--iterations", "1", "--payload", "1"]}]))
                records = []
                for position, variant in enumerate(["baseline", "candidate"]):
                    m = measurement(2)
                    method = baseline_method if variant == "baseline" else "portable_full_scan_v1"
                    if method is not None:
                        m["verification_method"] = method
                    records.append({"case": "tx", "variant": variant, "pair": 0, "exit_code": 0,
                                    "binary_sha256": "a" * 64, "position": position, "measurements": [m]})
                data = root / "runs.jsonl"
                data.write_text("\n".join(json.dumps(r) for r in records) + "\n")
                argv = ["analyze.py", str(data), "--cases", str(cases), "--expected-pairs", "1", "--bootstrap", "0"]
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(analyze.main(), expected_exit)
                result = json.loads((root / "summary.json").read_text())
                self.assertEqual(result["validation"]["ok"], expected_exit == 0)
                if expected_exit:
                    self.assertIn("verification_method", json.dumps(result["validation"]["excluded_pairs"]))

    def test_non_tx_legacy_peak_is_preserved(self):
        result = accepted(1, "baseline", "rx")
        self.assertEqual(result["metrics"]["sampled_peak_reserved_bytes"], 65)
        self.assertIsNone(result["metrics"]["warmup_reserved_bytes"])

    def test_invalid_warmup_metric_is_rejected(self):
        for value in [-1, True, "2336", float("nan")]:
            row = {"variant": "candidate", "pair": 0, "exit_code": 0,
                   "binary_sha256": "a" * 64, "measurements": [measurement(2)]}
            row["measurements"][0]["warmup_reserved_bytes"] = value
            result, errors = analyze.validate_row(row, 1, {}, None)
            self.assertIsNone(result)
            self.assertIn("invalid derived/source metric warmup_reserved_bytes", errors)

    def test_runner_and_analyzer_accept_v1_v2_without_executing_binaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, candidate = root / "baseline", root / "candidate"
            baseline.write_bytes(b"synthetic v1 fixture, not executable")
            candidate.write_bytes(b"synthetic v2 fixture, not executable")
            cases = root / "cases.json"
            cases.write_text(json.dumps([{"name": "tx", "args":
                                         ["tx-alloc", "--iterations", "1", "--payload", "1"]}]))
            output = root / "records"

            def fake_process(command, **kwargs):
                version = 1 if command[3] == str(baseline) else 2
                return subprocess.CompletedProcess(command, 0,
                                                   json.dumps(measurement(version)) + "\n", "")

            argv = ["run_ab.py", "--baseline", str(baseline), "--candidate", str(candidate),
                    "--cases", str(cases), "--outdir", str(output), "--pairs", "1"]
            with patch.object(sys, "argv", argv), patch.object(run_ab.subprocess, "run", side_effect=fake_process) as mocked:
                with patch.object(run_ab, "snapshot", return_value={}), contextlib.redirect_stdout(io.StringIO()):
                    run_ab.main()
            self.assertEqual(mocked.call_count, 4)  # synthetic warmup + one pair
            terminal = io.StringIO()
            with patch.object(sys, "argv", ["analyze.py", str(output), "--bootstrap", "0"]):
                with contextlib.redirect_stdout(terminal):
                    self.assertEqual(analyze.main(), 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertTrue(summary["validation"]["ok"])
            self.assertTrue(summary["validation"]["complete"])
            self.assertEqual(summary["validation"]["warmup_records_ignored"], 2)
            self.assertIn("warmup ledger KiB", terminal.getvalue())


if __name__ == "__main__":
    unittest.main()
