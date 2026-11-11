#!/usr/bin/env python3
"""Corrected-host factorial screening and independent noise/peak guards."""
import argparse
import json
from pathlib import Path
import random
import shutil
import subprocess
import time

import rtt_headroom  # register both memory profiles, including CLI budget mapping
import rtt_matrix as rig
from rtt_analyze import analyze as base_analyze, changes
from run import ROOT, save, sha, counters

BINARY = ROOT / "target/release/zfbench-rtt-final-20261023"


def freeze(out, phase, rows, binary, **extra):
    files = [f for parent in (ROOT / "src", ROOT / "bench/src", ROOT / "bench/capacity")
             for f in sorted(parent.rglob("*")) if f.suffix in (".rs", ".py")]
    files += list(sorted((ROOT / "docs/experiments/2026-10-23-rtt").glob("*.md")))
    files += [ROOT / "Cargo.lock", ROOT / "Cargo.toml", ROOT / "bench/Cargo.toml"]
    sources = {}
    for f in files:
        rel = f.relative_to(ROOT)
        sources[str(rel)] = sha(f)
        dest = out / "frozen_source" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    save(out / "plan.json", dict(phase=phase, rows=rows, arms=rig.ARMS, sources=sources,
                                 binary_sha256=sha(binary), environment=counters(),
                                 base_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), **extra))
    (out / "source.patch").write_bytes(subprocess.check_output(["git", "diff"], cwd=ROOT))


def sequence(phase, candidate):
    rng = random.Random(2026102390 + ["screen", "aa", "guard"].index(phase))
    rows = []
    if phase == "screen":
        arms = [a for a in rig.ARMS if a.endswith(("p0", "p1"))] + ["q1s0p2", "q1s0p3"]
        for block in range(2):
            cells = [(cc, arm) for cc in ("cubic", "bbr") for arm in arms]
            rng.shuffle(cells)
            for cc, arm in cells:
                c = rig.case(cc, 20, 200, 12)
                c["warmup"] = 8
                rows.append(dict(block=f"corrected-factor{block}", arm=arm, **c))
    elif phase == "aa":
        rows = rig.plan("aa", candidate)
        for r in rows:
            r["warmup"] = max(8, r["warmup"])
            r["measure"] = 20
            del r["index"]
    else:
        cells = [("cubic", 1), ("cubic", 2), ("bbr", 1)]
        rng.shuffle(cells)
        for cc, heavy in cells:
            arms = [rig.BASE, candidate]
            rng.shuffle(arms)
            for rep in range(2):
                for pos, arm in enumerate(arms if rep == 0 else arms[::-1]):
                    c = rig.case(cc, 0, heavy, 12)
                    c["lights"] = 0
                    rows.append(dict(block=f"{cc}-peak-h{heavy}-pair{rep}", position=pos, arm=arm, **c))
    return [dict(index=i, retry_budget=1, **r) for i, r in enumerate(rows)]


def analyze(out):
    result = base_analyze(out)
    for row in result["rows"]:
        raw = json.loads((out / row["name"] / "stdout.json").read_text())
        errors = row["errors"]
        if raw["config"]["budget_mb"] != (128 if row["arm"].endswith("p2") else 0):
            errors.append("budget identity mismatch")
        if result["phase"] == "guard" and (raw["config"]["rate_mbps"] != 10000 or row["rtt"] != 0):
            errors.append("not a host-throughput guard")
        row["valid"] = not errors
    plan = json.loads((out / "plan.json").read_text())
    if result["phase"] == "guard" and plan.get("candidate") == rig.BASE:
        result["comparisons"] = []
        for cc, heavy in sorted({(r["cc"],r["heavy"]) for r in result["rows"]}):
            group = [r for r in result["rows"] if (r["cc"],r["heavy"]) == (cc,heavy)]
            pairs = []
            for block in sorted({r["block"] for r in group}):
                pair = sorted([r for r in group if r["block"] == block], key=lambda r:r["position"])
                if len(pair) == 2: pairs.append(pair)
            result["comparisons"].append(dict(cc=cc, rtt=0, heavy=heavy, arm=rig.BASE,
                pairs=len(pairs), valid=len(pairs)==2 and all(a["valid"] and b["valid"] for a,b in pairs),
                changes_descriptive=changes(pairs)))
        result["scope"] = "Same binary and same configuration at host peak; A/A noise control, not an optimization."
    if result["phase"] == "screen":
        result["memory_comparisons"] = []
        for cc in ("cubic", "bbr"):
            for arm in ("q1s0p2", "q1s0p3"):
                pairs = []
                for block in ("corrected-factor0", "corrected-factor1"):
                    group = [r for r in result["rows"] if r["cc"] == cc and r["block"] == block]
                    aa = [r for r in group if r["arm"] == "q1s0p0"]
                    bb = [r for r in group if r["arm"] == arm]
                    if len(aa) == len(bb) == 1: pairs.append((aa[0], bb[0]))
                result["memory_comparisons"].append(dict(cc=cc, arm=arm, pairs=len(pairs),
                    valid=len(pairs) == 2 and all(a["valid"] and b["valid"] for a,b in pairs), changes_descriptive=changes(pairs)))
    result["valid"] = sum(r["valid"] for r in result["rows"])
    result["all_valid"] = result["valid"] == result["completed"] == result["planned"]
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=["screen", "aa", "guard"])
    p.add_argument("out", type=Path)
    p.add_argument("--candidate", default="q1s0p0", choices=list(rig.ARMS))
    p.add_argument("--container", default="zfstack-rtt-20261023")
    p.add_argument("--binary", type=Path, default=BINARY)
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
    rows = sequence(a.phase, a.candidate)
    freeze(out, a.phase, rows, binary, candidate=a.candidate)
    if a.phase == "guard":
        original = rig.arguments
        def guard_arguments(row):
            args = original(row)
            args[args.index("--rate-mbps") + 1] = "10000"
            return args
        rig.arguments = guard_arguments
    for row in rows:
        rig.execute(out, row, a.container, binary, digest)
    save(out / "complete.json", dict(attempts=len(rows), end_unix=time.time()))
