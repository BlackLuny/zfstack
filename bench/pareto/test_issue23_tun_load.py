"""Loader regressions for burst admission and recorded tail latency."""
import socket
import sys
import threading
import unittest
from unittest.mock import Mock

import issue23_tun_load as load


class UpstreamBurstAdmission(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "requires Linux loopback /8 source routing")
    def test_connection_uses_declared_source_identity(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            client, _ = load.connect(server.getsockname(), 1, "127.0.0.2")
            with client, server.accept()[0] as accepted:
                self.assertEqual(accepted.getpeername()[0], "127.0.0.2")

    def test_supported_burst_connects_before_handlers_are_scheduled(self):
        # Hold off accept/handler scheduling entirely. The listener must still
        # admit one supported wave; socketserver's default stalls at client 7.
        server = load.Upstream(("127.0.0.1", 0), 1)
        clients = []
        try:
            for _ in range(load.MAX_FLOWS):
                client = socket.socket()
                clients.append(client)
                client.settimeout(1)
                client.connect(server.server_address)
        finally:
            for client in clients:
                client.close()
            server.server_close()


class MixedLatency(unittest.TestCase):
    def probe(self, samples):
        probe = load.MixedProbe.__new__(load.MixedProbe)
        probe.stop = threading.Event()
        probe.start = threading.Event()
        probe.timeout = 1
        probe.thread = Mock()
        probe.thread.is_alive.return_value = False
        probe.error = None
        probe.samples = samples
        return probe

    def test_slow_request_started_under_load_is_not_discarded(self):
        probe = self.probe([
            {"begin_monotonic_ns": 10, "end_monotonic_ns": 20, "rtt_us": 10},
            {"begin_monotonic_ns": 90, "end_monotonic_ns": 200, "rtt_us": 110},
        ])
        result = probe.finish(0, 100)
        self.assertEqual(result["latency"]["n"], 2)
        self.assertEqual(result["latency"]["max_us"], 110)

    def test_all_late_completions_still_produce_latency(self):
        probe = self.probe([{"begin_monotonic_ns": 90, "end_monotonic_ns": 200, "rtt_us": 110}])
        result = probe.finish(0, 100)
        self.assertEqual(result["latency"]["n"], 1)
        self.assertEqual(result["latency"]["max_us"], 110)

    def test_requests_started_outside_load_window_are_excluded(self):
        probe = self.probe([
            {"begin_monotonic_ns": 1, "end_monotonic_ns": 20, "rtt_us": 19},
            {"begin_monotonic_ns": 10, "end_monotonic_ns": 20, "rtt_us": 10},
            {"begin_monotonic_ns": 100, "end_monotonic_ns": 200, "rtt_us": 100},
        ])
        result = probe.finish(10, 100)
        self.assertEqual(result["latency"]["n"], 1)
        self.assertEqual(result["latency"]["max_us"], 10)

    def test_worker_failure_remains_a_failure(self):
        probe = self.probe([])
        probe.error = "socket timeout"
        with self.assertRaisesRegex(RuntimeError, "socket timeout"):
            probe.finish(0, 100)


if __name__ == "__main__":
    unittest.main()
