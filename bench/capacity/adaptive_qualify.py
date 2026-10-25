#!/usr/bin/env python3
"""Apply the prospective BBR adaptive-pacing guards without pooling side cohorts."""
import argparse
import json
from pathlib import Path

import adaptive_cases as rig
from adaptive_analyze import protected
from adaptive_dynamic import analyze as dynamic_analyze
from run import save


def latency_ok(changes,upper):
    return all(m in changes and 'bootstrap_95_pct' in changes[m] and changes[m]['bootstrap_95_pct'][1]<=upper for m in ['user_equal_mean_ms','worst_user_p99_ms'])


def qualify(directory):
    summaries={p:json.loads((directory/(p+'-summary.json')).read_text()) for p in rig.PHASES}
    comparisons={p:json.loads((directory/(p+'-comparisons.json')).read_text()) for p in rig.PHASES}
    primary=comparisons['primary'];cells=primary['cells'];agg=primary['aggregates'];raw=summaries['primary']
    checks={}
    checks['all_cohorts_observed']=all(s['identity_valid'] and s['completed']==s['planned'] and s['observed_valid']==s['planned'] and not s['missing_requests'] and not s['throttled_events'] for s in summaries.values())
    checks['primary_observations']=raw['identity_valid'] and raw['completed']==raw['planned'] and raw['observed_valid']==raw['planned'] and not raw['missing_requests'] and not raw['throttled_events']
    checks['primary_candidate_progress']=all(r['heavy_zero_window_flows']==0 for r in raw['rows'] if r['arm']=='auto')
    point=[c for c,v in cells.items() if v['observed_valid'] and protected(v['changes'])]
    simultaneous=[c for c,v in cells.items() if v['observed_valid'] and protected(v['changes'],simultaneous=True)]
    checks['pointwise_nine_of_twelve']=len(point)>=9
    checks['simultaneous_seven_of_twelve']=len(simultaneous)>=7
    singles=[v for v in cells.values() if v['heavy']==1];contended=[v for v in cells.values() if v['heavy']>1]
    checks['all_single_throughput']=all(v['observed_valid'] and v['changes']['mbps']['bootstrap_95_pct'][0]>=-3 for v in singles)
    clear=[]
    for c,v in cells.items():
        if v['observed_valid'] and (v['changes']['mbps']['bootstrap_95_pct'][1]<-3 or v['changes']['stack_cpu_s_per_gb']['bootstrap_95_pct'][0]>3):clear.append(c)
    checks['no_clear_primary_regression']=not clear
    checks['primary_aggregates']=all(protected(agg[group]) for group in ['all','single','contended'])
    checks['broad_cpu_scopes']=all(m in agg[group] and agg[group][m]['bootstrap_95_pct'][1]<=3 for group in ['single','contended'] for m in ['stack_link_cpu_s_per_gb','process_cpu_s_per_gb'])
    checks['contended_latency_aggregate']=latency_ok(agg['contended'],-5)
    checks['contended_latency_cells']=all(v['observed_valid'] and latency_ok(v['changes'],5) for v in contended)
    improved=[c for c,v in cells.items() if v['heavy']>1 and v['observed_valid'] and latency_ok(v['changes'],-5)]
    checks['three_clearly_improved_cells']=len(improved)>=3
    side={}
    for p in ['bandwidth','heavy','disabled','peak']:
        s=summaries[p];c=comparisons[p];margin=10 if p in ['disabled','peak'] else 5
        observed=s['identity_valid'] and s['completed']==s['planned'] and s['observed_valid']==s['planned'] and not s['missing_requests'] and not s['throttled_events']
        protected_cells={k:bool(v['observed_valid'] and protected(v['changes'],margin=margin,peak=p=='peak') and (p in ['disabled','peak'] or latency_ok(v['changes'],5))) for k,v in c['cells'].items()}
        progress=p=='peak' or all(r['heavy_zero_window_flows']==0 for r in s['rows'] if r['arm'] not in ['off','old'])
        side[p]=dict(observed=observed,margin=margin,cells=protected_cells,candidate_progress=progress,passed=observed and progress and all(protected_cells.values()))
        checks[p]=side[p]['passed']
    dynamic={}
    checks['disabled_steady_3pct']=all(v['observed_valid'] and protected(v['changes'],margin=3) for v in comparisons['disabled']['cells'].values())
    for p in ['dynamic','demand']:
        d=dynamic_analyze(p,summaries[p]);save(directory/(p+'-transitions.json'),d);dynamic[p]=d
        # Every phase keeps all independent pair effects. A missing phase is
        # unavailable, never a fast response. Separate CIs avoid request pooling.
        from adaptive_analyze import bootstrap
        series={}
        for case in sorted({x['case'] for x in d['pairs']}):
            group=[x for x in d['pairs'] if x['case']==case]
            for phase in group[0]['latency_changes']:
                for metric in ['user_equal_mean_ms','worst_user_p99_ms']:
                    values=[x['latency_changes'][phase][metric] for x in group]
                    if all(v is not None and v>-100 for v in values):
                        import math
                        series[case+'|'+phase+'|'+metric]=[math.log1p(v/100) for v in values]
        phase_statistics=bootstrap(series) if series else dict(series={})
        save(directory/(p+'-phase-latency.json'),phase_statistics)
        required={x['case']+'|'+label+'|'+metric for x in d['pairs'] for label in x['latency_changes'] for metric in ['user_equal_mean_ms','worst_user_p99_ms']}
        phase_latency=all(key in phase_statistics['series'] and phase_statistics['series'][key]['bootstrap_95_pct'][1]<=5 for key in required)
        checks[p]=d['all_observations_valid'] and d['all_recovered'] and phase_latency
    return dict(candidate=rig.CANDIDATE,passed=all(checks.values()),default_enabled=False,checks=checks,
        pointwise_protected=point,simultaneously_protected=simultaneous,clearly_improved_latency_cells=improved,clear_primary_regressions=clear,side_cohorts=side,
        scope='Same-host native BBR, symmetric FIFO, sampled RTT/capacity grid. No universal unknown-capacity/default or unconditional starvation guarantee.',
        note='Original strict/observer failures retained. No pooling pilot, V5 disabled guards, bandwidth, heavy, peak or diagnostic traces into primary intervals.')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);p.add_argument('--candidate',choices=['a1'],default='a1');a=p.parse_args();rig.CANDIDATE=a.candidate
    if not (rig.PREP/'measurements-done.json').exists():raise RuntimeError('finish all measurements before effects/qualification')
    result=qualify(a.directory.resolve());save(a.directory.resolve()/'qualification.json',result);print(json.dumps(result,indent=2))
