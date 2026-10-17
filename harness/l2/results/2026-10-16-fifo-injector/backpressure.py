#!/usr/bin/env python3
"""Does a 500 ms egress hold charge the sending socket? (root, private netns)

Compares no impairment, netem, an NFQUEUE holder that verdicts after 500 ms,
and the tap FIFO injector. A UDP sender with a 256 KiB send buffer offers a
fixed 1000 x 1400 B/s schedule for 3 s and counts EAGAIN. Diagnostic only.
"""
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
L2 = HERE.parents[2]
NLMSG = struct.Struct("=IHHII")
NFGEN = struct.Struct("=BBH")


def sh(*args):
    return subprocess.run(args, check=True, capture_output=True, timeout=20).stdout.decode()


def attr(kind, payload):
    data = struct.pack("=HH", 4 + len(payload), kind) + payload
    return data + b"\0" * (-len(data) % 4)


def nfq_message(msg, payload, queue=0, flags=1 | 4):
    body = NFGEN.pack(socket.AF_UNSPEC, 0, socket.htons(queue)) + payload
    return NLMSG.pack(NLMSG.size + len(body), (3 << 8) | msg, flags, 0, 0) + body


def nfqueue_holder(delay):
    """Minimal raw-netlink NFQUEUE client: accept each packet after `delay`."""
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 12)
    sock.setsockopt(socket.SOL_SOCKET, 33, 64 << 20)
    sock.bind((0, 0))
    sock.send(nfq_message(2, attr(1, struct.pack("=BBH", 1, 0, 0))))
    sock.send(nfq_message(2, attr(2, struct.pack("!IB", 0, 1))))
    sock.send(nfq_message(2, attr(3, struct.pack("!I", 100000))))
    print("ready", flush=True)
    held, deadline = [], time.monotonic() + 8
    sock.settimeout(.001)
    while time.monotonic() < deadline:
        try:
            data = sock.recv(1 << 16)
            msg_type = NLMSG.unpack_from(data)[1]
            if msg_type == (3 << 8):
                offset = NLMSG.size + NFGEN.size
                while offset < len(data):
                    length, kind = struct.unpack_from("=HH", data, offset)
                    if kind & 0x7FFF == 1:
                        held.append((time.monotonic() + delay, struct.unpack_from("!I", data, offset + 4)[0]))
                    offset += (length + 3) & ~3
        except (socket.timeout, BlockingIOError):
            pass
        while held and held[0][0] <= time.monotonic():
            _, packet = held.pop(0)
            sock.send(nfq_message(1, attr(2, struct.pack("!II", 1, packet)), flags=1))


def sender(host, until):
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
    udp.setblocking(False)
    begin, sent, blocked, payload = time.monotonic(), 0, 0, b"x" * 1400
    while time.monotonic() < until:
        if time.monotonic() >= begin + sent / 1000:
            try:
                udp.sendto(payload, (host, 9))
                sent += 1
            except BlockingIOError:
                blocked += 1
                time.sleep(.0005)
        else:
            time.sleep(.0002)
    print(json.dumps({"sent": sent, "eagain": blocked, "offered": round((until - begin) * 1000)}))


def variant(name):
    for ns in ("rig-bbbbbb-worker", "rig-bbbbbb-target"):
        subprocess.run(["ip", "netns", "del", ns], capture_output=True)
        sh("ip", "netns", "add", ns)
    w = "rig-bbbbbb-worker"
    sh("ip", "link", "add", "eth0", "netns", w, "type", "veth", "peer", "name", "eth0", "netns", "rig-bbbbbb-target")
    for ns, addr in ((w, "10.45.0.1/24"), ("rig-bbbbbb-target", "10.45.0.2/24")):
        sh("ip", "-n", ns, "addr", "add", addr, "dev", "eth0")
        sh("ip", "-n", ns, "link", "set", "eth0", "up")
    sh("ip", "-n", w, "neigh", "add", "10.45.0.2", "lladdr",
       json.loads(sh("ip", "-n", "rig-bbbbbb-target", "-j", "link", "show", "eth0"))[0]["address"], "dev", "eth0")
    helper, now = None, time.monotonic()
    if name == "netem":
        sh("ip", "netns", "exec", w, "tc", "qdisc", "add", "dev", "eth0", "root", "netem", "limit", "100000", "delay", "500ms")
    elif name == "nfqueue":
        sh("ip", "netns", "exec", w, "nft", "add table inet q; add chain inet q post { type filter hook postrouting priority 0; }; "
           "add rule inet q post oifname \"eth0\" queue num 0")
        helper = subprocess.Popen(["ip", "netns", "exec", w, sys.executable, str(HERE), "nfqueue"], stdout=subprocess.PIPE)
        helper.stdout.readline()
    elif name == "fifo":
        config = {"kind": "fifo-delay-line", "node": "worker", "interface": "eth0", "phase": "loaded",
                  "delay_ms": 500.0, "limit_packets": 100000}
        out = Path("/tmp/vmout/bp-fifo")
        out.mkdir(exist_ok=True)
        for item in out.iterdir():
            item.unlink()
        t = now + 6
        spec = {"config": config, "report": str(out / "report.json"), "ledger": str(out / "ledger.bin.gz"),
                "planned": {"measurement_start_ns": round((t - 1) * 1e9), "start_ns": round(t * 1e9),
                            "end_ns": round((t + 3.2) * 1e9), "recovery_start_ns": round((t + 3.95) * 1e9),
                            "measurement_end_ns": round((t + 4.5) * 1e9)}}
        (out / "spec.json").write_text(json.dumps(spec))
        helper = subprocess.Popen(["ip", "netns", "exec", w, sys.executable, str(L2 / "fifo_injector.py"), "--spec",
                                   str(out / "spec.json")], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        helper.stdout.readline()
        time.sleep(max(0, t - time.monotonic()))
    result = json.loads(sh("ip", "netns", "exec", w, sys.executable, str(HERE), "send", str(time.monotonic() + 3)))
    if helper is not None:
        if name == "fifo":
            helper.stdin.write(b"finish\n")
            helper.stdin.close()
            helper.wait(timeout=60)
            report = json.loads((out / "report.json").read_text())
            result["fifo_max_frames"] = report["resources"]["max_frames"]
            result["fifo_errors"] = report["errors"]
        else:
            helper.kill()
            helper.wait()
    for ns in ("rig-bbbbbb-worker", "rig-bbbbbb-target"):
        subprocess.run(["ip", "netns", "del", ns], capture_output=True)
    return result


if __name__ == "__main__":
    if sys.argv[1:2] == ["nfqueue"]:
        nfqueue_holder(.5)
    elif sys.argv[1:2] == ["send"]:
        sender("10.45.0.2", float(sys.argv[2]))
    else:
        print(json.dumps({"kernel": os.uname().release,
                          **{name: variant(name) for name in ("none", "netem", "nfqueue", "fifo")}}, indent=2))
