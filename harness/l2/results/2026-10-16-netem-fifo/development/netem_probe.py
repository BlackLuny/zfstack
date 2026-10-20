#!/usr/bin/env python3
"""Feasibility probe: persistent root netem FIFO + mirror/egress TPACKET_V3 capture."""
import ctypes, hashlib, json, mmap, os, select, socket, struct, subprocess, sys, time

SOL_PACKET, PACKET_VERSION, PACKET_RX_RING, PACKET_STATISTICS = 263, 10, 5, 6
TPACKET_V3, SO_ATTACH_FILTER = 2, 26
BLOCK, BLOCKS, SNAP = 1 << 20, 128, 160


def netem_change(ifindex, delay_ns, rate_bytes, limit=100000):
    def attr(kind, payload):
        size = 4 + len(payload)
        return struct.pack("HH", size, kind) + payload + b"\0" * (-size % 4)
    nested = attr(10, struct.pack("q", delay_ns)) + attr(6, struct.pack("IiIi", min(rate_bytes, 0xFFFFFFFF), 0, 0, 0))
    if rate_bytes >= 1 << 32:
        nested += attr(8, struct.pack("Q", rate_bytes))
    options = attr(2, struct.pack("6I", 0, limit, 0, 0, 0, 0) + nested)
    body = struct.pack("BBHiIII", 0, 0, 0, ifindex, 0x00440000, 0xFFFFFFFF, 0) + attr(1, b"netem\0") + options
    msg = struct.pack("IHHII", 16 + len(body), 36, 1 | 4, 1, 0) + body
    nl = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    t0 = time.monotonic_ns()
    nl.send(msg)
    reply = nl.recv(65536)
    t1 = time.monotonic_ns()
    nl.close()
    error = struct.unpack_from("i", reply, 16)[0]
    if error:
        raise OSError(-error, "netem change failed")
    return t0, t1


