"""Real loopback churn, slow-response overlap, and independent supply checks."""
import argparse
import copy
import json
import os
from pathlib import Path
import queue
import socket
import socketserver
import selectors
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from load import receive
from open_loop import frozen_schedule, run_connection, validate_supply

HERE = Path(__file__).resolve().parent


class OpenLoopTests(unittest.TestCase):
    def ready_cli(self, command, env, *, prepare_delay=0, armed_delay=0):
        # This fixture barrier excludes Python/import cold startup from the
        # arrival window. It does not promise a production cold-start deadline.
        # After readiness the parent fixes start exactly once; the real CLI
        # still parses all arguments and runs its original generator/schedule.
        bootstrap = """
import json, runpy, sys, time
from pathlib import Path
script = sys.argv[3]
sys.path.insert(0, str(Path(script).resolve().parent))
import mixed_load, open_loop
time.sleep(float(sys.argv[1]))
print('ready', flush=True)
start = json.loads(sys.stdin.readline())
time.sleep(float(sys.argv[2]))
sys.argv = [script, *sys.argv[4:]]
sys.argv[sys.argv.index('--start') + 1] = str(start)
runpy.run_path(script, run_name='__main__')
"""
        argv = [command[0], "-u", "-c", bootstrap, str(prepare_delay), str(armed_delay), *command[1:]]
        with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as child:
            ready = queue.Queue()
            reader = threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True)
            reader.start()
            try:
                self.assertEqual(ready.get(timeout=5), b"ready\n")
                reader.join(timeout=1)
                start = time.monotonic() + .5
                stdout, stderr = child.communicate((json.dumps(start) + "\n").encode(), timeout=5)
                return start, subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)
                reader.join(timeout=1)

    def controlled(self, *, queue_limit=32, register_failure=False, pause=0, partial=False):
        clock = [.7]
        sockets = []
        class Socket:
            def __init__(self, *args):
                self.header, self.echo, self.closed, self.partial_sent = False, bytearray(), False, False
                sockets.append(self)
            def setblocking(self, value): pass
            def setsockopt(self, *args): pass
            def connect_ex(self, target): return 0
            def getsockopt(self, *args): return 0
            def send(self, data):
                if not self.header:
                    self.header = True
                elif partial:
                    if self.partial_sent: raise BlockingIOError()
                    self.partial_sent = True
                    self.echo.extend(data[:8])
                    return 8
                else:
                    self.echo.extend(data)
                return len(data)
            def recv(self, size):
                if not self.echo: raise BlockingIOError()
                data = bytes(self.echo); self.echo.clear()
                return data
            def close(self): self.closed = True
        class Selector:
            def __init__(self): self.items, self.paused = {}, False
            def register(self, sock, mask, item):
                if register_failure: raise OSError("controlled registration failure")
                self.items[sock] = (mask, item)
            def modify(self, sock, mask, item): self.items[sock] = (mask, item)
            def unregister(self, sock): self.items.pop(sock)
            def select(self, timeout):
                # Two 10 ms arrivals become visible in the same wakeup. Both
                # still have their original 50 ms first-send budget.
                clock[0] = round(clock[0] + .02, 6)
                if pause and clock[0] >= 1.1 and not self.paused:
                    self.paused = True; clock[0] += pause
                result = []
                for sock, (mask, item) in list(self.items.items()):
                    ready = mask & selectors.EVENT_WRITE
                    if sock.echo: ready |= selectors.EVENT_READ
                    if ready: result.append((SimpleNamespace(data=item), ready))
                return result
            def close(self): pass
        a = argparse.Namespace(host="127.0.0.1", port=1, flow=1, start=1,
                               baseline=.1, loaded=.2, recovery=.1, request_timeout=.5)
        with patch.dict(os.environ, {"L2_LIGHT_RATE_HZ": "100", "L2_MAX_GENERATOR_LAG_SECS": ".05"}), \
             patch("open_loop.time.monotonic", side_effect=lambda: clock[0]), \
             patch("open_loop.socket.socket", Socket), patch("open_loop.selectors.DefaultSelector", Selector), \
             patch("open_loop.MAX_PENDING_WRITES", queue_limit):
            result = run_connection(a, churn=8)
        self.assertTrue(all(sock.closed for sock in sockets))
        return result

    def test_coalesced_on_time_arrivals_drain_bounded_fifo_without_false_misses(self):
        result = self.controlled()
        self.assertFalse(result["errors"], result["errors"])
        self.assertEqual(result["counters"]["completed"], 40)
        self.assertEqual(result["counters"]["missed"], 0)
        limited = self.controlled(queue_limit=1)
        self.assertGreater(limited["counters"]["missed"], 0)
        self.assertEqual(limited["counters"]["scheduled"], 40)
        self.assertTrue(any("queue full" in error for error in limited["errors"]))
        paused = self.controlled(pause=.08)
        self.assertGreater(paused["counters"]["missed"], 0)
        self.assertTrue(any("arrival late" in error for error in paused["errors"]))

    def test_selector_registration_failure_keeps_full_schedule_and_closes_sockets(self):
        result = self.controlled(register_failure=True)
        self.assertEqual(result["counters"]["scheduled"], 40)
        self.assertEqual(result["counters"]["failed"], 40)
        self.assertEqual(len(result["connects"]), 5)
        self.assertTrue(all(row["status"] == "failed" for row in result["connects"]))

    def test_partial_frame_timeout_closes_epoch_without_reusing_corrupt_stream(self):
        result = self.controlled(partial=True)
        self.assertEqual(result["counters"]["scheduled"], 40)
        self.assertEqual(result["counters"]["completed"], 0)
        self.assertEqual(result["counters"]["unfinished"], 0)
        self.assertTrue(any("partially transmitted" in error for error in result["errors"]))
        self.assertTrue(all(row["status"] == "timed_out" for row in result["connects"]))

    def test_real_cli_open_loop_keeps_all_churn_arrivals(self):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0)); port = reserve.getsockname()[1]
        server = subprocess.Popen([sys.executable, str(HERE / "load.py"), "serve", "--host", "127.0.0.1", "--port", str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(server.stdout.readline().strip(), "ready")
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "client.json"
                command = [sys.executable, str(HERE / "mixed_load.py"), "light", "--host", "127.0.0.1", "--port", str(port),
                           "--flow", "1", "--start", "0", "--baseline", ".2", "--loaded", ".3",
                           "--recovery", ".2", "--request-timeout", "1", "--out", str(output)]
                # Preparation deliberately exceeds the old launch-to-start
                # allowance. All 140 original arrivals must still execute.
                start, child = self.ready_cli(command,
                                             {**os.environ, "L2_LIGHT_MODE": "open-loop", "L2_LIGHT_CONNS": "2", "L2_CHURN_REQUESTS": "8",
                                              "L2_LIGHT_RATE_HZ": "100", "L2_MAX_GENERATOR_LAG_SECS": ".05"}, prepare_delay=.6)
                raw = json.loads(output.read_text())
                if child.returncode:
                    failure = Path(tempfile.mkdtemp(prefix="zfstack-open-loop-failure-")) / "client.json"
                    failure.write_text(json.dumps({"returncode": child.returncode, "start": start,
                                                   "stderr": child.stderr.decode(errors="replace"), "raw": raw}, indent=2))
                self.assertEqual(child.returncode, 0, (str(failure) if child.returncode else None, raw["errors"], raw["counters"]))
                self.assertEqual(raw["arrival_mode"], "open-loop")
                self.assertEqual(raw["churn_requests"], 8)
                self.assertEqual(raw["counters"]["scheduled"], 140)
                self.assertEqual(raw["counters"]["completed"], 140)
                self.assertEqual(len({sample["seq"] for sample in raw["samples"]}), 140)
                config = {"phases_sec": {"baseline": .2, "loaded": .3, "recovery": .2}, "request_timeout_sec": 1,
                          "light_conns": 2, "churn_requests": 8, "request_rate_hz": 100.0, "max_generator_lag_sec": .05}
                self.assertFalse(validate_supply(raw, config, start), validate_supply(raw, config, start))
                for mutate in (lambda r: r["connections"][0]["samples"].pop(),
                               lambda r: r["connections"][0]["connects"].pop(),
                               lambda r: r["connections"][0]["connects"][0].update(scheduled_at=0),
                               lambda r: r["connections"][0].update(peak_open_epochs=65),
                               lambda r: r["samples"].pop()):
                    bad = copy.deepcopy(raw); mutate(bad)
                    self.assertTrue(validate_supply(bad, config, start))
                for lane in raw["connections"]:
                    self.assertEqual(len(lane["connects"]), 9)
                    self.assertTrue(all(row["status"] == "completed" for row in lane["connects"]))
                    self.assertEqual([sample["epoch"] for sample in lane["samples"]], [index // 8 for index in range(70)])
                    self.assertEqual({sample["phase"] for sample in lane["samples"]}, {"baseline", "loaded", "recovery"})
        finally:
            server.terminate(); server.communicate(timeout=5)

    def test_ready_cli_does_not_rebase_arrivals_after_an_armed_stall(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "client.json"
            # The deliberate stall extends past all arrivals, so no socket is
            # attempted. Every planned request/epoch must remain a supply miss.
            command = [sys.executable, str(HERE / "mixed_load.py"), "light", "--host", "127.0.0.1", "--port", "1",
                       "--flow", "1", "--start", "0", "--baseline", ".2", "--loaded", ".3",
                       "--recovery", ".2", "--request-timeout", "1", "--out", str(output)]
            start, child = self.ready_cli(command,
                                         {**os.environ, "L2_LIGHT_MODE": "open-loop", "L2_LIGHT_CONNS": "2", "L2_CHURN_REQUESTS": "8",
                                          "L2_LIGHT_RATE_HZ": "100", "L2_MAX_GENERATOR_LAG_SECS": ".05"}, armed_delay=1.3)
            raw = json.loads(output.read_text())
            self.assertNotEqual(child.returncode, 0)
            self.assertEqual(raw["counters"]["scheduled"], 140)
            self.assertEqual(raw["counters"]["missed"], 140)
            self.assertEqual(raw["counters"]["completed"], 0)
            self.assertEqual(len({sample["seq"] for sample in raw["samples"]}), 140)
            self.assertTrue(all(sample["started_at"] is None for sample in raw["samples"]))
            self.assertTrue(all(lane["schedule"]["arrivals"][0]["scheduled_at"] == start for lane in raw["connections"]))
            self.assertTrue(all(record["status"] == "missed" for lane in raw["connections"] for record in lane["connects"]))
            config = {"phases_sec": {"baseline": .2, "loaded": .3, "recovery": .2}, "request_timeout_sec": 1,
                      "light_conns": 2, "churn_requests": 8, "request_rate_hz": 100.0, "max_generator_lag_sec": .05}
            self.assertTrue(validate_supply(raw, config, start))

    def test_slow_responses_do_not_postpone_next_connection_epoch(self):
        class Slow(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    receive(self.request, 5)
                    while True:
                        data = receive(self.request, 256)
                        time.sleep(.06)
                        self.request.sendall(data)
                except (EOFError, OSError):
                    pass
        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
        with Server(("127.0.0.1", 0), Slow) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            a = argparse.Namespace(host="127.0.0.1", port=server.server_address[1], flow=1, start=time.monotonic() + .4,
                                   baseline=.1, loaded=.2, recovery=.1, request_timeout=.5)
            with patch.dict(os.environ, {"L2_LIGHT_RATE_HZ": "100", "L2_MAX_GENERATOR_LAG_SECS": ".05"}):
                row = run_connection(a, churn=2)
                a.start = time.monotonic() + .4
                with patch("open_loop.MAX_OPEN_EPOCHS", 1):
                    limited = run_connection(a, churn=2)
            server.shutdown(); thread.join()
        self.assertFalse(row["errors"], row["errors"])
        self.assertEqual(row["counters"]["scheduled"], 40)
        self.assertEqual(row["counters"]["completed"], 40)
        self.assertGreater(row["peak_open_epochs"], 1)
        first, second = row["connects"][:2]
        self.assertLess(second["started_at"], first["closed_at"])
        self.assertAlmostEqual(second["scheduled_at"] - first["scheduled_at"], .02)
        self.assertTrue(all(abs(record["started_at"] - record["scheduled_at"]) < .05 for record in row["connects"]))
        self.assertEqual(limited["peak_open_epochs"], 1)
        self.assertEqual(limited["counters"]["scheduled"], 40)
        self.assertGreater(limited["counters"]["missed"], 0)
        self.assertEqual(limited["counters"]["started"], limited["counters"]["completed"])
        self.assertTrue(all(error.startswith("missed arrival") for error in limited["errors"]), limited["errors"])

    def test_failed_connection_keeps_every_planned_epoch_and_request(self):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0)); port = reserve.getsockname()[1]
        a = argparse.Namespace(host="127.0.0.1", port=port, flow=1, start=time.monotonic() + .3,
                               baseline=.1, loaded=.1, recovery=.1, request_timeout=.2)
        with patch.dict(os.environ, {"L2_LIGHT_RATE_HZ": "40", "L2_MAX_GENERATOR_LAG_SECS": ".05"}):
            row = run_connection(a, churn=2)
        self.assertGreater(row["counters"]["failed"], 0)
        self.assertEqual(len(row["samples"]), len(row["schedule"]["arrivals"]))
        self.assertEqual(len(row["connects"]), len(row["schedule"]["epochs"]))
        self.assertEqual(row["counters"]["completed"], 0)
        self.assertEqual(row["counters"]["unfinished"], 0)

    def test_schedule_is_fixed_bounded_and_not_response_derived(self):
        a = argparse.Namespace(start=10, baseline=1, loaded=1, recovery=1, request_timeout=3)
        one = frozen_schedule(a, rate=100, churn=8)
        self.assertEqual(one, frozen_schedule(a, rate=100, churn=8))
        self.assertEqual(one[0]["epochs"][1]["scheduled_at"], 10.08 - .25)
        with self.assertRaises(ValueError): frozen_schedule(a, rate=1e8, churn=8)


if __name__ == "__main__":
    unittest.main()
