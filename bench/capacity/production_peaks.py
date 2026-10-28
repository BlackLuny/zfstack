#!/usr/bin/env python3
"""Cross-binary guards on the actual TUN/Tokio/splice client path."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import time

from analyze import summarize_run
from confirm import interval
import run as rig


def sequence():
    cases = [("down",1,12),("down",2,6),("up",1,6),("mixed",1,6)]
    random.Random(2026102403).shuffle(cases)
    rng=random.Random(2026102404)
    rows=[]
    for test,n,repeats in cases:
        arms=["A","B"];rng.shuffle(arms)
        for rep in range(repeats):
            for pos,arm in enumerate(arms if rep%2==0 else arms[::-1]):
                rows.append(dict(index=len(rows),case=f"client_{test}{n}",test=test,flows=n,block=f"pair{rep:02d}",position=pos,arm=arm))
    return rows


def analyze(out):
    plan=json.loads((out/'plan.json').read_text())
    rows=[summarize_run(p) for p in sorted(out.glob('[0-9][0-9][0-9]-*')) if (p/'end.json').exists()]
    comparisons=[]
    for case in sorted({r['case'] for r in rows}):
        pairs=[]
        for block in sorted({r['block'] for r in rows if r['case']==case}):
            a=[r for r in rows if (r['case'],r['block'],r['arm'])==(case,block,'A')]
            b=[r for r in rows if (r['case'],r['block'],r['arm'])==(case,block,'B')]
            if len(a)==len(b)==1:pairs.append((a[0],b[0]))
        metrics={}
        for key in ['mbps','service_cpu_s_per_gb','service_cpu_s_per_s','rss_hwm_kib']:
            values=[100*(b[key]/a[key]-1) for a,b in pairs if a.get(key) and b.get(key) is not None]
            if values:metrics[key]=interval(values)
        comparisons.append(dict(case=case,pairs=len(pairs),valid=bool(pairs) and all(a['valid'] and b['valid'] for a,b in pairs),changes=metrics))
    return dict(planned=len(plan['rows']),completed=len(rows),valid=sum(r['valid'] for r in rows),rows=rows,comparisons=comparisons)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('out',type=Path)
    p.add_argument('--container',default='zfstack-production-20261024')
    p.add_argument('--analyze',action='store_true')
    a=p.parse_args();out=a.out.resolve()
    if a.analyze:print(json.dumps(analyze(out),indent=2));raise SystemExit
    out.relative_to(rig.ROOT);out.mkdir(parents=True,exist_ok=False)
    binaries={'A':rig.ROOT/'target/release/zfbench-rtt-final-20261023','B':rig.ROOT/'target/release/zfbench-production-v3-20261024'}
    digests={arm:rig.sha(path) for arm,path in binaries.items()}
    rig.ARMS.update(A={},B={})
    rows=sequence()
    sources={str(f.relative_to(rig.ROOT)):rig.sha(f) for parent in [rig.ROOT/'src',rig.ROOT/'bench/src'] for f in sorted(parent.rglob('*.rs'))}
    sources[str(Path(__file__).relative_to(rig.ROOT))]=rig.sha(Path(__file__))
    rig.save(out/'plan.json',dict(rows=rows,binaries={arm:str(p.relative_to(rig.ROOT)) for arm,p in binaries.items()},binary_sha256=digests,sources=sources,
        base_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=rig.ROOT,text=True).strip(),environment=rig.counters()))
    for row in rows:
        args=['--client-proxy','zfstack-client','--splice','--driver-thread','--proxy-workers','2','--tun-mtu','65535',
              '--secs','16','--warmup','4','--flows',str(row['flows']),'--test',row['test']]
        if row['arm']=='B':args+=['--iface-egress-rate','12375000']
        name=f"{row['index']:03d}-{row['case']}-{row['block']}-{row['position']}-{row['arm']}"
        rig.execute(out,name,row,a.container,binaries[row['arm']],digests[row['arm']],extra=args)
    rig.save(out/'complete.json',dict(end_unix=time.time()))
