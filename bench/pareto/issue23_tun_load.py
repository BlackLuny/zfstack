#!/usr/bin/env python3
"""Real-TUN burst/idle/reburst load against one already-running proxy.

Run inside the same isolated network namespace as the proxy. This script
does not configure networking, create a namespace, or start/stop the proxy.
It owns only a local upstream TCP server and its workload connections.
"""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

HEADER = struct.Struct("<4sc3xQQ")
MAGIC = b"ZF23"
CHUNK = 65536
PATTERN = bytes(range(251)) * ((CHUNK + 250) // 251 + 2)


def pattern(salt, offset, count):
    start = (salt + offset) % 251
    return PATTERN[start:start + count]


def recv_exact(sock, count, eof_ok=False):
    chunks = []
    remaining = count
    while remaining:
        part = sock.recv(remaining)
        if not part:
            if eof_ok and remaining == count:
                return None
            raise RuntimeError("unexpected EOF")
        chunks.append(part)
        remaining -= len(part)
    return b"".join(chunks)


def address(value):
    host, port = value.rsplit(":", 1)
    socket.inet_pton(socket.AF_INET, host)
    return host, int(port)


def connect(target, timeout):
    begin = time.perf_counter_ns()
    sock = socket.create_connection(target, timeout)
    elapsed = (time.perf_counter_ns() - begin) / 1e6
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 0)
    sock.settimeout(timeout)
    return sock, elapsed


class Upstream(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, bind, timeout):
        self.timeout_secs = timeout
        self.condition = threading.Condition()
        self.running = 0
        self.errors = []
        # These are real connected upstream sockets, not a reusable cache.
        # Idle handlers terminate instead of pinning one Python thread per flow.
        self.idle_sockets = {}
        super().__init__(bind, Handler)

    def retain_idle(self, identity, sock):
        with self.condition:
            if identity in self.idle_sockets:
                raise RuntimeError("duplicate idle identity")
            self.idle_sockets[identity] = sock

    def shutdown_request(self, request):
        with self.condition:
            retained = any(sock is request for sock in self.idle_sockets.values())
        if not retained:
            super().shutdown_request(request)

    def idle_socket(self, identity):
        with self.condition:
            return self.idle_sockets[identity]

    def close_idle(self):
        with self.condition:
            sockets = list(self.idle_sockets.values())
            self.idle_sockets.clear()
        failures = []
        for sock in sockets:
            try:
                super().shutdown_request(sock)
            except Exception as error:
                failures.append(str(error))
        if failures:
            raise RuntimeError("idle upstream close failed: " + "; ".join(failures))

    def _request_done(self):
        with self.condition:
            self.running -= 1
            self.condition.notify_all()

    def process_request(self, request, client_address):
        # Count before starting the thread, including pending startup.
        with self.condition:
            self.running += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_done()
            raise

    def process_request_thread(self, request, client_address):
        try:
            # Includes socketserver shutdown_request, or retained-idle transfer.
            super().process_request_thread(request, client_address)
        except Exception as error:
            self.note_error(f"upstream framework close failed: {error}")
            raise
        finally:
            self._request_done()

    def note_error(self, error):
        with self.condition:
            self.errors.append(str(error))

    def wait_running_at_most(self, expected, timeout):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.running > expected:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeError(
                        f"upstream still has {self.running} handlers; expected <= {expected}"
                    )
                self.condition.wait(left)

    def error_snapshot(self):
        with self.condition:
            return list(self.errors)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 0)
            sock.settimeout(self.server.timeout_secs)
            raw = recv_exact(sock, HEADER.size, eof_ok=True)
            if raw is None:
                return
            magic, command, size, salt = HEADER.unpack(raw)
            if magic != MAGIC:
                raise RuntimeError("bad application header")
            if command == b"I":
                if size != 0:
                    raise RuntimeError("idle header must have size zero")
                self.server.retain_idle(salt, sock)
                sock.sendall(b"I")
                # shutdown_request leaves this socket owned by idle_sockets.
                return
            if command == b"E":
                if not 1 <= size <= CHUNK:
                    raise RuntimeError("invalid echo message size")
                # Held-mode idle can exceed the bulk-operation timeout.
                sock.settimeout(None)
                while True:
                    data = recv_exact(sock, size, eof_ok=True)
                    if data is None:
                        return
                    sock.sendall(data)
            elif command == b"D":
                offset = 0
                while offset < size:
                    count = min(CHUNK, size - offset)
                    sock.sendall(pattern(salt, offset, count))
                    offset += count
                sock.shutdown(socket.SHUT_WR)
                while sock.recv(CHUNK):
                    pass
            elif command == b"U":
                offset = 0
                digest = hashlib.sha256()
                while offset < size:
                    count = min(CHUNK, size - offset)
                    data = recv_exact(sock, count)
                    if data != pattern(salt, offset, count):
                        raise RuntimeError(f"upload corruption at offset {offset}")
                    digest.update(data)
                    offset += count
                sock.sendall(struct.pack("<Q", offset) + digest.digest())
                sock.shutdown(socket.SHUT_WR)
                while sock.recv(CHUNK):
                    pass
            else:
                raise RuntimeError("unknown application command")
        except Exception as error:
            self.server.note_error(error)


