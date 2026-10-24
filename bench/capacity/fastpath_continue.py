#!/usr/bin/env python3
"""Execute the declared serial V5 continuation; no retries or sample extension."""
import json
from pathlib import Path
import statistics
import time

import fastpath
from run import ROOT,save

PREP=ROOT/'qa/results/fastpath-20261024-build'


def phase(name):
    out=ROOT/'qa/results'/('fastpath-20261024-'+name)
    summary=PREP/(name+'-summary.json')
    if out.exists():
        # Only resume completed phases; never replace an attempt or partial plan.
        if not (out/'complete.json').exists() or not summary.exists():raise RuntimeError('incomplete existing phase: '+name)
        result=json.loads(summary.read_text())
    else:
        fastpath.run(out,name,'zfstack-fastpath-20261024')
        result=fastpath.analyze(out);save(summary,result)
    if not result['identity_audit']['identity_valid']:raise RuntimeError(name+': identity audit failed')
    print('SUMMARY',name,result['completed'],result['valid'],flush=True)
    return result


def loaded_passed(result):
    if result['planned']!=48 or result['observations_valid']!=48:return False
    for c in result['comparisons']:
        if not c['observation_valid'] or c['pairs']!=12:return False
        for metric,limit,side in [('mbps',-3,0),('user_equal_mean_ms',-20,1),('worst_user_p99_ms',-20,1),
                ('stack_cpu_s_per_gb',3,1),('stack_link_cpu_s_per_gb',3,1),('process_cpu_s_per_gb',3,1)]:
            bounds=c['changes'][metric]['bootstrap_95_pct']
            if (bounds[0]<limit if side==0 else bounds[1]>limit):return False
    return all(r.get('missing_requests')==0 and (r['arm']=='off' or (r['service_slo_passed'] and r['heavy_zero_window_flows']==0)) for r in result['rows'])


def main():
    smoke=phase('smoke')
    if smoke['valid']!=2:raise RuntimeError('native preflight invalid')
    preflight=phase('preflight')
    if preflight['valid']!=2:raise RuntimeError('controlled placement preflight invalid')
    aa=phase('aa')
    primary=phase('peak')
    loaded=phase('loaded')
    if any(not r['service_slo_passed'] or r['heavy_zero_window_flows'] for r in loaded['rows'] if r['arm']=='p99'):
        raise RuntimeError('candidate service/progress failure; investigate before continuing')
    targets=[c for c in primary['comparisons'] if c['left']=='old' and c['right']=='v5']
    clear_regression=any(c['valid'] and c['changes']['mbps']['bootstrap_95_pct'][1]<-3 for c in targets)
    ambiguous=any(c['valid'] and c['changes']['mbps']['median_pct']>=-3 and c['changes']['mbps']['bootstrap_95_pct'][0]<-3 and c['changes']['mbps']['bootstrap_95_pct'][1]>=0 for c in targets)
    noise={c['case']:statistics.median(abs(v) for v in c['changes']['mbps']['paired_pct']) for c in aa['comparisons'] if c['valid']}
    quieter=aa['valid']==aa['planned']==24 and all(noise[f'client_down{n}_pin']<noise[f'client_down{n}_shared'] for n in [1,2])
    decision=dict(loaded_passed=loaded_passed(loaded),clear_peak_regression=clear_regression,
        primary_ambiguous=ambiguous,controlled_aa_quieter_both_flows=quieter,aa_median_abs_paired_pct=noise,
        run_wan=loaded_passed(loaded) and not clear_regression,run_pin=ambiguous and quieter)
    save(PREP/'decisions.json',decision);print('DECISIONS',json.dumps(decision),flush=True)
    if decision['run_wan']:phase('wan')
    if decision['run_pin']:phase('pin')
    save(PREP/'done.json',dict(end_unix=time.time(),decisions=decision))


if __name__=='__main__':main()
