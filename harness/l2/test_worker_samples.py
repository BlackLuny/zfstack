"""Worker raw evidence closure and bounded verified-byte lock observations."""
import copy
import gzip
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from evidence import inspect_case, load_case, worker_evidence
from export_case import export_case
from load_contract import BOUNDARY_TOLERANCE_NS, Progress, worker_resources, worker_schedule
from test_evidence import fixture, identity


class WorkerEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "source"
        self.rig = self.output / "rig"; self.rig.mkdir(parents=True)
        self.result, self.raw = fixture(self.rig)
        self.original = copy.deepcopy((self.result, self.raw))

    def save(self, *, recompute=False):
        if recompute:
            self.result["worker"] = worker_resources(self.raw["worker-samples.json"], self.result["start_at"],
                                                      self.result["effective_config"]["phases_sec"])
        (self.rig / "result.json").write_text(json.dumps(self.result))
        (self.rig / "raw.json.gz").write_bytes(gzip.compress(json.dumps(self.raw).encode()))

    def assert_invalid(self, *, functional="PASS"):
        row = export_case(self.rig, identity(self.result["effective_config"]))
        self.assertEqual(row["validity"]["performance"], "INVALID", row)
        self.assertEqual(row["functional"]["status"], functional)
        self.assertEqual(row["validity"]["functional"], "VALID")
        self.assertIsNone(row["performance"]["metrics"]["worker_cpu_sec_per_gib"])
        return inspect_case(self.rig)

    def materialize(self):
        load = self.rig / "load"; load.mkdir()
        (self.rig / "raw.json.gz").unlink()
        for name, value in self.raw.items():
            if name.endswith(".json"):
                (load / name).write_text(json.dumps(value))
            else:
                (self.rig / name).write_text(value)
        return load

    def test_generated_worker_file_survives_freeze_archive_deletion_and_clean_import(self):
        repository = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repository))
        try:
            from qa.integration import freeze_l2_raw
            from qa.archive import archive
        finally:
            sys.path.pop(0)
        load = self.materialize()
        (load / "private-config.json").write_text('{"secret":"must-not-upload"}')
        self.assertEqual(load_case(self.rig)[1]["worker-samples.json"], self.raw["worker-samples.json"])
        freeze_l2_raw(self.output)
        (load / "worker-samples.json").unlink()
        frozen = load_case(self.rig)[1]["worker-samples.json"]
        self.assertEqual(frozen, self.raw["worker-samples.json"])
        bundle = self.root / "evidence.tar.gz"
        entries = archive(self.output, bundle)
        self.assertNotIn("rig/load/private-config.json", {entry["path"] for entry in entries})
        self.assertNotIn("rig/load/worker-samples.json", {entry["path"] for entry in entries})
        shutil.rmtree(self.output)
        restored = self.root / "restored"; (restored / "rig").mkdir(parents=True)
        with tarfile.open(bundle) as stream:
            for name in ("rig/result.json", "rig/raw.json.gz", "rig/worker.sha256"):
                (restored / name).write_bytes(stream.extractfile(name).read())
        command = [sys.executable, "-c", "import json,sys; from qa.integration import _l2_module; "
                   "r=_l2_module('evidence').inspect_case(sys.argv[1]); "
                   "print(json.dumps({'functional':r['functional_errors'],'performance':r['performance_errors'],"
                   "'metrics':r['metrics'],'raw':r['raw']['worker-samples.json']}))", str(restored / "rig")]
        child = subprocess.run(command, cwd=repository, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        report = json.loads(child.stdout)
        self.assertEqual(report["raw"], frozen)
        self.assertFalse(report["functional"])
        self.assertFalse(report["performance"], report["performance"])
        self.assertEqual(report["metrics"]["worker_cpu_sec_per_gib"], 2 * 1024 ** 3 / 900)

    def test_missing_raw_and_malformed_counts_invalidate_performance_without_erasing_function(self):
        changes = [lambda raw: raw.pop("worker-samples.json"),
                   lambda raw: raw["worker-samples.json"].pop(20),
                   lambda raw: raw["worker-samples.json"][20].update(cpu_sec="1"),
                   lambda raw: raw["worker-samples.json"][20].update(rss_kib=True),
                   lambda raw: raw["worker-samples.json"][20].update(verified_bytes=-1),
                   lambda raw: raw["worker-samples.json"][20].update(verified_bytes="12"),
                   lambda raw: raw["worker-samples.json"][20].pop("compilers")]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                self.result, self.raw = copy.deepcopy(self.original)
                change(self.raw); self.save()
                self.assert_invalid()
                self.raw["client-1.json"]["errors"].append("corrupt payload")
                self.save()
                self.assert_invalid(functional="FAIL")

    def test_all_derived_phase_cpu_bytes_rss_and_windows_are_compared(self):
        for phase in ("baseline", "loaded", "recovery"):
            for field in ("cpu_sec", "verified_bytes", "start_ns", "end_ns", "max_boundary_skew_ns"):
                with self.subTest(phase=phase, field=field):
                    self.result, self.raw = copy.deepcopy(self.original)
                    self.result["worker"][phase]["measurement_window"][field] += 1
                    self.save()
                    report = self.assert_invalid()
                    self.assertIn("differ from raw", str(report["performance_errors"]))
            self.result, self.raw = copy.deepcopy(self.original)
            self.result["worker"][phase]["rss_peak_kib"] += 1
            self.save(); self.assert_invalid()

    def test_raw_reset_or_missing_interior_sample_stays_invalid_after_summary_recompute(self):
        for field, value in (("verified_bytes", None), ("verified_bytes", 0), ("cpu_sec", 0)):
            with self.subTest(field=field, value=value):
                self.result, self.raw = copy.deepcopy(self.original)
                self.raw["worker-samples.json"][25][field] = value
                self.save(recompute=True)
                report = self.assert_invalid()
                self.assertTrue(any("reset" in error or "missing" in error for error in report["performance_errors"]))
                self.assertNotIn("windows differ from raw", str(report["performance_errors"]))

    def test_preparation_compilers_and_tampered_contamination_flag_are_recomputed(self):
        self.raw["worker-samples.json"][0]["compilers"] = [{"pid": 999, "name": "rustc"}]
        self.save()
        report = self.assert_invalid()
        self.assertIn("flag differs", str(report["performance_errors"]))
        self.result["performance_contaminated"] = True; self.save()
        self.assert_invalid()
        self.raw["worker-samples.json"][0]["compilers"] = []
        self.save()
        self.assertIn("flag differs", str(self.assert_invalid()["performance_errors"]))

    def test_late_boundary_and_extended_cpu_byte_read_budget_remain_invalid(self):
        for late_boundary in (True, False):
            self.result, self.raw = copy.deepcopy(self.original)
            row = self.raw["worker-samples.json"][1]
            if late_boundary:
                row["at_ns"] += 6_000_000; row["at"] = row["at_ns"] / 1e9
            else:
                row["boundary_skew_ns"] = 6_000_000
            self.save(recompute=True)
            self.assertIn("precision insufficient", str(self.assert_invalid()["performance_errors"]))
        self.result["effective_config"]["boundary_tolerance_ns"] = 50_000_000
        self.save()
        self.assertIn("boundary tolerance", str(self.assert_invalid()["performance_errors"]))

    def test_internal_sample_timing_remains_bounded_after_summary_recompute(self):
        for offset_ns in (-6_000_000, 6_000_000):
            with self.subTest(offset_ns=offset_ns):
                self.result, self.raw = copy.deepcopy(self.original)
                row = self.raw["worker-samples.json"][25]
                row["at_ns"] += offset_ns
                row["at"] = row["at_ns"] / 1e9
                self.save(recompute=True)
                report = self.assert_invalid()
                self.assertIn("scheduled sampling precision insufficient", str(report["performance_errors"]))
                self.assertNotIn("windows differ from raw", str(report["performance_errors"]))

    @unittest.skipUnless(sys.platform == "linux", "actual worker /proc sampling requires native Linux")
    def test_actual_proc_samples_are_frozen_and_recomputed_without_claiming_test_load(self):
        import load
        repository = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repository))
        try:
            from qa.integration import freeze_l2_raw
        finally:
            sys.path.pop(0)
        phases = {"baseline": .2, "loaded": .2, "recovery": .2}
        start = time.monotonic() + .1
        self.result.update(start_at=start)
        self.result["effective_config"]["phases_sec"] = phases
        progress = Progress(self.root / "progress", 1, create=True)
        rows = []
        try:
            for index, scheduled in enumerate([time.monotonic(), *worker_schedule(start, phases)]):
                time.sleep(max(0, scheduled - time.monotonic()))
                progress.write(0, index * 100)
                row = load.process_stats(os.getpid(), progress)
                row["scheduled_ns"] = round(scheduled * 1e9); rows.append(row)
        finally:
            progress.close()
        self.raw["worker-samples.json"] = rows
        self.result["worker"] = worker_resources(rows, start, phases)
        self.result["performance_contaminated"] = any(row["compilers"] for row in rows)
        before, before_errors = worker_evidence(rows, self.result, self.result["effective_config"])
        self.save(); load_dir = self.materialize()
        freeze_l2_raw(self.output)
        (load_dir / "worker-samples.json").unlink()
        retained_result, retained_raw, _ = load_case(self.rig)
        after, after_errors = worker_evidence(retained_raw["worker-samples.json"], retained_result, retained_result["effective_config"])
        self.assertEqual(before, after)
        self.assertEqual(before_errors, after_errors)
        self.assertEqual(after, self.result["worker"])
        # This measures the test process, not a DUT; observed host precision
        # misses remain errors rather than turning into a looser test budget.
        for phase, value in after.items():
            if value["measurement_window"]["errors"]:
                self.assertTrue(after_errors, phase)