class IdleConnection:
    def __init__(self, target, timeout, identity):
        self.identity = identity
        self.sock, self.connect_ms = connect(target, timeout)
        self.sock.sendall(HEADER.pack(MAGIC, b"I", 0, identity))
        if recv_exact(self.sock, 1) != b"I":
            raise RuntimeError("idle registration acknowledgement mismatch")

    def verify(self, server, generation):
        # Boundary-only verification, deliberately outside the quiet window.
        token = struct.pack("<QQ", self.identity, generation)
        upstream = server.idle_socket(self.identity)
        self.sock.sendall(token)
        if recv_exact(upstream, len(token)) != token:
            raise RuntimeError("idle upstream payload mismatch")
        upstream.sendall(token)
        if recv_exact(self.sock, len(token)) != token:
            raise RuntimeError("idle downstream payload mismatch")

    def close(self):
        self.sock.close()


class Echo:
    def __init__(self, target, size, timeout):
        self.sock, self.connect_ms = connect(target, timeout)
        self.size = size
        self.sequence = 0
        self.sock.sendall(HEADER.pack(MAGIC, b"E", size, 0))

    def once(self):
        message = pattern(17 + self.sequence, 0, self.size)
        self.sequence += 1
        begin = time.perf_counter_ns()
        self.sock.sendall(message)
        sent = time.perf_counter_ns()
        first = self.sock.recv(self.size)
        first_at = time.perf_counter_ns()
        if not first:
            raise RuntimeError("echo EOF")
        reply = first + recv_exact(self.sock, self.size - len(first))
        finished = time.perf_counter_ns()
        if reply != message:
            raise RuntimeError("echo payload mismatch")
        return {
            "sequence": self.sequence - 1,
            "rtt_us": (finished - begin) / 1000.0,
            "write_call_us": (sent - begin) / 1000.0,
            "first_response_us": (first_at - begin) / 1000.0,
            "bytes": self.size,
        }

    def close(self, drain=False):
        sock = self.sock
        if sock is None:
            return
        self.sock = None
        try:
            if drain:
                # Client sockets keep the existing finite operation timeout.
                sock.shutdown(socket.SHUT_WR)
                if sock.recv(1) != b"":
                    raise RuntimeError("unexpected echo bytes during close drain")
        finally:
            sock.close()


