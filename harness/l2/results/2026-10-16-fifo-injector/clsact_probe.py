#!/usr/bin/env python3
"""Does adding/deleting clsact on a busy noqueue veth silently drop egress?"""
import json, socket, subprocess, sys, time, struct, os

def run(*a):
    return subprocess.run(a, check=True, capture_output=True, timeout=20).stdout.decode()

def setup():
    for n in ("pa", "pb"):
        subprocess.run(["ip", "netns", "del", n], capture_output=True)
        run("ip", "netns", "add", n)
    run("ip", "link", "add", "eth0", "netns", "pa", "type", "veth", "peer", "name", "eth0", "netns", "pb")
    for n, a in (("pa", "10.9.0.1/24"), ("pb", "10.9.0.2/24")):
        run("ip", "netns", "exec", n, "ip", "addr", "add", a, "dev", "eth0")
        run("ip", "netns", "exec", n, "ip", "link", "set", "eth0", "up")
    run("ip", "netns", "exec", "pa", "ping", "-c", "1", "-W", "5", "10.9.0.2")

def trial(cycles):
    recv = subprocess.Popen(["ip", "netns", "exec", "pb", sys.executable, __file__, "recv"], stdout=subprocess.PIPE)
    assert recv.stdout.readline().strip() == b"ready"
    send = subprocess.Popen(["ip", "netns", "exec", "pa", sys.executable, __file__, "send"], stdout=subprocess.PIPE)
    time.sleep(.5)
    for _ in range(cycles):
        run("ip", "netns", "exec", "pa", "tc", "qdisc", "add", "dev", "eth0", "clsact")
        time.sleep(.02)
        run("ip", "netns", "exec", "pa", "tc", "qdisc", "del", "dev", "eth0", "clsact")
        time.sleep(.02)
    sent = int(send.communicate()[0])
    got = json.loads(recv.communicate()[0])
    link = json.loads(run("ip", "netns", "exec", "pa", "ip", "-s", "-s", "-j", "link", "show", "dev", "eth0"))[0]["stats64"]["tx"]
    return {"cycles": cycles, "sent": sent, "received": got["n"], "missing": sent - got["n"], "tx_dropped": link["dropped"]}

def main():
    if sys.argv[1:] == ["send"]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        n, end = 0, time.monotonic() + 3
        while time.monotonic() < end:
            s.sendto(struct.pack("<Q", n), ("10.9.0.2", 9999)); n += 1
            time.sleep(.0002)
        print(n); return
    if sys.argv[1:] == ["recv"]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, 33, 64 << 20)
        s.bind(("10.9.0.2", 9999)); s.settimeout(2)
        print("ready", flush=True)
        seen = set()
        try:
            while True:
                seen.add(struct.unpack("<Q", s.recv(64))[0])
        except socket.timeout:
            pass
        print(json.dumps({"n": len(seen)})); return
    setup()
    print(json.dumps([trial(0), trial(30), trial(0), trial(30)]))

main()
