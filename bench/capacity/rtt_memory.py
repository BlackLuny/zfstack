#!/usr/bin/env python3
"""Independent memory-for-CPU extension, frozen before any extension run."""
import argparse
import json
import random
import subprocess
import time
from pathlib import Path

import rtt_matrix as rig
from run import ROOT, save, sha, counters
from rtt_analyze import summarize, changes

WIDE = dict(ZFBENCH_MIN_PREFETCH="262144", ZFBENCH_PREFETCH_MS="20", ZFBENCH_PREFETCH_MAX="2097152")
for q in range(2):
    rig.ARMS[f"q{q}s0p2"] = dict(ZFBENCH_QUEUE_DISCIPLINE="peer" if q else "fifo", **WIDE)
original_arguments = rig.arguments


def arguments(row):
    args = original_arguments(row)
    if row["arm"].endswith("p2"):
        args += ["--budget-mb", "128"]
    return args


rig.arguments = arguments


def sequence():
    rng = random.Random(2026102360)
    cells = [(cc, q) for cc in ("cubic", "bbr") for q in range(2)]
    rng.shuffle(cells)
    rows = []
    for cc, q in cells:
        arms = [f"q{q}s0p0", f"q{q}s0p2"]
        if rng.choice([True, False]): arms.reverse()
        for rep in range(2):
            for pos, arm in enumerate(arms if rep == 0 else arms[::-1]):
                rows.append(dict(block=f"memory-{cc}-q{q}-pair{rep}", position=pos, arm=arm,
                                 **rig.case(cc, 20, 200, 16)))
    return [dict(index=i, **r) for i, r in enumerate(rows)]


def analyze(out):
    rows = [summarize(p) for p in sorted(out.glob("[0-9][0-9][0-9]-*")) if (p / "end.json").exists()]
    for row in rows:
        raw = json.loads((out / row["name"] / "stdout.json").read_text())
        if raw["config"]["budget_mb"] != (128 if row["arm"].endswith("p2") else 0):
            row["errors"].append("budget profile differs from plan")
            row["valid"] = False
    comparisons = []
    for cc in ("cubic", "bbr"):
        for q in range(2):
            pairs = []
            for rep in range(2):
                group = sorted([r for r in rows if r["block"] == f"memory-{cc}-q{q}-pair{rep}"], key=lambda r:r["arm"])
                if len(group) == 2: pairs.append(group)
            comparisons.append(dict(cc=cc, queue=q, pairs=len(pairs),
                                    valid=len(pairs) == 2 and all(a["valid"] and b["valid"] for a,b in pairs),
                                    changes_descriptive=changes(pairs)))
    return dict(planned=16, completed=len(rows), valid=sum(r["valid"] for r in rows), rows=rows, comparisons=comparisons)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--container", default="zfstack-rtt-20261023")
    p.add_argument("--analyze", action="store_true")
    a = p.parse_args()
    out = a.out.resolve()
    out.relative_to(ROOT)
    if a.analyze:
        print(json.dumps(analyze(out), indent=2, sort_keys=True, allow_nan=False))
        raise SystemExit(0)
    out.mkdir(parents=True, exist_ok=False)
    binary = ROOT / "target/release/zfbench-rtt-20261023"
    sources = {str(f.relative_to(ROOT)): sha(f) for parent in [ROOT / "src", ROOT / "bench/src"] for f in sorted(parent.rglob("*.rs"))}
    for f in [Path(__file__), ROOT / "bench/capacity/rtt_matrix.py", ROOT / "bench/capacity/rtt_analyze.py", ROOT / "docs/experiments/2026-10-23-rtt/MEMORY.md"]:
        sources[str(f.relative_to(ROOT))] = sha(f)
    digest = sha(binary)
    save(out / "plan.json", dict(phase="memory", rows=sequence(), arms=rig.ARMS,
                                sources=sources, binary_sha256=digest, environment=counters(),
                                base_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()))
    for row in sequence():
        rig.execute(out, row, a.container, binary, digest)
    save(out / "complete.json", dict(attempts=16, end_unix=time.time()))
