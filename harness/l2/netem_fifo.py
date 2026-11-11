#!/usr/bin/env python3
"""Persistent kernel netem FIFO for the queue negative control.

Runs inside the owned worker namespace. One root netem qdisc stays on eth0
from before the baseline until after recovery and is never replaced, reset or
deleted while measured traffic flows; phases only change its delay and rate in
place. Because a rate is always set, netem releases every frame at
max(arrival + delay, previous release) + qdisc_pkt_len / rate, so the kernel
queue itself is the single FIFO and no frame can overtake the backlog when a
phase changes. Packet forwarding never leaves the kernel.

Evidence is captured off the forwarding path: a clsact matchall mirror copies
each egress frame to a private dummy device before the queue and eth0's
outgoing tap sees it after the queue; both are TPACKET_V3 rings stamped by the
kernel, so waiting before the queue cannot hide from the ledger.
"""
import argparse
import ctypes
import gc
import hashlib
import json
import mmap
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

import netem_fifo_ledger as ledger

SOL_PACKET, PACKET_RX_RING, PACKET_STATISTICS, PACKET_VERSION, PACKET_VNET_HDR = 263, 5, 6, 10, 15
TPACKET_V3, SO_ATTACH_FILTER, ETH_P_ALL = 2, 26, 0x0003
RING_BLOCK, RING_BLOCKS, RING_FRAME, RING_RETIRE_MS = 1 << 20, 64, 2048, 10
VNET_HDR = struct.Struct("<BBHHHH")
COPY_LIMIT_BYTES = 512 * 1024 * 1024
LEDGER_DISK_MARGIN = 256 * 1024 * 1024
MEMORY_HEADROOM_BYTES = 512 * 1024 * 1024
CANCEL_OWNER_TIMEOUT_NS = 60_000_000_000
CLOCK_SAMPLE_NS = 100_000_000
QLEN_POLL_NS = 2_000_000
RTM_NEWQDISC, RTM_GETQDISC, NLMSG_ERROR, NLMSG_DONE = 36, 38, 2, 3
NLM_F_REQUEST, NLM_F_ACK, NLM_F_ECHO = 1, 4, 8
TCA_KIND, TCA_OPTIONS, TCA_STATS2, TCA_STATS_QUEUE = 1, 2, 7, 3
TCA_NETEM_RATE, TCA_NETEM_RATE64, TCA_NETEM_LATENCY64 = 6, 8, 10
TC_H_ROOT = 0xFFFFFFFF


def command(args, *, check=True):
    run = subprocess.run(args, capture_output=True, timeout=5)
    if len(run.stdout) + len(run.stderr) > 1 << 20 or (check and run.returncode):
        raise ValueError("bounded netem FIFO command failed: " + " ".join(args[:6]) + ": " + run.stderr.decode(errors="replace")[:200])
    return run.stdout.decode()


def qdiscs(stats=False):
    return json.loads(command(["tc", *(["-s"] if stats else []), "-j", "qdisc", "show", "dev", "eth0"]))


def link_stats(name):
    stats = json.loads(command(["ip", "-s", "-s", "-j", "link", "show", "dev", name]))[0]["stats64"]
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


def clock_sample():
    """The tightest of three (monotonic, realtime, monotonic) readings."""
    best = None
    for _ in range(3):
        sample = [time.monotonic_ns(), time.time_ns(), time.monotonic_ns()]
        if best is None or sample[2] - sample[0] < best[2] - best[0]:
            best = sample
    return best


def _attr(kind, payload):
    size = 4 + len(payload)
    return struct.pack("HH", size, kind) + payload + b"\0" * (-size % 4)


def _attrs(data):
    found, offset = {}, 0
    while offset + 4 <= len(data):
        size, kind = struct.unpack_from("HH", data, offset)
        if size < 4:
            break
        found[kind & 0x3FFF] = data[offset + 4:offset + size]
        offset += (size + 3) & ~3
    return found


