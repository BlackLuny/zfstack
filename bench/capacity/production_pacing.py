#!/usr/bin/env python3
"""Production-core pacing against the same FIFO in every arm."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import time

import rtt_matrix as rig
from rtt_analyze import summarize, changes
from run import ROOT, save, sha, counters

PROFILES = {"off": 0, "p100": 100, "p99": 99, "p97": 97}
rig.ARMS.update({name: {"ZFBENCH_QUEUE_DISCIPLINE": "fifo"} for name in PROFILES})
original_arguments = rig.arguments


def arguments(row):
    args = original_arguments(row)
    args[args.index("--rate-mbps") + 1] = str(row.get("rate_mbps", 100))
    if not row.get("legacy_binary"):
        args += ["--iface-egress-rate", str(row["egress_rate"])]
    return args


rig.arguments = arguments


def sequence(phase, candidate):
    rng = random.Random(2026102401)
    rows = []
    if phase == "screen":
        for block in range(2):
            cells = [(cc, arm) for cc in ("cubic", "bbr") for arm in PROFILES]
            rng.shuffle(cells)
            for cc, arm in cells:
                rows.append(dict(block=f"screen-{block}", arm=arm, **rig.case(cc, 20, 200, 12)))
    else:
        if phase == "guard":
            cells = [(cc, 0, n) for cc in ("cubic", "bbr") for n in (1, 2)]
        elif phase == "confirm":
            cells = [(cc, 20, 200) for cc in ("cubic", "bbr")]
        else:
            cells = [(cc, rtt, n) for cc in ("cubic", "bbr") for rtt in rig.RTTS for n in (1, 200)]
        rng.shuffle(cells)
        for cc, rtt, n in cells:
            arms = ["off", candidate]
            rng.shuffle(arms)
            for rep in range(12 if phase == "confirm" else 2):
                for pos, arm in enumerate(arms if rep % 2 == 0 else arms[::-1]):
                    c = rig.case(cc, rtt, n, 12 if phase == "guard" else 20)
                    c["warmup"] = max(8, round(20 * rtt / 1000)) if phase != "guard" else 4
                    if phase == "guard":
                        c.update(lights=0, rate_mbps=10000)
                    rows.append(dict(block=f"{cc}-r{rtt}-n{n}-pair{rep}", position=pos, arm=arm, **c))
    for i, row in enumerate(rows):
        row.update(index=i, retry_budget=1, egress_rate=int(row.get("rate_mbps", 100) * 125000 * PROFILES[row["arm"]] / 100))
    return rows


def analyze(out):
    plan = json.loads((out / "plan.json").read_text())
    rows = [summarize(p) for p in sorted(out.glob("[0-9][0-9][0-9]-*")) if (p / "end.json").exists()]
    for row in rows:
        raw = json.loads((out / row["name"] / "stdout.json").read_text())
        if raw["config"].get("iface_egress_rate", 0) != row["egress_rate"]:
            row["errors"].append("production rate differs from frozen command")
        if any(raw["link"][d]["queue_discipline"] != "fifo-service" for d in ("up", "down")):
            row["errors"].append("link was not the FIFO control")
        shards = raw["stack_thread"]["stack_stats"]["shards"]
        row["egress_paced_bytes"] = sum(s.get("egress_paced_bytes",0) for s in shards)
        row["egress_pacing_pauses"] = sum(s.get("egress_pacing_pauses",0) for s in shards)
        if row["heavy"] == 1 and not row["lights"] and row["egress_paced_bytes"]:
            row["errors"].append("single-peer fast path did not bypass")
        row["valid"] = not row["errors"]
    comparisons = []
    for cc,rtt,n in sorted({(r["cc"],r["rtt"],r["heavy"]) for r in rows}):
        group = [r for r in rows if (r["cc"],r["rtt"],r["heavy"]) == (cc,rtt,n)]
        for arm in sorted({r["arm"] for r in group} - {"off"}):
            pairs = []
            for block in sorted({r["block"] for r in group}):
                a = [r for r in group if r["block"]==block and r["arm"]=="off"]
                b = [r for r in group if r["block"]==block and r["arm"]==arm]
                if len(a)==len(b)==1: pairs.append((a[0],b[0]))
            comparisons.append(dict(cc=cc,rtt=rtt,heavy=n,arm=arm,pairs=len(pairs),
                valid=bool(pairs) and all(a["valid"] and b["valid"] for a,b in pairs),changes=changes(pairs)))
    return dict(phase=plan["phase"],planned=len(plan["rows"]),completed=len(rows),valid=sum(r["valid"] for r in rows),rows=rows,comparisons=comparisons)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase",choices=["screen","wan","guard","confirm"])
    p.add_argument("out",type=Path)
    p.add_argument("--candidate",choices=PROFILES,default="p100")
    p.add_argument("--binary",type=Path,default=ROOT/"target/release/zfbench-production-v1-20261024")
    p.add_argument("--container",default="zfstack-production-20261024")
    p.add_argument("--analyze",action="store_true")
    a=p.parse_args()
    out=a.out.resolve()
    if a.analyze:
        print(json.dumps(analyze(out),indent=2,sort_keys=True,allow_nan=False));raise SystemExit
    out.relative_to(ROOT);out.mkdir(parents=True,exist_ok=False)
    binary=a.binary.resolve();digest=sha(binary)
    sources={}
    for parent,glob in [(ROOT/"src","*.rs"),(ROOT/"bench/src","*.rs"),(ROOT/"bench/capacity","*.py"),(ROOT/"docs/experiments/2026-10-24-production-pacing","*.md")]:
        for f in sorted(parent.rglob(glob)):sources[str(f.relative_to(ROOT))]=sha(f)
    for name in sources:
        dest=out/"source"/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    save(out/"plan.json",dict(phase=a.phase,rows=sequence(a.phase,a.candidate),binary_sha256=digest,sources=sources,environment=counters(),
        base_sha=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()))
    for row in json.loads((out/"plan.json").read_text())["rows"]:rig.execute(out,row,a.container,binary,digest)
    save(out/"complete.json",dict(end_unix=time.time()))