class MixedProbe:
    def __init__(self, target, size, timeout, interval_ms):
        self.ready = threading.Event()
        self.start = threading.Event()
        self.stop = threading.Event()
        self.samples = []
        self.error = None
        self.timeout = timeout

        def worker():
            echo = None
            try:
                echo = Echo(target, size, timeout)
                self.ready.set()
                if not self.start.wait(timeout):
                    raise RuntimeError("mixed RR start barrier timed out")
                while not self.stop.is_set():
                    begin = time.monotonic_ns()
                    sample = echo.once()
                    sample.update(begin_monotonic_ns=begin, end_monotonic_ns=time.monotonic_ns())
                    self.samples.append(sample)
                    self.stop.wait(interval_ms / 1000.0)
            except Exception as error:
                self.error = str(error)
            finally:
                try:
                    if echo is not None:
                        echo.close(drain=self.error is None)
                except Exception as close_error:
                    detail = f"mixed close failed: {close_error}"
                    self.error = (detail if self.error is None
                                  else self.error + "; secondary: " + detail)
                finally:
                    self.ready.set()

        self.thread = threading.Thread(target=worker, name="mixed-rr", daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout) or self.error is not None:
            self.stop.set()
            self.start.set()
            self.thread.join(2 * self.timeout + 1)
            detail = self.error
            if self.thread.is_alive():
                detail = f"{detail}; worker still alive after bounded setup cleanup"
            raise RuntimeError(f"mixed RR setup failed: {detail}")

    def finish(self, begin_ns, end_ns):
        self.stop.set()
        self.start.set()
        # One in-flight RR timeout plus one graceful EOF-drain timeout.
        self.thread.join(2 * self.timeout + 1)
        if self.thread.is_alive():
            raise RuntimeError("mixed RR worker did not finish within its socket timeout")
        if self.error:
            raise RuntimeError(f"mixed RR error: {self.error}")
        measured = [sample for sample in self.samples
                    if begin_ns <= sample["begin_monotonic_ns"] < end_ns]
        if not measured:
            raise RuntimeError("no RR exchange started during the bulk window")
        return {"latency": percentiles([sample["rtt_us"] for sample in measured]),
                "raw_samples": self.samples, "started_during_bulk_samples": len(measured),
                "fully_overlapping_samples": sum(sample["end_monotonic_ns"] <= end_ns for sample in measured),
                "bulk_begin_monotonic_ns": begin_ns, "bulk_end_monotonic_ns": end_ns,
                "note": "exchanges started during the bulk window enter latency, including late completions; one actual PeerId(0) in tunproxy"}


def percentiles(samples):
    values = sorted(samples)
    n = len(values)

    def percentile(p):
        return values[min(n - 1, max(0, math.ceil(n * p) - 1))] if n else None

    return {
        "n": n,
        "p50_us": percentile(0.50),
        "p95_us": percentile(0.95),
        "p99_us": percentile(0.99) if n >= 100 else None,
        "p999_us": percentile(0.999) if n >= 1000 else None,
        "max_us": values[-1] if values else None,
    }


def bulk_flow(target, timeout, direction, flow, count):
    salt = (100 if direction == "down" else 500) + flow
    sock, connect_ms = connect(target, timeout)
    with sock:
        command = b"D" if direction == "down" else b"U"
        begin = time.perf_counter_ns()
        sock.sendall(HEADER.pack(MAGIC, command, count, salt))
        header_sent = time.perf_counter_ns()
        digest = hashlib.sha256()
        offset = 0
        first_response = None
        if direction == "down":
            while offset < count:
                data = sock.recv(min(CHUNK, count - offset))
                if not data:
                    raise RuntimeError(f"download EOF: flow {flow}, offset {offset}")
                if first_response is None:
                    first_response = time.perf_counter_ns()
                if data != pattern(salt, offset, len(data)):
                    raise RuntimeError(f"download corruption: flow {flow}, offset {offset}")
                digest.update(data)
                offset += len(data)
        else:
            while offset < count:
                size = min(CHUNK, count - offset)
                data = pattern(salt, offset, size)
                sock.sendall(data)
                digest.update(data)
                offset += size
            receipt = recv_exact(sock, 40)
            first_response = time.perf_counter_ns()
            received, remote_digest = struct.unpack("<Q32s", receipt)
            if received != count or remote_digest != digest.digest():
                raise RuntimeError(f"upload receipt mismatch: flow {flow}")
        if sock.recv(1) != b"":
            raise RuntimeError(f"extra payload after fixed workload: flow {flow}")
        finished = time.perf_counter_ns()
        return {
            "flow": flow,
            "direction": direction,
            "verified_bytes": offset,
            "sha256": digest.hexdigest(),
            "connect_ms": connect_ms,
            "header_write_call_us": (header_sent - begin) / 1000.0,
            "first_response_us": (
                (first_response - begin) / 1000.0 if first_response is not None else None
            ),
            "flow_wall_sec": (finished - begin) / 1e9,
            "byte_validation": "every received byte compared; upload receipt count and SHA256 verified",
        }


