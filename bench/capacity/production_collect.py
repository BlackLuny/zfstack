#!/usr/bin/env python3
"""Collect immutable production-pacing evidence and audit executed identities."""
import argparse
import json
from pathlib import Path

import production_pacing as native
import production_peaks as peaks
import production_mixed as mixed
import production_observe as observed
from run import ROOT, save, sha

COHORTS = [
    ("v1-screen",native.analyze),
    ("v2-tokio-smoke",native.analyze),("v2-tokio-pilot",native.analyze),
    ("v3-splice-smoke",native.analyze),("v3-splice-pilot",native.analyze),
    ("final-wan",native.analyze),("final-confirm",native.analyze),
    ("final-guard",native.analyze),("final-aa",native.analyze),
    ("final-cross-peak",peaks.analyze),
    ("mixed-smoke",mixed.analyze),("mixed-v2-smoke",mixed.analyze),("final-mixed",mixed.analyze),
    ("review-smoke",observed.analyze),("review-wan",native.analyze),("review-peak",peaks.analyze),
    ("review-loaded",observed.analyze),
]


def audit(out, result):
    plan=json.loads((out/'plan.json').read_text())
    errors=[]
    cross=isinstance(plan['binary_sha256'],dict)
    if len(result['rows'])!=len(plan['rows']):errors.append('planned attempts are missing')
    if not (out/'complete.json').exists():errors.append('cohort did not complete')
    snapshot=out/'source'
    if cross and not snapshot.is_dir():
        snapshot=ROOT/'qa/results/production-20261024-final-wan/source'
    for name,digest in plan['sources'].items():
        f=snapshot/name
        if not f.exists() or sha(f)!=digest:errors.append('frozen source mismatch: '+name)
    if cross:
        prior=ROOT/'qa/results/rtt-20261023-final-aa'
        old=json.loads((prior/'plan.json').read_text())
        if plan['binary_sha256']['A']!=old['binary_sha256']:errors.append('old binary differs from the original frozen source plan')
        for name,digest in old['sources'].items():
            f=prior/'frozen_source'/name
            if not f.exists() or sha(f)!=digest:errors.append('old frozen source mismatch: '+name)
    planned={r['index']:r for r in plan['rows']}
    for row in result['rows']:
        p=out/row['name'];start=json.loads((p/'start.json').read_text());end=json.loads((p/'end.json').read_text())
        expected=planned[row['index']]
        if start['row']!=expected:errors.append(row['name']+': row differs from plan')
        digest=plan['binary_sha256'][row['arm']] if cross else plan['binary_sha256']
        if start['binary_sha256']!=digest or not end['binary_unchanged']:errors.append(row['name']+': binary changed')
        argv=start['argv']
        binary=next((s.removeprefix('/work/') for s in argv if s.startswith('/work/target/release/')),None)
        if not binary or sha(ROOT/binary)!=digest:errors.append(row['name']+': retained binary differs')
        if not cross:
            raw=json.loads((p/'stdout.json').read_text());cfg=raw['config']
            values={'--stack':'zfstack-'+expected['cc'],'--test':'isolate','--ports':1,
                '--lights':expected['lights'],'--heavy-conns':expected['heavy'],
                '--rtt-ms':expected['rtt'],'--rate-mbps':expected.get('rate_mbps',100),
                '--queue-bytes':expected.get('queue_bytes',250000),'--sock-buf-kb':16384,
                '--secs':expected['warmup']+expected['measure'],'--warmup':expected['warmup'],
                '--idle-rr-secs':2 if expected['lights'] else 0,'--rr-size':256,
                '--rr-interval-ms':40,'--connect-timeout-ms':20000,'--seed':23,'--label':row['name']}
            for flag,value in values.items():
                if argv.count(flag)!=1 or argv[argv.index(flag)+1]!=str(value):
                    errors.append(row['name']+': invocation differs at '+flag)
            actual={'stack':'zfstack-'+expected['cc'],'test':'isolate','ports':1,
                'lights':expected['lights'],'heavy_conns':expected['heavy'],'rtt_ms':expected['rtt'],
                'rate_mbps':expected.get('rate_mbps',100),'rate_up_mbps':expected.get('rate_mbps',100),
                'sock_buf_kb':16384,'secs':expected['warmup']+expected['measure'],
                'warmup':expected['warmup'],'idle_rr_secs':2 if expected['lights'] else 0,
                'rr_size':256,'rr_interval_ms':40,'seed':23,'mtu':1420,
                'topology':'shared-shard','placement':'same-iface','loss':0,'loss_up':0}
            for key,value in actual.items():
                if cfg.get(key)!=value:errors.append(row['name']+': runtime config differs at '+key)
            requested=argv[argv.index('--iface-egress-rate')+1]
            if int(requested)!=expected['egress_rate'] or cfg['iface_egress_rate']!=expected['egress_rate']:
                errors.append(row['name']+': configured rate differs')
            for direction in ('up','down'):
                link=raw['link'][direction]
                if link['queue_discipline']!='fifo-service' or link['params']['queue_limit_bytes']!=expected.get('queue_bytes',250000):
                    errors.append(row['name']+': physical FIFO differs')
                if link['params']['rate_bps']!=expected.get('rate_mbps',100)*1_000_000:
                    errors.append(row['name']+': physical capacity differs')
                if link['params']['delay_us']!=expected['rtt']*500 or link['params']['loss']!=0:
                    errors.append(row['name']+': physical RTT/loss differs')
            if bool('--tokio-stack' in argv)!=expected.get('tokio_stack',False) or bool('--splice' in argv)!=expected.get('splice',False):
                errors.append(row['name']+': runtime/relay invocation differs')
            if expected.get('tokio_stack'):
                stats=raw['stack_thread']['stack_stats']
                if stats['impl']!='zfstack-tokio' or stats.get('production_splice',False)!=expected.get('splice',False):
                    errors.append(row['name']+': runtime/relay result differs')
            if expected['lights']==0 and row.get('egress_paced_bytes',0):
                errors.append(row['name']+': single-peer bypass intervened')
            if expected.get('rr_cpu') is not None:
                if start['env'].get('ZFBENCH_RR_CPU')!=str(expected['rr_cpu']):
                    errors.append(row['name']+': RR affinity invocation differs')
                streams=json.loads((p/'arrivals.json').read_text())
                if len(streams)!=6 or any(stream.get(key)!=expected['rr_cpu'] for stream in streams for key in ['requested_cpu','observer_cpu','sender_cpu']):
                    errors.append(row['name']+': actual arrival CPU differs')
        else:
            raw=json.loads((p/'stdout.json').read_text());cfg=raw['config']
            values={'--client-proxy':'zfstack-client','--proxy-workers':2,'--tun-mtu':65535,
                '--secs':16,'--warmup':4,'--flows':expected['flows'],'--test':expected['test'],'--label':row['name']}
            for flag,value in values.items():
                if argv.count(flag)!=1 or argv[argv.index(flag)+1]!=str(value):
                    errors.append(row['name']+': client invocation differs at '+flag)
            if '--splice' not in argv or '--driver-thread' not in argv:
                errors.append(row['name']+': client did not use the production relay/driver')
            if expected['arm']=='B' and ('--iface-egress-rate' not in argv or argv[argv.index('--iface-egress-rate')+1]!='12375000'):
                errors.append(row['name']+': client candidate did not configure the bypass guard rate')
    return dict(cohort=out.name,identity_valid=not errors,errors=errors,planned=len(plan['rows']),audited=len(result['rows']))


