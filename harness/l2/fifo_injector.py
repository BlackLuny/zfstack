#!/usr/bin/env python3
"""Persistent single-FIFO egress delay line for the queue negative control.

Runs inside the owned worker namespace. One shared tc mirred action redirects
every eth0 egress frame to a private tap; this single thread reads each frame
once, holds it in one FIFO and writes the identical vnet-header+frame bytes to
eth0 through an AF_PACKET socket that bypasses tc. The redirect is installed
before the baseline and removed after recovery, so only release times change.
The transparent switch is decided at frame admission, in this thread, from
the FIFO's own emptiness: no path change, queue reset or reorder can race it.

A tap rather than NFQUEUE: tun_net_xmit orphans the redirected skb, as netem
does for delayed packets. A queued NFQUEUE entry stays charged to the sending
socket and would add send-buffer backpressure the original injection lacked.
"""
import argparse
import collections
import errno
import fcntl
import gc
import hashlib
import json
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import fifo_ledger as ledger

TUNSETIFF, TUNSETOFFLOAD = 0x400454CA, 0x400454D0
IFF_TAP, IFF_NO_PI, IFF_VNET_HDR = 0x0002, 0x1000, 0x4000
TUN_F_CSUM, TUN_F_TSO4, TUN_F_TSO6, TUN_F_TSO_ECN, TUN_F_USO4, TUN_F_USO6 = 1, 2, 4, 8, 0x20, 0x40
SOL_PACKET, PACKET_VNET_HDR, PACKET_QDISC_BYPASS = 263, 15, 20
SO_SNDBUFFORCE = 32
VNET_HDR = struct.Struct("<BBHHHH")
FRAME_BUFFER = 65536 + 64
READ_BATCH = 128
SEND_BUFFER = 64 * 1024 * 1024
LEDGER_DISK_MARGIN = 256 * 1024 * 1024
CANCEL_OWNER_TIMEOUT_NS = 60_000_000_000


def header_length(frame):
    """Ethernet+IP+TCP/UDP header bytes of a vnet-prefixed frame, else 0."""
    offset = VNET_HDR.size + 14
    if len(frame) < offset:
        return 0
    ethertype = int.from_bytes(frame[offset - 2:offset], "big")
    if ethertype == 0x8100 and len(frame) >= offset + 4:
        ethertype = int.from_bytes(frame[offset + 2:offset + 4], "big")
        offset += 4
    if ethertype == 0x0800 and len(frame) >= offset + 20:
        protocol, offset = frame[offset + 9], offset + (frame[offset] & 0x0F) * 4
    elif ethertype == 0x86DD and len(frame) >= offset + 40:
        protocol, offset = frame[offset + 6], offset + 40
    else:
        return 0
    if protocol == 6 and len(frame) >= offset + 20:
        offset += (frame[offset + 12] >> 4) * 4
    elif protocol == 17:
        offset += 8
    else:
        return 0
    return offset - VNET_HDR.size if offset <= len(frame) else 0


def command(args, *, check=True):
    run = subprocess.run(args, capture_output=True, timeout=5)
    if len(run.stdout) + len(run.stderr) > 1 << 20 or (check and run.returncode):
        raise ValueError("bounded FIFO command failed: " + " ".join(args[:6]) + ": " + run.stderr.decode(errors="replace")[:200])
    return run.stdout.decode()


def qdiscs():
    return json.loads(command(["tc", "-j", "qdisc", "show", "dev", "eth0"]))


def tap_stats():
    links = json.loads(command(["ip", "-s", "-s", "-j", "link", "show", "dev", ledger.TAP]))
    stats = links[0]["stats64"]
    return {direction: {key: stats[direction][key] for key in ("packets", "bytes", "dropped", "errors")}
            for direction in ("rx", "tx")}


def action_stats():
    rows = json.loads(command(["tc", "-s", "-j", "actions", "get", "action", "mirred", "index", str(ledger.ACTION_INDEX)]))
    found = [action for row in rows for action in row.get("actions", []) if action.get("index") == ledger.ACTION_INDEX]
    if len(found) != 1 or found[0].get("kind") != "mirred":
        raise ValueError("owned mirred action is missing or ambiguous")
    stats = found[0].get("stats", {})
    return {"packets": stats.get("packets"), "bytes": stats.get("bytes"),
            "drops": stats.get("drops"), "overlimits": stats.get("overlimits")}


