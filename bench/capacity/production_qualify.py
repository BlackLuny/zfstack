#!/usr/bin/env python3
"""Evaluate declared final-code guards without replacing prior measurements."""
import argparse
import json
from pathlib import Path

from run import save


def qualify(directory):
    def read(name):return json.loads((directory/name).read_text())
    checks=[]
    def check(name,passed,blocking=True,**evidence):checks.append(dict(name=name,passed=bool(passed),blocking=blocking,**evidence))
    audits=read('audit.json');cohorts=read('cohorts.json')['cohorts']
    check('all retained cohort identities',all(a['identity_valid'] for a in audits))
    check('all planned attempts observed',all(c['completed']==c['planned'] for c in cohorts))
    smoke=read('review-smoke-summary.json')
    check('V4 isolated observer preflight',smoke['valid']==smoke['observations_valid']==smoke['planned']==2)
    loaded=read('review-loaded-summary.json')
    check('V4 loaded observations valid',loaded['observations_valid']==loaded['planned']==48)
    for c in loaded['comparisons']:
        prefix='V4 loaded '+c['cc']
        check(prefix+' paired observations',c['observation_valid'] and c['pairs']==12)
        for metric,limit,endpoint in [('mbps',-3,0),('user_equal_mean_ms',-20,1),('worst_user_p99_ms',-20,1),
                ('stack_cpu_s_per_gb',3,1),('stack_link_cpu_s_per_gb',3,1),('process_cpu_s_per_gb',3,1)]:
            values=c['changes'].get(metric,{})
            bounds=values.get('bootstrap_95_pct')
            passed=c['observation_valid'] and bounds is not None and (bounds[0]>=limit if endpoint==0 else bounds[1]<=limit)
            check(prefix+' '+metric,passed,limit_pct=limit,endpoint='lower' if endpoint==0 else 'upper',estimate=values)
    check('V4 candidate finite-window flow progress',all(r['heavy_zero_window_flows']==0 for r in loaded['rows'] if r['arm']=='p99'))
    check('V4 fixed-arrival completeness',all(r.get('missing_requests')==0 for r in loaded['rows']))
    check('V4 candidate service deadline',all(r['service_slo_passed'] for r in loaded['rows'] if r['arm']=='p99'))
    for name,count in [('review-wan',48),('review-peak',48)]:
        result=read(name+'-summary.json')
        check(name+' observations valid',result['valid']==result['planned']==count)
        for c in result['comparisons']:
            identity=c.get('case',f"{c.get('cc')} RTT {c.get('rtt')} ms")
            values=c['changes'].get('mbps',{});bounds=values.get('bootstrap_95_pct')
            context=name=='review-wan' and c.get('cc')=='bbr' and c.get('rtt')==500
            check(name+' '+identity+' throughput',c['valid'] and c['pairs']==12 and bounds is not None and bounds[0]>=-3,
                blocking=not context,limit_pct=-3,endpoint='lower',estimate=values)
            if context:
                check('steady follow-up eligibility',c['valid'] and c['pairs']==12 and bounds is not None and values['median_pct']>=-3 and bounds[1]>=0)
    steady=read('review-wan-long-summary.json')
    check('V4 steady single-flow observations',steady['valid']==steady['planned']==24)
    c=steady['comparisons'][0];values=c['changes']['mbps'];bounds=values['bootstrap_95_pct']
    check('V4 BBR500 sustained-throughput lower bound',c['valid'] and c['pairs']==12 and bounds[0]>=-3,
        limit_pct=-3,endpoint='lower',estimate=values)
    mixed=read('final-mixed-summary.json')
    check('V3 concurrent upload guard observed',mixed['valid']==mixed['planned']==24)
    result=dict(passed=all(c['passed'] for c in checks if c['blocking']),checks=checks,
        all_original_short_window_guards_passed=all(c['passed'] for c in checks if not c['blocking']),
        scope='Declared V4 loaded/physical peak guards and independent BBR500 sustained-throughput follow-up. The original 40-s BBR500 guard remains inconclusive and is explicitly retained. No every-short-window, universal starvation or unknown-capacity claim.',
        caveat='The initial V1 descriptive screen executed 4-s warmup despite 8 s in its prose protocol; it does not enter either formal confirmation.')
    save(directory/'qualification.json',result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    r=qualify(a.directory.resolve());print(json.dumps(dict(passed=r['passed'],checks=len(r['checks']),failed=[c['name'] for c in r['checks'] if not c['passed']]),indent=2))
