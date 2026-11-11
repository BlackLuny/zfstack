"""Only run under qa/isolate-linux.sh. All interfaces belong to that namespace."""
import fcntl
import json
import os
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "source/harness/l2"))
from isolation import verify

assert not verify(), verify()
rows = []


def run(argv):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    rows.append({"argv": argv, "rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr})
    if r.returncode:
        raise RuntimeError("capability probe failed: " + " ".join(argv))


fd = packet = None
try:
    run(["ip", "link", "add", "eth0", "type", "veth", "peer", "name", "peer0"])
    run(["ip", "link", "set", "eth0", "up"])
    run(["ip", "link", "set", "peer0", "up"])
    fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
    fcntl.ioctl(fd, 0x400454CA, struct.pack("16sH", b"qa75probe", 0x0002 | 0x1000 | 0x4000))
    run(["ip", "link", "set", "qa75probe", "up"])
    run(["tc", "qdisc", "add", "dev", "eth0", "clsact"])
    run(["tc", "filter", "add", "dev", "eth0", "egress", "pref", "44", "protocol", "all", "matchall",
         "action", "mirred", "egress", "redirect", "dev", "qa75probe"])
    run(["tc", "-s", "-j", "filter", "show", "dev", "eth0", "egress"])
    packet = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0)
    packet.setsockopt(263, 15, 1)
    packet.setsockopt(263, 20, 1)
    packet.bind(("eth0", 0))
    rows.append({"AF_PACKET_VNET_HDR": True, "PACKET_QDISC_BYPASS": True})
    run(["ip", "link", "add", "qa75wg", "type", "wireguard"])
    run(["ip", "link", "add", "qa75br", "type", "bridge"])
    run(["nft", "-j", "list", "ruleset"])
finally:
    if packet is not None:
        packet.close()
    if fd is not None:
        os.close(fd)
    (ROOT / "evidence/capabilities.json").write_text(json.dumps({
        "observed_unix_ns": time.time_ns(), "rows": rows,
        "namespace_ids": {n: os.readlink("/proc/self/ns/"+n) for n in ("net", "mnt", "pid")},
        "isolation_errors": verify()}, indent=2)+"\n")
