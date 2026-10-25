#!/usr/bin/env python3
"""Native capacity/demand transitions from useful bytes and planned arrivals."""
import json
import math
from pathlib import Path
import statistics

import adaptive_cases as rig
from run import save


def cumulative(progress,at):
    at*=1000
    if not progress or at<progress[0]['elapsed_ms'] or at>progress[-1]['elapsed_ms']:raise ValueError('progress does not cover requested time')
    for left,right in zip(progress,progress[1:]):
        if left['elapsed_ms']<=at<=right['elapsed_ms']:
            a,b=sum(left['bytes']),sum(right['bytes']);span=right['elapsed_ms']-left['elapsed_ms']
            if span<=0 or b<a:raise ValueError('invalid progress order')
            return a+(b-a)*(at-left['elapsed_ms'])/span
    raise ValueError('uncovered progress time')


def throughput(progress,start,end):
    return (cumulative(progress,end)-cumulative(progress,start))*8/(end-start)/1_000_000


def recovery(progress,at,limit,reference):
    # Report the end of the first full two-second window, at 250-ms resolution.
    for k in range(int((limit-2)*4)+1):
        start=at+k/4
        rate=throughput(progress,start,start+2)
        if rate>=.8*reference:return dict(seconds=start+2-at,mbps=rate,passed=True)
    return dict(seconds=None,mbps=None,passed=False)


def latency_phases(dest,windows,measure):
    streams=json.loads((dest/'arrivals.json').read_text());loaded=[s for s in streams if s['count']==measure*25]
    if len(loaded)!=3 or any(s.get('measurement_origin_ns') is None for s in loaded):
        return {label:dict(valid=False,reason='missing native monotonic phase origin') for label,_,_ in windows}
    result={}
    for label,start,end in windows:
        users=[];missing=0
        for stream in loaded:
            rows=[a for a in stream['arrivals'] if start*1e9<=stream['measurement_origin_ns']+a['planned_ns']<end*1e9]
            invalid=[a for a in rows if a['outcome']!='completed' or a['started_ns'] is None or a['completed_ns'] is None or a['completed_ns']<a['started_ns'] or a['started_ns']-a['planned_ns']>20_000_000]
            missing+=len(invalid)
            if invalid or not rows:users.append(dict(planned=len(rows),missing=len(invalid),mean_ms=None,p99_ms=None));continue
            values=sorted((a['completed_ns']-a['planned_ns'])/1e6 for a in rows)
            users.append(dict(planned=len(rows),missing=0,mean_ms=statistics.mean(values),p99_ms=values[math.ceil(len(values)*.99)-1],late=sum(v>3000 for v in values)))
        valid=not missing and all(u['mean_ms'] is not None for u in users)
        result[label]=dict(valid=valid,missing=missing,users=users,user_equal_mean_ms=statistics.mean(u['mean_ms'] for u in users) if valid else None,
            worst_user_p99_ms=max(u['p99_ms'] for u in users) if valid else None,
            note='Nearest-rank per-user P99; phases assigned by exact monotonic planned-arrival time, not completion time.')
    return result


def analyze(phase,summary):
    if phase not in ['dynamic','demand']:raise ValueError(phase)
    out=rig.output(phase);rows=summary['rows'];results=[]
    for case in sorted({r['case'] for r in rows}):
        for rep in sorted({r['round'] for r in rows if r['case']==case}):
            pair=[r for r in rows if r['case']==case and r['round']==rep]
            control=next(r for r in pair if r['arm']=='off');candidate=next(r for r in pair if r['arm']=='auto')
            raw={r['arm']:json.loads((out/rig.name(r)/'stdout.json').read_text()) for r in pair}
            progress={arm:r['results']['heavy']['progress_250ms'] for arm,r in raw.items()}
            bound=max(5,20*candidate['rtt']/1000)
            item=dict(case=case,round=rep,observed_valid=all(r['observation_valid'] for r in pair),recovery_limit_sec=bound,
                adaptive_trace=raw['auto']['stack_thread']['stack_stats']['adaptive_trace'])
            if phase=='dynamic':
                windows=[('initial',0,10),('lower',10,25),('restored',25,50)]
                item['stages']={}
                for label,step,end in windows:
                    reference=throughput(progress['off'],end-5,end)
                    observed=throughput(progress['auto'],end-5,end)
                    item['stages'][label]=dict(baseline_plateau_mbps=reference,candidate_plateau_mbps=observed,
                        plateau_ratio=observed/reference,plateau_passed=observed>=.9*reference,
                        recovery=None if step==0 else recovery(progress['auto'],step,bound,reference))
            else:
                windows=[('busy',0,8),('producer_paused',8,28),('resumed',28,50)]
                reference=throughput(progress['off'],45,50)
                silent=[]
                for arm in ['off','auto']:
                    minimum=min(throughput(progress[arm],8+k/4,9+k/4) for k in range(77))
                    silent.append(dict(arm=arm,minimum_one_second_mbps=minimum,observed_idle=minimum<.01*reference))
                observed=throughput(progress['auto'],45,50)
                item.update(actual_app_idle=silent,actual_app_idle_validated=all(x['observed_idle'] for x in silent),baseline_plateau_mbps=reference,
                    candidate_plateau_mbps=observed,plateau_ratio=observed/reference,plateau_passed=observed>=.9*reference,
                    recovery=recovery(progress['auto'],28,bound,reference))
            item['latency']={r['arm']:latency_phases(out/rig.name(r),windows,r['measure']) for r in pair}
            item['latency_changes']={}
            for label,_,_ in windows:
                a=item['latency']['off'][label];b=item['latency']['auto'][label]
                item['latency_changes'][label]={m:100*(b[m]/a[m]-1) if a['valid'] and b['valid'] and a[m]>0 else None for m in ['user_equal_mean_ms','worst_user_p99_ms']}
            results.append(item)
    return dict(phase=phase,pairs=results,all_observations_valid=all(x['observed_valid'] for x in results),
        all_recovered=all(all(s['plateau_passed'] and (s['recovery'] is None or s['recovery']['passed']) for s in x['stages'].values()) if phase=='dynamic' else x['actual_app_idle_validated'] and x['plateau_passed'] and x['recovery']['passed'] for x in results),
        note='Diagnostic controller traces are separate from uninstrumented primary CPU. Incomplete arrivals retain unavailable phase latency; no imputation. Per-phase CIs are formed later across full independent pairs.')
