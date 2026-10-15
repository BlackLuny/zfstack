"""Endpoint accounting negatives and real collector/archive closure."""
import copy
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from endpoints import EndpointSampler, boundaries, expected_endpoints, read_snapshot, sample_schedule, validate_evidence


def endpoint_fixture(config, start=1):
    report = {"schema_version": 1, "clock": "CLOCK_MONOTONIC", "scope": "fixture",
              "planned_boundaries": boundaries(start, config["phases_sec"]),
              "boundary_tolerance_ns": config["boundary_tolerance_ns"], "errors": [], "endpoints": {}}
    for ordinal, (name, role) in enumerate(expected_endpoints(config).items()):
        identity = {"pid": ordinal + 100, "starttime_ticks": ordinal + 200, "comm": "python3", "exe": "/usr/bin/python3", "netns": f"net:[{ordinal + 10}]"}
        rows = []
        for index, scheduled in enumerate(sample_schedule(start, config["phases_sec"])):
            rows.append({"scheduled_ns": scheduled, "at_ns": scheduled + 1000, "read_duration_ns": 500, "boundary_error_ns": 1000,
                         "pid": identity["pid"], "starttime_ticks": identity["starttime_ticks"], "identity_observation": "live",
                         "state": "S", "cpu_ticks": index + 1, "ticks_per_sec": 100, "cpu_sec": (index + 1) / 100,
                         "cpu_resolution_ns": 10_000_000, "rss_kib": 1024, "peak_rss_kib": 2048})
        report["endpoints"][name] = {**role, "identity": identity, "samples": rows}
    return report


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from test_evidence import fixture
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.result, self.raw = fixture(self.root)
        self.config = self.result["effective_config"]

    def test_missing_endpoint_reset_identity_and_phase_are_invalid(self):
        report = endpoint_fixture(self.config)
        self.assertFalse(validate_evidence(report, self.result, self.config)[0])
        for mutate in (lambda r: r["endpoints"].pop("target-business"),
                       lambda r: r["endpoints"]["generator-heavy-0"]["samples"].pop(),
                       lambda r: r["endpoints"]["generator-heavy-0"]["samples"][-1].update(cpu_ticks=0, cpu_sec=0),
                       lambda r: r["endpoints"]["generator-heavy-0"]["samples"][-1].update(starttime_ticks=999),
                       lambda r: r["endpoints"]["target-business"]["identity"].update(pid=100),
                       lambda r: r["endpoints"]["target-business"]["samples"][-1].update(rss_kib=None)):
            bad = copy.deepcopy(report); mutate(bad)
            self.assertTrue(validate_evidence(bad, self.result, self.config)[0])

    def test_zombie_retains_cpu_but_rss_must_stay_unknown(self):
        report = endpoint_fixture(self.config)
        final = report["endpoints"]["generator-heavy-0"]["samples"][-1]
        final.update(state="Z", identity_observation="retained-zombie", rss_kib=None, peak_rss_kib=None)
        errors, diagnostics = validate_evidence(report, self.result, self.config)
        self.assertFalse(errors, errors)
        self.assertGreater(diagnostics["generator-heavy-0"]["phases"]["recovery"]["cpu_sec"], 0)
        final["rss_kib"] = 0
        self.assertTrue(validate_evidence(report, self.result, self.config)[0])

    def test_missing_perf_endpoint_does_not_erase_functional_success_or_failure(self):
        from evidence import inspect_case
        self.raw.pop("endpoint-samples.json")
        (self.root / "raw.json.gz").write_bytes(gzip.compress(json.dumps(self.raw).encode()))
        inspected = inspect_case(self.root)
        self.assertFalse(inspected["functional_errors"])
        self.assertFalse(inspected["functional_evidence_errors"])
        self.assertTrue(inspected["performance_errors"])
        self.raw["client-1.json"]["errors"].append("corrupt payload")
        (self.root / "raw.json.gz").write_bytes(gzip.compress(json.dumps(self.raw).encode()))
        self.assertIn("corrupt payload", inspect_case(self.root)["functional_errors"])

    def test_observer_failure_cannot_skip_generator_queue_or_sample_cleanup(self):
        import load
        a = argparse.Namespace(out=str(self.root / "load"), host="127.0.0.1", port=19001, conns=1, request_timeout=1,
                               baseline=2, loaded=2, recovery=2, clients=["a", "b", "c", "d"], worker_pid=5)
        for failed_begin in (False, True):
            with self.subTest(begin_failed=failed_begin):
                children = [Mock(pid=index + 10, returncode=0) for index in range(4)]
                for child in children:
                    child.poll.return_value = None
                observer, progress, queue = Mock(), Mock(), Mock()
                observer.finish.side_effect = OSError("observer output failed")
                if failed_begin:
                    observer.begin.side_effect = RuntimeError("cannot start observer")
                with patch.dict(os.environ, {"L2_UDP_PROBES": "0", "L2_LIGHT_CONNS": "1", "L2_CHURN_REQUESTS": "0"}), \
                     patch("load.Progress", return_value=progress), patch("load.subprocess.Popen", side_effect=children), \
                     patch("load.process_stats", side_effect=ValueError("worker sample failed")), patch("load.wait_until"), \
                     patch("endpoints.EndpointSampler", return_value=observer), \
                     patch("queue_control.configuration", return_value={"enabled": True}), patch("queue_control.QueueControl", return_value=queue):
                    with self.assertRaisesRegex(ValueError, "worker sample failed"):
                        load.run(a)
                for child in children:
                    child.kill.assert_called_once(); child.wait.assert_called_once()
                progress.close.assert_called_once()
                queue.finish.assert_called_once_with(cancelled=True)
                self.assertEqual(json.loads((Path(a.out) / "worker-samples.json").read_text()), [])

    @unittest.skipIf(os.name == "nt", "portable /proc parser fixture uses symlinks")
    def test_proc_reader_never_collects_command_lines_and_preserves_zombie(self):
        proc = self.root / "proc"; target = proc / "42"; (target / "ns").mkdir(parents=True)
        fields = ["0"] * 40
        fields[0], fields[11], fields[12], fields[19] = "S", "12", "3", "80"
        (target / "stat").write_text("42 (unusual name) " + " ".join(fields))
        (target / "status").write_text("Name:\tpython3\nVmRSS:\t100 kB\nVmHWM:\t120 kB\n")
        (target / "comm").write_text("python3\n")
        (target / "exe").symlink_to("/usr/bin/python3")
        (target / "ns/net").symlink_to("net:[7]")
        (target / "cmdline").write_text("do-not-collect-secret-key")
        identity, sample = read_snapshot(42, time.monotonic_ns(), proc=proc, ticks=100)
        self.assertEqual(sample["cpu_sec"], .15)
        self.assertNotIn("secret", json.dumps([identity, sample]))
        fields[0] = "Z"
        (target / "stat").write_text("42 (unusual name) " + " ".join(fields))
        (target / "status").write_text("Name:\tpython3\n")
        (target / "exe").unlink(); (target / "ns/net").unlink()
        same, sample = read_snapshot(42, time.monotonic_ns(), identity, proc=proc, ticks=100)
        self.assertEqual(identity, same)
        self.assertEqual(sample["cpu_sec"], .15)
        self.assertIsNone(sample["rss_kib"])

    @unittest.skipUnless(sys.platform == "linux", "actual endpoint /proc CPU sampling requires Linux")
    def test_real_sampler_file_survives_freeze_and_independent_inspection(self):
        from evidence import inspect_case, load_case
        repository = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repository))
        try:
            from qa.integration import freeze_l2_raw
        finally:
            sys.path.pop(0)
        output = self.root / "case"; rig = output / "rig"; load = rig / "load"; load.mkdir(parents=True)
        result, config, raw = copy.deepcopy(self.result), copy.deepcopy(self.config), copy.deepcopy(self.raw)
        config["phases_sec"] = {"baseline": .15, "loaded": .15, "recovery": .15}
        config["boundary_tolerance_ns"] = 50_000_000  # This fixture does not certify the production 5 ms budget.
        result.update(start_at=time.monotonic() + .15, effective_config=config)
        children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"]) for _ in expected_endpoints(config)]
        try:
            sampler = EndpointSampler(config, result["start_at"], dict(zip(expected_endpoints(config), (child.pid for child in children))), load / "endpoint-samples.json")
            sampler.begin(); sampler.finish()
            report = json.loads((load / "endpoint-samples.json").read_text())
            self.assertFalse(validate_evidence(report, result, config)[0])
            (rig / "result.json").write_text(json.dumps(result))
            (rig / "worker.sha256").write_text(raw.pop("worker.sha256"))
            raw.pop("endpoint-samples.json")
            for name, payload in raw.items():
                (load / name).write_text(json.dumps(payload))
            self.assertEqual(load_case(rig)[1]["endpoint-samples.json"], report)
            freeze_l2_raw(output)
            self.assertEqual(load_case(rig)[1]["endpoint-samples.json"], report)
            (load / "endpoint-samples.json").unlink()
            inspected = inspect_case(rig)
            self.assertEqual(set(inspected["endpoint_diagnostics"]), set(expected_endpoints(config)))
            self.assertFalse([error for error in inspected["performance_errors"] if "endpoint" in error])
            archived = load_case(rig)[1]
            archived["endpoint-samples.json"]["endpoints"].pop("target-business")
            (rig / "raw.json.gz").write_bytes(gzip.compress(json.dumps(archived).encode()))
            self.assertTrue([error for error in inspect_case(rig)["performance_errors"] if "endpoint" in error])
        finally:
            for child in children:
                child.terminate(); child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
