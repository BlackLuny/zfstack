#!/usr/bin/env python3
"""Queue-memory factorial and independently selected BDP policy validation."""
import argparse
import json
import math
from pathlib import Path
import random
import subprocess
import time

import rtt_matrix as rig
from rtt_final import BINARY, freeze
from rtt_analyze import summarize, changes
from run import ROOT, save, sha, counters

def sequence(phase, multiplier, candidate="q1s0p0"):
    rng = random.Random(2026102380 + ["depth", "policy", "confirm"].index(phase))
    rows = []
    if phase == "depth":
        cells = [(cc, heavy) for cc in ("cubic", "bbr") for heavy in (1, 200)]
        rng.shuffle(cells)
        for cc, heavy in cells:
            depths = [0, 1, 2]
            rng.shuffle(depths)
            for rep in range(2):
                for pos, depth in enumerate(depths if rep == 0 else depths[::-1]):
                    c = rig.case(cc, 500, heavy, 20)
                    c["warmup"] = 20
                    rows.append(dict(block=f"{cc}-h{heavy}-depth{rep}", position=pos, arm=candidate, depth=depth,
                                     queue_bytes=max(250000, depth * 12500 * 500), **c))
    else:
        cells = [(cc, rtt, heavy) for cc in ("cubic", "bbr") for rtt in rig.RTTS for heavy in (1, 200)]
        repeats = 2
        if phase == "confirm":
            cells, repeats = [("cubic", 20, 200)], 12
        rng.shuffle(cells)
        for cc, rtt, heavy in cells:
            first = rng.choice([True, False])
            for rep in range(repeats):
                arms = [rig.BASE, candidate]
                if (rep % 2 == 0) == first: arms.reverse()
                for pos, arm in enumerate(arms):
                    c = rig.case(cc, rtt, heavy, 20)
                    c["warmup"] = max(8, c["warmup"])
                    depth = multiplier if arm != rig.BASE else 0
                    rows.append(dict(block=f"{cc}-rtt{rtt}-h{heavy}-pair{rep}", position=pos, arm=arm, depth=depth,
                                     queue_bytes=max(250000, depth * 12500 * rtt), **c))
    return [dict(index=i, retry_budget=1, **r) for i,r in enumerate(rows)]


def analyze(out):
    plan = json.loads((out / "plan.json").read_text())
    rows = [summarize(p) for p in sorted(out.glob("[0-9][0-9][0-9]-*")) if (p / "end.json").exists()]
    for row in rows:
        raw = json.loads((out / row["name"] / "stdout.json").read_text())
        if any(raw["config"]["queue_bytes_" + side] != row["queue_bytes"] for side in ("up", "down")):
            row["errors"].append("queue capacity identity mismatch")
            row["valid"] = False
    cells = sorted({(r["cc"], r["rtt"], r["heavy"]) for r in rows})
    comparisons = []
    for cc, rtt, heavy in cells:
        for depth in ([1,2] if plan["phase"] == "depth" else [plan["multiplier"]]):
            group = [r for r in rows if (r["cc"], r["rtt"], r["heavy"]) == (cc, rtt, heavy)]
            pairs = []
            for block in sorted({r["block"] for r in group}):
                aa = [r for r in group if r["block"] == block and r["depth"] == 0]
                bb = [r for r in group if r["block"] == block and r["depth"] == depth]
                if len(aa) == len(bb) == 1: pairs.append((aa[0],bb[0]))
            comparisons.append(dict(cc=cc, rtt=rtt, heavy=heavy, depth=depth, pairs=len(pairs),
                                    valid=bool(pairs) and all(a["valid"] and b["valid"] for a,b in pairs),
                                    changes_descriptive=changes(pairs)))
    return dict(phase=plan["phase"], planned=len(plan["rows"]), completed=len(rows), valid=sum(r["valid"] for r in rows),
                all_valid=len(rows)==len(plan["rows"]) and all(r["valid"] for r in rows), rows=rows, comparisons=comparisons)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=["depth", "policy", "confirm"])
    p.add_argument("out", type=Path)
    p.add_argument("--multiplier", type=int, choices=[1,2], default=1)
    p.add_argument("--container", default="zfstack-rtt-20261023")
    p.add_argument("--binary", type=Path, default=BINARY)
    p.add_argument("--candidate", choices=["q1s0p0", "q1s0p2", "q1s0p3"], default="q1s0p0")
    p.add_argument("--analyze", action="store_true")
    a = p.parse_args()
    out = a.out.resolve()
    out.relative_to(ROOT)
    if a.analyze:
        print(json.dumps(analyze(out), indent=2, sort_keys=True, allow_nan=False))
        raise SystemExit(0)
    out.mkdir(parents=True, exist_ok=False)
    binary = a.binary.resolve()
    digest = sha(binary)
    rows = sequence(a.phase, a.multiplier, a.candidate)
    freeze(out, a.phase, rows, binary, multiplier=a.multiplier, candidate=a.candidate)
    for row in rows:
        rig.execute(out, row, a.container, binary, digest)
    save(out / "complete.json", dict(attempts=len(rows), end_unix=time.time()))
