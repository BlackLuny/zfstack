#!/usr/bin/env python3
"""Frozen, sequential factorial/RTT experiments; every attempt is retained."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import subprocess
import time

from run import ROOT, counters, save, sha

ARMS = {}
for q in range(2):
    for s in range(2):
        for p in range(2):
            name = f"q{q}s{s}p{p}"
            env = {"ZFBENCH_QUEUE_DISCIPLINE": "peer" if q else "fifo"}
            if s:
                env.update(ZFBENCH_QUANTUM="16384", ZFBENCH_ROUND_CAP="65536")
            if p:
                env.update(ZFBENCH_MIN_PREFETCH="16384", ZFBENCH_PREFETCH_MS="4", ZFBENCH_PREFETCH_MAX="524288")
            ARMS[name] = env
BASE = "q0s0p0"
RTTS = [1, 10, 50, 100, 250, 500]


def case(cc, rtt, heavy, measure=16):
    return dict(cc=cc, rtt=rtt, heavy=heavy, lights=3 if heavy > 1 else 0,
                measure=measure, warmup=max(4, math.ceil(rtt * 20 / 1000)))


def arguments(c):
    return ["--test", "isolate", "--stack", "zfstack-" + c["cc"], "--ports", "1",
            "--lights", str(c["lights"]), "--heavy-conns", str(c["heavy"]), "--rtt-ms", str(c["rtt"]),
            "--rate-mbps", "100", "--queue-bytes", str(c.get("queue_bytes", 250000)),
            "--sock-buf-kb", "16384", "--secs", str(c["warmup"] + c["measure"]),
            "--warmup", str(c["warmup"]), "--idle-rr-secs", "2" if c["lights"] else "0",
            "--rr-size", "256", "--rr-interval-ms", "40", "--connect-timeout-ms", "20000", "--seed", "23"]


def plan(phase, candidate):
    rng = random.Random(2026102300 + ["pilot", "screen", "wan", "confirm", "aa"].index(phase))
    rows = []
    if phase == "pilot":
        for cc, rtt, heavy, arm in [("cubic", 1, 1, BASE), ("cubic", 500, 1, BASE),
                                    ("cubic", 500, 200, BASE), ("bbr", 500, 200, "q1s0p0")]:
            rows.append(dict(block="pilot", arm=arm, **case(cc, rtt, heavy, 12)))
    elif phase == "screen":
        for block in range(2):
            cells = [(cc, arm) for cc in ("cubic", "bbr") for arm in ARMS]
            rng.shuffle(cells)
            for cc, arm in cells:
                rows.append(dict(block=f"factor{block}", arm=arm, **case(cc, 20, 200, 12)))
    else:
        cells = [(cc, rtt, heavy) for cc in ("cubic", "bbr") for rtt in RTTS for heavy in (1, 200)]
        repeats = 2
        if phase == "confirm":
            cells, repeats = [("cubic", 20, 200)], 12
        elif phase == "aa":
            cells, repeats = [(cc, rtt, heavy) for cc in ("cubic", "bbr") for rtt in (1, 500) for heavy in (1, 200)], 1
        rng.shuffle(cells)
        for cc, rtt, heavy in cells:
            first = rng.choice([True, False])
            for rep in range(repeats):
                arms = [BASE, BASE if phase == "aa" else candidate]
                if (rep % 2 == 0) == first:
                    arms.reverse()
                for position, arm in enumerate(arms):
                    rows.append(dict(block=f"{cc}-rtt{rtt}-h{heavy}-pair{rep}", position=position,
                                     arm=arm, **case(cc, rtt, heavy, 20 if phase == "confirm" else 16)))
    return [dict(index=i, **r) for i, r in enumerate(rows)]


def execute(out, row, container, binary, digest):
    name = f"{row['index']:03d}-{row['cc']}-r{row['rtt']}-h{row['heavy']}-{row['arm']}"
    dest = out / name
    dest.mkdir()
    rel = dest.relative_to(ROOT)
    env = dict(ARMS[row["arm"]], ZFBENCH_RR_RAW="1", ZFBENCH_RR_OPEN_HZ="25",
               ZFBENCH_RR_ARRIVALS=f"/work/{rel}/arrivals.json", ZFBENCH_CONNECT_PARALLEL="32",
               ZFBENCH_HEAVY_PROGRESS="1")
    if "retry_budget" in row:
        env["ZFBENCH_RETRY_BUDGET_WRITERS"] = str(row["retry_budget"])
    argv = ["docker", "exec", container, "env"] + [f"{k}={v}" for k, v in env.items()]
    argv += ["timeout", "-k", "5", "180", "python3", "/work/bench/tail69_exec.py",
             f"/work/{rel}/resources.json", "taskset", "-c", "0-3", f"/work/{binary.relative_to(ROOT)}"]
    argv += arguments(row) + ["--label", name]
    if sha(binary) != digest:
        raise RuntimeError("frozen binary changed")
    save(dest / "start.json", dict(row=row, argv=argv, env=env, binary_sha256=digest,
                                  start_unix=time.time(), cgroup=counters()))
    with (dest / "stdout.json").open("x") as stdout, (dest / "stderr.txt").open("x") as stderr:
        try:
            rc = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=195).returncode
        except subprocess.TimeoutExpired:
            rc = "outer_timeout"
    save(dest / "end.json", dict(returncode=rc, end_unix=time.time(), cgroup=counters(), binary_unchanged=sha(binary) == digest))
    print(json.dumps(dict(name=name, returncode=rc)), flush=True)
    if rc in (124, 137, "outer_timeout") or sha(binary) != digest:
        raise RuntimeError("attempt bounded/identity failed; no silent retry")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=["pilot", "screen", "wan", "confirm", "aa"])
    p.add_argument("out", type=Path)
    p.add_argument("--candidate", choices=list(ARMS), default="q1s0p0")
    p.add_argument("--container", default="zfstack-rtt-20261023")
    a = p.parse_args()
    out = a.out.resolve()
    out.relative_to(ROOT)
    out.mkdir(parents=True, exist_ok=False)
    binary = ROOT / "target/release/zfbench-rtt-20261023"
    sources = {str(f.relative_to(ROOT)): sha(f) for parent in [ROOT / "src", ROOT / "bench/src"] for f in sorted(parent.rglob("*.rs"))}
    for f in [Path(__file__), ROOT / "bench/capacity/rtt_analyze.py", ROOT / "docs/experiments/2026-10-23-rtt/PROTOCOL.md", ROOT / "Cargo.lock"]:
        sources[str(f.relative_to(ROOT))] = sha(f)
    manifest = dict(phase=a.phase, candidate=a.candidate, rows=plan(a.phase, a.candidate), arms=ARMS,
                    binary_sha256=sha(binary), sources=sources, environment=counters(), host=list(os.uname()),
                    base_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip())
    save(out / "plan.json", manifest)
    (out / "source.patch").write_bytes(subprocess.check_output(["git", "diff"], cwd=ROOT))
    for row in manifest["rows"]:
        execute(out, row, a.container, binary, manifest["binary_sha256"])
    save(out / "complete.json", dict(attempts=len(manifest["rows"]), end_unix=time.time()))


if __name__ == "__main__":
    main()