def sh(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def ns(name, *args):
    return sh("ip", "netns", "exec", name, *args)


class Ring:
    def __init__(self, ifname):
        self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        prog = [(0x20, 0, 0, 0xFFFFF004), (0x15, 0, 1, 4), (0x06, 0, 0, SNAP), (0x06, 0, 0, 0)]
        buf = ctypes.create_string_buffer(b"".join(struct.pack("HBBI", *row) for row in prog))
        fprog = struct.pack("HL", len(prog), ctypes.addressof(buf))
        self.sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, fprog)
        self.sock.setsockopt(SOL_PACKET, PACKET_VERSION, TPACKET_V3)
        req = struct.pack("7I", BLOCK, BLOCKS, 2048, BLOCK * BLOCKS // 2048, 16, 0, 0)
        self.sock.setsockopt(SOL_PACKET, PACKET_RX_RING, req)
        self.map = mmap.mmap(self.sock.fileno(), BLOCK * BLOCKS, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        self.sock.bind((ifname, 0))
        self.next, self.blocks = 0, []
        self.stats()

    def stats(self):
        return struct.unpack("3I", self.sock.getsockopt(SOL_PACKET, PACKET_STATISTICS, 12))

    def drain(self):
        n = 0
        while True:
            off = self.next * BLOCK
            if not struct.unpack_from("I", self.map, off + 8)[0] & 1:
                return n
            used = struct.unpack_from("I", self.map, off + 20)[0]
            self.blocks.append(bytes(self.map[off:off + used]))
            struct.pack_into("I", self.map, off + 8, 0)
            self.next = (self.next + 1) % BLOCKS
            n += 1

    def frames(self):
        out = []
        for block in self.blocks:
            count, first = struct.unpack_from("II", block, 12)
            off = first
            for _ in range(count):
                nxt, sec, nsec, snaplen, length, _, mac = struct.unpack_from("IIIIIIH", block, off)
                data = block[off + mac:off + mac + snaplen]
                out.append((sec * 1_000_000_000 + nsec, length, data))
                off += nxt
        return out


def main():
    rate_hz = float(sys.argv[1]) if len(sys.argv) > 1 else 20000
    for name in ("pa", "pb"):
        sh("ip", "netns", "add", name)
    sh("ip", "link", "add", "eth0", "netns", "pa", "type", "veth", "peer", "eth0", "netns", "pb")
    for name, addr in (("pa", "10.9.0.1/24"), ("pb", "10.9.0.2/24")):
        ns(name, "ip", "addr", "add", addr, "dev", "eth0")
        ns(name, "ip", "link", "set", "eth0", "up")
        ns(name, "ip", "link", "set", "lo", "up")
    ns("pa", "ip", "link", "add", "qa44in", "type", "dummy")
    ns("pa", "ip", "link", "set", "qa44in", "up")
    ns("pa", "ip", "neigh", "replace", "10.9.0.2", "lladdr",
       json.loads(ns("pb", "ip", "-j", "link", "show", "eth0"))[0]["address"], "dev", "eth0", "nud", "permanent")
    fd = os.open("/run/netns/pa", os.O_RDONLY)
    os.setns(fd, os.CLONE_NEWNET)
    sh("tc", "qdisc", "add", "dev", "eth0", "root", "handle", "44:", "netem", "limit", "100000", "delay", "0", "rate", "10Tbit")
    sh("tc", "qdisc", "add", "dev", "eth0", "clsact")
    sh("tc", "filter", "add", "dev", "eth0", "egress", "pref", "44", "protocol", "all", "matchall",
       "action", "mirred", "egress", "mirror", "dev", "qa44in")
    rin, rout = Ring("qa44in"), Ring("eth0")
    mono0, real0 = time.monotonic_ns(), time.time_ns()
    start = time.monotonic_ns() + 300_000_000
    senders = []
    for sid in range(2):
        code = ("import socket,time,struct,sys\n"
                f"s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);t0={start};sid={sid};gap=1e9/{rate_hz / 2}\n"
                "while time.monotonic_ns()<t0: pass\n"
                "i=0\n"
                "while True:\n"
                " due=t0+i*gap\n"
                " if due>t0+4.0e9: break\n"
                " while time.monotonic_ns()<due: pass\n"
                " s.sendto(struct.pack('!II',sid,i)+b'x'*(100+(i%7)*190),('10.9.0.2',9))\n"
                " i+=1\n")
        senders.append(subprocess.Popen([sys.executable, "-c", code]))
    plan = [(1.0, ["delay", "500ms", "rate", "10Tbit"]), (2.5, ["delay", "0", "rate", "2Gbit"]),
            (3.2, ["delay", "0", "rate", "10Tbit"])]
    changes = []
    poller = select.poll()
    poller.register(rin.sock, select.POLLIN)
    poller.register(rout.sock, select.POLLIN)
    end = start + 5_500_000_000
    while time.monotonic_ns() < end:
        now = time.monotonic_ns()
        if plan and now >= start + plan[0][0] * 1e9:
            at, args = plan.pop(0)
            delay = 500_000_000 if args[1] == "500ms" else 0
            rate = 250_000_000 if args[3] == "2Gbit" else 1_250_000_000_000
            t0, t1 = netem_change(socket.if_nametoindex("eth0"), delay, rate)
            shown = json.loads(sh("tc", "-j", "qdisc", "show", "dev", "eth0"))[0]["options"]
            changes.append({"at": at, "args": args, "t0": t0, "t1": t1, "shown": shown})
        poller.poll(5)
        rin.drain(); rout.drain()
    for p in senders:
        p.wait()
    time.sleep(.1)
    rin.drain(); rout.drain()
    stats = {"in": rin.stats(), "out": rout.stats()}
    qd = json.loads(sh("tc", "-s", "-j", "qdisc", "show", "dev", "eth0"))
    mono1, real1 = time.monotonic_ns(), time.time_ns()
    fin, fout = rin.frames(), rout.frames()
    with open("/tmp/vmout/probe-frames.json", "w") as stream:
        json.dump({"offset": real0 - mono0, "changes": changes, "start": start,
                   "in": [(t, n, d.hex()) for t, n, d in fin], "out": [(t, n, d.hex()) for t, n, d in fout]}, stream)
    key = lambda f: hashlib.blake2b(f[2], digest_size=8).digest()
    kin, kout = [key(f) for f in fin], [key(f) for f in fout]
    pos = {k: i for i, k in enumerate(kout)}
    inversions = sum(1 for a, b in zip(kin, kin[1:]) if a in pos and b in pos and pos[a] > pos[b])
    # netem chaining model in capture (realtime) clock
    offset = real0 - mono0
    def policy(t_in):
        delay, rate = 0, 1.25e12
        for c in changes:
            if t_in - offset >= c["t1"]:
                delay = 500_000_000 if c["args"][1] == "500ms" else 0
                rate = 2.5e8 if c["args"][3] == "2Gbit" else 1.25e12
        return delay, rate
    late, early, prev = [], 0, 0
    outmap = {k: f[0] for k, f in zip(kout, fout)}
    ambiguous = 0
    for f, k in zip(fin, kin):
        if any(c["t0"] <= f[0] - offset <= c["t1"] for c in changes):
            ambiguous += 1
        d, r = policy(f[0])
        due = max(f[0] + d, prev) + int(f[1] * 1e9 / r)
        prev = due
        t_out = outmap.get(k)
        if t_out is None:
            continue
        late.append(t_out - due)
        early += t_out < due - 50_000
    late.sort()
    result = {"rate_hz": rate_hz, "frames_in": len(fin), "frames_out": len(fout), "missing": len(set(kin) - set(kout)),
              "extra": len(set(kout) - set(kin)), "dup_in": len(kin) - len(set(kin)), "inversions": inversions,
              "capture_stats": stats, "qdisc": qd, "changes": [{**c, "window_us": (c["t1"] - c["t0"]) // 1000} for c in changes],
              "ambiguous_frames": ambiguous, "early_over_50us": early,
              "lateness_us": {q: late[min(len(late) - 1, int(q * len(late)))] // 1000 for q in (0, .5, .99, .999)} | {"max": late[-1] // 1000},
              "clock_offset_drift_ns": (real1 - mono1) - offset,
              "in_ts_vs_wall_ns": None}
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
