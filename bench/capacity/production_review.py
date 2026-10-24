#!/usr/bin/env python3
"""Fixed V4 review confirmation, preserving the complete prior V3 study."""
import argparse
import json
import random
import subprocess
import time
from pathlib import Path

import production_pacing as pacing
import production_peaks as peaks
import production_observe as observed
import rtt_matrix as native
import run as client
from run import ROOT,save,sha,counters


def sequence(phase):
    rng=random.Random(2026102409)
    cells=[('cubic',100,1),('bbr',500,1)] if phase=='wan' else [('cubic',20,200),('bbr',20,200)]
    rng.shuffle(cells);rows=[]
    for cc,rtt,n in cells:
        arms=['off','p99'];rng.shuffle(arms)
        for rep in range(12):
            for pos,arm in enumerate(arms if rep%2==0 else arms[::-1]):
                row=native.case(cc,rtt,n,40 if phase=='wan' else 20)
                row.update(index=len(rows),block=f'{cc}-r{rtt}-n{n}-pair{rep:02d}',position=pos,arm=arm,
                    warmup=max(8,round(40*rtt/1000)),retry_budget=1,
                    egress_rate=0 if arm=='off' else 12_375_000,tokio_stack=True,splice=True)
                if phase=='loaded':row['rr_cpu']=4
                rows.append(row)
    return rows


def peak_sequence():
    rng=random.Random(2026102411);cases=[1,2];rng.shuffle(cases);rows=[]
    for n in cases:
        arms=['A','B'];rng.shuffle(arms)
        for rep in range(12):
            for pos,arm in enumerate(arms if rep%2==0 else arms[::-1]):
                rows.append(dict(index=len(rows),case=f'client_down{n}',test='down',flows=n,
                    block=f'pair{rep:02d}',position=pos,arm=arm))
    return rows


def run(out,phase,container):
    out.relative_to(ROOT);out.mkdir(parents=True,exist_ok=False)
    binary=ROOT/'target/release/zfbench-production-v4-20261024'
    binaries={'A':ROOT/'target/release/zfbench-rtt-final-20261023','B':binary}
    sources={str(f.relative_to(ROOT)):sha(f) for parent,pattern in [(ROOT/'src','*.rs'),(ROOT/'bench/src','*.rs'),(ROOT/'bench/capacity','*.py'),(ROOT/'docs/experiments/2026-10-24-production-pacing','*.md')] for f in sorted(parent.rglob(pattern))}
    for name in sources:
        dest=out/'source'/name;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/name).read_bytes())
    if phase=='peak':
        rows=peak_sequence()
        digests={k:sha(v) for k,v in binaries.items()}
    else:
        rows=sequence(phase);digests=sha(binary)
    save(out/'plan.json',dict(phase='review-'+phase,rows=rows,binary_sha256=digests,sources=sources,
        base_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),environment=counters()))
    if phase=='peak':
        client.ARMS.update(A={},B={})
        for row in rows:
            args=['--client-proxy','zfstack-client','--splice','--driver-thread','--proxy-workers','2',
                '--tun-mtu','65535','--secs','16','--warmup','4','--flows',str(row['flows']),'--test','down']
            if row['arm']=='B':args+=['--iface-egress-rate','12375000']
            name=f"{row['index']:03d}-{row['case']}-{row['block']}-{row['position']}-{row['arm']}"
            client.execute(out,name,row,container,binaries[row['arm']],digests[row['arm']],extra=args)
    else:
        if phase=='loaded':
            for arm in ['off','p99']:native.ARMS[arm]['ZFBENCH_RR_CPU']='4'
        for row in rows:native.execute(out,row,container,binary,digests)
    save(out/'complete.json',dict(end_unix=time.time()))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['wan','peak','loaded']);p.add_argument('out',type=Path)
    p.add_argument('--container',default='zfstack-production-20261024');p.add_argument('--analyze',action='store_true')
    a=p.parse_args()
    if a.analyze:print(json.dumps((peaks.analyze if a.phase=='peak' else observed.analyze if a.phase=='loaded' else pacing.analyze)(a.out.resolve()),indent=2))
    else:run(a.out.resolve(),a.phase,a.container)
