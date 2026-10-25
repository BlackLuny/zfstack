#!/usr/bin/env python3
"""Frozen BBR/unknown-capacity experiments; explicit disturbances never reach core."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import time

import production_pacing
import rtt_matrix as native
from analyze import summarize_run
from common_cases import observer_audit
from common_reaudit import independent_masks, rr_transition_error, startup_transition_error
from rtt_analyze import summarize
from run import ROOT, counters, save, sha

DIRECTORY = ROOT/'docs/experiments/2026-10-25-adaptive-pacing'
PREP = ROOT/'qa/results/adaptive-20261025-preparation'
CONTAINER = 'zfstack-adaptive-20261025'
PHASES = ['aa', 'primary', 'bandwidth', 'heavy', 'disabled', 'peak', 'dynamic', 'demand']
V5 = ('target/release/zfbench-production-v5-20261024', '96695f880dd600172e3dfa5b32304da0fa296579357785b5f83dbb87ea1a26c5')


def binaries():
    b=json.loads((PREP/'build-a0.json').read_text())
    return dict(a0=(b['binary'],b['binary_sha256']),v5=V5)


def sequence(phase):
    rng=random.Random(2026102505+(['pilot']+PHASES).index(phase))
    cells=[]
    def cell(rate,rtt,n,kind='steady',pairs=8):
        return dict(kind='native',case=f'{kind}-m{rate}-r{rtt}-n{n}',rate_mbps=rate,cc='bbr',rtt=rtt,heavy=n,
            lights=0 if n==1 else 3,placement='fixed',warmup=max(8,round(40*rtt/1000)),measure=50 if kind in ['dynamic','demand'] else 20,
            pairs=pairs,scenario=kind,tokio_stack=True,splice=True,retry_budget=1,egress_rate=0)
    if phase=='pilot':
        cells=[cell(100,rtt,n,pairs=1) for rtt in [1,500] for n in [1,8]]
        cells += [cell(100,50,8,kind=k,pairs=1) for k in ['dynamic','demand']]
    elif phase in ['primary','aa']:
        cells=[cell(100,rtt,n,pairs=8 if phase=='primary' else 2) for rtt in ([1,10,50,100,250,500] if phase=='primary' else [1,500]) for n in ([1,8] if phase=='primary' else [1])]
    elif phase=='bandwidth':cells=[cell(rate,rtt,8,pairs=4) for rate in [10,300] for rtt in [50,500]]
    elif phase=='heavy':cells=[cell(100,20,200,pairs=8)]
    elif phase=='disabled':cells=[cell(100,rtt,1,pairs=4) for rtt in [1,500]]
    elif phase=='dynamic':cells=[cell(100,rtt,8,kind='dynamic',pairs=4) for rtt in [50,500]]
    elif phase=='demand':cells=[cell(100,50,8,kind='demand',pairs=4)]
    elif phase=='peak':
        cells=[dict(kind='peak',case=f'peak-down{n}',flows=n,placement='original',warmup=8,measure=30,pairs=6,scenario='peak',egress_rate=0) for n in [1,2]]
    orders={c['case']:['AB']*(c['pairs']//2)+['BA']*(c['pairs']//2) if c['pairs']>1 else [rng.choice(['AB','BA'])] for c in cells}
    for values in orders.values():rng.shuffle(values)
    rows=[]
    for rep in range(max(c['pairs'] for c in cells)):
        ordered=list(cells);rng.shuffle(ordered)
        for c in ordered:
            if rep>=c['pairs']:continue
            for position,letter in enumerate(orders[c['case']][rep]):
                control=letter=='A'
                binary='v5' if control and phase in ['peak','disabled'] else 'a0'
                mode='adaptive' if not control and phase not in ['aa','disabled'] else 'off'
                arm=('old' if control else 'new-off') if phase=='disabled' else ('old' if control else 'auto') if phase=='peak' else ('off' if control else 'off-repeat') if phase=='aa' else ('off' if control else 'auto')
                rows.append(dict(index=len(rows),**{k:v for k,v in c.items() if k!='pairs'},round=rep,position=position,
                    block=f"{c['case']}-r{rep:02d}",arm=arm,binary=binary,mode=mode))
    return rows


def name(row):return f"{row['index']:04d}-{row['block']}-{row['position']}-{row['arm']}"


def command(out,row,container=CONTAINER):
    rel=(out/name(row)).relative_to(ROOT)
    env={}
    if row['kind']=='native':
        env=dict(ZFBENCH_QUEUE_DISCIPLINE='fifo',ZFBENCH_RR_RAW='1',ZFBENCH_RR_OPEN_HZ='25',
            ZFBENCH_RR_ARRIVALS=f'/work/{rel}/arrivals.json',ZFBENCH_CONNECT_PARALLEL='32',
            ZFBENCH_HEAVY_PROGRESS='1',ZFBENCH_RETRY_BUDGET_WRITERS='1')
        if row['lights']:env['ZFBENCH_RR_CPU']='4'
        if row['scenario']=='dynamic':env.update(ZFBENCH_LINK_RATE_STEPS='10000:20000000,25000:100000000',ZFBENCH_ADAPTIVE_TRACE='1')
        if row['scenario']=='demand':env.update(ZFBENCH_DOWN_PAUSE_MS='8000:28000',ZFBENCH_ADAPTIVE_TRACE='1')
        args=native.arguments(row)
    else:
        args=['--client-proxy','zfstack-client','--splice','--driver-thread','--proxy-workers','2','--tun-mtu','65535',
            '--secs',str(row['warmup']+row['measure']),'--warmup',str(row['warmup']),'--flows',str(row['flows']),'--test','down','--iface-egress-rate','0']
    if row['mode']=='adaptive':args+=['--adaptive-egress']
    binary=binaries()[row['binary']][0]
    argv=['docker','--host=unix:///var/run/docker.sock','exec',container,'env']+[k+'='+v for k,v in env.items()]
    argv+=['timeout','-k','5','180','python3','/work/bench/capacity/common_env.py',row['kind'],row['placement'],str(int(bool(row.get('lights',0)))),
        f'/work/{rel}/environment.json',f'/work/{rel}/resources.json','/work/'+binary]
    return argv+args+['--label',name(row)],env


def inspect(out,row,container=CONTAINER):
    dest=out/name(row)
    result=(summarize if row['kind']=='native' else summarize_run)(dest)
    raw=json.loads((dest/'stdout.json').read_text());start=json.loads((dest/'start.json').read_text());end=json.loads((dest/'end.json').read_text())
    observer=json.loads((dest/'environment.json').read_text())
    audit=observer_audit(observer,row);masks=independent_masks(observer,row)
    certified=[e for e in observer['errors'] if rr_transition_error(e,row,observer) or startup_transition_error(e,row,observer)]
    raw_process_ok=observer['returncode']==0 and raw.get('ok') is True
    placement_ok=masks['valid'] and not set(observer['errors']).difference(certified)
    errors=[];argv,env=command(out,row,container);path,digest=binaries()[row['binary']]
    if start['argv']!=argv or start['env']!=env or start['row']!=row or start['binary_sha256']!=digest or not end['binary_unchanged'] or sha(ROOT/path)!=digest:errors.append('invocation/binary differs')
    cfg=raw['config']
    expected=dict(secs=row['warmup']+row['measure'],warmup=row['warmup'],iface_egress_rate=0)
    if bool(cfg.get('adaptive_egress',False))!=(row['mode']=='adaptive'):errors.append('adaptive selection differs')
    if row['kind']=='native':
        expected.update(stack='zfstack-bbr',test='isolate',ports=1,heavy_conns=row['heavy'],lights=row['lights'],rtt_ms=row['rtt'],rate_mbps=row['rate_mbps'],rate_up_mbps=row['rate_mbps'],mtu=1420,sock_buf_kb=16384,seed=23,loss=0,loss_up=0,tokio_stack=True,tokio_splice=True)
        stats=raw['stack_thread']['stack_stats'];shards=stats['shards']
        if stats['impl']!='zfstack-tokio' or not stats['production_splice'] or stats['reserved_after_shutdown'] or stats['fixture_app_errors'] or stats['kernel_upstream_threads_after_shutdown']:errors.append('native payload/teardown')
        result.update(egress_paced_bytes=sum(s['egress_paced_bytes'] for s in shards),egress_pacing_pauses=sum(s['egress_pacing_pauses'] for s in shards),
            adaptive_updates=sum(s.get('adaptive_egress_updates',0) for s in shards),adaptive_probes=sum(s.get('adaptive_egress_probes',0) for s in shards),
            adaptive_last_rate=shards[0].get('adaptive_egress_last_rate',0),adaptive_last_delivery_rate=shards[0].get('adaptive_egress_last_delivery_rate',0),adaptive_last_queue_us=shards[0].get('adaptive_egress_last_queue_us',0))
        if row['heavy']==1 and (result['egress_paced_bytes'] or result['adaptive_updates']):errors.append('single peer did not bypass')
        if row['mode']=='off' and result['adaptive_updates']:errors.append('disabled controller executed')
        if row['mode']=='adaptive' and row['lights'] and not result['adaptive_updates']:errors.append('controller never learned')
        for direction in ['up','down']:
            link=raw['link'][direction];params=link['params']
            if link['queue_discipline']!='fifo-service' or params['queue_limit_bytes']!=250000 or params['rate_bps']!=row['rate_mbps']*1_000_000 or params['delay_us']!=row['rtt']*500 or params['loss']!=0:errors.append('external FIFO configuration differs')
            events=link.get('rate_changes',[])
            if row['scenario']=='dynamic':
                if len(events)!=2 or [(x['planned_ms'],x['rate_bps']) for x in events]!=[(10000,20000000),(25000,100000000)] or any(x['actual_ms']<x['planned_ms'] or x['actual_ms']-x['planned_ms']>100 for x in events):errors.append('capacity schedule execution differs')
            elif events:errors.append('unexpected capacity change')
        if row['lights']:
            streams=json.loads((dest/'arrivals.json').read_text())
            if len(streams)!=6 or any(s.get(k)!=4 for s in streams for k in ['requested_cpu','observer_cpu','sender_cpu']):errors.append('RR CPU policy differs')
        result.update(window_steal_seconds=raw['cpu']['system_window']['steal_sec'],window_softirq_seconds=raw['cpu']['system_window']['softirq_sec'])
    else:
        expected.update(proxy='zfstack-client',test='down',flows=row['flows'],splice=True,driver_thread=True,proxy_workers=2,tun_mtu=65535)
    for k,v in expected.items():
        if cfg.get(k)!=v:errors.append('configuration differs: '+k)
    if result['nr_throttled'] or result['throttled_usec']:errors.append('cgroup throttling')
    strict=result['valid'] and audit['valid'] and not errors
    # Preserve the original outer/strict result. The second view only admits
    # predeclared, independently proved observer races, never generator misses.
    independently_observed=placement_ok and raw_process_ok and not errors and not result.get('missing_requests',0) and (result['valid'] or (certified and result['errors']==['process or identity failed']))
    # The summarizer's valid bit uses the wrapper RC, whereas observer errors
    # affect the outer docker RC. Its normal errors may already be empty.
    if placement_ok and raw_process_ok and not errors and not result.get('missing_requests',0) and not result.get('errors'):
        independently_observed=True
    result.update(original_strict_valid=strict,observation_valid=independently_observed,strict_valid=independently_observed,
        audit_errors=sorted(set(errors)),telemetry=audit,independent_masks=masks,certified_observer_errors=certified,
        original_outer_returncode=end['returncode'],original_observer_errors=observer['errors'])
    return result


def run(phase):
    out=ROOT/'qa/results'/('adaptive-20261025-'+phase)
    out.mkdir(parents=True,exist_ok=False)
    build=json.loads((PREP/'build-a0.json').read_text())
    for path,digest in build['sources'].items():
        if sha(ROOT/path)!=digest:raise RuntimeError('candidate source changed: '+path)
    for path,digest in binaries().values():
        if sha(ROOT/path)!=digest:raise RuntimeError('frozen executable changed')
    rows=sequence(phase)
    sources={str(f.relative_to(ROOT)):sha(f) for parent,pattern in [(ROOT/'src','*.rs'),(ROOT/'bench/src','*.rs'),(ROOT/'bench/capacity','*.py'),(DIRECTORY,'*.md')] for f in sorted(parent.rglob(pattern))}
    for f in ['Cargo.toml','Cargo.lock','bench/Cargo.toml','bench/tail69_exec.py']:sources[f]=sha(ROOT/f)
    for path in sources:
        dest=out/'source'/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/path).read_bytes())
    save(out/'plan.json',dict(phase=phase,rows=rows,sources=sources,binaries=binaries(),container=CONTAINER,base_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),environment=counters()))
    for row in rows:
        dest=out/name(row);dest.mkdir();argv,env=command(out,row);binary,digest=binaries()[row['binary']]
        save(dest/'start.json',dict(row=row,argv=argv,env=env,binary_sha256=digest,start_unix=time.time(),cgroup=counters()))
        with (dest/'stdout.json').open('x') as stdout,(dest/'stderr.txt').open('x') as stderr:
            try:rc=subprocess.run(argv,stdout=stdout,stderr=stderr,timeout=195).returncode
            except subprocess.TimeoutExpired:rc='outer_timeout'
        save(dest/'end.json',dict(returncode=rc,end_unix=time.time(),cgroup=counters(),binary_unchanged=sha(ROOT/binary)==digest))
        print(json.dumps(dict(phase=phase,name=dest.name,returncode=rc)),flush=True)
        if rc in [124,137,'outer_timeout'] or sha(ROOT/binary)!=digest:raise RuntimeError('timeout/identity failure; no retry')
    save(out/'complete.json',dict(planned=len(rows),end_unix=time.time()))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['pilot']+PHASES)
    a=p.parse_args();run(a.phase)
