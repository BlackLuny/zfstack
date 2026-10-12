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
        await server.serve_forever()


def light(a):
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
                data = struct.pack("!II", a.flow, seq) + pattern(a.flow)[:248]
                sock.sendall(data)
                if receive(sock, 256) != data:
                    raise ValueError(f"corrupt response peer={a.flow} seq={seq}")
                end = time.monotonic()
                samples.append({"phase": phase, "seq": seq, "at": t,
                                "latency_ms": (end - t) * 1000})
                seq += 1
                next_request = max(next_request + .01, end)
                wait_until(next_request)
    except Exception as e:
        errors.append(str(e))
    Path(a.out).write_text(json.dumps({"peer": a.flow, "samples": samples,
                                      "connect_sec": time.monotonic() - connect_start if errors else connected_at - connect_start,
                                      "errors": errors}, indent=2) + "\n")
    return bool(errors)


def heavy(a):
    rows = [None] * a.conns
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
                row["ended_at"] = time.monotonic()
        except Exception as e:
            row["errors"].append(str(e))
        rows[index] = row
    threads = [threading.Thread(target=download, args=(i,)) for i in range(a.conns)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    Path(a.out).write_text(json.dumps({"peer": 0, "flows": rows,
                                      "window_sec": a.loaded}, indent=2) + "\n")
    return any(row["errors"] or not row["bytes"] for row in rows)


def quantiles(values):
    values = sorted(values)
    def q(p, minimum=1):
        return values[math.ceil(len(values) * p) - 1] if len(values) >= minimum else None
    return {"n": len(values), "p50_ms": q(.5), "p95_ms": q(.95),
            "p99_ms": q(.99, 100), "p999_ms": q(.999, 1000)}


def process_stats(pid):
    ticks = os.sysconf("SC_CLK_TCK")
    def cpu(path):
        fields = Path(path).read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / ticks
    status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())
    threads = {}
    for path in Path(f"/proc/{pid}/task").iterdir():
        try:
            threads[path.name] = {"name": (path / "comm").read_text().strip(),
                                  "cpu_sec": cpu(path / "stat")}
        except FileNotFoundError:
            pass
    return {"at": time.monotonic(), "cpu_sec": cpu(f"/proc/{pid}/stat"),
            "rss_kib": int(status["VmRSS"].split()[0]),
            "peak_rss_kib": int(status["VmHWM"].split()[0]), "threads": threads}


def run(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    start = time.monotonic() + 3
    common = ["--host", a.host, "--port", str(a.port), "--start", str(start),
              "--baseline", str(a.baseline), "--loaded", str(a.loaded),
              "--recovery", str(a.recovery), "--request-timeout", str(a.request_timeout)]
    children, logs = [], []
    for i, ns in enumerate(a.clients):
        logfile = open(out / f"client-{i}.log", "w")
        logs.append(logfile)
        command = ["ip", "netns", "exec", ns, sys.executable, __file__,
                   "heavy" if i == 0 else "light", *common,
                   "--out", str(out / f"client-{i}.json")]
        command += ["--conns", str(a.conns)] if i == 0 else ["--flow", str(i)]
        children.append(subprocess.Popen(command, stdout=logfile, stderr=subprocess.STDOUT))
    timeline = []
    try:
        while time.monotonic() < start + a.baseline + a.loaded + a.recovery:
            timeline.append(process_stats(a.worker_pid))
            time.sleep(.5)
        for child in children:
            child.wait(timeout=10)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait()
        for logfile in logs:
            logfile.close()
        (out / "worker-samples.json").write_text(json.dumps(timeline, indent=2) + "\n")
    errors = [f"client {i} exited {child.returncode}" for i, child in enumerate(children) if child.returncode]
    rows = []
    for i in range(1, len(a.clients)):
        peer = json.loads((out / f"client-{i}.json").read_text())
        errors.extend(peer["errors"])
        phases = {}
        for phase in ("baseline", "loaded", "recovery"):
            values = [s["latency_ms"] for s in peer["samples"] if s["phase"] == phase]
            phases[phase] = quantiles(values)
            if not values:
                errors.append(f"peer {i} had no {phase} samples")
        rows.append({"peer": i, "placement": "same-port" if i == 1 else "cross-port", "phases": phases})
    downloads = json.loads((out / "client-0.json").read_text())
    total = sum(row["bytes"] for row in downloads["flows"])
    errors.extend(error for row in downloads["flows"] for error in row["errors"])
    if not total:
        errors.append("heavy made no progress")
    resources = {}
    for phase, begin, end in (("baseline", start, start + a.baseline),
                              ("loaded", start + a.baseline, start + a.baseline + a.loaded),
                              ("recovery", start + a.baseline + a.loaded, start + a.baseline + a.loaded + a.recovery)):
        ts = [s for s in timeline if begin <= s["at"] < end]
        resources[phase] = {"worker_cpu_sec": ts[-1]["cpu_sec"] - ts[0]["cpu_sec"],
                            "sample_window_sec": ts[-1]["at"] - ts[0]["at"],
                            "rss_peak_kib": max(s["rss_kib"] for s in ts),
                            "rss_end_kib": ts[-1]["rss_kib"]}
    result = {"layer": "L2", "ok": not errors, "errors": errors,
              "clock": "wall-monotonic", "start_at": start,
              "phases_sec": {"baseline": a.baseline, "loaded": a.loaded, "recovery": a.recovery},
              "request_timeout_sec": a.request_timeout,
              "uplink_delay_ms": float(os.environ.get("L2_UPLINK_DELAY_MS", "0")),
              "heavy_bytes": total, "heavy_mbps": total * 8 / a.loaded / 1e6,
              "light_users": rows, "worker": resources,
              "comparability": "ZFC + real WG + production Tokio adapter. Whole worker CPU includes crypto/UDP/management. Python kernel-TCP endpoints and a shared host constrain throughput; no core speed comparison or numerical regression threshold."}
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
    args = parser.parse_args()
    if min(args.baseline, args.loaded, args.recovery) <= 1 or args.conns < 1 or args.request_timeout <= 0:
        parser.error("phases must exceed 1s and conns must be positive")
    if args.mode == "serve":
        asyncio.run(serve(args.host, args.port))
        return 0
    return {"light": light, "heavy": heavy, "run": run}[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
