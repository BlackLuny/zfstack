#!/usr/bin/env python3
"""Same-binary diagnostic of the L1 host's budget-blocked writer recovery."""
import argparse
import json
from pathlib import Path
import subprocess
import time

import rtt_headroom  # register q1s0p3
import rtt_matrix as rig
from rtt_analyze import summarize, changes
from run import ROOT, save, sha, counters


def sequence():
    return [dict(index=i, block=f"retry-pair{i//2}", position=i%2, arm="q1s0p3", retry_budget=enabled,
                 **rig.case("cubic", 20, 200, 12)) for i, enabled in enumerate([0, 1, 1, 0])]


def analyze(out):
    rows = [summarize(p) for p in sorted(out.glob("[0-9][0-9][0-9]-*")) if (p / "end.json").exists()]
    for row in rows:
        raw = json.loads((out / row["name"] / "stdout.json").read_text())
        row["retry_observations"] = [s["app_budget_retry"] for s in raw["stack_thread"]["stack_stats"]["shards"]]
    pairs = []
    for i in range(2):
        group = sorted([r for r in rows if r["block"] == f"retry-pair{i}"], key=lambda r:r["retry_budget"])
        if len(group)==2: pairs.append(group)
    return dict(planned=4, completed=len(rows), valid=sum(r["valid"] for r in rows), rows=rows,
                changes_descriptive=changes(pairs), purpose="Fixture diagnosis, not a production optimization comparison")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--container", default="zfstack-rtt-20261023")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/zfbench-rtt-retry-v4-20261023")
    a = p.parse_args()
    out = a.out.resolve()
    out.relative_to(ROOT)
    if a.analyze:
        print(json.dumps(analyze(out), indent=2, sort_keys=True, allow_nan=False))
        raise SystemExit(0)
    out.mkdir(parents=True, exist_ok=False)
    binary = a.binary.resolve()
    binary.relative_to(ROOT)
    digest = sha(binary)
    sources = {str(f.relative_to(ROOT)): sha(f) for parent in [ROOT / "src", ROOT / "bench/src"] for f in sorted(parent.rglob("*.rs"))}
    for f in [Path(__file__), ROOT / "bench/capacity/rtt_matrix.py", ROOT / "bench/capacity/rtt_analyze.py", ROOT / "bench/capacity/rtt_headroom.py", ROOT / "bench/capacity/rtt_memory.py", ROOT / "docs/experiments/2026-10-23-rtt/HOST-RETRY.md"]:
        sources[str(f.relative_to(ROOT))] = sha(f)
    save(out / "plan.json", dict(phase="host-retry", rows=sequence(), arms=rig.ARMS, sources=sources,
                                binary_sha256=digest, environment=counters(),
                                base_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()))
    for row in sequence():
        rig.execute(out, row, a.container, binary, digest)
    save(out / "complete.json", dict(attempts=4, end_unix=time.time()))
