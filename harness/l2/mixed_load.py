#!/usr/bin/env python3
"""Concurrent small requests and optional connection churn for the L2 rig.

The common load controller still owns phase timing, heavy downloads and CPU
sampling. Every reply is checked; sequence numbers remain unique per WG peer.
Connection latency is recorded separately from request latency.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import struct
import threading
import time

from load import pattern, receive, wait_until
from load_contract import counters


def light(a):
    count = int(os.environ.get("L2_LIGHT_CONNS", "1"))
    churn = int(os.environ.get("L2_CHURN_REQUESTS", "0"))
    if not 1 <= count <= 128 or churn < 0:
        raise ValueError("invalid light count/churn")
    if os.environ.get("L2_LIGHT_MODE", "closed-loop") == "open-loop":
        from open_loop import MAX_ARRIVALS
        if count * (a.baseline + a.loaded + a.recovery) * float(os.environ.get("L2_LIGHT_RATE_HZ", "100")) > MAX_ARRIVALS:
            raise ValueError("combined open-loop schedule exceeds bounded arrival budget")
    finish = a.start + a.baseline + a.loaded + a.recovery
    rows = [None] * count
    def phase(t):
        return "baseline" if t < a.start + a.baseline else "loaded" if t < a.start + a.baseline + a.loaded else "recovery"
    def client(index):
        if os.environ.get("L2_LIGHT_MODE", "closed-loop") == "open-loop":
            from open_loop import run_connection
            rows[index] = run_connection(a, index, churn=churn)
            return
        row = {"connection": index, "samples": [], "connects": [], "errors": []}
        seq = index << 24
        wait_until(a.start)
        try:
            while time.monotonic() < finish:
                begin = time.monotonic()
                with socket.create_connection((a.host, a.port), timeout=a.request_timeout) as sock:
                    connected = time.monotonic()
                    row["connects"].append({"phase": phase(begin), "at": begin, "latency_ms": (connected - begin) * 1000})
                    sock.settimeout(a.request_timeout)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    sock.sendall(b"E" + struct.pack("!I", a.flow))
                    requests = 0
                    while time.monotonic() < finish and (churn == 0 or requests < churn):
                        t = time.monotonic()
                        sample = {"phase": phase(t), "seq": seq, "at": t, "scheduled_at": t, "started_at": t, "status": "unfinished"}
                        row["samples"].append(sample)
                        data = struct.pack("!II", a.flow, seq) + pattern(a.flow)[:248]
                        sock.sendall(data)
                        if receive(sock, 256) != data:
                            raise ValueError(f"corrupt reply connection={index} seq={seq}")
                        end = time.monotonic()
                        sample.update(status="completed", ended_at=end, latency_ms=(end - t) * 1000)
                        seq += 1
                        requests += 1
                        wait_until(max(t + 1 / float(os.environ.get("L2_LIGHT_RATE_HZ", "100")), end))
        except Exception as exc:
            row["errors"].append(str(exc))
            if row["samples"] and row["samples"][-1]["status"] == "unfinished":
                row["samples"][-1].update(status="timed_out" if isinstance(exc, TimeoutError) else "failed", ended_at=time.monotonic())
        row["counters"] = counters(row["samples"])
        rows[index] = row
    threads = [threading.Thread(target=client, args=(i,)) for i in range(count)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    samples = sorted((s for row in rows for s in row["samples"]), key=lambda s: s.get("scheduled_at", s.get("at", 0)))
    errors = [e for row in rows for e in row["errors"]]
    if any(not row["samples"] for row in rows): errors.append("a light connection made no progress")
    Path(a.out).write_text(json.dumps({"peer": a.flow, "samples": samples, "errors": errors, "connections": rows, "light_conns": count, "churn_requests": churn, "counters": counters(samples), "arrival_mode": os.environ.get("L2_LIGHT_MODE", "closed-loop")}, indent=2) + "\n")
    return bool(errors)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["light"])
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=19001)
    p.add_argument("--flow", type=int, required=True)
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--baseline", type=float, required=True)
    p.add_argument("--loaded", type=float, required=True)
    p.add_argument("--recovery", type=float, required=True)
    p.add_argument("--request-timeout", type=float, default=3)
    p.add_argument("--out", required=True)
    raise SystemExit(light(p.parse_args()))
