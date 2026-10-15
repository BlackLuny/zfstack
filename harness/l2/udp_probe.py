#!/usr/bin/env python3
"""Open-loop UDP echo retaining every scheduled datagram and onset failure."""
import argparse
import json
import os
from pathlib import Path
import select
import socket
import struct
import time

from load import pattern, quantiles
from load_contract import arrivals, counters


def run(a):
    duration = a.baseline + a.loaded + a.recovery
    rate = float(os.environ.get("L2_UDP_RATE_HZ", "100"))
    max_lag = float(os.environ.get("L2_MAX_GENERATOR_LAG_SECS", ".02"))
    def phase(t):
        return "baseline" if t < a.start + a.baseline else "loaded" if t < a.start + a.baseline + a.loaded else "recovery"
    table = arrivals(a.start, duration, rate)
    samples = [{"phase": phase(t), "seq": i, "scheduled_at": t, "started_at": None, "status": "unfinished"} for i, t in enumerate(table)]
    pending, errors, cursor, sent, received = {}, [], 0, 0, 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((a.host, a.port))
        sock.setblocking(False)
        while cursor < len(samples) or pending:
            now = time.monotonic()
            while cursor < len(samples) and samples[cursor]["scheduled_at"] <= now:
                sample = samples[cursor]
                cursor += 1
                sample["generator_lag_ms"] = (now - sample["scheduled_at"]) * 1000
                if now - sample["scheduled_at"] > max_lag:
                    sample["status"] = "missed"
                    errors.append(f"missed datagram arrival seq={sample['seq']}")
                    continue
                data = struct.pack("!II", a.flow, sample["seq"]) + pattern(a.flow) * 4
                try:
                    sock.send(data)
                except (BlockingIOError, OSError) as exc:
                    sample.update(status="failed", ended_at=now)
                    errors.append(f"send failed seq={sample['seq']}: {exc}")
                    continue
                sample.update(started_at=now, at=now)
                pending[sample["seq"]] = sample
                sent += 1
            ready, _, _ = select.select([sock], [], [], .002)
            if ready:
                try:
                    data = sock.recv(2048)
                    if len(data) != 1032: raise ValueError("short/oversize datagram")
                    flow, index = struct.unpack("!II", data[:8])
                    if flow != a.flow or index >= len(samples) or data != struct.pack("!II", flow, index) + pattern(a.flow) * 4:
                        raise ValueError("unexpected/corrupt datagram")
                    sample, end = samples[index], time.monotonic()
                    if sample["status"] == "timed_out":
                        sample["late_completed_at"] = end
                    elif index not in pending:
                        raise ValueError("duplicate/unscheduled datagram")
                    else:
                        pending.pop(index)
                        sample.update(status="completed", ended_at=end,
                                      latency_ms=(end - sample["started_at"]) * 1000,
                                      arrival_to_completion_ms=(end - sample["scheduled_at"]) * 1000)
                        received += 1
                except (OSError, ValueError) as exc:
                    errors.append(str(exc))
            now = time.monotonic()
            for index, sample in list(pending.items()):
                if now - sample["scheduled_at"] >= a.request_timeout:
                    pending.pop(index)
                    sample.update(status="timed_out", ended_at=now)
                    errors.append(f"datagram timeout seq={index}")
    counts = counters(samples)
    result = {"peer": a.flow, "sent": sent, "received": received, "bytes": received * 1032,
              "errors": errors, "samples": samples, "counters": counts,
              "scheduled": counts["scheduled"], "timed_out": counts["timed_out"],
              "unfinished": counts["unfinished"], "missed": counts["missed"],
              "arrival_mode": "open-loop", "offered_rate_hz": rate,
              "phases": {p: quantiles([s["latency_ms"] for s in samples if s["phase"] == p and s["status"] == "completed"])
                         for p in ("baseline", "loaded", "recovery")}}
    Path(a.out).write_text(json.dumps(result, indent=2) + "\n")
    return bool(errors) or received != len(samples)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=19001)
    p.add_argument("--flow", type=int, required=True)
    p.add_argument("--start", type=float, required=True)
    p.add_argument("--baseline", type=float, required=True)
    p.add_argument("--loaded", type=float, required=True)
    p.add_argument("--recovery", type=float, required=True)
    p.add_argument("--request-timeout", type=float, default=3)
    p.add_argument("--out", required=True)
    raise SystemExit(run(p.parse_args()))
