#!/usr/bin/env python3
"""Paired, whole-round adaptive-pacing analysis. Never replace incomplete runs."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import random
import statistics

import adaptive_cases as rig
from common_collect import METRICS, PEAK_METRICS
from run import ROOT, save, sha


def bootstrap(series,simultaneous_keys=(),draws=20_000):
    keys=sorted(series);n=len(series[keys[0]])
    if n<4 or any(len(series[k])!=n for k in keys):raise ValueError('short/incomplete paired series')
    if any(not math.isfinite(v) for values in series.values() for v in values):raise ValueError('nonfinite series')
    rng=random.Random(2026102506)
    weights=[]
    for _ in range(draws):
        count=Counter(rng.choices(range(n),k=n));weights.append([count[i] for i in range(n)])
    centers={k:statistics.mean(series[k]) for k in keys}
    boot={k:[sum(w*v for w,v in zip(count,series[k]))/n for count in weights] for k in keys}
    se={k:statistics.stdev(series[k])/math.sqrt(n) for k in keys}
    def quantile(values,q):return sorted(values)[math.ceil(len(values)*q)-1]
    critical=None
    if simultaneous_keys:
        variable=[k for k in simultaneous_keys if se[k]>1e-15]
        critical=quantile([max(abs((boot[k][i]-centers[k])/se[k]) for k in variable) for i in range(draws)],.95) if variable else 0.
    pct=lambda x:100*math.expm1(x)
    result={}
    for k in keys:
        result[k]=dict(geometric_mean_pct=pct(centers[k]),median_paired_pct=pct(statistics.median(series[k])),
            bootstrap_95_pct=[pct(quantile(boot[k],.025)),pct(quantile(boot[k],.975))],paired_pct=[pct(v) for v in series[k]],log_effects=series[k])
        if k in simultaneous_keys:result[k]['simultaneous_95_pct']=[pct(centers[k]-critical*se[k]),pct(centers[k]+critical*se[k])]
    return dict(series=result,rounds=n,draws=draws,seed=2026102506,simultaneous_critical=critical,
        note='Approximate finite-sample whole-round intervals; four-pair side cohorts are weak evidence. No request-level pseudoreplication.')


def cohort(phase):
    out=rig.output(phase);plan=json.loads((out/'plan.json').read_text());errors=[]
    if plan['rows']!=rig.sequence(phase):errors.append('sequence differs')
    if not (out/'complete.json').exists():errors.append('phase incomplete')
    for path,digest in plan['sources'].items():
        if sha(out/'source'/path)!=digest:errors.append('frozen snapshot differs: '+path)
    rows=[]
    for row in plan['rows']:
        dest=out/rig.name(row)
        if not (dest/'end.json').exists():errors.append('missing attempt: '+dest.name);continue
        rows.append(rig.inspect(out,row,plan['container']))
    return dict(phase=phase,planned=len(plan['rows']),completed=len(rows),identity_valid=not errors,errors=errors,
        original_strict_valid=sum(x['original_strict_valid'] for x in rows),observed_valid=sum(x['observation_valid'] for x in rows),
        missing_requests=sum(x.get('missing_requests',0) or 0 for x in rows),throttled_events=sum(x.get('nr_throttled',0) or 0 for x in rows),rows=rows)


def pairs_by_case(result):
    control='old' if result['phase'] in ['disabled','peak'] else 'off';cases={}
    for case in sorted({r['case'] for r in result['rows']}):
        group=[r for r in result['rows'] if r['case']==case];pairs=[]
        for rep in sorted({r['round'] for r in group}):
            block=[r for r in group if r['round']==rep];a=[r for r in block if r['arm']==control];b=[r for r in block if r['arm']!=control]
            if len(a)!=1 or len(b)!=1:raise ValueError('incomplete pair '+case)
            pairs.append((a[0],b[0]))
        cases[case]=pairs
    return cases


def comparison(result):
    cases=pairs_by_case(result);series={};metadata={};simultaneous=[]
    metrics=PEAK_METRICS if result['phase']=='peak' else METRICS
    for case,pairs in cases.items():
        metadata[case]=dict(pairs=len(pairs),observed_valid=all(a['observation_valid'] and b['observation_valid'] for a,b in pairs),
            candidate_valid=all(b['strict_valid'] for _,b in pairs),orders=['AB' if a['position']<b['position'] else 'BA' for a,b in pairs],
            heavy=pairs[0][0].get('heavy'),rtt=pairs[0][0].get('rtt'),rate_mbps=pairs[0][0].get('rate_mbps'))
        for metric in metrics:
            values=[(a.get(metric),b.get(metric)) for a,b in pairs]
            if all(a is not None and b is not None and a>0 and b>0 for a,b in values):
                key=case+'|'+metric;series[key]=[math.log(b/a) for a,b in values]
                if result['phase']=='primary' and metric in ['mbps','stack_cpu_s_per_gb']:simultaneous.append(key)
    groups=dict(all=list(cases))
    if result['phase']!='peak':groups.update(single=[c for c in cases if metadata[c]['heavy']==1],contended=[c for c in cases if metadata[c]['heavy']>1])
    rounds=len(next(iter(cases.values())))
    for label,selected in groups.items():
        if not selected:continue
        for metric in metrics:
            keys=[c+'|'+metric for c in selected]
            if all(k in series for k in keys):series['aggregate:'+label+'|'+metric]=[statistics.mean(series[k][r] for k in keys) for r in range(rounds)]
    if rounds<4:
        stats=dict(series={k:dict(paired_pct=[100*math.expm1(v) for v in values],geometric_mean_pct=100*math.expm1(statistics.mean(values))) for k,values in series.items()},rounds=rounds,note='Pilot/A-A descriptive only; no confidence interval')
    else:stats=bootstrap(series,simultaneous)
    return dict(cells={c:dict(**metadata[c],changes={m:stats['series'][c+'|'+m] for m in metrics if c+'|'+m in stats['series']}) for c in cases},
        aggregates={label:{m:stats['series']['aggregate:'+label+'|'+m] for m in metrics if 'aggregate:'+label+'|'+m in stats['series']} for label in groups},
        statistics={k:v for k,v in stats.items() if k!='series'})


def protected(changes,margin=3,simultaneous=False,peak=False):
    field='simultaneous_95_pct' if simultaneous else 'bootstrap_95_pct';cpu='service_cpu_s_per_gb' if peak else 'stack_cpu_s_per_gb'
    return all(k in changes and field in changes[k] for k in ['mbps',cpu]) and changes['mbps'][field][0]>=-margin and changes[cpu][field][1]<=margin


def collect(destination,phases):
    destination.mkdir(parents=True,exist_ok=True)
    result={}
    for phase in phases:
        raw=cohort(phase);comp=comparison(raw)
        save(destination/(phase+'-summary.json'),raw);save(destination/(phase+'-comparisons.json'),comp)
        result[phase]=dict(planned=raw['planned'],completed=raw['completed'],identity_valid=raw['identity_valid'],observed_valid=raw['observed_valid'],original_strict_valid=raw['original_strict_valid'],missing_requests=raw['missing_requests'])
    save(destination/'cohorts.json',result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path);p.add_argument('--pilot',action='store_true');p.add_argument('--candidate',choices=['a0','a1'],default='a1');a=p.parse_args();rig.CANDIDATE=a.candidate
    if not a.pilot and not (rig.PREP/'measurements-done.json').exists():raise RuntimeError('finish all frozen measurements before effects')
    print(json.dumps(collect(a.destination.resolve(),['pilot'] if a.pilot else rig.PHASES),indent=2))
