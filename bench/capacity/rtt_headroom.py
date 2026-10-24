#!/usr/bin/env python3
"""Repeat wide prefetch with the original peer/port/global headroom."""
import argparse
from pathlib import Path
import subprocess
import time
import rtt_memory as memory
from run import ROOT, save, sha, counters

rig = memory.rig
for q in range(2):
    rig.ARMS[f"q{q}s0p3"] = dict(ZFBENCH_QUEUE_DISCIPLINE="peer" if q else "fifo", **memory.WIDE)


def sequence():
    rows = memory.sequence()
    for row in rows:
        row["arm"] = row["arm"].replace("p2", "p3")
    return rows


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--container", default="zfstack-rtt-20261023")
    a = p.parse_args()
    out = a.out.resolve()
    out.relative_to(ROOT)
    out.mkdir(parents=True, exist_ok=False)
    binary = ROOT / "target/release/zfbench-rtt-20261023"
    digest = sha(binary)
    sources = {str(f.relative_to(ROOT)): sha(f) for parent in [ROOT / "src", ROOT / "bench/src"] for f in sorted(parent.rglob("*.rs"))}
    for f in [Path(__file__), ROOT / "bench/capacity/rtt_memory.py", ROOT / "bench/capacity/rtt_matrix.py", ROOT / "bench/capacity/rtt_analyze.py", ROOT / "docs/experiments/2026-10-23-rtt/MEMORY-HEADROOM.md"]:
        sources[str(f.relative_to(ROOT))] = sha(f)
    save(out / "plan.json", dict(phase="headroom", rows=sequence(), arms=rig.ARMS, sources=sources,
                                binary_sha256=digest, environment=counters(),
                                base_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()))
    for row in sequence():
        rig.execute(out, row, a.container, binary, digest)
    save(out / "complete.json", dict(attempts=16, end_unix=time.time()))
