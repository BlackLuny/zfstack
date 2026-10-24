#!/usr/bin/env python3
"""Recompute and qualify the complete V5 continuation without pooling V4 data."""
import argparse
import json
from pathlib import Path

import fastpath
from run import ROOT,save

PHASES=['smoke','preflight','preflight-v2','aa','peak','loaded','wan','pin']


def placement_audit(path):
    a=json.loads(path.read_text());errors=list(a['errors'])
    roles={t['name'] for t in a['threads'].values() if t['proxy']}
    if not {'zfc-driver','zfc-tun-rx'}<=roles:errors.append('required proxy roles absent')
    for tid,t in a['threads'].items():
        expected=[0] if t['proxy'] and t['name']=='zfc-driver' else [1] if t['proxy'] else [2,3]
        if t['requested']!=expected or t['last_affinity']!=expected:errors.append('thread policy mismatch: '+tid)
        if t['proxy'] and t['first_seen_sec']>2:errors.append('proxy first observed too late: '+tid)
    if not a['samples']:errors.append('post-assignment samples absent')
    for sample in a['samples']:
        if sample['elapsed_sec']<=2:errors.append('sample before assignment deadline')
        for t in sample['threads']:
            if t['actual']!=a['threads'][str(t['tid'])]['requested']:errors.append('observed thread mask differs')
    return dict(valid=a['valid'] and not errors,errors=sorted(set(errors)))


def qualify(results,decisions):
    checks=[]
    def check(name,passed,blocking=True,**evidence):checks.append(dict(name=name,passed=bool(passed),blocking=blocking,**evidence))
    def bound(name,c,metric,limit,side):
        value=c['changes'].get(metric,{});bounds=value.get('bootstrap_95_pct')
        check(name,c.get('observation_valid',c['valid']) and c['pairs']==12 and bounds is not None and (bounds[0]>=limit if side==0 else bounds[1]<=limit),
            limit_pct=limit,endpoint='lower' if side==0 else 'upper',estimate=value)
    check('all retained identities',all(r['identity_audit']['identity_valid'] for r in results.values()))
    check('all retained plans complete',all(r['completed']==r['planned'] for r in results.values()))
    check('V5 native preflight',results['smoke']['valid']==results['smoke']['planned']==2)
    check('corrected placement preflight',results['preflight-v2']['valid']==results['preflight-v2']['planned']==2)
    check('A/A observed',results['aa']['valid']==results['aa']['planned']==24)
    peak=results['peak'];check('primary three-arm observations',peak['valid']==peak['planned']==72)
    for c in peak['comparisons']:
        if (c['left'],c['right'])!=('old','v5'):continue
        bound(c['case']+' useful throughput',c,'mbps',-3,0)
        bound(c['case']+' service CPU/GB',c,'service_cpu_s_per_gb',3,1)
    loaded=results['loaded']
    check('V5 loaded observations',loaded['observations_valid']==loaded['planned']==48)
    for c in loaded['comparisons']:
        check(c['cc']+' complete loaded pairs',c['observation_valid'] and c['pairs']==12)
        for key,limit,side in [('mbps',-3,0),('user_equal_mean_ms',-20,1),('worst_user_p99_ms',-20,1),
                ('stack_cpu_s_per_gb',3,1),('stack_link_cpu_s_per_gb',3,1),('process_cpu_s_per_gb',3,1)]:
            bound(c['cc']+' loaded '+key,c,key,limit,side)
    check('all loaded arrivals completed',all(r['missing_requests']==0 for r in loaded['rows']))
    check('loaded candidate SLOs',all(r['service_slo_passed'] for r in loaded['rows'] if r['arm']=='p99'))
    check('loaded candidate finite flow progress',all(r['heavy_zero_window_flows']==0 for r in loaded['rows'] if r['arm']=='p99'))
    if decisions['run_wan']:
        wan=results['wan'];check('V5 WAN observations',wan['valid']==wan['planned']==96)
        check('WAN candidate finite flow progress',all(r['heavy_zero_window_flows']==0 for r in wan['rows'] if r['arm']=='p99'))
        check('WAN planned arrivals completed',all(r.get('missing_requests',0)==0 for r in wan['rows']))
        check('WAN single-peer bypass',all(r['egress_paced_bytes']==0 for r in wan['rows'] if not r['lights']))
    if decisions['run_pin']:
        controlled=results['pin'];check('controlled observations',controlled['valid']==controlled['planned']==48,blocking=False)
        for c in controlled['comparisons']:
            value=c['changes']['mbps'];check(c['case']+' controlled throughput',c['valid'] and value['bootstrap_95_pct'][0]>=-3,
                blocking=False,estimate=value)
    return dict(passed=all(c['passed'] for c in checks if c['blocking']),checks=checks,decisions=decisions,
        scope='V5 primary unrestricted local peak guards and independent loaded confirmation; WAN has two pairs/cell, not all-RTT non-inferiority. Old V4 results are retained and never pooled. Pointwise CIs are not familywise.',
        caveat='Two original placement preflight attempts are invalid and retained. PPID-based preflight-v2 passes; no failed preflight sample enters a CI. Controlled placement cannot replace an unqualified primary bound.')


def collect(destination):
    prep=ROOT/'qa/results/fastpath-20261024-build'
    if not (prep/'done.json').exists():raise RuntimeError('measurements must finish before collection')
    destination.mkdir(parents=True,exist_ok=True);results={};cohorts=[]
    for name in PHASES:
        out=ROOT/'qa/results'/('fastpath-20261024-'+name)
        if not out.exists():continue
        result=fastpath.analyze(out)
        for row in result['rows']:
            if row.get('pinned'):
                check=placement_audit(out/row['name']/'affinity.json');row['placement_audit']=check
                if row['valid'] and not check['valid']:
                    row['valid']=False;row['errors'].append('independent placement audit failed')
        result['valid']=sum(r['valid'] for r in result['rows'])
        save(destination/(name+'-summary.json'),result);results[name]=result
        cohorts.append(dict(name=name,planned=result['planned'],completed=result['completed'],valid=result['valid'],
            observations_valid=result.get('observations_valid'),identity_valid=result['identity_audit']['identity_valid'],
            planned_requests=sum(r.get('planned_requests',0) for r in result['rows']),
            missing_requests=sum(r.get('missing_requests',0) for r in result['rows']),
            throttled_events=sum(r.get('nr_throttled',0) for r in result['rows']),
            invalid=[dict(name=r['name'],errors=r['errors']) for r in result['rows'] if not r['valid']]))
    counts={k:sum(c[k] for c in cohorts) for k in ['planned','completed','valid','planned_requests','missing_requests','throttled_events']}
    save(destination/'cohorts.json',dict(cohorts=cohorts,counts=counts))
    decisions=json.loads((prep/'decisions.json').read_text());q=qualify(results,decisions)
    save(destination/'qualification.json',q)
    return dict(counts=counts,qualified=q['passed'],failed=[c['name'] for c in q['checks'] if c['blocking'] and not c['passed']])


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path);a=p.parse_args()
    print(json.dumps(collect(a.destination.resolve()),indent=2))