class Netlink:
    """Minimal rtnetlink client for in-place netem changes and queue length."""

    def __init__(self, ifindex):
        self.ifindex, self.seq = ifindex, 0
        self.sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
        self.sock.bind((0, 0))
        self.sock.settimeout(1)

    def _request(self, kind, flags, body):
        self.seq += 1
        self.sock.send(struct.pack("IHHII", 16 + len(body), kind, flags | NLM_F_REQUEST, self.seq, 0) + body)
        replies = []
        while True:
            data = self.sock.recv(1 << 16)
            offset = 0
            while offset + 16 <= len(data):
                size, kind, _, seq, _ = struct.unpack_from("IHHII", data, offset)
                payload = data[offset + 16:offset + size]
                offset += (size + 3) & ~3
                if seq != self.seq:
                    continue
                if kind == NLMSG_ERROR:
                    error = struct.unpack_from("i", payload)[0]
                    if error:
                        raise OSError(-error, os.strerror(-error))
                    return replies
                if kind == NLMSG_DONE:
                    return replies
                replies.append((kind, payload))

    def change(self, delay_ns, rate, limit):
        nested = _attr(TCA_NETEM_LATENCY64, struct.pack("q", delay_ns))
        nested += _attr(TCA_NETEM_RATE, struct.pack("IiIi", min(rate, 0xFFFFFFFF), 0, 0, 0))
        if rate >= 1 << 32:
            nested += _attr(TCA_NETEM_RATE64, struct.pack("Q", rate))
        body = struct.pack("BBHiIII", 0, 0, 0, self.ifindex, ledger.HANDLE_ID, TC_H_ROOT, 0)
        body += _attr(TCA_KIND, b"netem\0") + _attr(TCA_OPTIONS, struct.pack("6I", 0, limit, 0, 0, 0, 0) + nested)
        t0 = time.monotonic_ns()
        self._request(RTM_NEWQDISC, NLM_F_ACK, body)
        return t0, time.monotonic_ns()

    def qlen(self):
        # A single-qdisc GET answers only multicast listeners unless echoed.
        body = struct.pack("BBHiIII", 0, 0, 0, self.ifindex, ledger.HANDLE_ID, TC_H_ROOT, 0)
        for kind, payload in self._request(RTM_GETQDISC, NLM_F_ACK | NLM_F_ECHO, body):
            _, _, _, ifindex, handle, _, _ = struct.unpack_from("BBHiIII", payload)
            attrs = _attrs(payload[20:])
            if (kind == RTM_NEWQDISC and ifindex == self.ifindex and handle == ledger.HANDLE_ID
                    and attrs.get(TCA_KIND, b"").rstrip(b"\0") == b"netem" and TCA_STATS2 in attrs):
                queue = _attrs(attrs[TCA_STATS2]).get(TCA_STATS_QUEUE)
                if queue and len(queue) >= 4:
                    return struct.unpack_from("I", queue)[0]
        raise ValueError("owned netem root is missing from its own netlink reply")

    def close(self):
        self.sock.close()


