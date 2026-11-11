#!/usr/bin/env python3
"""Frozen CUBIC/50-ms/single-flow ABBA retest, with same-setting controls."""
import argparse
import json
from pathlib import Path
import random
import statistics
import subprocess
import time

import production_pacing
import rtt_matrix as native
from rtt_analyze import summarize
from run import ROOT,save,sha,counters
from cubic50_env import ROLES,desired_mask

BINARY=ROOT/'target/release/zfbench-production-v5-20261024'
DIGEST='96695f880dd600172e3dfa5b32304da0fa296579357785b5f83dbb87ea1a26c5'
DIRECTORY=ROOT/'docs/experiments/2026-10-24-cubic50-retest'
PREP=ROOT/'qa/results/cubic50-20261024-preparation'
CONTAINER='zfstack-cubic50-20261024'


def sequence(preflight=False):
    if preflight:
        basic=[dict(round=0,placement='original',order='preflight',position=0,setting='off'),
               dict(round=0,placement='fixed',order='preflight',position=0,setting='on')]
    else:
        rng=random.Random(2026102427);orders={}
        for placement in ['original','fixed']:
            orders[placement]=['ABBA']*8+['BAAB']*8;rng.shuffle(orders[placement])
        placements=['original','fixed'];rng.shuffle(placements);basic=[]
        for rep in range(16):
            for placement in (placements if rep%2==0 else placements[::-1]):
                order=orders[placement][rep]
                for position,letter in enumerate(order):
                    basic.append(dict(round=rep,placement=placement,order=order,position=position,setting='off' if letter=='A' else 'on'))
    rows=[]
    for index,r in enumerate(basic):
        rows.append(dict(index=index,block=f"r{r['round']:02d}-{r['placement']}",arm='off' if r['setting']=='off' else 'p99',
            cc='cubic',rtt=50,heavy=1,lights=0,warmup=8,measure=20,retry_budget=1,
            egress_rate=0 if r['setting']=='off' else 12_375_000,tokio_stack=True,splice=True,**r))
    return rows


def environment():
    c=counters()
    for name in ['cpu.pressure','memory.events']:
        p=Path('/sys/fs/cgroup')/name;c[name]=p.read_text() if p.exists() else None
    return c


def name(row):return f"{row['index']:03d}-{row['block']}-{row['position']}-{row['setting']}"


def command(out,row,container):
    rel=(out/name(row)).relative_to(ROOT)
    env=dict(ZFBENCH_QUEUE_DISCIPLINE='fifo',ZFBENCH_RR_RAW='1',ZFBENCH_RR_OPEN_HZ='25',
        ZFBENCH_RR_ARRIVALS=f'/work/{rel}/arrivals.json',ZFBENCH_CONNECT_PARALLEL='32',
        ZFBENCH_HEAVY_PROGRESS='1',ZFBENCH_RETRY_BUDGET_WRITERS='1')
    argv=['docker','exec',container,'env']+[k+'='+v for k,v in env.items()]
    argv+=['timeout','-k','5','180','python3','/work/bench/capacity/cubic50_env.py',row['placement'],
        f'/work/{rel}/environment.json',f'/work/{rel}/resources.json','/work/'+str(BINARY.relative_to(ROOT))]
    argv+=native.arguments(row)+['--label',name(row)]
    return argv,env


def run(out,preflight=False,container=CONTAINER):
    out.relative_to(ROOT);out.mkdir(parents=True,exist_ok=False)
    if sha(BINARY)!=DIGEST:raise RuntimeError('frozen V5 binary differs')
    sources={str(f.relative_to(ROOT)):sha(f) for parent,glob in [(ROOT/'src','*.rs'),(ROOT/'bench/src','*.rs'),(ROOT/'bench/capacity','*.py'),(DIRECTORY,'*.md')] for f in sorted(parent.rglob(glob))}
    for f in [ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'bench/Cargo.toml']:sources[str(f.relative_to(ROOT))]=sha(f)
    for p in [PREP/'container.json',PREP/'sysctls.json',PREP/'telemetry-capabilities.json']:sources[str(p.relative_to(ROOT))]=sha(p)
    for path in sources:
        dest=out/'source'/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes((ROOT/path).read_bytes())
    rows=sequence(preflight)
    save(out/'plan.json',dict(phase='preflight' if preflight else 'confirmation',rows=rows,binary=str(BINARY.relative_to(ROOT)),binary_sha256=DIGEST,sources=sources,container=container,
        base_sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),environment=environment()))
    for row in rows:
        dest=out/name(row);dest.mkdir();argv,env=command(out,row,container)
        if sha(BINARY)!=DIGEST:raise RuntimeError('binary changed')
        save(dest/'start.json',dict(row=row,argv=argv,env=env,binary_sha256=DIGEST,start_unix=time.time(),cgroup=environment()))
        with (dest/'stdout.json').open('x') as stdout,(dest/'stderr.txt').open('x') as stderr:
            try:rc=subprocess.run(argv,stdout=stdout,stderr=stderr,timeout=195).returncode
            except subprocess.TimeoutExpired:rc='outer_timeout'
        save(dest/'end.json',dict(returncode=rc,end_unix=time.time(),binary_unchanged=sha(BINARY)==DIGEST,cgroup=environment()))
        print(json.dumps(dict(name=dest.name,returncode=rc)),flush=True)
        if rc in [124,137,'outer_timeout'] or sha(BINARY)!=DIGEST:raise RuntimeError('bounded/identity failure; no retry')
    save(out/'complete.json',dict(end_unix=time.time()))


