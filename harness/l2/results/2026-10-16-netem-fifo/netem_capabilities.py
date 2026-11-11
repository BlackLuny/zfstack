#!/usr/bin/env python3
"""Probe every kernel facility netem-fifo needs, on a throwaway private veth.

Run only inside qa/isolate-linux.sh. It builds one private namespace with a
veth, installs the same root netem, dummy, clsact, mirred mirror and matchall
filter as netem_fifo.py, performs one netlink change and qlen read, opens
both TPACKET_V3 rings, then removes everything. Exit 0 only if every step
succeeded; the JSON report lists each step.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

L2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(L2))
from isolation import verify  # noqa: E402

NS = "qa44cap"


def inner():
    import socket
    import netem_fifo
    import netem_fifo_ledger as ledger
    config = {"limit_packets": 100000, "delay_ms": 500.0}
    steps = []
    try:
        client = netem_fifo.Netlink(socket.if_nametoindex("eth0"))
        for phase in (ledger.ACTIVE, ledger.DRAIN):
            delay, rate = ledger.policy(phase, 500_000_000)
            window = client.change(delay, rate, config["limit_packets"])
            shown = next(row for row in netem_fifo.qdiscs() if row.get("root"))["options"]
            steps.append({"step": "change-" + ledger.PHASES[phase], "window_ns": window[1] - window[0], "shown": shown,
                          "ok": ledger.options_match(shown, phase, config)})
        steps.append({"step": "qlen", "value": client.qlen(), "ok": True})
        rings = [netem_fifo.Ring(name, lambda size: True) for name in (ledger.DUMMY, "eth0")]
        steps.append({"step": "rings", "value": [ring.stats() for ring in rings], "ok": True})
        for ring in rings:
            ring.close()
    except Exception as error:  # noqa: BLE001 - reported, never hidden
        steps.append({"step": "error", "error": f"{type(error).__name__}: {error}", "ok": False})
    print(json.dumps(steps))
    return 0 if steps and all(row["ok"] for row in steps) else 1


def main():
    if verify():
        raise SystemExit("capability probe requires the verified QA namespace wrapper")
    output = Path(sys.argv[1])
    rows = []

    def run(argv, namespace=True):
        full = (["ip", "netns", "exec", NS] if namespace else []) + argv
        result = subprocess.run(full, capture_output=True, text=True, timeout=30)
        rows.append({"argv": full, "rc": result.returncode, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]})
        return result

    ok = True
    try:
        setup = [(["ip", "netns", "add", NS], False),
                 (["ip", "link", "add", "eth0", "type", "veth", "peer", "name", "peer0"], True),
                 (["ip", "link", "set", "eth0", "up"], True), (["ip", "link", "set", "peer0", "up"], True),
                 (["ip", "link", "add", "qa44in", "type", "dummy"], True),
                 (["ip", "link", "set", "dev", "qa44in", "arp", "off", "up"], True),
                 (["tc", "qdisc", "add", "dev", "eth0", "root", "handle", "44:", "netem", "limit", "100000",
                   "delay", "0", "rate", "10000000000000bit"], True),
                 (["tc", "qdisc", "add", "dev", "eth0", "clsact"], True),
                 (["tc", "actions", "add", "action", "mirred", "egress", "mirror", "dev", "qa44in", "index", "44"], True),
                 (["tc", "filter", "add", "dev", "eth0", "egress", "pref", "44", "protocol", "all", "matchall",
                   "action", "mirred", "index", "44"], True)]
        for argv, namespace in setup:
            if run(argv, namespace).returncode:
                ok = False
                break
        if ok:
            probe = run([sys.executable, __file__, "--inner"])
            ok = probe.returncode == 0
    finally:
        run(["ip", "netns", "del", NS], False)
    report = {"ok": ok, "kernel": os.uname().release, "observed_unix_ns": time.time_ns(),
              "modules": sorted(line.split()[0] for line in Path("/proc/modules").read_text().splitlines()
                                if line.split()[0] in ("sch_netem", "dummy", "act_mirred", "cls_matchall", "sch_ingress", "veth")),
              "steps": rows}
    output.write_text(json.dumps(report, indent=2) + "\n")
    print("capabilities", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(inner() if sys.argv[1:] == ["--inner"] else main())
