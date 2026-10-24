#!/usr/bin/env python3
"""V5 cold-state/outlined-loop continuation with immutable independent cohorts."""
import argparse
import itertools
import json
from pathlib import Path
import random
import statistics
import subprocess
import time

from analyze import summarize_run
from confirm import interval
import production_pacing as pacing
import production_observe as observed
import rtt_matrix as native
import run as client
from run import ROOT,save,sha,counters

BINARIES={
    'old':ROOT/'target/release/zfbench-rtt-final-20261023',
    'v4':ROOT/'target/release/zfbench-production-v4-20261024',
    'v5':ROOT/'target/release/zfbench-production-v5-20261024',
}
PROTOCOL=ROOT/'docs/experiments/2026-10-24-pacing-fast-path/PROTOCOL.md'


def sequence(phase):
    rng=random.Random(2026102419);rows=[]
    if phase in ['smoke','loaded','wan']:
        rows=pacing.sequence('confirm' if phase=='loaded' else phase,'p99')
        for row in rows:
            row.update(binary='v5',tokio_stack=True,splice=True)
            if row['lights']:row['rr_cpu']=4
        return rows
    cells=[(n,pin) for n in [1,2] for pin in ([False,True] if phase=='aa' else [phase in ['pin','preflight']])]
    if phase=='preflight':cells=[(1,True)]
    rng.shuffle(cells)
    for n,pin in cells:
        if phase=='peak':
            orders=list(itertools.permutations(['old','v4','v5']))*2;rng.shuffle(orders)
        elif phase=='preflight':
            orders=[['A','B']]
        else:
            arms=['A','B'];rng.shuffle(arms)
            count=1 if phase=='preflight' else 3 if phase=='aa' else 12
            orders=[arms if i%2==0 else arms[::-1] for i in range(count)]
        for block,order in enumerate(orders):
            for pos,arm in enumerate(order):
                binary=arm if phase=='peak' else 'old' if phase=='aa' or arm=='A' else 'v5'
                rows.append(dict(index=len(rows),case=f'client_down{n}_'+('pin' if pin else 'shared'),
                    block=f'block{block:02d}',position=pos,arm=arm,binary=binary,
                    test='down',flows=n,pinned=pin,egress_rate=0 if binary=='old' else 12_375_000))
    return rows


def peak_args(row):
    args=['--client-proxy','zfstack-client','--splice','--driver-thread','--proxy-workers','2',
        '--tun-mtu','65535','--secs','16','--warmup','4','--flows',str(row['flows']),'--test','down']
    if row['binary']!='old':args+=['--iface-egress-rate','12375000']
    return args


def execute_pinned(out,name,row,container,binary,digest):
    dest=out/name;dest.mkdir();rel=dest.relative_to(ROOT)
    argv=['docker','exec',container,'timeout','-k','5','150','python3','/work/bench/capacity/fastpath_affinity.py',
        f'/work/{rel}/affinity.json',f'/work/{rel}/resources.json',f'/work/{binary.relative_to(ROOT)}']+peak_args(row)+['--label',name]
    assert sha(binary)==digest
    save(dest/'start.json',dict(row=row,argv=argv,env={},binary_sha256=digest,start_unix=time.time(),cgroup=counters()))
    with (dest/'stdout.json').open('x') as stdout,(dest/'stderr.txt').open('x') as stderr:
        try:rc=subprocess.run(argv,stdout=stdout,stderr=stderr,timeout=165).returncode
        except subprocess.TimeoutExpired:rc='outer_timeout'
    save(dest/'end.json',dict(returncode=rc,end_unix=time.time(),cgroup=counters(),binary_unchanged=sha(binary)==digest))
    print(json.dumps(dict(name=name,returncode=rc)),flush=True)
    if rc in [124,137,'outer_timeout'] or sha(binary)!=digest:raise RuntimeError('bounded/identity failure; no retry')


def run(out,phase,container):
    out.relative_to(ROOT);out.mkdir(parents=True,exist_ok=False)
    sources={str(f.relative_to(ROOT)):sha(f) for parent,glob in [(ROOT/'src','*.rs'),(ROOT/'bench/src','*.rs'),(ROOT/'bench/capacity','*.py')] for f in sorted(parent.rglob(glob))}
    for f in [ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'bench/Cargo.toml',PROTOCOL]:sources[str(f.relative_to(ROOT))]=sha(f)
    for name in sources:
        target=out/'source'/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((ROOT/name).read_bytes())
    digests={tag:sha(binary) for tag,binary in BINARIES.items()}
    rows=sequence(phase)
    save(out/'plan.json',dict(phase=phase,rows=rows,binary_sha256=digests,binaries={k:str(v.relative_to(ROOT)) for k,v in BINARIES.items()},
        sources=sources,base_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),environment=counters()))
    for row in rows:
        tag=row['binary'];binary=BINARIES[tag];digest=digests[tag]
        if phase in ['smoke','loaded','wan']:
            native.ARMS[row['arm']].update(ZFBENCH_RR_CPU='4') if row['lights'] else native.ARMS[row['arm']].pop('ZFBENCH_RR_CPU',None)
            native.execute(out,row,container,binary,digest)
        else:
            name=f"{row['index']:03d}-{row['case']}-{row['block']}-{row['position']}-{row['arm']}"
            if row['pinned']:execute_pinned(out,name,row,container,binary,digest)
            else:
                client.ARMS[row['arm']]={}
                client.execute(out,name,row,container,binary,digest,extra=peak_args(row))
    save(out/'complete.json',dict(end_unix=time.time()))