def interval(values):
    rng=random.Random(2026102428);draws=[]
    for _ in range(10000):draws.append(statistics.median(rng.choices(values,k=len(values))))
    draws.sort()
    return dict(median_pct=statistics.median(values),mean_pct=statistics.mean(values),bootstrap_95_pct=[draws[250],draws[9749]],block_pct=values)


def weighted(rows,key,denominator='gb'):
    # Primary numerator is exact measured CPU, denominator exact useful work.
    return sum(r[key] for r in rows)/sum(r[denominator] for r in rows)


def telemetry(a):
    errors=list(a['errors']);roles=a['required_roles']
    if set(roles)!=set(ROLES):errors.append('required roles missing')
    if any(v['first_seen_sec']>2 for v in roles.values()):errors.append('role assignment late')
    if a['observer_affinity']!=[4]:errors.append('observer CPU differs')
    for sample in a['samples']:
        for t in sample['threads']:
            if t['affinity']!=desired_mask(t['name'],a['placement']):errors.append('sampled mask differs')
    if not a['samples']:errors.append('no post-assignment samples')
    diagnostic={}
    if 'zfb-stack' in roles:
        tid=roles['zfb-stack']['tid'];v=[t for s in a['samples'] for t in s['threads'] if t['tid']==tid]
        if len(v)>=2:
            first,last=v[0],v[-1]
            for key in ['runtime_ns','runqueue_ns','timeslices']:
                diagnostic[key+'_observed_delta']=(last['schedstat'][key]-first['schedstat'][key]) if first['schedstat'] and last['schedstat'] else None
            diagnostic['sampled_cpu_changes']=sum(x['last_cpu']!=y['last_cpu'] for x,y in zip(v,v[1:]))
            for group in ['context_switches','scheduler_counts']:
                diagnostic[group+'_observed_delta']={k:last[group][k]-first[group][k] for k in first[group] if k in last[group]}
            diagnostic['observed_cpu_locations']=sorted({t['last_cpu'] for t in v})
    return dict(valid=a['valid'] and not errors,errors=sorted(set(errors)),observer_cpu_sec=a['observer_cpu_sec'],driver=diagnostic)


