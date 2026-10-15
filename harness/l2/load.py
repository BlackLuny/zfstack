#!/usr/bin/env python3
"""L2 application/load generator. Only the worker runs ZFStack; this is kernel TCP.

Each light request contains its peer and sequence. Heavy streams have distinct
byte patterns. Results include every sample; quantiles use nearest rank and
suppress P99/P99.9 below 100/1000 samples.
"""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time

from load_contract import BOUNDARY_TOLERANCE_NS, Progress, counters, effective_configuration, worker_resources


def pattern(flow):
    return bytes((i + flow) % 256 for i in range(256))


def receive(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size - len(chunks))
        if not part:
            raise EOFError("early EOF")
        chunks.extend(part)
    return bytes(chunks)


def wait_until(deadline):
    time.sleep(max(0, deadline - time.monotonic()))


async def serve(host, port):
    class EchoDatagram(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
        def datagram_received(self, data, addr):
            self.transport.sendto(data, addr)
    udp, _ = await asyncio.get_running_loop().create_datagram_endpoint(EchoDatagram, local_addr=(host, port))
    events = open(os.environ["L2_SERVER_EVENTS"], "w") if os.environ.get("L2_SERVER_EVENTS") else None
    async def connection(reader, writer):
        try:
            header = await reader.readexactly(5)
            flow = struct.unpack("!I", header[1:])[0]
            if header[:1] == b"D":
                block = pattern(flow) * 256
                while True:
                    writer.write(block)
                    await writer.drain()
            elif header[:1] == b"E":
                while True:
                    data = await reader.readexactly(256)
                    received = time.monotonic()
                    writer.write(data)
                    await writer.drain()
                    if events:
                        seq = struct.unpack("!I", data[4:8])[0]
                        events.write(json.dumps({"peer": flow, "seq": seq, "received_at": received, "replied_at": time.monotonic()}) + "\n")
                        if seq % 100 == 0:
                            events.flush()
            else:
                raise ValueError("bad application header")
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            if events:
                events.flush()
    server = await asyncio.start_server(connection, host, port)
    print("ready", flush=True)
    async with server:
        try:
            await server.serve_forever()
        finally:
            udp.close()


def light(a):
    if os.environ.get("L2_LIGHT_MODE", "closed-loop") == "open-loop":
        from open_loop import run_connection
        row = run_connection(a)
        row["peer"] = a.flow
        Path(a.out).write_text(json.dumps(row, indent=2) + "\n")
        return bool(row["errors"]) or row["counters"]["completed"] != row["counters"]["scheduled"]
    samples, errors = [], []
    connect_start = time.monotonic()
    try:
        with socket.create_connection((a.host, a.port), timeout=5) as sock:
            sock.settimeout(a.request_timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(b"E" + struct.pack("!I", a.flow))
            connected_at = time.monotonic()
            if connected_at > a.start:
                raise TimeoutError("client missed common window start")
            wait_until(a.start)
            boundaries = [a.start, a.start + a.baseline,
                          a.start + a.baseline + a.loaded,
                          a.start + a.baseline + a.loaded + a.recovery]
            seq = 0
            next_request = a.start
            while time.monotonic() < boundaries[-1]:
                t = time.monotonic()
                phase = ("baseline" if t < boundaries[1] else
                         "loaded" if t < boundaries[2] else "recovery")
                sample = {"phase": phase, "seq": seq, "scheduled_at": next_request,
                          "started_at": t, "at": t, "status": "unfinished"}
                samples.append(sample)
                data = struct.pack("!II", a.flow, seq) + pattern(a.flow)[:248]
                sock.sendall(data)
                if receive(sock, 256) != data:
                    raise ValueError(f"corrupt response peer={a.flow} seq={seq}")
                end = time.monotonic()
                sample.update(status="completed", ended_at=end, latency_ms=(end - t) * 1000)
                seq += 1
                next_request = max(next_request + 1 / float(os.environ.get("L2_LIGHT_RATE_HZ", "100")), end)
                wait_until(next_request)
    except Exception as e:
        errors.append(str(e))
        if samples and samples[-1]["status"] == "unfinished":
            samples[-1].update(status="timed_out" if isinstance(e, TimeoutError) else "failed", ended_at=time.monotonic())
    Path(a.out).write_text(json.dumps({"peer": a.flow, "samples": samples,
                                      "connect_sec": time.monotonic() - connect_start if errors else connected_at - connect_start,
                                      "errors": errors, "counters": counters(samples), "arrival_mode": "closed-loop"}, indent=2) + "\n")
    return bool(errors)


def heavy(a):
    rows = [None] * a.conns
    progress = Progress(a.progress, a.conns) if getattr(a, "progress", None) else None
    deadline = a.start + a.baseline + a.loaded
    def download(index):
        row = {"flow": 100 + index, "bytes": 0, "errors": []}
        wait_until(a.start + a.baseline)
        begin = time.monotonic()
        try:
            with socket.create_connection((a.host, a.port), timeout=5) as sock:
                row["connect_sec"] = time.monotonic() - begin
                sock.settimeout(3)
                sock.sendall(b"D" + struct.pack("!I", row["flow"]))
                pat = pattern(row["flow"]) * 257
                while time.monotonic() < deadline:
                    data = sock.recv(65536)
                    if not data:
                        raise EOFError("download early EOF")
                    offset = row["bytes"] % 256
                    if data != pat[offset:offset + len(data)]:
                        raise ValueError(f"download corrupt at byte {row['bytes']}")
                    if time.monotonic() <= deadline:
                        row["bytes"] += len(data)
                        if progress: progress.write(index, row["bytes"])
                row["ended_at"] = time.monotonic()
        except Exception as e:
            row["errors"].append(str(e))
        rows[index] = row
    threads = [threading.Thread(target=download, args=(i,)) for i in range(a.conns)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if progress: progress.close()
    Path(a.out).write_text(json.dumps({"peer": 0, "flows": rows,
                                      "window_sec": a.loaded}, indent=2) + "\n")
    return any(row["errors"] or not row["bytes"] for row in rows)


def quantiles(values):
    values = sorted(values)
    def q(p, minimum=1):
        return values[math.ceil(len(values) * p) - 1] if len(values) >= minimum else None
    return {"n": len(values), "p50_ms": q(.5), "p95_ms": q(.95),
            "p99_ms": q(.99, 100), "p999_ms": q(.999, 1000)}


def process_stats(pid, progress=None):
    ticks = os.sysconf("SC_CLK_TCK")
    def cpu(path):
        fields = Path(path).read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / ticks
    boundary_start = time.monotonic_ns()
    cpu_value = cpu(f"/proc/{pid}/stat")
    verified = progress.read(deadline_ns=boundary_start + BOUNDARY_TOLERANCE_NS) if progress else None
    boundary_end = time.monotonic_ns()
    status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())
    threads = {}
    for path in Path(f"/proc/{pid}/task").iterdir():
        try:
            threads[path.name] = {"name": (path / "comm").read_text().strip(),
                                  "cpu_sec": cpu(path / "stat")}
        except FileNotFoundError:
            pass
    # The rig can also host remote-compile jobs. Keep their presence in the
    # raw samples so a valid functional run cannot become false perf evidence.
    compilers = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = path.read_text().strip()
            if name in ("cargo", "rustc", "cc1", "cc1plus", "rust-lld", "ld.lld"):
                compilers.append({"pid": int(path.parent.name), "name": name})
        except (FileNotFoundError, ProcessLookupError):
            pass
    return {"at": boundary_end / 1e9, "at_ns": boundary_end, "cpu_sec": cpu_value,
            "verified_bytes": verified, "boundary_skew_ns": boundary_end - boundary_start,
            "cpu_resolution_ns": math.ceil(1e9 / ticks),
            "compilers": compilers,
            "host_cpu_pressure": Path("/proc/pressure/cpu").read_text().strip(),
            "rss_kib": int(status["VmRSS"].split()[0]),
            "peak_rss_kib": int(status["VmHWM"].split()[0]), "threads": threads}


def run(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic() + 3
    common = ["--host", a.host, "--port", str(a.port), "--start", str(start),
              "--baseline", str(a.baseline), "--loaded", str(a.loaded),
              "--recovery", str(a.recovery), "--request-timeout", str(a.request_timeout)]
    children, logs, endpoint_pids = [], [], {}
    progress_path = out / "verified-progress.bin"
    progress = Progress(progress_path, a.conns, create=True)
    from queue_control import QueueControl, configuration as queue_configuration
    queue_config = queue_configuration()
    queue = QueueControl(queue_config, os.environ.get("L2_QUEUE_WORKER_NS"), start + a.baseline,
                         start + a.baseline + a.loaded, out / "queue-control.json") if queue_config else None
    if queue:
        queue.begin()
    for i, ns in enumerate(a.clients):
        logfile = open(out / f"client-{i}.log", "w")
        logs.append(logfile)
        script = __file__
        if i != 0 and (int(os.environ.get("L2_LIGHT_CONNS", "1")) > 1 or int(os.environ.get("L2_CHURN_REQUESTS", "0")) > 0):
            script = str(Path(__file__).with_name("mixed_load.py"))
        command = ["ip", "netns", "exec", ns, sys.executable, script,
                   "heavy" if i == 0 else "light", *common,
                   "--out", str(out / f"client-{i}.json")]
        command += ["--conns", str(a.conns), "--progress", str(progress_path)] if i == 0 else ["--flow", str(i)]
        children.append(subprocess.Popen(command, stdout=logfile, stderr=subprocess.STDOUT))
        endpoint_pids[f"generator-{'heavy' if i == 0 else 'light'}-{i}"] = children[-1].pid
    udp_enabled = os.environ.get("L2_UDP_PROBES", "0") == "1"
    if udp_enabled:
        for i, ns in enumerate(a.clients):
            logfile = open(out / f"udp-{i}.log", "w")
            logs.append(logfile)
            command = ["ip", "netns", "exec", ns, sys.executable, str(Path(__file__).with_name("udp_probe.py")),
                       *common, "--flow", str(i), "--out", str(out / f"udp-{i}.json")]
            children.append(subprocess.Popen(command, stdout=logfile, stderr=subprocess.STDOUT))
            endpoint_pids[f"generator-udp-{i}"] = children[-1].pid
    from endpoints import EndpointSampler
    for name, key in (("target-business", "L2_TARGET_PID"), ("target-probe", "L2_PROBE_TARGET_PID"),
                      ("relay-forwarder", "L2_FORWARDER_PID"), ("generator-idle", "L2_IDLE_GENERATOR_PID")):
        value = os.environ.get(key, "")
        endpoint_pids[name] = int(value) if value.isdigit() else None
    endpoint_config = {**effective_configuration(a), "mode": os.environ.get("L2_MODE", "load")}
    endpoints = EndpointSampler(endpoint_config, start, endpoint_pids, out / "endpoint-samples.json")
    endpoints_finished, endpoint_failure = False, None
    timeline, completed, queue_report = [], False, None
    try:
        try:
            endpoints.begin()
        except Exception as error:
            endpoint_failure = str(error)
        duration = a.baseline + a.loaded + a.recovery
        boundaries = (start, start + a.baseline, start + a.baseline + a.loaded, start + duration)
        schedule = sorted({time.monotonic(), *boundaries,
                           *(start + index * .5 for index in range(math.ceil(duration / .5)))})
        for scheduled in schedule:
            wait_until(scheduled)
            sample = process_stats(a.worker_pid, progress)
            sample["scheduled_ns"] = round(scheduled * 1e9)
            timeline.append(sample)
        # Preserve zombie /proc CPU until every endpoint has observed the last
        # boundary. Reaping first would erase final CPU without any RSS value.
        try:
            endpoints.finish()
            endpoints_finished = True
        except Exception as error:
            endpoint_failure = str(error)
        for child in children:
            child.wait(timeout=10)
        completed = True
    finally:
        try:
            if not endpoints_finished:
                try:
                    endpoints.finish(cancelled=True)
                except Exception as error:
                    endpoint_failure = str(error)
        finally:
            try:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                    child.wait()
                for logfile in logs:
                    logfile.close()
                progress.close()
            finally:
                try:
                    if queue:
                        queue_report = queue.finish(cancelled=not completed)
                finally:
                    (out / "worker-samples.json").write_text(json.dumps(timeline, indent=2) + "\n")
    errors = [f"test rig: client {i} exited {child.returncode}; inspect retained request outcomes" for i, child in enumerate(children) if child.returncode]
    rows = []
    for i in range(1, len(a.clients)):
        peer = json.loads((out / f"client-{i}.json").read_text())
        errors.extend(peer["errors"])
        phases = {}
        for phase in ("baseline", "loaded", "recovery"):
            values = [s["latency_ms"] for s in peer["samples"] if s["phase"] == phase and s.get("status", "completed") == "completed"]
            phases[phase] = quantiles(values)
            if not values:
                errors.append(f"peer {i} had no {phase} samples")
        rows.append({"peer": i, "placement": "same-port" if i == 1 else "cross-port", "phases": phases})
    downloads = json.loads((out / "client-0.json").read_text())
    udp = [json.loads((out / f"udp-{i}.json").read_text()) for i in range(len(a.clients))] if udp_enabled else []
    errors.extend(error for row in udp for error in row["errors"])
    total = sum(row["bytes"] for row in downloads["flows"])
    errors.extend(error for row in downloads["flows"] for error in row["errors"])
    if not total:
        errors.append("heavy made no progress")
    resources = worker_resources(timeline, start, endpoint_config["phases_sec"])
    effective_config = endpoint_config
    load_counters = {f"client-{i}": json.loads((out / f"client-{i}.json").read_text()).get("counters") for i in range(1, len(a.clients))}
    result = {"layer": "L2", "ok": not errors, "errors": errors,
              "clock": "wall-monotonic", "start_at": start,
              "effective_config": effective_config, "load_counters": load_counters,
              "startup_anomalies": [s for row in udp for s in row.get("samples", [])
                                    if s.get("phase") == "loaded" and s.get("scheduled_at", s.get("at", 0)) < start + a.baseline + 1
                                    and s.get("status", "completed") != "completed"],
              "phases_sec": {"baseline": a.baseline, "loaded": a.loaded, "recovery": a.recovery},
              "request_timeout_sec": a.request_timeout,
              "uplink_delay_ms": float(os.environ.get("L2_UPLINK_DELAY_MS", "0")),
              "light_conns": int(os.environ.get("L2_LIGHT_CONNS", "1")),
              "churn_requests": int(os.environ.get("L2_CHURN_REQUESTS", "0")),
              "udp": [{k: row[k] for k in ("peer", "sent", "received", "bytes", "errors", "phases", "counters", "scheduled", "timed_out", "unfinished", "missed")} for row in udp],
              "performance_contaminated": any(s["compilers"] for s in timeline),
              "endpoint_observer_error": endpoint_failure,
              "heavy_bytes": total, "heavy_mbps": total * 8 / a.loaded / 1e6,
              "light_users": rows, "worker": resources,
              "comparability": "ZFC + real WG + production Tokio adapter. Whole worker CPU includes crypto/UDP/management. Python kernel-TCP endpoints and a shared host constrain throughput; no core speed comparison or numerical regression threshold."}
    if queue_report is not None:
        result["queue_control"] = queue_report
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return bool(errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("serve", "light", "heavy", "run"))
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=19001)
    parser.add_argument("--flow", type=int, default=1)
    parser.add_argument("--conns", type=int, default=4)
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--baseline", type=float, default=8)
    parser.add_argument("--loaded", type=float, default=16)
    parser.add_argument("--recovery", type=float, default=8)
    parser.add_argument("--request-timeout", type=float, default=3)
    parser.add_argument("--out")
    parser.add_argument("--clients", nargs=4)
    parser.add_argument("--worker-pid", type=int)
    parser.add_argument("--progress")
    args = parser.parse_args()
    if min(args.baseline, args.loaded, args.recovery) <= 1 or args.conns < 1 or args.request_timeout <= 0:
        parser.error("phases must exceed 1s and conns must be positive")
    if args.mode == "serve":
        asyncio.run(serve(args.host, args.port))
        return 0
    return {"light": light, "heavy": heavy, "run": run}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
