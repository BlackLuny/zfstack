#!/usr/bin/env python3
"""Separate complete latency observations from service-deadline success."""
import json

import production_pacing

DEADLINE_ERROR='invalid completion time or deadline exceeded'


def analyze(out):
    result=production_pacing.analyze(out)
    for row in result['rows']:
        streams=json.loads((out/row['name']/'arrivals.json').read_text())
        arrivals=[a for stream in streams for a in stream['arrivals']]
        affinity_ok=row.get('rr_cpu') is None or all(stream.get(key)==row['rr_cpu'] for stream in streams for key in ['requested_cpu','observer_cpu','sender_cpu'])
        complete_times=bool(arrivals) and all(
            a['outcome']=='completed' and a['started_ns'] is not None and
            a['completed_ns'] is not None and a['planned_ns']<=a['started_ns']<=a['completed_ns'] and
            a['started_ns']-a['planned_ns']<=20_000_000 for a in arrivals)
        row['completed_after_deadline']=sum(a['outcome']=='completed' and a['completed_ns'] is not None and a['completed_ns']-a['planned_ns']>3_000_000_000 for a in arrivals)
        # Preserve the strict verdict and its errors. A fully observed late
        # response remains in the actual mean/P99, and remains an SLO failure.
        row['observation_valid']=affinity_ok and (row['valid'] or (complete_times and row['errors']==[DEADLINE_ERROR]))
        row['observation_errors']=[] if affinity_ok else ['RR affinity differs from the frozen plan']
        row['service_slo_passed']=row['valid']
    for comparison in result['comparisons']:
        group=[r for r in result['rows'] if (r['cc'],r['rtt'],r['heavy'])==(comparison['cc'],comparison['rtt'],comparison['heavy'])]
        comparison['observation_valid']=comparison['pairs']==12 and all(r['observation_valid'] for r in group)
    result['observations_valid']=sum(r['observation_valid'] for r in result['rows'])
    result['observation_note']='Strict validity/SLO failures are retained. Only a fully completed, independently monotonic late response may enter an observed-mean comparison despite missing the 3-s service deadline. Generator misses/loss, payload failures and missing observations still block confirmation; no pairs are discarded.'
    return result