class Injector:
    def __init__(self, spec):
        self.config, self.planned = spec["config"], spec["planned"]
        self.report_path, self.ledger_path = Path(spec["report"]), Path(spec["ledger"])
        self.delay_ns = round(self.config["delay_ms"] * 1_000_000)
        self.limit = self.config["limit_packets"]
        self.report = {"schema_version": ledger.SCHEMA_VERSION, "strategy": dict(ledger.STRATEGY),
                       "scope": "test-side shared worker egress queue; persistent userspace FIFO, not historical root-cause reproduction",
                       "config": self.config, "clock": "CLOCK_MONOTONIC", "planned": self.planned,
                       "actual": {}, "kernel": {}, "resources": {"max_frames": 0, "max_bytes": 0},
                       "ledger": None, "errors": [], "cancelled": False, "cleanup": "UNKNOWN"}
        self.fifo = collections.deque()
        self.records = []
        self.seq = self.held_bytes = 0
        self.previous_due = 0
        self.transparent_ns = None
        self.ordered = True
        self.tap = self.sender = None
        self.clsact = self.action = self.filter = False
        self.stop_reason = None
        self.owner_released = self.owner_gone = False
        self.cancel_deadline_ns = None

    def request_cancel(self):
        """Record cancellation without withdrawing a path with live senders.

        The parent owns the load clients. Only its final control message (or
        pipe EOF after owner death) releases that ownership. A signal alone
        cannot attest that the clients stopped sending.
        """
        self.stop_reason = "cancel"
        if self.cancel_deadline_ns is None:
            now = time.monotonic_ns()
            self.cancel_deadline_ns = now + CANCEL_OWNER_TIMEOUT_NS
            self.report["actual"]["cancel_requested_ns"] = now
            self.report["cancelled"] = True
            self.report["errors"].append("FIFO injector cancelled before the measurement completed")

    def install(self):
        self.report["kernel"]["qdisc_before"] = before = qdiscs()
        if [row.get("kind") for row in before] != ["noqueue"] or before[0].get("root") is not True:
            raise ValueError("refuse to attach to a worker eth0 that is not a bare noqueue root")
        mtu = json.loads(command(["ip", "-j", "link", "show", "dev", "eth0"]))[0]["mtu"]
        self.tap = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        fcntl.ioctl(self.tap, TUNSETIFF, struct.pack("16sH", ledger.TAP.encode(), IFF_TAP | IFF_NO_PI | IFF_VNET_HDR))
        offloads = TUN_F_CSUM | TUN_F_TSO4 | TUN_F_TSO6 | TUN_F_TSO_ECN
        try:
            fcntl.ioctl(self.tap, TUNSETOFFLOAD, offloads | TUN_F_USO4 | TUN_F_USO6)
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise
            fcntl.ioctl(self.tap, TUNSETOFFLOAD, offloads)
        sysctl = Path(f"/proc/sys/net/ipv6/conf/{ledger.TAP}/disable_ipv6")
        if sysctl.exists():
            sysctl.write_text("1\n")
        # The default tap root (pfifo_fast) has priority bands that could
        # reorder redirected frames; the FIFO must be the only queue.
        command(["tc", "qdisc", "replace", "dev", ledger.TAP, "root", "noqueue"])
        command(["ip", "link", "set", "dev", ledger.TAP, "mtu", str(mtu), "txqueuelen", str(self.limit), "arp", "off", "up"])
        self.report["kernel"]["tap_qdisc"] = json.loads(command(["tc", "-j", "qdisc", "show", "dev", ledger.TAP]))
        self.sender = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
        self.sender.setsockopt(SOL_PACKET, PACKET_VNET_HDR, 1)
        self.sender.setsockopt(SOL_PACKET, PACKET_QDISC_BYPASS, 1)
        self.sender.setsockopt(socket.SOL_SOCKET, SO_SNDBUFFORCE, SEND_BUFFER)
        self.sender.bind(("eth0", 0))
        self.report["kernel"]["tap_before"] = tap_stats()
        # Grafting clsact runs dev_deactivate on eth0, which silently drops
        # egress for an instant; install and removal stay outside measurement.
        command(["tc", "qdisc", "add", "dev", "eth0", "clsact"])
        self.clsact = True
        command(["tc", "actions", "add", "action", "mirred", "egress", "redirect", "dev", ledger.TAP,
                 "index", str(ledger.ACTION_INDEX)])
        self.action = True
        command(["tc", "filter", "add", "dev", "eth0", "egress", "pref", str(ledger.FILTER_PREF), "protocol", "all",
                 "matchall", "action", "mirred", "index", str(ledger.ACTION_INDEX)])
        self.filter = True
        self.report["actual"]["installed_ns"] = time.monotonic_ns()
        self.report["kernel"]["qdisc_installed"] = qdiscs()

    def phase(self, t_in):
        if self.transparent_ns is None and t_in >= self.planned["end_ns"] and not self.fifo:
            # Admission and emission share this thread: an empty FIFO here
            # means every earlier frame has already been transmitted.
            self.transparent_ns = t_in
            self.report["actual"]["transparent_ns"] = t_in
        if self.transparent_ns is not None:
            return ledger.TRANSPARENT
        if t_in < self.planned["start_ns"]:
            return ledger.PRE
        return ledger.ACTIVE if t_in < self.planned["end_ns"] else ledger.DRAIN

    def admit(self, frame, t_in):
        phase = self.phase(t_in)
        if phase == ledger.ACTIVE:
            due = max(t_in + self.delay_ns, self.previous_due)
        elif phase == ledger.DRAIN:
            due = max(t_in, self.previous_due) + ledger.tx_ns(len(frame) - VNET_HDR.size)
        else:
            due = t_in
        _, gso_type, _, gso_size, _, _ = VNET_HDR.unpack_from(frame)
        digest = int.from_bytes(hashlib.blake2b(frame, digest_size=8).digest(), "little")
        seq, self.seq = self.seq, self.seq + 1
        row = [seq, phase, ledger.UNSENT, gso_type, gso_size, len(frame) - VNET_HDR.size, t_in,
               min(due - t_in, ledger.NOT_SENT - 1), ledger.NOT_SENT, digest, header_length(frame)]
        if len(self.fifo) >= self.limit or self.held_bytes + len(frame) > ledger.MEMORY_LIMIT_BYTES:
            row[2] = ledger.OVERFLOW
            self.ordered = self.ordered and not self.fifo
            self._record(row)
            return
        self.previous_due = due
        self.fifo.append((due, frame, row))
        self.held_bytes += len(frame)
        resources = self.report["resources"]
        resources["max_frames"] = max(resources["max_frames"], len(self.fifo))
        resources["max_bytes"] = max(resources["max_bytes"], self.held_bytes)

    def _record(self, row):
        if len(self.records) >= ledger.MAX_FRAMES:
            if "FIFO ledger capacity exceeded" not in self.report["errors"]:
                self.report["errors"].append("FIFO ledger capacity exceeded")
            return
        self.records.append(tuple(row))

    def emit(self, now, *, flush=False):
        while self.fifo and (flush or self.fifo[0][0] <= now):
            _, frame, row = self.fifo.popleft()
            self.held_bytes -= len(frame)
            try:
                sent = self.sender.send(frame)
                row[2] = ledger.DELIVERED if sent == len(frame) else ledger.SEND_FAILED
            except OSError:
                row[2] = ledger.SEND_FAILED
            row[8] = min(time.monotonic_ns() - row[6], ledger.NOT_SENT - 1)
            self._record(row)
            now = time.monotonic_ns()

    def read_batch(self):
        for _ in range(READ_BATCH):
            try:
                frame = os.read(self.tap, FRAME_BUFFER)
            except BlockingIOError:
                return
            t_in = time.monotonic_ns()
            self.admit(frame, t_in)
            self.emit(t_in)

    def serve(self, control):
        poller = select.epoll()
        poller.register(self.tap, select.EPOLLIN)
        poller.register(control, select.EPOLLIN)
        while True:
            self.emit(time.monotonic_ns())
            # Read the clock after emit: a long flush must not stamp the
            # switch before the last backlog frame actually left.
            now = time.monotonic_ns()
            if self.transparent_ns is None and now >= self.planned["end_ns"] and not self.fifo:
                self.transparent_ns = self.report["actual"]["transparent_ns"] = now
            if self.cancel_deadline_ns is not None and now >= self.cancel_deadline_ns:
                self.report["errors"].append("FIFO cancellation timed out waiting for owner release/drain")
                return
            if (self.owner_released and (self.owner_gone or now >= self.planned["measurement_end_ns"])
                    and not self.fifo):
                return
            timeout = .05
            if self.fifo:
                timeout = max(0, self.fifo[0][0] - now) / 1e9
            for fd, _ in poller.poll(timeout):
                if fd == self.tap:
                    self.read_batch()
                elif fd == control:
                    line = os.read(control, 64)
                    self.owner_released = True
                    self.owner_gone = not line
                    self.report["actual"]["owner_released_ns"] = time.monotonic_ns()
                    if line.strip() != b"finish":
                        self.request_cancel()
                    poller.unregister(control)

    def teardown(self):
        errors = []
        cancelled = self.stop_reason == "cancel"
        if cancelled:
            self.request_cancel()
            if self.tap is not None and self.sender is not None:
                # Release the backlog in order while the redirect still owns
                # the path, so direct frames cannot overtake it after removal.
                deadline = time.monotonic_ns() + 2_000_000_000
                while time.monotonic_ns() < deadline:
                    before = self.seq
                    self.emit(0, flush=True)
                    self.read_batch()
                    self.emit(0, flush=True)
                    if self.seq == before and not self.fifo:
                        break
        if self.filter:
            try:
                command(["tc", "filter", "del", "dev", "eth0", "egress", "pref", str(ledger.FILTER_PREF)])
                self.filter = False
                self.report["actual"]["removed_ns"] = time.monotonic_ns()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        if self.tap is not None and self.sender is not None:
            # Frames already redirected keep their FIFO order after the
            # filter is gone; only an empty tap ring and FIFO may be closed.
            quiet_since = time.monotonic_ns()
            deadline = quiet_since + (ledger.LATENESS_LIMIT_NS if cancelled else self.delay_ns) + 2_000_000_000
            while time.monotonic_ns() < deadline:
                before = self.seq
                self.read_batch()
                self.emit(time.monotonic_ns(), flush=cancelled)
                if self.seq != before or self.fifo:
                    quiet_since = time.monotonic_ns()
                elif time.monotonic_ns() - quiet_since > 50_000_000:
                    break
                time.sleep(.001)
            if self.fifo:
                errors.append("FIFO could not drain before teardown")
                self.emit(0, flush=True)
        if self.action:
            try:
                self.report["kernel"]["action"] = action_stats()
            except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        if self.tap is not None:
            try:
                self.report["kernel"]["tap_after"] = tap_stats()
            except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
                errors.append(str(error))
            os.close(self.tap)
            self.tap = None
        if self.sender is not None:
            self.sender.close()
        for flag, args in (("action", ["tc", "actions", "del", "action", "mirred", "index", str(ledger.ACTION_INDEX)]),
                           ("clsact", ["tc", "qdisc", "del", "dev", "eth0", "clsact"])):
            if getattr(self, flag):
                try:
                    command(args)
                    setattr(self, flag, False)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    errors.append(str(error))
        self.report["actual"]["closed_ns"] = time.monotonic_ns()
        try:
            self.report["kernel"]["qdisc_restored"] = restored = qdiscs()
            if [row.get("kind") for row in restored] != ["noqueue"]:
                errors.append("worker eth0 qdiscs were not restored")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(str(error))
        self.report["errors"].extend(errors)
        self.report["cleanup"] = "FAIL" if errors or self.filter or self.action or self.clsact else "PASS"

    def write(self):
        records = self.records if self.ordered else sorted(self.records)
        blob = ledger.encode(records)
        if shutil.disk_usage(self.ledger_path.parent).free < len(blob) + LEDGER_DISK_MARGIN:
            self.report["errors"].append("FIFO ledger disk guard refused the write")
        else:
            with self.ledger_path.open("xb") as stream:
                stream.write(blob)
            self.report["ledger"] = {"path": self.ledger_path.name, "sha256": hashlib.sha256(blob).hexdigest(),
                                     "frames": len(records), "record_format": ledger.RECORD_FORMAT}
        self.report["summary"] = ledger.summary(records)
        self.report_path.write_text(json.dumps(self.report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    injector = Injector(json.loads(args.spec.read_text()))
    control = sys.stdin.fileno()
    signal.signal(signal.SIGTERM, lambda *_: injector.request_cancel())
    signal.signal(signal.SIGINT, lambda *_: injector.request_cancel())
    gc.disable()
    try:
        injector.install()
        print("ready", injector.report["actual"]["installed_ns"], flush=True)
        injector.serve(control)
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        injector.report["errors"].append(str(error))
    finally:
        injector.teardown()
        injector.write()
    return 0 if not injector.report["errors"] and injector.report["cleanup"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
