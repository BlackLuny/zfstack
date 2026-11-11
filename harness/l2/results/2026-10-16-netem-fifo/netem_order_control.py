#!/usr/bin/env python3
"""Control: receiver UDP order behind a plain netem with the historical v3 timeline.

Same veth pair, sender, receiver and default load as fifo_selftest.py, but
worker eth0 gets no injector: at loaded start a root `netem delay 500ms`, at
the drain boundary an in-place change to `delay 0 rate 2Gbit`, and deletion
once its backlog reads empty. Only receiver order and loss are recorded; this
is not a qualification of any injector. Run inside qa/isolate-linux.sh.
"""
import argparse
import json
import secrets
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fifo_selftest import links, run  # noqa: E402
from isolation import verify  # noqa: E402

SELFTEST = str(Path(__file__).resolve().parents[2] / "fifo_selftest.py")


def tc(worker, *args):
    started = time.monotonic_ns()
    run(["ip", "netns", "exec", worker, "tc", "qdisc", *args])
    return [started, time.monotonic_ns()]


def backlog(worker):
    rows = json.loads(run(["ip", "netns", "exec", worker, "tc", "-s", "-j", "qdisc", "show", "dev", "eth0"]))
    root = next(row for row in rows if row.get("root"))
    return root.get("kind"), root.get("qlen", 0), root.get("drops", 0)


def sleep_until(at):
    time.sleep(max(0, at - time.monotonic()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lead", type=float, default=10)
    parser.add_argument("--baseline", type=float, default=8)
    parser.add_argument("--loaded", type=float, default=16)
    parser.add_argument("--recovery", type=float, default=8)
    a = parser.parse_args()
    if verify():
        raise SystemExit("control requires the verified QA namespace wrapper")
    out = a.output.with_suffix("")
    out.mkdir()
    prefix = "rig-" + secrets.token_hex(3)
    worker, target = prefix + "-worker", prefix + "-target"
    procs = []
    try:
        for name in (worker, target):
            run(["ip", "netns", "add", name])
        run(["ip", "link", "add", "eth0", "netns", worker, "type", "veth", "peer", "name", "eth0", "netns", target])
        for name, address in ((worker, "10.44.0.1/24"), (target, "10.44.0.2/24")):
            run(["ip", "netns", "exec", name, "ip", "addr", "add", address, "dev", "eth0"])
            run(["ip", "netns", "exec", name, "ip", "link", "set", "lo", "up"])
            run(["ip", "netns", "exec", name, "ip", "link", "set", "eth0", "up"])
        t0 = time.monotonic() + a.lead
        start, recovery = t0 + a.baseline, t0 + a.baseline + a.loaded
        end, stop = recovery - .75, recovery + a.recovery
        common = ["--host", "10.44.0.2", "--rate", "500", "--size", "1032", "--tcp-rate", "2000000"]
        receiver = subprocess.Popen(["ip", "netns", "exec", target, sys.executable, SELFTEST, "receive", *common,
                                     "--until", str(stop + 1.5), "--out", str(out / "receiver.json")], stdout=subprocess.PIPE)
        procs.append(receiver)
        if receiver.stdout.readline().strip() != b"ready":
            raise SystemExit("receiver did not start")
        before = {name: links(name) for name in (worker, target)}
        sender = subprocess.Popen(["ip", "netns", "exec", worker, sys.executable, SELFTEST, "send", *common,
                                   "--begin", str(t0 - a.lead / 2), "--until", str(stop - .05),
                                   "--out", str(out / "sender.json")])
        procs.append(sender)
        actual = {}
        sleep_until(start)
        actual["install"] = tc(worker, "add", "dev", "eth0", "root", "handle", "44:", "netem", "limit", "100000", "delay", "500ms")
        sleep_until(end)
        actual["drain"] = tc(worker, "change", "dev", "eth0", "root", "handle", "44:", "netem", "limit", "100000",
                             "delay", "0ms", "rate", "2000000000bit")
        while True:
            kind, qlen, drops = backlog(worker)
            if not qlen or time.monotonic() >= recovery:
                break
            time.sleep(.002)
        actual["final_drops"] = drops
        actual["remove"] = tc(worker, "del", "dev", "eth0", "root")
        sender.wait(timeout=stop - time.monotonic() + 30)
        receiver.wait(timeout=60)
        after = {name: links(name) for name in (worker, target)}
        received = json.loads((out / "receiver.json").read_text())
        sent = json.loads((out / "sender.json").read_text())
        rows = received["udp"]
        best, overtaken = -1, []
        for seq, sent_ns, _, _ in rows:
            if seq < best:
                overtaken.append(sent_ns)
            best = max(best, seq)
        phase = lambda ns: ("baseline" if ns < start * 1e9 else "loaded" if ns < end * 1e9
                            else "drain-or-guard" if ns < recovery * 1e9 else "recovery")
        by_phase = {}
        for ns in overtaken:
            by_phase[phase(ns)] = by_phase.get(phase(ns), 0) + 1
        seqs = [row[0] for row in rows]
        summary = {"kernel": run(["uname", "-r"]).strip(), "sent": sent, "udp_received": len(rows),
                   "udp_unique": len(set(seqs)), "udp_overtaken": len(overtaken), "udp_overtaken_by_send_phase": by_phase,
                   "planned_ns": {"start": round(start * 1e9), "end": round(end * 1e9), "recovery": round(recovery * 1e9)},
                   "actual_ns": actual, "link_before": before, "link_after": after}
        a.output.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps({key: summary[key] for key in ("udp_received", "udp_unique", "udp_overtaken",
                                                         "udp_overtaken_by_send_phase")}))
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for name in (worker, target):
            subprocess.run(["ip", "netns", "del", name], capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