def analyze(out):
    plan=json.loads((out/'plan.json').read_text());rows=[];identity=[]
    if not (out/'complete.json').exists():identity.append('incomplete cohort')
    for p,h in plan['sources'].items():
        if sha(out/'source'/p)!=h:identity.append('frozen source differs: '+p)
    for row in plan['rows']:
        dest=out/name(row)
        if not (dest/'end.json').exists():continue
        summary=summarize(dest);raw=json.loads((dest/'stdout.json').read_text());start=json.loads((dest/'start.json').read_text());end=json.loads((dest/'end.json').read_text())
        errors=summary['errors'];audit=telemetry(json.loads((dest/'environment.json').read_text()))
        if not audit['valid']:errors+=['environment observation: '+e for e in audit['errors']]
        stats=raw['stack_thread']['stack_stats'];shards=stats['shards']
        if stats['impl']!='zfstack-tokio' or not stats['production_splice'] or stats['reserved_after_shutdown'] or stats['fixture_app_errors'] or stats['kernel_upstream_threads_after_shutdown']:
            errors.append('native relay/teardown invalid')
        paced=sum(s['egress_paced_bytes'] for s in shards)
        if paced:errors.append('single-peer pacing intervened')
        expected,env=command(out,row,plan['container'])
        if start['row']!=row or start['argv']!=expected or start['env']!=env or start['binary_sha256']!=DIGEST or not end['binary_unchanged']:identity.append(name(row)+': invocation/identity differs')
        cfg=raw['config']
        expected_cfg=dict(stack='zfstack-cubic',test='isolate',ports=1,lights=0,heavy_conns=1,rtt_ms=50,rate_mbps=100,rate_up_mbps=100,mtu=1420,sock_buf_kb=16384,secs=28,warmup=8,seed=23,loss=0,loss_up=0,iface_egress_rate=row['egress_rate'],tokio_stack=True,tokio_splice=True)
        for k,v in expected_cfg.items():
            if cfg.get(k)!=v:identity.append(name(row)+': config '+k)
        for d in ['up','down']:
            link=raw['link'][d]
            if link['queue_discipline']!='fifo-service' or link['params']['queue_limit_bytes']!=250000 or link['params']['rate_bps']!=100_000_000 or link['params']['delay_us']!=25000 or link['params']['loss']!=0:identity.append(name(row)+': FIFO '+d)
        cpu=raw['cpu'];summary.update(valid=not errors,errors=sorted(set(errors)),telemetry=audit,egress_paced_bytes=paced,
            gb=cpu['window_effective_bytes']/1e9,stack_seconds=cpu['stack_thread_window_sec'],process_seconds=cpu['process_window_sec'],
            link_stack_seconds=cpu['stack_thread_window_sec']+cpu['link_up_window_sec']+cpu['link_down_window_sec'],
            window_steal_seconds=cpu['system_window']['steal_sec'],window_softirq_seconds=cpu['system_window']['softirq_sec'],
            stack_lifetime_pkts_in=raw['stack_thread']['pkts_in'],stack_lifetime_pkts_out=raw['stack_thread']['pkts_out'],
            stack_lifetime_runs=sum(s['runs'] for s in shards))
        rows.append(summary)
    if sha(BINARY)!=DIGEST:identity.append('retained V5 binary differs')
    if len(rows)!=len(plan['rows']):identity.append('missing planned attempts')
    groups={}
    for placement in ['original','fixed']:
        blocks=[]
        for rep in sorted({r['round'] for r in rows if r['placement']==placement}):
            group=sorted([r for r in rows if r['placement']==placement and r['round']==rep],key=lambda r:r['position'])
            a=[r for r in group if r['setting']=='off'];b=[r for r in group if r['setting']=='on']
            if len(a)!=2 or len(b)!=2:continue
            effects={}
            for key in ['stack_seconds','process_seconds','link_stack_seconds']:
                effects[key+'_per_gb']=100*(weighted(b,key)/weighted(a,key)-1)
                effects[key+'_per_s']=100*(weighted(b,key,'wall_sec')/weighted(a,key,'wall_sec')-1)
            effects['throughput']=100*((sum(r['gb'] for r in b)/sum(r['wall_sec'] for r in b))/(sum(r['gb'] for r in a)/sum(r['wall_sec'] for r in a))-1)
            aa={}
            for label,setting in [('off',a),('on',b)]:
                aa[label]=100*(setting[1]['stack_cpu_s_per_gb']/setting[0]['stack_cpu_s_per_gb']-1)
            pairs=[(group[0],group[1]),(group[3],group[2])] if group[0]['setting']=='off' else [(group[1],group[0]),(group[2],group[3])]
            blocks.append(dict(round=rep,valid=all(r['valid'] for r in group),effects=effects,aa_cpu_pct=aa,
                adjacent_paired_cpu_pct=[100*(y['stack_cpu_s_per_gb']/x['stack_cpu_s_per_gb']-1) for x,y in pairs]))
        changes={k:interval([b['effects'][k] for b in blocks]) for k in blocks[0]['effects']} if blocks else {}
        groups[placement]=dict(blocks=blocks,rounds=len(blocks),valid=len(blocks)==16 and all(b['valid'] for b in blocks),changes=changes)
    interaction=[]
    for a,b in zip(groups['original']['blocks'],groups['fixed']['blocks']):
        if a['round']==b['round']:interaction.append(100*((1+b['effects']['stack_seconds_per_gb']/100)/(1+a['effects']['stack_seconds_per_gb']/100)-1))
    return dict(phase=plan['phase'],planned=len(plan['rows']),completed=len(rows),valid=sum(r['valid'] for r in rows),
        identity_audit=dict(valid=not identity,errors=identity),rows=rows,placements=groups,
        fixed_vs_original_cpu_interaction=interval(interaction) if interaction else None,
        interval_scope='Pointwise whole-round 95% bootstrap intervals, 16 independent rounds; no pooled historical data or all-environment causation claim.')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=['preflight','confirmation']);p.add_argument('out',type=Path);p.add_argument('--analyze',action='store_true');a=p.parse_args()
    if a.analyze:print(json.dumps(analyze(a.out.resolve()),indent=2))
    else:run(a.out.resolve(),a.phase=='preflight')


if __name__=='__main__':main()