class Ring:
    """TPACKET_V3 capture of one device's outgoing frames with vnet headers."""

    def __init__(self, ifname, guard):
        self.guard = guard
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
        program = [(0x20, 0, 0, 0xFFFFF004), (0x15, 0, 1, socket.PACKET_OUTGOING), (0x06, 0, 0, ledger.SNAPLEN), (0x06, 0, 0, 0)]
        self.program = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *row) for row in program))
        self.sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, struct.pack("HL", len(program), ctypes.addressof(self.program)))
        self.sock.setsockopt(SOL_PACKET, PACKET_VNET_HDR, 1)
        self.sock.setsockopt(SOL_PACKET, PACKET_VERSION, TPACKET_V3)
        self.sock.setsockopt(SOL_PACKET, PACKET_RX_RING, struct.pack(
            "7I", RING_BLOCK, RING_BLOCKS, RING_FRAME, RING_BLOCK * RING_BLOCKS // RING_FRAME, RING_RETIRE_MS, 0, 0))
        self.map = mmap.mmap(self.sock.fileno(), RING_BLOCK * RING_BLOCKS, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        # The protocol hook is registered only by this bind, so no frame of
        # another device can enter the ring first.
        self.sock.bind((ifname, ETH_P_ALL))
        self.next, self.blocks = 0, []
        self.stats()

    def fileno(self):
        return self.sock.fileno()

    def stats(self):
        packets, drops, freezes = struct.unpack("3I", self.sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 12))
        return {"packets": packets, "drops": drops, "freezes": freezes}

    def drain(self):
        while True:
            offset = self.next * RING_BLOCK
            if not struct.unpack_from("I", self.map, offset + 8)[0] & 1:
                return
            used = struct.unpack_from("I", self.map, offset + 20)[0]
            if self.guard(used):
                self.blocks.append(bytes(self.map[offset:offset + min(used, RING_BLOCK)]))
            struct.pack_into("I", self.map, offset + 8, 0)
            self.next = (self.next + 1) % RING_BLOCKS

    def frames(self):
        for block in self.blocks:
            count, offset = struct.unpack_from("II", block, 12)
            for _ in range(count):
                step, sec, nsec, snaplen, length, _, mac = struct.unpack_from("IIIIIIH", block, offset)
                _, gso_type, _, gso_size, _, _ = VNET_HDR.unpack_from(block, offset + mac - VNET_HDR.size)
                yield sec * 1_000_000_000 + nsec, length, gso_type, gso_size, block[offset + mac:offset + mac + snaplen]
                offset += step

    def close(self):
        self.map.close()
        self.sock.close()


class Injector:
    def __init__(self, spec):
        self.config, self.planned = spec["config"], spec["planned"]
        self.report_path, self.ledger_path = Path(spec["report"]), Path(spec["ledger"])
        self.delay_ns = round(self.config["delay_ms"] * 1_000_000)
        self.report = {"schema_version": ledger.SCHEMA_VERSION, "strategy": dict(ledger.STRATEGY),
                       "scope": "test-side shared worker egress queue; persistent kernel netem FIFO, not historical root-cause reproduction",
                       "config": self.config, "clock": "CLOCK_MONOTONIC", "planned": self.planned,
                       "actual": {"changes": []}, "clock_samples": [], "kernel": {},
                       "resources": {"ring_bytes": 2 * RING_BLOCK * RING_BLOCKS, "copied_bytes": 0},
                       "ledger": None, "errors": [], "cancelled": False, "cleanup": "UNKNOWN"}
        self.netlink = self.rings = None
        self.netem = self.dummy = self.clsact = self.action = self.filter = False
        self.next_sample = 0
        self.stop_reason = None
        self.owner_released = self.owner_gone = False
        self.cancel_deadline_ns = None

    def request_cancel(self):
        """Record cancellation without withdrawing a path with live senders."""
        self.stop_reason = "cancel"
        if self.cancel_deadline_ns is None:
            now = time.monotonic_ns()
            self.cancel_deadline_ns = now + CANCEL_OWNER_TIMEOUT_NS
            self.report["actual"]["cancel_requested_ns"] = now
            self.report["cancelled"] = True
            self.report["errors"].append("netem FIFO cancelled before the measurement completed")

    def _guard(self, size):
        resources = self.report["resources"]
        if resources["copied_bytes"] + size > COPY_LIMIT_BYTES:
            if "capture copy guard refused a block" not in self.report["errors"]:
                self.report["errors"].append("capture copy guard refused a block")
            return False
        resources["copied_bytes"] += size
        return True

    def _sample(self, force=False):
        now = time.monotonic_ns()
        if force or now >= self.next_sample:
            self.report["clock_samples"].append(clock_sample())
            self.next_sample = now + CLOCK_SAMPLE_NS

    def _drain(self):
        if self.rings:
            for ring in self.rings:
                ring.drain()

    def install(self):
        self.report["kernel"]["qdisc_before"] = before = qdiscs()
        if [row.get("kind") for row in before] != ["noqueue"] or before[0].get("root") is not True:
            raise ValueError("refuse to attach to a worker eth0 that is not a bare noqueue root")
        available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                             if line.startswith("MemAvailable:"))) * 1024
        if available < self.report["resources"]["ring_bytes"] + COPY_LIMIT_BYTES + MEMORY_HEADROOM_BYTES:
            raise ValueError("refuse to pin capture rings without the frozen memory headroom")
        command(["ip", "link", "add", ledger.DUMMY, "type", "dummy"])
        self.dummy = True
        sysctl = Path(f"/proc/sys/net/ipv6/conf/{ledger.DUMMY}/disable_ipv6")
        if sysctl.exists():
            sysctl.write_text("1\n")
        command(["ip", "link", "set", "dev", ledger.DUMMY, "arp", "off", "up"])
        self.report["kernel"]["dummy_before"] = link_stats(ledger.DUMMY)
        self.netlink = Netlink(socket.if_nametoindex("eth0"))
        self._sample(force=True)
        # Grafting a root or clsact runs dev_deactivate on eth0, which drops
        # egress for an instant; both stay outside the measurement.
        command(["tc", "qdisc", "add", "dev", "eth0", "root", "handle", ledger.HANDLE, "netem",
                 "limit", str(self.config["limit_packets"]), "delay", "0", "rate", f"{ledger.TRANSPARENT_RATE_BPS}bit"])
        self.netem = True
        self.report["actual"]["installed_ns"] = time.monotonic_ns()
        command(["tc", "qdisc", "add", "dev", "eth0", "clsact"])
        self.clsact = True
        command(["tc", "actions", "add", "action", "mirred", "egress", "mirror", "dev", ledger.DUMMY,
                 "index", str(ledger.ACTION_INDEX)])
        self.action = True
        self.rings = (Ring(ledger.DUMMY, self._guard), Ring("eth0", self._guard))
        command(["tc", "filter", "add", "dev", "eth0", "egress", "pref", str(ledger.FILTER_PREF), "protocol", "all",
                 "matchall", "action", "mirred", "index", str(ledger.ACTION_INDEX)])
        self.filter = True
        self.report["actual"]["mirror_installed_ns"] = time.monotonic_ns()
        self._sample(force=True)
        self.report["kernel"]["qdisc_installed"] = qdiscs()

    def change(self, phase, reason=None):
        delay_ns, rate = ledger.policy(ledger.CHANGES.index(phase) + 1, self.delay_ns)
        t0, t1 = self.netlink.change(delay_ns, rate, self.config["limit_packets"])
        row = {"phase": phase, "t0_ns": t0, "t1_ns": t1}
        if reason:
            row["reason"] = reason
        self.report["actual"]["changes"].append(row)
        roots = [q for q in qdiscs() if q.get("root") is True]
        row["shown"] = roots[0].get("options") if len(roots) == 1 else None

    def _done(self, phase):
        return phase in [row["phase"] for row in self.report["actual"]["changes"]]

    def serve(self, control):
        poller = select.epoll()
        for ring in self.rings:
            poller.register(ring.fileno(), select.EPOLLIN)
        poller.register(control, select.EPOLLIN)
        next_poll = 0
        while True:
            now = time.monotonic_ns()
            if self.stop_reason == "cancel" and not self._done("transparent"):
                self.change("transparent", "cancel")
            elif not self._done("active"):
                if now >= self.planned["start_ns"]:
                    self.change("active")
            elif not self._done("drain"):
                if now >= self.planned["end_ns"]:
                    self.change("drain")
                    next_poll = 0
            elif not self._done("transparent") and now >= next_poll:
                deadline = self.planned["recovery_start_ns"] - ledger.TRANSPARENT_DEADLINE_LEAD_NS
                if now >= deadline:
                    self.change("transparent", "deadline")
                elif self.netlink.qlen() == 0:
                    self.change("transparent", "empty")
                next_poll = time.monotonic_ns() + QLEN_POLL_NS
            self._sample()
            self._drain()
            now = time.monotonic_ns()
            if self.cancel_deadline_ns is not None and now >= self.cancel_deadline_ns:
                self.report["errors"].append("netem FIFO cancellation timed out waiting for owner release")
                return
            if (self.owner_released and (self.owner_gone or now >= self.planned["measurement_end_ns"])
                    and (self._done("transparent") or self.stop_reason == "cancel")):
                return
            wake = [now + 10_000_000, self.next_sample]
            if not self._done("active"):
                wake.append(self.planned["start_ns"])
            elif not self._done("drain"):
                wake.append(self.planned["end_ns"])
            elif not self._done("transparent"):
                wake.append(next_poll)
            for fd, _ in poller.poll(max(0, min(wake) - now) / 1e9):
                if fd == control:
                    line = os.read(control, 64)
                    self.owner_released = True
                    self.owner_gone = not line
                    self.report["actual"]["owner_released_ns"] = time.monotonic_ns()
                    if line.strip() != b"finish":
                        self.request_cancel()
                    poller.unregister(control)

    def teardown(self):
        errors = []
        actual, kernel = self.report["actual"], self.report["kernel"]
        if self.rings:
            self._sample(force=True)
        try:
            if self.netem and self.netlink and self.stop_reason == "cancel" and not self._done("transparent"):
                self.change("transparent", "cancel")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(str(error))
        if self.filter:
            try:
                actual["mirror_remove_ns"] = time.monotonic_ns()
                command(["tc", "filter", "del", "dev", "eth0", "egress", "pref", str(ledger.FILTER_PREF)])
                self.filter = False
                actual["mirror_removed_ns"] = time.monotonic_ns()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        if self.netem and self.netlink:
            # Only an empty queue may be removed: deleting a root resets it.
            deadline = time.monotonic_ns() + self.delay_ns + 2_000_000_000
            try:
                while self.netlink.qlen():
                    self._sample()
                    self._drain()
                    if time.monotonic_ns() >= deadline:
                        errors.append("netem FIFO could not drain before teardown")
                        break
                    time.sleep(.005)
            except (OSError, ValueError) as error:
                errors.append(str(error))
            actual["drained_ns"] = time.monotonic_ns()
        if self.rings:
            # Partially filled blocks retire on the ring timeout.
            quiet = time.monotonic_ns() + 5 * RING_RETIRE_MS * 1_000_000
            while time.monotonic_ns() < quiet:
                self._drain()
                time.sleep(.002)
            self._drain()
            self._sample(force=True)
            try:
                kernel["capture"] = {"in": self.rings[0].stats(), "out": self.rings[1].stats()}
            except OSError as error:
                errors.append(str(error))
        for key, read in (("action", action_stats if self.action else None),
                          ("dummy_after", (lambda: link_stats(ledger.DUMMY)) if self.dummy else None),
                          ("qdisc_final", (lambda: qdiscs(stats=True)) if self.netem else None)):
            if read:
                try:
                    kernel[key] = read()
                except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
                    errors.append(str(error))
        if self.netem:
            try:
                command(["tc", "qdisc", "del", "dev", "eth0", "root", "handle", ledger.HANDLE])
                self.netem = False
                actual["removed_ns"] = time.monotonic_ns()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                errors.append(str(error))
        if self.rings:
            for ring in self.rings:
                ring.close()
        if self.netlink:
            self.netlink.close()
        for flag, args in (("action", ["tc", "actions", "del", "action", "mirred", "index", str(ledger.ACTION_INDEX)]),
                           ("clsact", ["tc", "qdisc", "del", "dev", "eth0", "clsact"]),
                           ("dummy", ["ip", "link", "del", "dev", ledger.DUMMY])):
            if getattr(self, flag):
                try:
                    command(args)
                    setattr(self, flag, False)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    errors.append(str(error))
        actual["closed_ns"] = time.monotonic_ns()
        try:
            kernel["qdisc_restored"] = restored = qdiscs()
            if [row.get("kind") for row in restored] != ["noqueue"]:
                errors.append("worker eth0 qdiscs were not restored")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(str(error))
        self.report["errors"].extend(errors)
        self.report["cleanup"] = "FAIL" if (errors or self.filter or self.action or self.clsact
                                            or self.netem or self.dummy) else "PASS"

    def write(self):
        in_rows, out_rows = [], []
        if self.rings:
            for t_in, length, gso_type, gso_size, snap in self.rings[0].frames():
                in_rows.append((t_in, length, ledger.header_length(snap), gso_type, 0, gso_size, 0,
                                ledger.flow_hash(snap), ledger.digest(length, snap)))
            for t_out, length, _, _, snap in self.rings[1].frames():
                out_rows.append((t_out, length, 0, ledger.digest(length, snap)))
        if len(in_rows) > ledger.MAX_FRAMES or len(out_rows) > ledger.MAX_FRAMES:
            self.report["errors"].append("netem FIFO ledger capacity exceeded")
            in_rows, out_rows = in_rows[:ledger.MAX_FRAMES], out_rows[:ledger.MAX_FRAMES]
        blob = ledger.encode(in_rows, out_rows)
        if shutil.disk_usage(self.ledger_path.parent).free < len(blob) + LEDGER_DISK_MARGIN:
            self.report["errors"].append("netem FIFO ledger disk guard refused the write")
        else:
            with self.ledger_path.open("xb") as stream:
                stream.write(blob)
            self.report["ledger"] = {"path": self.ledger_path.name, "sha256": hashlib.sha256(blob).hexdigest(),
                                     "in_frames": len(in_rows), "out_frames": len(out_rows),
                                     "record_format": ledger.RECORD_FORMAT}
        self.report["summary"] = ledger.summary(in_rows, out_rows)
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
        print("ready", injector.report["actual"]["mirror_installed_ns"], flush=True)
        injector.serve(control)
    except (OSError, ValueError, KeyError, IndexError, StopIteration, subprocess.SubprocessError) as error:
        injector.report["errors"].append(str(error))
    finally:
        injector.teardown()
        injector.write()
    return 0 if not injector.report["errors"] and injector.report["cleanup"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