class Observer:
    COUNTERS = ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")

    def __init__(self, pid, device, target):
        self.pid = pid
        self.device = device
        self.hz = os.sysconf("SC_CLK_TCK")
        self.start_ticks = self.proc()["start_ticks"]
        self.ifindex = self.read_device("ifindex", statistics=False)
        own_namespace = os.readlink("/proc/self/ns/net")
        proxy_namespace = os.readlink(f"/proc/{pid}/ns/net")
        if own_namespace != proxy_namespace:
            raise RuntimeError("load generator and proxy are in different network namespaces")
        self.namespace = own_namespace
        with open(f"/proc/{pid}/cmdline", "rb") as source:
            command = source.read().split(b"\0")
        if b"--tun-proxy" not in command:
            raise RuntimeError("declared proxy PID is not a --tun-proxy process")
        result = subprocess.run(
            ["ip", "route", "get", target[0]],
            text=True, capture_output=True, check=True
        )
        self.route = result.stdout.strip()
        route_words = result.stdout.split()
        if "dev" not in route_words or route_words[route_words.index("dev") + 1] != device:
            raise RuntimeError(f"target does not route through {device}: {self.route}")
        digest = hashlib.sha256()
        with open(f"/proc/{pid}/exe", "rb") as binary:
            for chunk in iter(lambda: binary.read(1 << 20), b""):
                digest.update(chunk)
        self.binary_sha256 = digest.hexdigest()

    def read_device(self, name, statistics=True):
        middle = "statistics/" if statistics else ""
        with open(f"/sys/class/net/{self.device}/{middle}{name}") as source:
            return int(source.read().strip())

    def proc(self):
        with open(f"/proc/{self.pid}/stat") as source:
            fields = source.read().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            raise RuntimeError("proxy is a zombie process")
        start = int(fields[19])
        if hasattr(self, "start_ticks") and start != self.start_ticks:
            raise RuntimeError("proxy PID was reused")
        status = {}
        with open(f"/proc/{self.pid}/status") as source:
            for line in source:
                if line.startswith(("VmRSS:", "VmHWM:")):
                    name, value, unit = line.split()
                    if unit == "kB":
                        status[name.rstrip(":")] = int(value) * 1024
        return {
            "pid": self.pid,
            "start_ticks": start,
            "cpu_sec": (int(fields[11]) + int(fields[12])) / self.hz,
            "minor_faults": int(fields[7]),
            "major_faults": int(fields[9]),
            "rss_bytes": status.get("VmRSS"),
            "hwm_bytes": status.get("VmHWM"),
        }

    def snapshot(self):
        if self.read_device("ifindex", statistics=False) != self.ifindex:
            raise RuntimeError("TUN device was replaced")
        return {
            "unix_ns": time.time_ns(),
            "monotonic_ns": time.monotonic_ns(),
            "proxy": self.proc(),
            "tun": {name: self.read_device(name) for name in self.COUNTERS},
            "pool_metrics": None,
            "global_port_peer_reserved": None,
            "timer_wakeups": None,
        }


