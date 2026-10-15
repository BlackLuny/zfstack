"""Evidence admission negatives plus request accounting and aligned windows."""
import argparse
import builtins
import copy
import gzip
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import evidence
from evidence import aligned_metrics, digest, inspect_case
from export_case import export_case
from load_contract import Progress, arrivals, counters, effective_configuration, resources_for_phase, worker_resources, worker_schedule
from test_netem import clean_report
from test_endpoints import endpoint_fixture


def worker_fixture(config, start=1):
    """Synthetic raw accounting fixture; never a real performance qualification."""
    loaded_start = start + config["phases_sec"]["baseline"]
    loaded_duration = config["phases_sec"]["loaded"]
    rows = []
    for at in [start - .5, *worker_schedule(start, config["phases_sec"])]:
        fraction = min(max((at - loaded_start) / loaded_duration, 0), 1)
        at_ns = round(at * 1e9) + 1000
        rows.append({"scheduled_ns": round(at * 1e9), "at_ns": at_ns, "at": at_ns / 1e9,
                     "cpu_sec": 2 * fraction, "verified_bytes": int(900 * fraction),
                     "boundary_skew_ns": 1000, "cpu_resolution_ns": 10_000_000,
                     "rss_kib": 1024, "peak_rss_kib": 1024, "compilers": [],
                     "host_cpu_pressure": "some avg10=0.00 avg60=0.00 avg300=0.00 total=0",
                     "threads": {"1": {"name": "synthetic-worker", "cpu_sec": 2 * fraction}}})
    return rows