def collect(destination):
    destination.mkdir(parents=True,exist_ok=True)
    cohorts=[];audits=[]
    for name,analyze in COHORTS:
        out=ROOT/'qa/results'/('production-20261024-'+name)
        result=analyze(out)
        if name=='v1-screen':
            result['protocol_deviations']=['The prose initial-screen protocol said 8-s warmup, but its frozen executable plan used 4 s in every arm. This descriptive screen is not a formal confirmation; native pilots and both independent confirmations use the declared 8 s.']
        record=audit(out,result);audits.append(record)
        save(destination/(name+'-summary.json'),result)
        cohorts.append(dict(name=name,planned=result['planned'],completed=result['completed'],valid=result['valid'],
            observations_valid=result.get('observations_valid'),
            planned_requests=sum(r.get('planned_requests',0) for r in result['rows']),
            missing_requests=sum(r.get('missing_requests',0) for r in result['rows']),
            throttled_events=sum(r.get('nr_throttled',0) for r in result['rows']),
            identity_valid=record['identity_valid'],
            invalid=[dict(name=r['name'],errors=r['errors']) for r in result['rows'] if not r['valid']]))
    save(destination/'audit.json',audits)
    save(destination/'cohorts.json',dict(cohorts=cohorts,counts={k:sum(c[k] for c in cohorts) for k in ['planned','completed','valid','planned_requests','missing_requests','throttled_events']}))
    return dict(identity_valid=all(a['identity_valid'] for a in audits),cohorts=len(cohorts))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path);a=p.parse_args()
    print(json.dumps(collect(a.destination.resolve()),indent=2))