@unittest.skipIf(os.name == "nt", "native POSIX record-lock accounting")
class ProgressDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "progress"
        self.progress = Progress(self.path, 1, create=True)
        self.addCleanup(self.progress.close)
        self.progress.write(0, 29)

    def test_short_lock_contention_retries_within_the_original_deadline(self):
        real = self.progress.fcntl
        now, attempts = [1_000_000_000], []
        def lockf(fd, operation):
            if operation != real.LOCK_UN:
                attempts.append(now[0])
                self.assertEqual(operation, real.LOCK_SH | real.LOCK_NB)
                if len(attempts) < 4:
                    raise BlockingIOError()
            return real.lockf(fd, operation)
        self.progress.fcntl = types.SimpleNamespace(LOCK_SH=real.LOCK_SH, LOCK_NB=real.LOCK_NB, LOCK_UN=real.LOCK_UN, lockf=lockf)
        with patch("load_contract.time.monotonic_ns", side_effect=lambda: now[0]), \
             patch("load_contract.time.sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + round(seconds * 1e9))):
            self.assertEqual(self.progress.read(deadline_ns=now[0] + BOUNDARY_TOLERANCE_NS), 29)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(now[0], 1_000_150_000)

    def test_real_cross_process_lock_timeout_returns_missing_without_fabrication(self):
        code = ("import fcntl,sys; f=open(sys.argv[1],'r+b'); fcntl.lockf(f,fcntl.LOCK_EX); "
                "print('locked',flush=True); sys.stdin.buffer.read(1); fcntl.lockf(f,fcntl.LOCK_UN)")
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(select.select([child.stdout], [], [], 5)[0], "lock child did not become ready")
            self.assertEqual(child.stdout.readline(), b"locked\n")
            begin = time.monotonic_ns()
            self.assertIsNone(self.progress.read(deadline_ns=begin + BOUNDARY_TOLERANCE_NS))
            self.assertGreaterEqual(time.monotonic_ns() - begin, BOUNDARY_TOLERANCE_NS)
            _, stderr = child.communicate(b"x", timeout=5)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(self.progress.read(), 29)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_cpu_observation_cost_is_included_in_the_progress_deadline(self):
        import load
        proc = self.root / "proc"; target = proc / "42"; (target / "task/42").mkdir(parents=True)
        fields = ["0"] * 40; fields[0], fields[11], fields[12] = "S", "12", "3"
        for directory in (target, target / "task/42"):
            (directory / "stat").write_text("42 (test worker) " + " ".join(fields))
            (directory / "comm").write_text("test-worker\n")
        (target / "status").write_text("VmRSS:\t100 kB\nVmHWM:\t120 kB\n")
        (proc / "pressure").mkdir(); (proc / "pressure/cpu").write_text("some avg10=0.00 total=0\n")
        observer = Mock(); observer.read.return_value = None
        with patch("load.Path", side_effect=lambda path: proc / str(path).removeprefix("/proc").lstrip("/")), \
             patch("load.os.sysconf", return_value=100), \
             patch("load.time.monotonic_ns", side_effect=[1_000_000_000, 1_006_000_000]):
            row = load.process_stats(42, observer)
        observer.read.assert_called_once_with(deadline_ns=1_005_000_000)
        self.assertIsNone(row["verified_bytes"])
        self.assertEqual(row["boundary_skew_ns"], 6_000_000)


if __name__ == "__main__":
    unittest.main()