def fixture(path):
    a = argparse.Namespace(conns=1, request_timeout=3, baseline=8, loaded=16, recovery=8, port=19001)
    config = effective_configuration(a, {"L2_RUNTIME_ID": "tokio-production", "L2_PROFILE_ID": "release"})
    result = {"ok": True, "errors": [], "performance_contaminated": False, "start_at": 1,
              "request_timeout_sec": 3, "uplink_delay_ms": 0, "effective_config": config,
              "heavy_bytes": 1000, "qdisc": clean_report(0)}
    before = {"target": {}, "worker": {"from_target": {"bytes": 0}}}
    after = {"target": {}, "worker": {"from_target": {"bytes": 1000}}}
    for name in ("client", "fwd1", "fwd2", "fwd3"):
        before["target"][f"from_{name}"] = {"bytes": 0}
        after["target"][f"from_{name}"] = {"bytes": 0}
        before["worker"][f"from_{name}"] = {"bytes": 0}
        after["worker"][f"from_{name}"] = {"bytes": 2000}
    result["path_evidence"] = {"before": before, "after": after, "startup_ports": 2, "errors": []}
    raw = {"client-0.json": {"flows": [{"bytes": 1000, "errors": []}]}, "worker.sha256": "a" * 64 + " worker"}
    raw["endpoint-samples.json"] = endpoint_fixture(config)
    raw["worker-samples.json"] = worker_fixture(config)
    result["worker"] = worker_resources(raw["worker-samples.json"], result["start_at"], config["phases_sec"])
    for i in range(1, 4):
        samples = [{"seq": n, "phase": phase, "status": "completed", "scheduled_at": n,
                    "started_at": n, "latency_ms": 1} for n, phase in enumerate(("baseline", "loaded", "recovery"))]
        raw[f"client-{i}.json"] = {"samples": samples, "counters": counters(samples), "errors": []}
    (path / "result.json").write_text(json.dumps(result))
    (path / "raw.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))
    return result, raw


def identity(config):
    return {"run_id": "run", "block_id": "block", "arm": "B", "case_id": "C16.custom",
            "case_version": 1, "case_instance_id": "1" * 64, "tested_sha": "a" * 40,
            "plan_hash": "2" * 64, "build_manifest_hash": "3" * 64, "environment_hash": "4" * 64,
            "config_hash": digest(config), "binary_sha256": "a" * 64,
            "execution": {"status": "completed", "exit_code": 0, "start_ns": 1, "end_ns": 2,
                          "clock": "CLOCK_MONOTONIC", "cleanup": "PASS"}}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.result, self.raw = fixture(self.path)

    def save(self):
        (self.path / "result.json").write_text(json.dumps(self.result))
        (self.path / "raw.json.gz").write_bytes(gzip.compress(json.dumps(self.raw).encode()))

    def test_offline_accounting_imports_without_posix_file_locks(self):
        original = builtins.__import__
        def without_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ModuleNotFoundError("fcntl is unavailable on Windows")
            return original(name, *args, **kwargs)
        spec = importlib.util.spec_from_file_location("portable_load_contract", Path(__file__).with_name("load_contract.py"))
        module = importlib.util.module_from_spec(spec)
        with patch("builtins.__import__", side_effect=without_fcntl):
            spec.loader.exec_module(module)
            self.assertEqual(module.counters([])["scheduled"], 0)
            self.assertEqual(module.arrivals(0, 1, 2), [0, .5])
            with self.assertRaises(ModuleNotFoundError):
                module.Progress(self.path / "unsupported-counter", 1, create=True)
        self.assertFalse((self.path / "unsupported-counter").exists())

    def test_general_case_export_preserves_three_distinct_scopes(self):
        row = export_case(self.path, identity(self.result["effective_config"]))
        self.assertEqual(row["case_id"], "C16.custom")
        self.assertEqual(row["validity"], {"functional": "VALID", "performance": "VALID", "reasons": []})
        self.assertEqual(row["functional"]["status"], "PASS")
        self.assertEqual(row["performance"]["status"], "INCONCLUSIVE")
        self.assertAlmostEqual(row["performance"]["metrics"]["worker_cpu_sec_per_gib"], 2 * 1024 ** 3 / 900)

    def test_frozen_case_recomputes_from_clean_qa_import_without_sibling_path(self):
        command = [sys.executable, "-c", "import json,sys; from qa.integration import _l2_module; "
                   "r=_l2_module('evidence').inspect_case(sys.argv[1]); "
                   "print(json.dumps({'functional':r['functional_errors'],'performance':r['performance_errors'],"
                   "'endpoints':list(r['endpoint_diagnostics'])}))", str(self.path)]
        for mode in ("closed-loop", "open-loop"):
            self.result["effective_config"]["light_mode"] = mode; self.save()
            process = subprocess.run(command, cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            row = json.loads(process.stdout)
            self.assertFalse(row["functional"])
            self.assertTrue(row["endpoints"])
            if mode == "closed-loop":
                self.assertFalse(row["performance"])
            else:
                # Existing closed-loop samples cannot impersonate the separately
                # frozen open-loop schedule. Importing its verifier must work.
                self.assertIn("open-loop", str(row["performance"]))

    def test_compiler_contamination_keeps_functional_evidence(self):
        self.result["performance_contaminated"] = True; self.save()
        row = export_case(self.path, identity(self.result["effective_config"]))
        self.assertEqual(row["validity"]["functional"], "VALID")
        self.assertEqual(row["functional"]["status"], "PASS")
        self.assertEqual(row["validity"]["performance"], "INVALID")
        self.raw["client-1.json"]["errors"].append("corrupt payload"); self.save()
        row = export_case(self.path, identity(self.result["effective_config"]))
        self.assertEqual(row["functional"]["status"], "FAIL")
        self.assertEqual(row["validity"]["performance"], "INVALID")

    def test_missing_light_peer_and_path_never_become_functional_pass(self):
        self.raw.pop("client-3.json"); self.save()
        row = export_case(self.path, identity(self.result["effective_config"]))
        self.assertEqual(row["functional"]["status"], "NOT_RUN")
        self.assertEqual(row["validity"]["functional"], "UNKNOWN")
        self.result.pop("path_evidence"); self.save()
        self.assertIn("path", str(inspect_case(self.path)["functional_evidence_errors"]))

    def test_wrong_config_or_binary_cannot_certify_functional_pass(self):
        for key in ("config_hash", "binary_sha256"):
            ids = identity(self.result["effective_config"]); ids[key] = "f" * 64
            row = export_case(self.path, ids)
            self.assertEqual(row["functional"]["status"], "NOT_RUN")
            self.assertEqual(row["validity"]["functional"], "UNKNOWN")

    def test_unknown_runtime_profile_and_bypassed_path_block(self):
        self.result["effective_config"]["runtime"] = None
        self.result["path_evidence"]["after"]["target"]["from_client"]["bytes"] = 1
        self.save()
        row = export_case(self.path, identity(self.result["effective_config"]))
        self.assertEqual(row["validity"]["functional"], "UNKNOWN")
        self.assertIn("runtime", str(row["validity"]["reasons"]))

    def test_zero_missing_negative_and_reset_byte_evidence_is_not_low_cpu(self):
        for value in (None, 0, -1, True, "900"):
            self.result["worker"]["loaded"]["measurement_window"]["verified_bytes"] = value
            self.save()
            observed = inspect_case(self.path)
            self.assertIsNone(observed["metrics"]["worker_cpu_sec_per_gib"])
            self.assertTrue(observed["performance_errors"])
        self.raw["client-0.json"]["flows"][0]["bytes"] = 0; self.save()
        self.assertIn("no progress", str(inspect_case(self.path)["functional_errors"]))

    def test_cpu_and_verified_bytes_use_same_actual_boundaries(self):
        metrics, errors = aligned_metrics(self.result)
        self.assertFalse(errors)
        self.assertAlmostEqual(metrics["worker_cpu_cores"], 2 / 16)
        self.assertAlmostEqual(metrics["heavy_mbps"], 900 * 8 / 16 / 1e6)
        # The application total is intentionally different from the aligned
        # counter delta. Changing it may not silently change sampled CPU/GiB.
        self.result["heavy_bytes"] = 100_000_000
        self.assertEqual(aligned_metrics(self.result)[0], metrics)
        self.result["worker"]["loaded"]["measurement_window"]["max_boundary_skew_ns"] = 20_000_000
        self.assertIsNone(aligned_metrics(self.result)[0]["worker_cpu_sec_per_gib"])

    def test_missing_identity_timestamps_are_not_fabricated(self):
        ids = identity(self.result["effective_config"]); ids.pop("execution")
        with self.assertRaisesRegex(ValueError, "execution"):
            export_case(self.path, ids)

    def test_nonfinite_duplicate_and_expanding_input_are_rejected(self):
        path = self.path / "bad.json"
        for text in ('{"n":NaN}', '{"n":Infinity}', '{"n":1e999}', '{"n":1,"n":2}'):
            path.write_text(text)
            with self.assertRaises(ValueError): evidence.read_json(path)
        path = self.path / "bomb.json.gz"
        path.write_bytes(gzip.compress(b' ' * 3000))
        with patch.object(evidence, "MAX_JSON_BYTES", 1024), self.assertRaisesRegex(ValueError, "decompressed"):
            evidence.read_json(path)


class MeasurementContractTests(unittest.TestCase):
    def test_arrivals_are_fixed_without_completion_feedback(self):
        self.assertEqual(arrivals(10, 1, 4), [10, 10.25, 10.5, 10.75])
        for rate in (0, -1, float("inf")):
            with self.assertRaises(ValueError): arrivals(0, 1, rate)

    def test_failures_and_unstarted_arrivals_stay_in_denominator(self):
        samples = [{"status": "completed", "started_at": 1}, {"status": "timed_out", "started_at": 2},
                   {"status": "missed", "started_at": None}]
        result = counters(samples, scheduled=5)
        self.assertEqual(result, {"scheduled": 5, "started": 2, "completed": 1, "failed": 0,
                                  "timed_out": 1, "unfinished": 2, "missed": 1})

    def test_empty_single_and_counter_reset_samples_do_not_crash_or_invent_zero(self):
        def sample(at, cpu, amount):
            return {"at": at, "at_ns": at * 10 ** 9, "cpu_sec": cpu, "verified_bytes": amount,
                    "boundary_skew_ns": 10, "rss_kib": 100}
        self.assertIsNone(resources_for_phase([], 0, 10)["measurement_window"]["cpu_sec"])
        self.assertIsNone(resources_for_phase([sample(1, 1, 1)], 0, 10)["measurement_window"]["verified_bytes"])
        window = resources_for_phase([sample(1, 2, 5), sample(2, 1, 4)], 0, 10)["measurement_window"]
        self.assertIn("verified-byte counter reset", window["errors"])
        self.assertIn("worker CPU counter reset", window["errors"])

    def test_interior_window_cannot_exclude_load_onset(self):
        data = [{"at": t, "at_ns": t * 10 ** 9, "cpu_sec": t, "verified_bytes": t * 100,
                 "boundary_skew_ns": 10, "rss_kib": 100} for t in (1, 2)]
        interior = resources_for_phase(data, 0, 3)["measurement_window"]
        self.assertIn("load onset", str(interior["errors"]))
        for row in data: row["scheduled_ns"] = row["at_ns"]
        exact = resources_for_phase(data, 1, 2)["measurement_window"]
        self.assertFalse(exact["errors"])
        self.assertEqual(exact["verified_bytes"], 100)

    def test_verified_progress_accumulates_all_flows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "progress"
            progress = Progress(path, 3, create=True)
            try:
                progress.write(0, 7); progress.write(2, 13)
                self.assertEqual(progress.read(), 20)
            finally: progress.close()


if __name__ == "__main__":
    unittest.main()
