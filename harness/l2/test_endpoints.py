"""Endpoint accounting negatives and real collector/archive closure."""
import copy
import argparse
import gzip
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import endpoints
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

    def make_proc(self, pid=42):
        proc = self.root / "proc"; target = proc / str(pid); (target / "ns").mkdir(parents=True)
        fields = ["0"] * 40
        fields[0], fields[11], fields[12], fields[19] = "S", "12", "3", str(pid + 80)
        (target / "stat").write_text(str(pid) + " (unusual name) " + " ".join(fields))
        (target / "status").write_text("Name:\tpython3\nVmRSS:\t100 kB\nVmHWM:\t120 kB\n")
        (target / "comm").write_text("python3\n")
        (target / "exe").symlink_to("/usr/bin/python3")
        (target / "ns/net").symlink_to("net:[7]")
        return proc, target, fields

    def _check_repeated_open_cost_is_outside_the_frozen_sampling_window(self):
        # Inject only 0.2 ms per open. Reopening 3 files for 9/11 endpoints
        # costs 5.4/6.6 ms per round; this is not a real-host qualification.
        for outbound, count in (("direct", 9), ("relay", 11)):
            with self.subTest(outbound=outbound):
                config = {**self.config, "outbound": outbound, "udp_enabled": True,
                          "phases_sec": {"baseline": 1, "loaded": 1, "recovery": 1}}
                pids = dict(zip(expected_endpoints(config), range(100, 100 + count)))
                for pid in pids.values():
                    if not (self.root / "proc" / str(pid)).exists():
                        self.make_proc(pid)
                clock = [0]
                path_open, fd_open = Path.open, os.open
                prepared_reader = getattr(endpoints, "_ProcReader", None)

                def slow_open(open_fn, *args, **kwargs):
                    clock[0] += 200_000
                    return open_fn(*args, **kwargs)

                def wait(delay):
                    clock[0] += round(delay * 1e9)
                    return False

                sampler = EndpointSampler(config, 1, pids, self.root / (outbound + ".json"))
                with patch.dict(read_snapshot.__kwdefaults__, {"proc": self.root / "proc"}), \
                     patch("endpoints._ProcReader", create=True, side_effect=lambda ids: prepared_reader(ids, proc=self.root / "proc")), \
                     patch.object(Path, "open", new=lambda *a, **kw: slow_open(path_open, *a, **kw)), \
                     patch("endpoints.os.open", side_effect=lambda *a, **kw: slow_open(fd_open, *a, **kw)), \
                     patch("endpoints.time.monotonic_ns", side_effect=lambda: clock[0]), \
                     patch.object(sampler.stop, "wait", side_effect=wait):
                    sampler._run()
                report = sampler.report
                self.assertFalse(report["errors"], report["errors"])
                rows = [row for record in report["endpoints"].values() for row in record["samples"]]
                self.assertEqual(max(row["boundary_error_ns"] for row in rows), 0)
                for record in report["endpoints"].values():
                    self.assertEqual([row["scheduled_ns"] for row in record["samples"]], sample_schedule(1, config["phases_sec"]))
                    self.assertEqual({row["boundary_error_ns"] for row in record["samples"]}, {0})
                self.assertFalse(validate_evidence(report, self.result, config)[0])

    def _check_prepared_reader_refreshes_values_and_checks_every_identity_field(self):
        proc, target, fields = self.make_proc()
        reader = endpoints._ProcReader({"endpoint": 42}, proc=proc)
        self.addCleanup(reader.close)
        identity, first = reader(42, 0, None)
        fields[11] = "25"
        (target / "stat").write_text("42 (unusual name) " + " ".join(fields))
        (target / "status").write_text("Name:\tpython3\nVmRSS:\t105 kB\nVmHWM:\t125 kB\n")
        with patch("endpoints.time.monotonic_ns", side_effect=[100, 900]):
            same, row = reader(42, 80, identity)
        self.assertEqual(same, identity)
        self.assertGreater(row["cpu_ticks"], first["cpu_ticks"])
        self.assertEqual((row["rss_kib"], row["peak_rss_kib"]), (105, 125))
        self.assertEqual((row["at_ns"], row["read_duration_ns"], row["boundary_error_ns"]), (900, 800, 820))
        for name, value in (("comm", "changed\n"), ("exe", "/usr/bin/other"), ("ns/net", "net:[8]")):
            with self.subTest(field=name):
                path = target / name
                old = os.readlink(path) if path.is_symlink() else path.read_text()
                if path.is_symlink():
                    path.unlink(); path.symlink_to(value)
                else:
                    path.write_text(value)
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    reader(42, 0, identity)
                if path.is_symlink():
                    path.unlink(); path.symlink_to(old)
                else:
                    path.write_text(old)
        fields[19] = "999"
        (target / "stat").write_text("42 (unusual name) " + " ".join(fields))
        with self.assertRaisesRegex(ValueError, "identity changed"):
            reader(42, 0, identity)

    def _check_prepared_reader_closes_partial_preparation_and_retries_missing_process(self):
        proc, target, _ = self.make_proc()
        (target / "status").unlink()
        opened, open_fd = [], os.open

        def tracked_open(*args, **kwargs):
            fd = open_fd(*args, **kwargs); opened.append(fd); return fd

        with patch("endpoints.os.open", side_effect=tracked_open):
            reader = endpoints._ProcReader({"missing": 43, "partial": 42, "invalid": None}, proc=proc)
        self.addCleanup(reader.close)
        self.assertFalse(reader.files)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)
        with self.assertRaises(OSError):
            reader(42, 0, None)
        (target / "status").write_text("VmRSS:\t100 kB\nVmHWM:\t120 kB\n")
        self.assertEqual(reader(42, 0, None)[1]["rss_kib"], 100)

    def _check_unexpected_preparation_failure_releases_all_owned_descriptors(self):
        proc, _, _ = self.make_proc(42)
        self.make_proc(43)
        opened, calls, open_fd, fdopen = [], [], os.open, os.fdopen

        def tracked_open(*args, **kwargs):
            fd = open_fd(*args, **kwargs); opened.append(fd); return fd

        def fail_later(fd):
            calls.append(fd)
            if len(calls) == 4:
                raise RuntimeError("cannot wrap descriptor")
            return fdopen(fd)

        with patch("endpoints.os.open", side_effect=tracked_open), patch("endpoints.os.fdopen", side_effect=fail_later):
            with self.assertRaisesRegex(RuntimeError, "cannot wrap descriptor"):
                endpoints._ProcReader({"first": 42, "second": 43}, proc=proc)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_cleanup_error_does_not_skip_other_handles_or_error_archive(self):
        reader = endpoints._ProcReader({})
        failed, other = Mock(), Mock()
        failed.close.side_effect = OSError("close failed")
        reader.files = {42: {"directory": 99, "stat": other, "comm": failed}}
        with patch("endpoints.os.close") as close:
            with self.assertRaisesRegex(OSError, "close failed"):
                reader.close()
        failed.close.assert_called_once(); other.close.assert_called_once()
        close.assert_called_once_with(99)
        self.assertFalse(reader.files)
        reader.close = Mock(side_effect=OSError("close failed"))
        sampler = EndpointSampler(self.config, 1, {}, self.root / "cleanup-error.json")
        sampler.stop.set()
        with patch("endpoints._ProcReader", return_value=reader):
            sampler._run()
        report = json.loads((self.root / "cleanup-error.json").read_text())
        self.assertIn("endpoint sampler cleanup failed: close failed", report["errors"])

    def _check_preparation_and_cleanup_failure_also_close_previous_endpoints(self):
        proc, _, _ = self.make_proc(42)
        _, target, _ = self.make_proc(43)
        (target / "status").unlink()
        opened, open_fd, close_fd, failed = [], os.open, os.close, []

        def tracked_open(*args, **kwargs):
            fd = open_fd(*args, **kwargs); opened.append(fd); return fd

        def failed_close(fd):
            close_fd(fd)
            if not failed:
                failed.append(fd)
                raise OSError("directory close failed")

        with patch("endpoints.os.open", side_effect=tracked_open), patch("endpoints.os.close", side_effect=failed_close):
            with self.assertRaisesRegex(OSError, "directory close failed"):
                endpoints._ProcReader({"first": 42, "second": 43}, proc=proc)
        for fd in opened:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_prepared_text_reader_consumes_short_reads_and_restarts_at_zero(self):
        class ShortReads(io.BytesIO):
            def read1(self, size=-1):
                return super().read(min(size, 7) if size >= 0 else 7)

        value = "Name:\tpython3\nVmRSS:\t100 kB\nVmHWM:\t120 kB\n"
        with io.TextIOWrapper(ShortReads(value.encode())) as stream:
            for _ in range(2):
                self.assertEqual(endpoints._proc_text(Path("/unused"), "status", {"status": stream}), value)

    def _check_prepared_reader_rejects_oversized_or_invalid_text_and_closes_on_cancel(self):
        proc, target, _ = self.make_proc()
        reader = endpoints._ProcReader({"endpoint": 42}, proc=proc)
        self.addCleanup(reader.close)
        fds = [value if name == "directory" else value.fileno() for name, value in reader.files[42].items()]
        (target / "comm").write_text("x" * 65_537)
        with self.assertRaisesRegex(ValueError, "snapshot limit"):
            reader(42, 0, None)
        (target / "comm").write_bytes(b"\xff")
        with self.assertRaises(UnicodeError):
            reader(42, 0, None)
        sampler = EndpointSampler(self.config, 1, {"generator-heavy-0": 42}, self.root / "cancelled.json")
        sampler.stop.set()
        with patch("endpoints._ProcReader", return_value=reader):
            sampler._run()
        self.assertTrue(sampler.report["errors"])
        self.assertTrue(all(not record["samples"] for record in sampler.report["endpoints"].values()))
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def _check_prepared_reader_preserves_zombie_and_rejects_reaped_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        reader = endpoints._ProcReader({"endpoint": child.pid})
        try:
            identity, _ = reader(child.pid, time.monotonic_ns(), None)
            child.terminate()
            os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
            same, row = reader(child.pid, time.monotonic_ns(), identity)
            self.assertEqual(same, identity)
            self.assertEqual(row["identity_observation"], "retained-zombie")
            self.assertIsNone(row["rss_kib"])
            child.wait(timeout=5)
            with self.assertRaises(OSError):
                reader(child.pid, time.monotonic_ns(), identity)
        finally:
            reader.close()
            if child.poll() is None:
                child.kill(); child.wait(timeout=5)

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
        # Exercise reusable streams under the same Unix parser fixture and
        # platform requirements as the ordinary snapshot reader below.
        original_root = self.root
        for check in (self._check_repeated_open_cost_is_outside_the_frozen_sampling_window,
                      self._check_prepared_reader_refreshes_values_and_checks_every_identity_field,
                      self._check_prepared_reader_closes_partial_preparation_and_retries_missing_process,
                      self._check_unexpected_preparation_failure_releases_all_owned_descriptors,
                      self._check_preparation_and_cleanup_failure_also_close_previous_endpoints,
                      self._check_prepared_reader_rejects_oversized_or_invalid_text_and_closes_on_cancel):
            with self.subTest(check=check.__name__):
                self.root = original_root / check.__name__
                self.root.mkdir()
                try:
                    check()
                finally:
                    self.root = original_root
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
        self._check_prepared_reader_preserves_zombie_and_rejects_reaped_pid()
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
