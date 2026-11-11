#!/usr/bin/env python3
"""Few-flow unshaped guards for a selected prefetch profile, same binary."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import time

import rtt_headroom  # registers the wider, original-budget profile
import rtt_matrix as rig
import run as old
from analyze import summarize_run
from rtt_analyze import summarize, changes
from run import ROOT, save, sha, counters

CASES = ["server_cubic_1", "server_cubic_2", "server_bbr_1"]
original_arguments = rig.arguments


def arguments(row):
    args = original_arguments(row)
    args[args.index("--rate-mbps") + 1] = "10000"
    return args


def sequence(candidate):
    rng = random.Random(2026102370)
    cells = list(CASES)
    rng.shuffle(cells)
    rows = []
    for cell in cells:
        kind, mode, n = cell.split("_")
        arms = [rig.BASE, candidate]
        if rng.choice([True, False]): arms.reverse()
        for rep in range(2):
            for pos, arm in enumerate(arms if rep == 0 else arms[::-1]):
                row = dict(case=cell, block=f"{cell}-pair{rep}", position=pos, arm=arm,
                           **rig.case(mode if kind == "server" else "cubic", 0, int(n), 12))
                row["lights"] = 0
                rows.append(row)
    return [dict(index=i, **r) for i, r in enumerate(rows)]


def analyze(out):
    rows = []
    for p in sorted(out.glob("[0-9][0-9][0-9]-*")):
        if not (p / "end.json").exists(): continue
        start = json.loads((p / "start.json").read_text())
        if start["row"]["case"].startswith("client"):
            r = summarize_run(p)
            raw = json.loads((p / "stdout.json").read_text())
            expected_min = 262144 if r["arm"].endswith("p3") else None
            if raw["trial_tuning"]["min_prefetch_bytes"] != expected_min:
                r["errors"].append("client trial profile mismatch")
                r["valid"] = False
            r["stack_cpu_s_per_gb"] = r["service_cpu_s_per_gb"]
            r["stack_cpu_s_per_s"] = r["service_cpu_s_per_s"]
        else:
            r = summarize(p)
            raw = json.loads((p / "stdout.json").read_text())
            if raw["config"]["rate_mbps"] != 10000 or raw["config"]["rtt_ms"] != 0:
                r["errors"].append("not a host-throughput guard")
                r["valid"] = False
        rows.append(r)
    comparisons = []
    for case in CASES:
        pairs = []
        for rep in range(2):
            group = sorted([r for r in rows if r["block"] == f"{case}-pair{rep}"], key=lambda r:r["arm"])
            if len(group) == 2: pairs.append(group)
        comparisons.append(dict(case=case, pairs=len(pairs), valid=len(pairs) == 2 and all(a["valid"] and b["valid"] for a,b in pairs),
                                changes_descriptive=changes(pairs)))
    return dict(planned=4 * len(CASES), completed=len(rows), valid=sum(r["valid"] for r in rows), rows=rows, comparisons=comparisons)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--candidate", default="q1s0p0", choices=["q1s0p0", "q1s0p3"])
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
    digest = sha(binary)
    sources = {str(f.relative_to(ROOT)): sha(f) for parent in [ROOT / "src", ROOT / "bench/src"] for f in sorted(parent.rglob("*.rs"))}
    for f in [Path(__file__), ROOT / "bench/capacity/rtt_headroom.py", ROOT / "bench/capacity/rtt_memory.py", ROOT / "bench/capacity/rtt_matrix.py", ROOT / "bench/capacity/run.py", ROOT / "docs/experiments/2026-10-23-rtt/GUARDS.md"]:
        sources[str(f.relative_to(ROOT))] = sha(f)
    rows = sequence(a.candidate)
    save(out / "plan.json", dict(phase="guard", candidate=a.candidate, rows=rows, arms=rig.ARMS,
                                sources=sources, binary_sha256=digest, environment=counters(),
                                base_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()))
    rig.arguments = arguments
    old.ARMS.update(rig.ARMS)
    for row in rows:
        if row["case"].startswith("server"):
            rig.execute(out, row, a.container, binary, digest)
        else:
            _, test, count = row["case"].split("_")
            args = ["--client-proxy", "zfstack-client", "--splice", "--driver-thread", "--proxy-workers", "2",
                    "--tun-mtu", "65535", "--flows", count, "--secs", "16", "--warmup", "4", "--test", test]
            name = f"{row['index']:03d}-{row['case']}-{row['arm']}"
            old.execute(out, name, row, a.container, binary, digest, extra=args)
    save(out / "complete.json", dict(attempts=len(rows), end_unix=time.time()))
