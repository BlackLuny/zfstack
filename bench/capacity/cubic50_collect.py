#!/usr/bin/env python3
"""Collect the completed targeted CPU retest, preserving all observations."""
import argparse
import json
from pathlib import Path
import statistics

import cubic50_retest as rig
from cubic50_env import ROLES,desired_mask
from run import ROOT,save,sha

HISTORICAL=13.915343916787236


def independent_observer_audit(a):
    errors=[];seen={r:set() for r in ROLES}
    for sample in a['samples']:
        if sample['elapsed_sec']<=2:errors.append('sample before assignment deadline')
        for t in sample['threads']:
            if t['affinity']!=desired_mask(t['name'],a['placement']):errors.append('sample mask differs')
            if t['name'] in seen:seen[t['name']].add(t['tid'])
    for role,tids in seen.items():
        if role not in a['required_roles'] or tids!={a['required_roles'][role]['tid']}:errors.append('ambiguous or missing role: '+role)
    return dict(valid=not errors,errors=sorted(set(errors)))


def stats(values):
    return dict(median=statistics.median(values),minimum=min(values),maximum=max(values),values=values) if values else None


def collect(destination):
    if not (rig.PREP/'done.json').exists():raise RuntimeError('finish every measurement before collection')
    destination.mkdir(parents=True,exist_ok=True);results={}
    for phase in ['preflight','confirmation']:
        out=ROOT/'qa/results'/('cubic50-20261024-'+phase);result=rig.analyze(out)
        extra=[]
        for row in result['rows']:
            raw=json.loads((out/row['name']/'stdout.json').read_text())
            a=json.loads((out/row['name']/'environment.json').read_text());audit=independent_observer_audit(a)
            row['independent_observer_audit']=audit
            if not audit['valid']:extra.append(row['name']+': observer audit failed')
            tid=a['required_roles']['zfb-stack']['tid']
            span=[s['elapsed_sec'] for s in a['samples'] if any(t['tid']==tid for t in s['threads'])]
            row['driver_observed_span_sec']=span[-1]-span[0]
            row['stack_lifetime_cpu_seconds']=raw['cpu']['stack_thread_total_sec']
            row['stack_lifetime_us_per_packet_out']=raw['cpu']['stack_thread_total_sec']*1e6/raw['stack_thread']['pkts_out']
        result['independent_observer_audit']=dict(valid=not extra,errors=extra)
        save(destination/(phase+'-summary.json'),result);results[phase]=result
    r=results['confirmation'];all_valid=r['valid']==r['planned']==128 and r['identity_audit']['valid'] and r['independent_observer_audit']['valid']
    pre=results['preflight'];all_valid=all_valid and pre['valid']==pre['planned']==2 and pre['identity_audit']['valid'] and pre['independent_observer_audit']['valid']
    decisions={};aa={};diagnostics={}
    for placement in ['original','fixed']:
        p=r['placements'][placement];cpu=p['changes']['stack_seconds_per_gb'];through=p['changes']['throughput']
        valid=all_valid and p['valid'];lo,hi=cpu['bootstrap_95_pct']
        status='invalid observations' if not valid else 'repeatable material CPU increase' if lo>3 else 'within 3% CPU/throughput margins' if hi<=3 and through['bootstrap_95_pct'][0]>=-3 else 'inconclusive'
        decisions[placement]=dict(status=status,valid=valid,cpu_effect=cpu,throughput_effect=through,
            sustained_13_915_pct_excluded=valid and hi<HISTORICAL,cpu_upper_le_3=valid and hi<=3,
            throughput_lower_ge_minus_3=valid and through['bootstrap_95_pct'][0]>=-3)
        aa[placement]={}
        diagnostics[placement]={}
        for setting in ['off','on']:
            values=[b['aa_cpu_pct'][setting] for b in p['blocks']]
            aa[placement][setting]=dict(**stats(values),median_absolute_pct=statistics.median(abs(v) for v in values),
                count_absolute_ge_historical=sum(abs(v)>=HISTORICAL for v in values))
            rows=[v for v in r['rows'] if v['placement']==placement and v['setting']==setting]
            metrics={k:stats([v[k] for v in rows]) for k in ['stack_cpu_s_per_gb','stack_cpu_s_per_s','process_cpu_s_per_gb','mbps',
                'stack_lifetime_us_per_packet_out','stack_lifetime_pkts_in','stack_lifetime_pkts_out','stack_lifetime_runs',
                'driver_observed_span_sec','window_steal_seconds','window_softirq_seconds']}
            metrics['observer_cpu_seconds']=stats([v['telemetry']['observer_cpu_sec'] for v in rows])
            metrics['driver_observed_runqueue_ms']=stats([v['telemetry']['driver']['runqueue_ns_observed_delta']/1e6 for v in rows if v['telemetry']['driver'].get('runqueue_ns_observed_delta') is not None])
            metrics['driver_observed_migrations']=stats([v['telemetry']['driver']['scheduler_counts_observed_delta']['se.nr_migrations'] for v in rows if 'se.nr_migrations' in v['telemetry']['driver'].get('scheduler_counts_observed_delta',{})])
            metrics['driver_observed_cpu_locations']=sorted({cpu for v in rows for cpu in v['telemetry']['driver'].get('observed_cpu_locations',[])})
            metrics['lifetime_fifo_drops']={d:stats([v['bottleneck_drops'][d] for v in rows]) for d in ['up','down']}
            metrics['lifetime_link_late_mean_us']={d:stats([v['link_late_mean_us'][d] for v in rows]) for d in ['up','down']}
            diagnostics[placement][setting]=metrics
    summary=dict(all_observations_valid=all_valid,decisions=decisions,
        both_placements_within_3pct=all(v['cpu_upper_le_3'] and v['throughput_lower_ge_minus_3'] for v in decisions.values()),
        causal_source_of_historical_spike_established=False,separate_project_peak_guards_unchanged=True,
        historical_median_pct=HISTORICAL,aa=aa,diagnostics=diagnostics,
        scope='Same frozen V5 executable and exact original workload/window; observer present in both retest arms. Pointwise whole-round CIs; no proof of strictly zero CPU overhead or exclusive cores/frequency control.')
    save(destination/'verdict.json',summary)
    counts=dict(planned=sum(v['planned'] for v in results.values()),completed=sum(v['completed'] for v in results.values()),valid=sum(v['valid'] for v in results.values()),
        throttled_events=sum(row.get('nr_throttled',0) for v in results.values() for row in v['rows']),
        single_peer_paced_bytes=sum(row['egress_paced_bytes'] for v in results.values() for row in v['rows']))
    save(destination/'cohorts.json',dict(counts=counts,cohorts=[dict(phase=k,planned=v['planned'],completed=v['completed'],valid=v['valid'],identity=v['identity_audit'],observer_audit=v['independent_observer_audit']) for k,v in results.items()]))
    return dict(counts=counts,decisions={k:dict(status=v['status'],cpu_median=v['cpu_effect']['median_pct'],cpu_95=v['cpu_effect']['bootstrap_95_pct'],through_95=v['throughput_effect']['bootstrap_95_pct']) for k,v in decisions.items()})


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path);a=p.parse_args();print(json.dumps(collect(a.destination.resolve()),indent=2))
