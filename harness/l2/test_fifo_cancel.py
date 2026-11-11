"""A cancellation signal must not tear down a path with live clients."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fifo_injector import CANCEL_OWNER_TIMEOUT_NS, Injector
from test_fifo_ledger import CONFIG, PLANNED


class CancellationOwnershipTests(unittest.TestCase):
    def injector(self, directory):
        injector = Injector({"config": CONFIG, "planned": PLANNED,
                             "report": str(Path(directory) / "report.json"),
                             "ledger": str(Path(directory) / "ledger.gz")})
        injector.tap = 10
        return injector

    def serve(self, injector, steps, messages):
        clock = [PLANNED["start_ns"]]
        calls = []

        class Poll:
            def register(self, *args): pass
            def unregister(self, *args): pass
            def poll(self, timeout):
                calls.append(timeout)
                if not steps:
                    raise AssertionError("injector did not terminate after owner release/deadline")
                now, action, events = steps.pop(0)
                clock[0] = now
                if action:
                    action()
                return events

        with patch("fifo_injector.time.monotonic_ns", side_effect=lambda: clock[0]), \
                patch("fifo_injector.select", SimpleNamespace(epoll=Poll, EPOLLIN=1)), \
                patch("fifo_injector.os.read", side_effect=messages), \
                patch.object(injector, "emit"):
            injector.serve(11)
        return calls

    def test_signal_keeps_path_until_parent_reaps_clients_and_window_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            injector = self.injector(directory)
            before_end = PLANNED["measurement_end_ns"] - 50_000_000
            steps = [(10_000_000_000, injector.request_cancel, []),
                     (before_end, None, [(11, 1)]),
                     (PLANNED["measurement_end_ns"], None, [])]
            calls = self.serve(injector, steps, [b"cancel\n"])
            self.assertEqual(len(calls), 3)
            self.assertTrue(injector.owner_released)
            self.assertFalse(injector.owner_gone)
            self.assertTrue(injector.report["cancelled"])
            self.assertEqual(injector.report["actual"]["owner_released_ns"], before_end)

    def test_signal_has_bounded_cleanup_if_live_owner_never_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            injector = self.injector(directory)
            self.serve(injector, [(10_000_000_000, injector.request_cancel, []),
                                  (10_000_000_000 + CANCEL_OWNER_TIMEOUT_NS, None, [])], [])
            self.assertFalse(injector.owner_released)
            self.assertTrue(any("timed out waiting" in error for error in injector.report["errors"]))

    def test_owner_death_cancels_and_releases_without_waiting_for_window_end(self):
        with tempfile.TemporaryDirectory() as directory:
            injector = self.injector(directory)
            calls = self.serve(injector, [(10_000_000_000, None, [(11, 1)])], [b""])
            self.assertEqual(len(calls), 1)
            self.assertTrue(injector.owner_gone)
            self.assertTrue(injector.report["cancelled"])

    def test_healthy_finish_still_waits_for_full_measurement_window(self):
        with tempfile.TemporaryDirectory() as directory:
            injector = self.injector(directory)
            calls = self.serve(injector, [(10_000_000_000, None, [(11, 1)]),
                                         (PLANNED["measurement_end_ns"], None, [])], [b"finish\n"])
            self.assertEqual(len(calls), 2)
            self.assertFalse(injector.report["cancelled"])

    def test_repeated_signals_do_not_extend_deadline_or_duplicate_error(self):
        with tempfile.TemporaryDirectory() as directory:
            injector = self.injector(directory)
            with patch("fifo_injector.time.monotonic_ns", side_effect=[10, 20]):
                injector.request_cancel()
                injector.request_cancel()
            self.assertEqual(injector.cancel_deadline_ns, 10 + CANCEL_OWNER_TIMEOUT_NS)
            self.assertEqual(injector.report["actual"]["cancel_requested_ns"], 10)
            self.assertEqual(len(injector.report["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
