#!/usr/bin/env python3
"""Independent active-flow queue floor screen with fixed BDP multiplier."""
import argparse
import json
from pathlib import Path
import random
import time

from rtt_final import BINARY, freeze
import rtt_matrix as rig
from rtt_analyze import summarize, changes
from run import ROOT, save, sha


def sequence(multiplier):
    rng = random.Random(20261023101)
    cells = [(cc, rtt) for cc in ("cubic", "bbr") for rtt in (1, 20)]
    rng.shuffle(cells)
    rows = []
    for cc, rtt in cells:
        floors = [0, 4, 8]
        rng.shuffle(floors)
        for rep in range(2):
            for pos, packets in enumerate(floors if rep == 0 else floors[::-1]):
                c = rig.case(cc, rtt, 200, 16)
                c["warmup"] = 8
                rows.append(dict(block=f"{cc}-rtt{rtt}-floor{rep}", position=pos, arm="q1s0p0",
                    floor_packets=packets, queue_bytes=max(250000, multiplier * 12500 * rtt, packets * 1420 * 200), **c))
    return [dict(index=i, retry_budget=1, **r) for i,r in enumerate(rows)]


def analyze(out):
    plan = json.loads((out / "plan.json").read_text())
    rows = [summarize(p) for p in sorted(out.glob("[0-9][0-9][0-9]-*")) if (p / "end.json").exists()]
    comparisons = []
    for cc, rtt in sorted({(r["cc"],r["rtt"]) for r in rows}):
        for floor in (4,8):
            pairs = []
            for rep in range(2):
                group = [r for r in rows if r["block"] == f"{cc}-rtt{rtt}-floor{rep}"]
                aa = [r for r in group if r["floor_packets"] == 0]
                bb = [r for r in group if r["floor_packets"] == floor]
                if len(aa) == len(bb) == 1: pairs.append((aa[0],bb[0]))
            comparisons.append(dict(cc=cc, rtt=rtt, floor_packets=floor, pairs=len(pairs),
                valid=len(pairs)==2 and all(a["valid"] and b["valid"] for a,b in pairs), changes_descriptive=changes(pairs)))
    return dict(phase="floor", planned=len(plan["rows"]), completed=len(rows), valid=sum(r["valid"] for r in rows),
                all_valid=len(rows)==len(plan["rows"]) and all(r["valid"] for r in rows), rows=rows, comparisons=comparisons)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--multiplier", type=int, choices=[1,2], required=True)
    p.add_argument("--container", default="zfstack-rtt-20261023")
    p.add_argument("--analyze", action="store_true")
    a = p.parse_args()
    out = a.out.resolve()
    out.relative_to(ROOT)
    if a.analyze:
        print(json.dumps(analyze(out), indent=2, sort_keys=True, allow_nan=False))
        raise SystemExit(0)
    out.mkdir(parents=True, exist_ok=False)
    rows, digest = sequence(a.multiplier), sha(BINARY)
    freeze(out, "floor", rows, BINARY, multiplier=a.multiplier)
    for row in rows:
        rig.execute(out, row, a.container, BINARY, digest)
    save(out / "complete.json", dict(attempts=len(rows), end_unix=time.time()))
