"""Send failures remain fatal ledger outcomes and retain bounded raw detail."""
import errno
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import fifo_ledger as L
from fifo_injector import Injector, VNET_HDR
from test_fifo_ledger import CONFIG, PLANNED, fifo_fixture, replace, seal


class SendDiagnosticsTests(unittest.TestCase):
    def injector(self, directory, count=1):
        injector = Injector({"config": CONFIG, "planned": PLANNED,
                             "report": str(Path(directory) / "report.json"),
                             "ledger": str(Path(directory) / "ledger.gz")})
        frame = VNET_HDR.pack(0, 0, 0, 0, 0, 0) + b"\0" * 100
        for index in range(count):
            injector.admit(frame, PLANNED["measurement_start_ns"] + index)
        injector.sender = Mock()
        return injector, frame

    def test_success_keeps_bytes_order_and_completion_time_without_early_release(self):
        with tempfile.TemporaryDirectory() as directory:
            injector, frame = self.injector(directory, 2)
            start = PLANNED["measurement_start_ns"]
            injector.sender.send.return_value = len(frame)
            with patch("fifo_injector.time.monotonic_ns", side_effect=[start, start]) as clock:
                injector.emit(start)
            self.assertEqual(clock.call_count, 2)
            self.assertEqual(injector.sender.send.call_args_list, [call(frame)])
            self.assertEqual(injector.records[0][2], L.DELIVERED)
            self.assertEqual(injector.records[0][8], 0)
            self.assertEqual(injector.fifo[0][2][0], 1)
            with patch("fifo_injector.time.monotonic_ns", side_effect=[start + 1, start + 4]):
                injector.emit(start + 1)
            diagnostics = injector.report["send_diagnostics"]
            self.assertEqual([r[0] for r in injector.records], [0, 1])
            self.assertEqual(diagnostics["attempts"], 2)
            self.assertEqual(diagnostics["failed"], 0)
            self.assertEqual(diagnostics["max_send"], {"seq": 1, "started_ns": start + 1,
                                                      "completed_ns": start + 4, "wall_ns": 3})

    def test_errno_and_short_write_are_distinct_and_never_retried(self):
        for outcome, kind, error_number, returned in [
                (OSError(errno.ENOBUFS, "busy"), "os_error", errno.ENOBUFS, None),
                (BlockingIOError(errno.EAGAIN, "busy"), "os_error", errno.EAGAIN, None),
                (OSError("no errno supplied"), "os_error", None, None),
                (10, "short_write", None, 10)]:
            with self.subTest(kind=kind, errno=error_number), tempfile.TemporaryDirectory() as directory:
                injector, frame = self.injector(directory)
                start = PLANNED["measurement_start_ns"]
                injector.sender.send.side_effect = [outcome]
                with patch("fifo_injector.time.monotonic_ns", side_effect=[start + 5, start + 25]):
                    injector.emit(start)
                injector.sender.send.assert_called_once_with(frame)
                self.assertFalse(injector.fifo)
                self.assertEqual(injector.held_bytes, 0)
                self.assertEqual(injector.records[0][2], L.SEND_FAILED)
                self.assertEqual(injector.records[0][8], 25)
                detail = injector.report["send_diagnostics"]["failures"][0]
                self.assertEqual(detail, {"seq": 0, "phase": L.PRE, "kind": kind,
                    "errno": error_number, "expected_bytes": len(frame), "returned_bytes": returned,
                    "started_ns": start + 5, "completed_ns": start + 25, "wall_ns": 20})

    def test_failure_detail_is_bounded_but_all_failures_stay_in_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            injector, _ = self.injector(directory, 5)
            injector.sender.send.side_effect = OSError(errno.ENOBUFS, "busy")
            start = PLANNED["measurement_start_ns"]
            with patch("fifo_injector.SEND_FAILURE_LIMIT", 2), \
                    patch("fifo_injector.time.monotonic_ns", return_value=start + 100):
                injector.emit(start + 100, flush=True)
            diagnostics = injector.report["send_diagnostics"]
            self.assertEqual(diagnostics["attempts"], 5)
            self.assertEqual(diagnostics["failed"], 5)
            self.assertEqual(len(diagnostics["failures"]), 2)
            self.assertEqual(diagnostics["omitted_failures"], 3)
            self.assertEqual(len(injector.records), 5)
            self.assertTrue(all(r[2] == L.SEND_FAILED for r in injector.records))

    def test_diagnostics_cannot_override_raw_failure_or_lateness(self):
        for field, value, message in [("outcome", L.SEND_FAILED, "dropped, failed or left unsent"),
                                      ("out", L.LATENESS_LIMIT_NS + 1, "lateness bound")]:
            with self.subTest(field=field):
                report, rows, _ = fifo_fixture()
                rows = list(rows)
                replace(rows, 0, **{field: value})
                blob = seal(report, rows)
                report["send_diagnostics"] = {"attempts": len(rows), "failed": 0, "failures": [],
                                              "max_send": {"wall_ns": 0}}
                self.assertTrue(any(message in e for e in L.validate(report, CONFIG, blob)))


if __name__ == "__main__":
    unittest.main()