def changes(before, after):
    for key in ("pid", "start_ticks"):
        if before["proxy"][key] != after["proxy"][key]:
            raise RuntimeError("proxy identity changed")
    deltas = {
        name: after["tun"][name] - before["tun"][name]
        for name in Observer.COUNTERS
    }
    if any(value < 0 for value in deltas.values()):
        raise RuntimeError("TUN counters went backwards")
    return {
        "proxy_cpu_sec": after["proxy"]["cpu_sec"] - before["proxy"]["cpu_sec"],
        "proxy_minor_faults": after["proxy"]["minor_faults"] - before["proxy"]["minor_faults"],
        "proxy_major_faults": after["proxy"]["major_faults"] - before["proxy"]["major_faults"],
        "tun": deltas,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy-pid", type=int, required=True)
    parser.add_argument("--device", default="zfcT")
    parser.add_argument("--target", default="198.18.0.2:5201")
    parser.add_argument("--upstream", default="127.0.0.1:5201")
    parser.add_argument("--mtu", type=int, required=True)
    parser.add_argument("--idle-secs", default="5,30,60,180")
    parser.add_argument("--idle-connections", choices=("closed", "held"), default="closed")
    parser.add_argument("--idle-count", type=int, default=0,
                        help="Additional real idle TCP connections; use closed RR mode for an exact idle total")
    parser.add_argument("--settle-secs", type=float, default=1.0)
    parser.add_argument("--rr-sizes", default="64,512,1400,4096")
    parser.add_argument("--rr-samples", type=int, default=1000)
    parser.add_argument("--rr-interval-ms", type=float, default=0.0)
    parser.add_argument("--low-secs", type=float, default=60.0)
    parser.add_argument("--low-interval-ms", type=float, default=1000.0)
    parser.add_argument("--mixed-rr-interval-ms", type=float,
                        help="Opt in to one simultaneous RR flow during each bulk phase; uses the first rr-size")
    parser.add_argument("--flows", type=int, default=8)
    parser.add_argument("--bulk-bytes-per-flow", type=int, default=8 << 20)
    parser.add_argument("--direction", choices=("down", "up", "both"), default="both")
    parser.add_argument("--timeout-secs", type=float, default=60.0)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--pair", type=int, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    idle_secs = [float(value) for value in args.idle_secs.split(",")]
    rr_sizes = [int(value) for value in args.rr_sizes.split(",")]
    if not idle_secs or any(value < 0 or value > 3600 for value in idle_secs):
        raise RuntimeError("invalid idle duration")
    if not rr_sizes or any(value < 1 or value > CHUNK for value in rr_sizes):
        raise RuntimeError("RR sizes must be in 1..65536")
    if not 1 <= args.flows <= 64 or args.bulk_bytes_per_flow <= 0:
        raise RuntimeError("invalid bulk workload")
    if args.rr_samples < 1 or args.settle_secs < 0 or args.low_secs < 0:
        raise RuntimeError("invalid sample count or duration")
    if args.low_secs and args.low_interval_ms <= 0:
        raise RuntimeError("low-interval-ms must be positive")
    if args.rr_interval_ms < 0 or args.timeout_secs <= 0:
        raise RuntimeError("invalid interval or timeout")

    if not 0 <= args.idle_count <= 1000:
        raise RuntimeError("idle-count must be in 0..1000")
    if args.idle_count and args.idle_connections != "closed":
        raise RuntimeError("idle-count requires --idle-connections closed so the quiet count is exact")
    if args.idle_count:
        import resource
        needed_fds = 2 * args.idle_count + 2 * args.flows + 128
        soft_fds, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft_fds != resource.RLIM_INFINITY and soft_fds < needed_fds:
            raise RuntimeError(f"load generator RLIMIT_NOFILE {soft_fds} < {needed_fds}; adjust this process before running")

    if args.mixed_rr_interval_ms is not None and args.mixed_rr_interval_ms < 1:
        raise RuntimeError("mixed-rr-interval-ms must be >= 1")

    target, upstream_address = address(args.target), address(args.upstream)
    observer = Observer(args.proxy_pid, args.device, target)
    actual_mtu = observer.read_device("mtu", statistics=False)
    if actual_mtu != args.mtu:
        raise RuntimeError(f"actual TUN MTU {actual_mtu} differs from declared {args.mtu}")
    output = open(args.out, "x", buffering=1)

    def emit(row):
        row.update(
            tool="tun_idle_load", schema_version=1,
            variant=args.variant, pair=args.pair
        )
        output.write(json.dumps(row, allow_nan=False) + "\n")

    def emit_preserving(row):
        # A reporting failure must not replace the operation's original error.
        try:
            emit(row)
        except Exception as report_error:
            fallback = dict(row)
            fallback["report_error"] = str(report_error)
            try:
                print(json.dumps(fallback, allow_nan=False), file=sys.stderr)
            except Exception:
                pass

    def report_secondary(stage, error):
        emit_preserving({
            "type": "secondary_failure", "ok": False,
            "stage": stage, "error": str(error),
        })

    server = Upstream(upstream_address, args.timeout_secs)
    server_thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=1.0),
        name="upstream-accept", daemon=True
    )
    server_thread.start()
    echoes = {}
    idle_connections = []
    errors = []
    begin = time.monotonic()
    main_failed = False

    def close_echoes(drain=True):
        pending = list(echoes.values())
        echoes.clear()
        failures = []
        for echo in pending:
            try:
                echo.close(drain=drain)
            except Exception as error:
                failures.append(str(error))
        if failures:
            raise RuntimeError("echo close failed: " + "; ".join(failures))

    def verify_idle(generation, stage):
        before = observer.snapshot()
        for connection in idle_connections:
            connection.verify(server, generation)
        after = observer.snapshot()
        emit({"type": "idle_liveness", "stage": stage, "generation": generation,
              "requested": args.idle_count, "verified": len(idle_connections),
              "changes": changes(before, after),
              "note": "real bidirectional application bytes; outside quiet measurement; these sockets remain live state, not free cache"})

    def phase(number, name):
        before = observer.snapshot()
        emit({"type": "snapshot", "stage": name + "_begin", "phase": number, "sample": before})
        for size in rr_sizes:
            if size not in echoes:
                echoes[size] = Echo(target, size, args.timeout_secs)
            echo = echoes[size]
            samples = []
            for _ in range(args.rr_samples):
                samples.append(echo.once())
                if args.rr_interval_ms:
                    time.sleep(args.rr_interval_ms / 1000.0)
            emit({
                "type": "rr", "phase": number, "stage": name, "size": size,
                "connect_ms": echo.connect_ms,
                "first_request": samples[0],
                "latency": percentiles([sample["rtt_us"] for sample in samples]),
                "raw_samples": samples,
                "note": "connect is separate; first_response is from send start; full RTT verifies the complete reply",
            })
        if args.idle_connections == "closed":
            close_echoes()
        directions = ("down", "up") if args.direction == "both" else (args.direction,)
        for direction in directions:
            mixed = None
            if args.mixed_rr_interval_ms is not None:
                mixed = MixedProbe(target, rr_sizes[0], args.timeout_secs, args.mixed_rr_interval_ms)
            bulk_before = observer.snapshot()
            generator_before = time.process_time()
            bulk_begin = time.monotonic()
            bulk_begin_ns = time.monotonic_ns()
            mixed_report = None
            if mixed is not None:
                mixed.start.set()
            bulk_failed = False
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.flows) as executor:
                    futures = [
                        executor.submit(
                            bulk_flow, target, args.timeout_secs, direction,
                            flow, args.bulk_bytes_per_flow
                        )
                        for flow in range(args.flows)
                    ]
                    reports = [future.result() for future in futures]
            except BaseException:
                bulk_failed = True
                raise
            finally:
                bulk_end_ns = time.monotonic_ns()
                bulk_end = time.monotonic()
                generator_end = time.process_time()
                secondary = []
                try:
                    bulk_after = observer.snapshot()
                except Exception as error:
                    secondary.append(("bulk_after_snapshot", error))
                try:
                    if mixed is not None:
                        mixed_report = mixed.finish(bulk_begin_ns, bulk_end_ns)
                except Exception as error:
                    secondary.append(("mixed_finish", error))
                if secondary:
                    if bulk_failed:
                        for stage, error in secondary:
                            report_secondary(stage, error)
                    else:
                        for stage, error in secondary[1:]:
                            report_secondary(stage, error)
                        raise secondary[0][1]
            wall = bulk_end - bulk_begin
            generator_cpu = generator_end - generator_before
            delta = changes(bulk_before, bulk_after)
            delivered = sum(report["verified_bytes"] for report in reports)
            emit({
                "type": "bulk", "phase": number, "stage": name, "direction": direction,
                "verified_bytes": delivered,
                "goodput_mbps": delivered * 8.0 / 1e6 / wall,
                "window_wall_sec": wall,
                "proxy_cpu_sec": delta["proxy_cpu_sec"],
                "proxy_cpu_sec_per_GB": delta["proxy_cpu_sec"] / (delivered / 1e9),
                "generator_process_cpu_sec": generator_cpu,
                "tun_delta": delta["tun"],
                "per_flow": reports,
                "mixed_rr": mixed_report,
                "before": bulk_before, "after": bulk_after,
                "scope": "real TUN and whole proxy process; Python endpoints can limit throughput",
            })
        server.wait_running_at_most(len(echoes), args.timeout_secs)
        after = observer.snapshot()
        emit({"type": "snapshot", "stage": name + "_end", "phase": number, "sample": after})

    try:
        emit({
            "type": "metadata", "config": vars(args),
            "network_namespace": observer.namespace,
            "target_route": observer.route,
            "proxy_binary_sha256": observer.binary_sha256,
            "generator_affinity": sorted(os.sched_getaffinity(0)),
            "cpu_tick_hz": observer.hz,
            "proxy_identity": observer.proc(),
            "actual_tun_mtu": actual_mtu,
            "same_proxy_required": True,
            "keepalive_note": "zfstack default/client keepalive_idle=25s, interval=25s; held mode retains these defaults",
            "idle_sampling": "only boundaries; no proxy notification or network request during quiet sleep",
            "private_metrics": "pool, budget and timer diagnostics require separately captured proxy instrumentation",
        })
        population_begin = time.monotonic()
        for identity in range(args.idle_count):
            idle_connections.append(IdleConnection(target, args.timeout_secs, identity))
        emit({"type": "idle_population", "requested": args.idle_count,
              "established": len(idle_connections),
              "connect_wall_sec": time.monotonic() - population_begin,
              "connect_ms": [connection.connect_ms for connection in idle_connections],
              "note": "server-side protocol acknowledgement proves upstream connection; no persistent Python handler thread per idle socket"})
        verify_idle(0, "before_initial_burst")
        phase(0, "initial_burst")
        for number, seconds in enumerate(idle_secs, 1):
            time.sleep(args.settle_secs)
            before = observer.snapshot()
            emit({"type": "snapshot", "stage": "quiet_begin", "phase": number, "sample": before})
            generator_before = time.process_time()
            idle_begin = time.monotonic()
            time.sleep(seconds)
            wall = time.monotonic() - idle_begin
            generator_cpu = time.process_time() - generator_before
            after = observer.snapshot()
            delta = changes(before, after)
            silent = all(value == 0 for value in delta["tun"].values())
            if args.idle_connections == "closed" and args.idle_count == 0 and not silent:
                errors.append(f"closed-mode quiet phase {number} had wire traffic")
            emit({
                "type": "quiet", "phase": number,
                "requested_sec": seconds, "actual_sec": wall,
                "connection_mode": args.idle_connections,
                "actual_idle_connections": len(idle_connections) + len(echoes),
                "complete_wire_silence": silent,
                "proxy_cpu_sec": delta["proxy_cpu_sec"],
                "generator_process_cpu_sec": generator_cpu,
                "tun_delta": delta["tun"],
                "before": before, "after": after,
                "note": "held connections can produce default keepalive packets; counters are retained",
            })
            verify_idle(number, "after_quiet_before_reburst")
            phase(number, "reburst")

        close_echoes()
        server.wait_running_at_most(0, args.timeout_secs)
        if args.low_secs:
            echo = Echo(target, rr_sizes[0], args.timeout_secs)
            before = observer.snapshot()
            samples = []
            low_begin = time.monotonic()
            next_request = low_begin
            low_failed = False
            try:
                while time.monotonic() - low_begin < args.low_secs:
                    now = time.monotonic()
                    if next_request > now:
                        time.sleep(next_request - now)
                    if time.monotonic() - low_begin >= args.low_secs:
                        break
                    samples.append(echo.once())
                    next_request += args.low_interval_ms / 1000.0
            except BaseException:
                low_failed = True
                raise
            finally:
                try:
                    echo.close(drain=not low_failed)
                except Exception as close_error:
                    if low_failed:
                        report_secondary("low_traffic_close", close_error)
                    else:
                        raise
            after = observer.snapshot()
            emit({
                "type": "low_traffic", "requested_sec": args.low_secs,
                "actual_sec": time.monotonic() - low_begin,
                "interval_ms": args.low_interval_ms,
                "size": rr_sizes[0], "requests": len(samples),
                "latency": percentiles([sample["rtt_us"] for sample in samples]),
                "raw_samples": samples, "changes": changes(before, after),
                "before": before, "after": after,
            })

        close_echoes()
        server.wait_running_at_most(0, args.timeout_secs)
        verify_idle(len(idle_secs) + 1, "before_final_close")
        for connection in idle_connections:
            connection.close()
        idle_connections.clear()
        server.close_idle()
        errors.extend(server.error_snapshot())
        main_failed = bool(errors)
        emit({
            "type": "summary", "ok": not errors, "errors": errors,
            "wall_sec": time.monotonic() - begin,
            "final_proxy_sample": observer.snapshot(),
            "real_tun_path": True,
            "same_proxy_pid": args.proxy_pid,
            "automatic_trim_proved_by_this_script_alone": False,
            "core_only_cpu": None,
            "instructions": None,
        })
        return 0 if not errors else 1
    except Exception as error:
        main_failed = True
        emit_preserving({"type": "failure", "ok": False, "error": str(error)})
        return 1
    finally:
        pending_exception = sys.exc_info()[0] is not None
        cleanup_errors = []
        actions = [("echoes", lambda: close_echoes(drain=False))]
        actions.extend(
            (f"idle_client_{connection.identity}", connection.close)
            for connection in list(idle_connections)
        )
        actions.extend([
            ("idle_upstream", server.close_idle),
            ("upstream_shutdown", server.shutdown),
            ("upstream_server_close", server.server_close),
        ])
        for stage, action in actions:
            try:
                action()
            except Exception as error:
                cleanup_errors.append((stage, str(error)))
                report_secondary(stage, error)
        try:
            output.close()
        except Exception as error:
            cleanup_errors.append(("output_close", str(error)))
            report_secondary("output_close", error)

        # Never return from finally: preserve primary failures and BaseException.
        # A prior workload summary alone is insufficient; exit status must be zero.
        if cleanup_errors and not main_failed and not pending_exception:
            raise RuntimeError(f"cleanup failed: {cleanup_errors}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"type": "failure", "ok": False, "error": str(error)}))
        sys.exit(1)
