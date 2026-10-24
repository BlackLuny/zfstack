#!/usr/bin/env python3
"""Independent 120-second steady single-flow confirmation, no retuning."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import time

import production_pacing as pacing
import rtt_matrix as native
from run import ROOT,save,sha,counters


def sequence():
    arms=['off','p99'];random.Random(2026102413).shuffle(arms)
    return [dict(index=2*rep+position,block=f'pair{rep:02d}',position=position,arm=arm,
        cc='bbr',rtt=500,heavy=1,lights=0,warmup=20,measure=120,retry_budget=1,
        egress_rate=0 if arm=='off' else 12_375_000,tokio_stack=True,splice=True)
        for rep in range(12) for position,arm in enumerate(arms if rep%2==0 else arms[::-1])]


def run(out,container):
    out.relative_to(ROOT);out.mkdir(parents=True,exist_ok=False)
    binary=ROOT/'target/release/zfbench-production-v4-20261024';digest=sha(binary)
    sources={str(f.relative_to(ROOT)):sha(f) for parent,pattern in [(ROOT/'src','*.rs'),(ROOT/'bench/src','*.rs'),(ROOT/'bench/capacity','*.py'),(ROOT/'docs/experiments/2026-10-24-production-pacing','*.md')] for f in sorted(parent.rglob(pattern))}
    for name in sources:
        dest=out/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    save(out/'plan.json',dict(phase='steady-single',rows=sequence(),binary_sha256=digest,sources=sources,
        base_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),environment=counters()))
    for row in json.loads((out/'plan.json').read_text())['rows']:native.execute(out,row,container,binary,digest)
    save(out/'complete.json',dict(end_unix=time.time()))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('out',type=Path)
    p.add_argument('--container',default='zfstack-production-20261024');p.add_argument('--analyze',action='store_true')
    a=p.parse_args()
    if a.analyze:print(json.dumps(pacing.analyze(a.out.resolve()),indent=2))
    else:run(a.out.resolve(),a.container)