def audit(out,result):
    plan=json.loads((out/'plan.json').read_text());errors=[]
    if result['completed']!=len(plan['rows']) or not (out/'complete.json').exists():errors.append('incomplete cohort')
    for name,digest in plan['sources'].items():
        if sha(out/'source'/name)!=digest:errors.append('source mismatch: '+name)
    by_index={r['index']:r for r in plan['rows']}
    for row in result['rows']:
        dest=out/row['name'];start=json.loads((dest/'start.json').read_text());end=json.loads((dest/'end.json').read_text())
        planned=by_index[row['index']];tag=planned['binary'];digest=plan['binary_sha256'][tag]
        if start['row']!=planned or start['binary_sha256']!=digest or not end['binary_unchanged'] or sha(ROOT/plan['binaries'][tag])!=digest:
            errors.append(row['name']+': row/binary identity differs')
        argv=start['argv'];raw=json.loads((dest/'stdout.json').read_text());cfg=raw['config']
        if f"/work/{plan['binaries'][tag]}" not in argv:errors.append(row['name']+': invoked binary differs')
        if 'flows' in row:
            expected=peak_args(planned)+['--label',row['name']]
            if argv[-len(expected):]!=expected:errors.append(row['name']+': peak invocation differs')
            for k,value in dict(proxy='zfstack-client',test='down',flows=row['flows'],splice=True,driver_thread=True,proxy_workers=2,tun_mtu=65535,secs=16,warmup=4).items():
                if cfg.get(k)!=value:errors.append(row['name']+': peak config '+k)
            if cfg.get('iface_egress_rate',0)!=planned['egress_rate']:errors.append(row['name']+': peak bypass rate differs')
        else:
            expected=native.arguments(planned)+['--label',row['name']]
            if argv[-len(expected):]!=expected:errors.append(row['name']+': native invocation differs')
            for k,value in dict(stack='zfstack-'+row['cc'],test='isolate',lights=row['lights'],heavy_conns=row['heavy'],rtt_ms=row['rtt'],rate_mbps=100,rate_up_mbps=100,sock_buf_kb=16384,secs=row['warmup']+row['measure'],warmup=row['warmup'],iface_egress_rate=row['egress_rate'],loss=0,loss_up=0).items():
                if cfg.get(k)!=value:errors.append(row['name']+': native config '+k)
            for d in ['up','down']:
                link=raw['link'][d]
                if link['queue_discipline']!='fifo-service' or link['params']['queue_limit_bytes']!=250000 or link['params']['rate_bps']!=100_000_000 or link['params']['delay_us']!=row['rtt']*500:
                    errors.append(row['name']+': physical FIFO/RTT differs')
            if row['lights']:
                streams=json.loads((dest/'arrivals.json').read_text())
                if len(streams)!=6 or any(s.get(k)!=4 for s in streams for k in ['requested_cpu','observer_cpu','sender_cpu']):errors.append(row['name']+': actual RR CPU differs')
            elif row['egress_paced_bytes']:errors.append(row['name']+': single-peer pacing intervened')
    return dict(identity_valid=not errors,errors=errors)


def analyze(out):
    plan=json.loads((out/'plan.json').read_text());phase=plan['phase']
    if phase in ['smoke','loaded','wan']:
        result=(observed.analyze if phase in ['smoke','loaded'] else pacing.analyze)(out)
    else:
        rows=[summarize_run(p) for p in sorted(out.glob('[0-9][0-9][0-9]-*')) if (p/'end.json').exists()]
        for row in rows:
            if row['pinned']:
                ledger=json.loads((out/row['name']/'affinity.json').read_text())
                if not ledger['valid']:row['errors']+=['controlled affinity invalid: '+e for e in ledger['errors']]
                row['valid']=row['valid'] and ledger['valid'];row['observer_cpu_sec']=ledger['observer_cpu_sec']
        comparisons=[]
        for case in sorted({r['case'] for r in rows}):
            kinds=[('old','v4'),('old','v5'),('v4','v5')] if phase=='peak' else [('A','B')]
            for left,right in kinds:
                pairs=[]
                for block in sorted({r['block'] for r in rows if r['case']==case}):
                    group=[r for r in rows if (r['case'],r['block'])==(case,block)]
                    a=[r for r in group if r['arm']==left];b=[r for r in group if r['arm']==right]
                    if len(a)==len(b)==1:pairs.append((a[0],b[0]))
                changes={}
                for k in ['mbps','service_cpu_s_per_gb','service_cpu_s_per_s','rss_hwm_kib']:
                    values=[100*(b[k]/a[k]-1) for a,b in pairs if a.get(k) and b.get(k) is not None]
                    if values:changes[k]=interval(values) if len(values)>=6 else dict(paired_pct=values,median_pct=statistics.median(values))
                comparisons.append(dict(case=case,left=left,right=right,pairs=len(pairs),valid=bool(pairs) and all(a['valid'] and b['valid'] for a,b in pairs),changes=changes))
        result=dict(phase=phase,planned=len(plan['rows']),completed=len(rows),valid=sum(r['valid'] for r in rows),rows=rows,comparisons=comparisons)
    result['identity_audit']=audit(out,result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['smoke','preflight','aa','peak','loaded','wan','pin']);p.add_argument('out',type=Path)
    p.add_argument('--container',default='zfstack-fastpath-20261024');p.add_argument('--analyze',action='store_true');a=p.parse_args()
    if a.analyze:print(json.dumps(analyze(a.out.resolve()),indent=2))
    else:run(a.out.resolve(),a.phase,a.container)
