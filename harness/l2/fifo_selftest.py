#!/usr/bin/env python3
"""Privileged end-to-end check of the FIFO injector on a real Linux kernel.

Builds a private worker/target veth pair, drives sequenced UDP datagrams and a
byte-verified TCP stream from the worker through the real injector, and checks
receiver order, payload, latency and sender backpressure independently of the
ledger oracle. Run only inside qa/isolate-linux.sh, never on a shared host:

  bash qa/isolate-linux.sh python3 harness/l2/fifo_selftest.py --output /abs/new.json

`--mode cancel` signals the injector mid-load and `--mode overflow` forces the
minimum 100-packet limit; both must leave a clean kernel and an oracle FAIL.
"""
import argparse
import hashlib
import json
import os
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import fifo_ledger
from isolation import verify
from queue_control import FifoQueueControl, LEDGER_NAME, validate_config

UDP_PORT, TCP_PORT = 19101, 19102
DATAGRAM = struct.Struct("<QQ")
CHUNK = 65536
SO_RCVBUFFORCE = 33


def pattern(seq, size):
    seed = hashlib.blake2b(seq.to_bytes(8, "little"), digest_size=32).digest()
    return (seed * (size // 32 + 1))[:size]


def stream_bytes(offset, size):
    block, skip = divmod(offset, CHUNK)
    data = b"".join(pattern(block + index, CHUNK) for index in range((skip + size) // CHUNK + 1))
    return data[skip:skip + size]


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, timeout=10, **kwargs).stdout.decode()


def links(namespace):
    rows = json.loads(run(["ip", "netns", "exec", namespace, "ip", "-s", "-s", "-j", "link", "show", "dev", "eth0"]))
    return rows[0]["stats64"]


def udp_counters():
    """This namespace's UDP MIB, to tell receiver-socket loss from path loss."""
    lines = [line.split() for line in Path("/proc/net/snmp").read_text().splitlines() if line.startswith("Udp:")]
    return dict(zip(lines[0][1:], map(int, lines[1][1:])))


def receive(a):
    """Target side: record every datagram and verify the TCP byte stream."""
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, SO_RCVBUFFORCE, 64 << 20)
    udp.bind((a.host, UDP_PORT))
    udp.setblocking(False)
    listener = socket.create_server((a.host, TCP_PORT))
    listener.setblocking(False)
    before = udp_counters()
    print("ready", flush=True)
    rows, tcp_bytes, tcp_errors, connection = [], 0, [], None
    while time.monotonic() < a.until:
        readable, _, _ = select.select([udp, connection or listener], [], [], .05)
        while True:
            try:
                data = udp.recv(4096)
            except BlockingIOError:
                break
            now = time.monotonic_ns()
            seq, sent = DATAGRAM.unpack_from(data)
            rows.append([seq, sent, now, data[DATAGRAM.size:] == pattern(seq, a.size - DATAGRAM.size)])
        if connection is None:
            if listener in readable:
                connection, _ = listener.accept()
                connection.setblocking(False)
        elif connection in readable:
            try:
                data = connection.recv(CHUNK)
            except BlockingIOError:
                data = None
            if data:
                if data != stream_bytes(tcp_bytes, len(data)):
                    tcp_errors.append(tcp_bytes)
                tcp_bytes += len(data)
            elif data == b"":
                connection.close()
                connection = False
    after = udp_counters()
    Path(a.out).write_text(json.dumps({"udp": rows, "tcp_bytes": tcp_bytes, "tcp_errors": tcp_errors[:10],
                                       "udp_mib_delta": {key: after[key] - before[key] for key in after}}))


def send(a):
    """Worker side: absolute UDP schedule plus a paced TCP stream."""
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
    udp.setblocking(False)
    tcp = socket.create_connection((a.host, TCP_PORT), timeout=5)
    tcp.setblocking(False)
    period = 1 / a.rate
    sent = blocked = tcp_bytes = 0
    pending = b""
    while time.monotonic() < a.begin:
        time.sleep(.001)
    while True:
        now = time.monotonic()
        if now >= a.until:
            break
        scheduled = a.begin + sent * period
        if now >= scheduled:
            payload = DATAGRAM.pack(sent, time.monotonic_ns()) + pattern(sent, a.size - DATAGRAM.size)
            try:
                udp.sendto(payload, (a.host, UDP_PORT))
                sent += 1
            except BlockingIOError:
                blocked += 1
                time.sleep(.0002)
            continue
        budget = int((now - a.begin) * a.tcp_rate) - tcp_bytes
        if budget > 0:
            if not pending:
                pending = stream_bytes(tcp_bytes, min(CHUNK, budget))
            try:
                count = tcp.send(pending)
                tcp_bytes += count
                pending = pending[count:]
            except BlockingIOError:
                pass
        time.sleep(min(.0005, max(0, scheduled - time.monotonic())))
    tcp.close()
    Path(a.out).write_text(json.dumps({"sent": sent, "blocked": blocked, "tcp_bytes": tcp_bytes}))


def scenario(a):
    if verify():
        raise SystemExit("fifo self-test requires the verified QA namespace wrapper")
    prefix = "rig-" + secrets.token_hex(3)
    worker, target = prefix + "-worker", prefix + "-target"
    out = Path(a.output).with_suffix("")
    out.mkdir()
    procs = []
    try:
        for name in (worker, target):
            run(["ip", "netns", "add", name])
        run(["ip", "link", "add", "eth0", "netns", worker, "type", "veth", "peer", "name", "eth0", "netns", target])
        for name, address in ((worker, "10.44.0.1/24"), (target, "10.44.0.2/24")):
            run(["ip", "netns", "exec", name, "ip", "addr", "add", address, "dev", "eth0"])
            run(["ip", "netns", "exec", name, "ip", "link", "set", "lo", "up"])
            run(["ip", "netns", "exec", name, "ip", "link", "set", "eth0", "up"])
        config = validate_config({"kind": fifo_ledger.KIND, "node": "worker", "interface": "eth0", "phase": "loaded",
                                  "delay_ms": 500.0, "limit_packets": 100 if a.mode == "overflow" else a.limit})
        t0 = time.monotonic() + a.lead
        phases = (t0, t0 + a.baseline, t0 + a.baseline + a.loaded, t0 + a.baseline + a.loaded + a.recovery)
        common = ["--host", "10.44.0.2", "--rate", str(a.rate), "--size", str(a.size), "--tcp-rate", str(a.tcp_rate)]
        receiver = subprocess.Popen(["ip", "netns", "exec", target, sys.executable, __file__, "receive", *common,
                                     "--until", str(phases[3] + 1.5), "--out", str(out / "receiver.json")],
                                    stdout=subprocess.PIPE)
        procs.append(receiver)
        if receiver.stdout.readline().strip() != b"ready":
            raise SystemExit("receiver did not start")
        link_before = {name: links(name) for name in (worker, target)}
        control = FifoQueueControl(config, worker, *phases, out)
        control.begin()
        sender = subprocess.Popen(["ip", "netns", "exec", worker, sys.executable, __file__, "send", *common,
                                   "--begin", str(t0 - a.lead / 2), "--until", str(phases[3] - .05),
                                   "--out", str(out / "sender.json")])
        procs.append(sender)
        cancelled = False
        lifecycle = {"measurement_end_ns": round(phases[3] * 1e9)}
        if a.mode == "cancel":
            time.sleep(max(0, phases[1] + a.loaded / 2 - time.monotonic()))
            lifecycle["cancel_signal_before_ns"] = time.monotonic_ns()
            os.kill(control.process.pid, signal.SIGTERM)
            lifecycle["cancel_signal_after_ns"] = time.monotonic_ns()
            cancelled = True
        sender.wait(timeout=phases[3] - time.monotonic() + 30)
        lifecycle["sender_reaped_ns"] = time.monotonic_ns()
        report = control.finish(cancelled=cancelled)
        lifecycle["injector_reaped_ns"] = time.monotonic_ns()
        receiver.wait(timeout=60)
        link_after = {name: links(name) for name in (worker, target)}
        (out / "link-counters.json").write_text(json.dumps({"before": link_before, "after": link_after}, indent=2) + "\n")
        (out / "lifecycle.json").write_text(json.dumps(lifecycle, indent=2) + "\n")
        blob = (out / LEDGER_NAME).read_bytes() if (out / LEDGER_NAME).is_file() else None
        rows = fifo_ledger.decode(blob) if blob else []
        oracle = fifo_ledger.validate(report, config, blob)
        received = json.loads((out / "receiver.json").read_text())
        sent = json.loads((out / "sender.json").read_text())
        planned = report.get("planned", {})
        sequence = [row[0] for row in received["udp"]]
        latency = {"active": [], "recovery": [], "baseline": []}
        for seq, sent_ns, recv_ns, _ in received["udp"]:
            if planned.get("start_ns", 0) + 50_000_000 <= sent_ns < planned.get("end_ns", 0):
                latency["active"].append(recv_ns - sent_ns)
            elif sent_ns >= planned.get("recovery_start_ns", 2**63):
                latency["recovery"].append(recv_ns - sent_ns)
            elif sent_ns < planned.get("start_ns", 0):
                latency["baseline"].append(recv_ns - sent_ns)
        drops = {name: {d: link_after[name][d]["dropped"] - link_before[name][d]["dropped"] for d in ("rx", "tx")}
                 for name in link_before}
        summary_extra = {}
        receiver_clean = (not received["udp_mib_delta"].get("RcvbufErrors")
                          and not received["udp_mib_delta"].get("InErrors"))
        checks = {
            "udp_payload_intact": all(row[3] for row in received["udp"]),
            "receiver_socket_no_loss": receiver_clean,
            "no_sender_backpressure": sent["blocked"] == 0,
            "link_zero_drops": not any(value for row in drops.values() for value in row.values()),
            "qdisc_restored": [q.get("kind") for q in report.get("kernel", {}).get("qdisc_restored", [])] == ["noqueue"],
            "cleanup_pass": report.get("cleanup") == "PASS",
        }
        if a.mode != "overflow":
            checks.update({
                "udp_exactly_once_in_order": sequence == list(range(sent["sent"])),
                "tcp_stream_intact": received["tcp_bytes"] == sent["tcp_bytes"] and not received["tcp_errors"],
                "all_frames_delivered": bool(rows) and all(row[2] == fifo_ledger.DELIVERED for row in rows),
                "gso_frames_preserved": any(row[3] for row in rows),
            })
        if a.mode == "full":
            checks.update({
                "oracle_pass": oracle == [],
                "active_delay_applied": bool(latency["active"]) and min(latency["active"]) >= 500_000_000,
                "recovery_transparent": bool(latency["recovery"]) and max(latency["recovery"]) < 500_000_000 // 4,
            })
        elif a.mode == "cancel":
            # A signal requests cancellation; the parent's final message after
            # reaping clients releases the path. No post-removal loss/order
            # exception is permitted while these senders are still alive.
            removed = report.get("actual", {}).get("removed_ns", 0)
            by_seq = {row[0]: row for row in received["udp"]}
            inversions = [row for previous, row in zip(received["udp"], received["udp"][1:]) if row[0] < previous[0]]
            missing = [seq for seq in range(sent["sent"]) if seq not in by_seq]
            checks.update({"cancel_recorded": report.get("cancelled") is True,
                           "oracle_rejects_cancel": any("cancelled" in error for error in oracle),
                           "udp_no_duplicates": len(by_seq) == len(sequence),
                           "redirect_kept_until_clients_reaped_and_measurement_ended":
                               removed >= max(lifecycle["sender_reaped_ns"], lifecycle["measurement_end_ns"])})
            summary_extra = {"udp_inversions": len(inversions), "udp_missing_after_removal": missing}
        else:
            overflowed = [row for row in rows if row[2] == fifo_ledger.OVERFLOW]
            udp_overflowed = sum(1 for row in overflowed if row[10] == 42 and row[5] == 42 + a.size)
            checks.update({"overflow_recorded": bool(overflowed),
                           "oracle_rejects_overflow": any("dropped, failed or left unsent" in e for e in oracle),
                           "udp_loss_equals_ledger_overflow": udp_overflowed > 0
                                                              and len(set(sequence)) == len(sequence)
                                                              and sent["sent"] - len(sequence) == udp_overflowed})
        summary = {"mode": a.mode, "kernel": os.uname().release, "checks": checks, "ok": all(checks.values()),
                   "oracle_errors": oracle, "sender": sent, "frames": len(rows),
                   "gso_frames": sum(1 for row in rows if row[3]), "link_drop_deltas": drops,
                   "udp_received": len(sequence), "udp_mib_delta": received["udp_mib_delta"],
                   "tcp_bytes": received["tcp_bytes"],
                   "latency_ms": {k: {"n": len(v), "min": min(v) / 1e6, "max": max(v) / 1e6} if v else None
                                  for k, v in latency.items()},
                   "resources": report.get("resources"), "summary": report.get("summary"),
                   "actual": report.get("actual"), "planned": planned, "report_errors": report.get("errors"),
                   **summary_extra}
        Path(a.output).write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps({"ok": summary["ok"], "checks": checks, "oracle_errors": oracle}, indent=2))
        return 0 if summary["ok"] else 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for name in (worker, target):
            subprocess.run(["ip", "netns", "del", name], capture_output=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("role", choices=("scenario", "receive", "send"), nargs="?", default="scenario")
    parser.add_argument("--mode", choices=("full", "cancel", "overflow"), default="full")
    parser.add_argument("--output")
    parser.add_argument("--host")
    parser.add_argument("--out")
    parser.add_argument("--begin", type=float)
    parser.add_argument("--until", type=float)
    parser.add_argument("--rate", type=float, default=500)
    parser.add_argument("--size", type=int, default=1032)
    parser.add_argument("--tcp-rate", type=float, default=2_000_000)
    parser.add_argument("--limit", type=int, default=100000)
    parser.add_argument("--lead", type=float, default=10)
    parser.add_argument("--baseline", type=float, default=8)
    parser.add_argument("--loaded", type=float, default=16)
    parser.add_argument("--recovery", type=float, default=8)
    a = parser.parse_args()
    if a.role == "receive":
        return receive(a)
    if a.role == "send":
        return send(a)
    if not a.output or not os.path.isabs(a.output) or os.path.exists(a.output):
        parser.error("--output must be a fresh absolute path")
    return scenario(a)


if __name__ == "__main__":
    sys.exit(main())
