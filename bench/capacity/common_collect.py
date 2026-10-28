#!/usr/bin/env python3
"""Replay all independent common-case observations and whole-round intervals."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import random
import statistics

import common_cases as rig
from run import ROOT, save, sha

METRICS = ['mbps', 'stack_cpu_s_per_gb', 'stack_cpu_s_per_s', 'stack_link_cpu_s_per_gb',
    'process_cpu_s_per_gb', 'process_cpu_s_per_s', 'rss_hwm_kib', 'user_equal_mean_ms',
    'worst_user_p99_ms', 'heavy_jain', 'max_observed_no_read_sec']
PEAK_METRICS = ['mbps', 'service_cpu_s_per_gb', 'service_cpu_s_per_s', 'rss_hwm_kib']


def bootstrap(series, simultaneous_keys=(), draws=20_000):
    keys = sorted(series)
    n = len(series[keys[0]])
    if any(len(series[k]) != n for k in keys) or n < 6:
        raise ValueError('incomplete or short paired series')
    if any(not math.isfinite(v) for values in series.values() for v in values):
        raise ValueError('nonfinite metric')
    rng = random.Random(2026102502)
    weights = []
    for _ in range(draws):
        count = Counter(rng.choices(range(n), k=n))
        weights.append([count[i] for i in range(n)])
    center = {k:statistics.mean(series[k]) for k in keys}
    boot = {k:[sum(w*v for w, v in zip(count, series[k]))/n for count in weights] for k in keys}
    se = {k:statistics.stdev(series[k])/math.sqrt(n) for k in keys}
    def quantile(values, q):
        return sorted(values)[math.ceil(len(values)*q)-1]
    critical = None
    if simultaneous_keys:
        variable = [k for k in simultaneous_keys if se[k] > 1e-15]
        critical = quantile([max(abs((boot[k][i]-center[k])/se[k]) for k in variable)
            for i in range(draws)], .95) if variable else 0.
    def pct(value):
        return 100*math.expm1(value)
    result = {}
    for key in keys:
        result[key] = dict(geometric_mean_pct=pct(center[key]),
            median_paired_pct=pct(statistics.median(series[key])),
            bootstrap_95_pct=[pct(quantile(boot[key], .025)), pct(quantile(boot[key], .975))],
            paired_pct=[pct(v) for v in series[key]], log_effects=series[key])
        if key in simultaneous_keys:
            result[key]['simultaneous_95_pct'] = [pct(center[key]-critical*se[key]), pct(center[key]+critical*se[key])]
    return dict(series=result, rounds=n, draws=draws, seed=2026102502,
        simultaneous_critical=critical, engine='Python standard library, nearest-rank quantiles',
        interval_note='Approximate finite-sample whole-round percentile intervals and standardized maximum-deviation band, not distribution-free bounds.')


def cohort(phase):
    out = ROOT/'qa/results'/('common-20261025-'+phase)
    plan = json.loads((out/'plan.json').read_text())
    errors = []
    if plan['rows'] != rig.sequence(phase) or not (out/'complete.json').exists():
        errors.append('planned sequence differs or incomplete cohort')
    for path, digest in plan['sources'].items():
        if sha(out/'source'/path) != digest:
            errors.append('frozen source differs: '+path)
    rows = []
    for row in plan['rows']:
        if not (out/rig.name(row)/'end.json').exists():
            errors.append('missing attempt: '+rig.name(row))
            continue
        try:
            rows.append(rig.inspect(out, row, plan['container']))
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(rig.name(row)+': '+str(error))
    return dict(phase=phase, planned=len(plan['rows']), completed=len(rows),
        strict_valid=sum(r['strict_valid'] for r in rows),
        observed_valid=sum(r['observation_valid'] for r in rows),
        identity_valid=not errors, errors=errors, rows=rows)


def pairs_by_case(result, control):
    cases = {}
    for case in sorted({r['case'] for r in result['rows']}):
        pairs = []
        group = [r for r in result['rows'] if r['case'] == case]
        for rep in sorted({r['round'] for r in group}):
            block = [r for r in group if r['round'] == rep]
            a = [r for r in block if r['arm'] == control]
            b = [r for r in block if r['arm'] != control]
            if len(a) != 1 or len(b) != 1:
                raise ValueError('incomplete pair: '+case)
            pairs.append((a[0], b[0]))
        cases[case] = pairs
    return cases


def comparison(result, peak=False):
    cases = pairs_by_case(result, 'old' if peak else 'off')
    series, metadata = {}, {}
    metrics = PEAK_METRICS if peak else METRICS
    simultaneous = []
    for case, pairs in cases.items():
        metadata[case] = dict(pairs=len(pairs), observed_valid=all(a['observation_valid'] and b['observation_valid'] for a, b in pairs),
            candidate_strict_valid=all(b['strict_valid'] for _, b in pairs),
            orders=['AB' if a['position'] < b['position'] else 'BA' for a, b in pairs],
            **({} if peak else {key:pairs[0][0][key] for key in ['cc', 'rtt', 'heavy', 'lights']}))
        for metric in metrics:
            values = [(a.get(metric), b.get(metric)) for a, b in pairs]
            if all(a is not None and b is not None and a > 0 and b > 0 for a, b in values):
                key = case+'|'+metric
                series[key] = [math.log(b/a) for a, b in values]
                if not peak and metric in ['mbps', 'stack_cpu_s_per_gb']:
                    simultaneous.append(key)
    groups = {'all':list(cases)}
    if not peak:
        groups.update(single=[c for c in cases if metadata[c]['heavy'] == 1],
            contended=[c for c in cases if metadata[c]['heavy'] > 1],
            cubic=[c for c in cases if metadata[c]['cc'] == 'cubic'],
            bbr=[c for c in cases if metadata[c]['cc'] == 'bbr'])
    for label, selected in groups.items():
        for metric in metrics:
            keys = [c+'|'+metric for c in selected]
            if all(k in series for k in keys):
                series['aggregate:'+label+'|'+metric] = [statistics.mean(series[k][rep] for k in keys) for rep in range(12)]
    stats = bootstrap(series, simultaneous)
    comparisons = {}
    for case in cases:
        changes = {k:stats['series'][case+'|'+k] for k in metrics if case+'|'+k in stats['series']}
        comparisons[case] = dict(**metadata[case], changes=changes)
    aggregates = {label:{m:stats['series']['aggregate:'+label+'|'+m] for m in metrics if 'aggregate:'+label+'|'+m in stats['series']} for label in groups}
    return dict(cells=comparisons, aggregates=aggregates,
        statistics={k:v for k, v in stats.items() if k != 'series'})


def primary_protected(changes, peak=False, simultaneous=False, margin=3):
    field = 'simultaneous_95_pct' if simultaneous else 'bootstrap_95_pct'
    cpu = 'service_cpu_s_per_gb' if peak else 'stack_cpu_s_per_gb'
    return changes['mbps'][field][0] >= -margin and changes[cpu][field][1] <= margin


def decisions(result, c):
    cells = c['cells']
    eligible = lambda v:v['observed_valid'] and v['candidate_strict_valid']
    pointwise = [k for k, v in cells.items() if eligible(v) and primary_protected(v['changes'])]
    simultaneous = [k for k, v in cells.items() if eligible(v) and primary_protected(v['changes'], simultaneous=True)]
    strict_cpu = [k for k, v in cells.items() if eligible(v) and v['changes']['stack_cpu_s_per_gb']['bootstrap_95_pct'][1] <= 0]
    singles = [v for v in cells.values() if v['heavy'] == 1]
    loaded = [v for v in cells.values() if v['heavy'] > 1]
    clear_regression = [k for k, v in cells.items() if v['changes']['mbps']['bootstrap_95_pct'][1] < -3 or v['changes']['stack_cpu_s_per_gb']['bootstrap_95_pct'][0] > 3]
    aggregates = c['aggregates']
    guard = {label:primary_protected(changes) for label, changes in aggregates.items()}
    broad_cpu = {label:all(aggregates[label][m]['bootstrap_95_pct'][1] <= 3 for m in ['stack_link_cpu_s_per_gb', 'process_cpu_s_per_gb']) for label in ['single', 'contended']}
    latency_ok = [v for v in loaded if eligible(v) and all(m in v['changes'] and v['changes'][m]['bootstrap_95_pct'][1] <= 5 for m in ['user_equal_mean_ms', 'worst_user_p99_ms'])]
    latency_clear = [k for k, v in cells.items() if v['heavy'] > 1 and any(m in v['changes'] and v['changes'][m]['bootstrap_95_pct'][0] > 5 for m in ['user_equal_mean_ms', 'worst_user_p99_ms'])]
    candidate = [r for r in result['rows'] if r['arm'] != 'off']
    checks = dict(observations_complete=result['observed_valid'] == result['completed'] == result['planned'] == 576 and result['identity_valid'],
        candidates_strict=all(r['strict_valid'] for r in candidate),
        candidate_bulk_progress=all(r['heavy_zero_window_flows'] == 0 for r in candidate),
        majority_pointwise=len(pointwise) >= 18, majority_simultaneous=len(simultaneous) >= 13,
        all_single_throughput=all(v['changes']['mbps']['bootstrap_95_pct'][0] >= -3 for v in singles),
        no_clear_primary_regression=not clear_regression, all_primary_aggregates=all(guard.values()),
        broad_cpu_aggregates=all(broad_cpu.values()),
        contended_latency_aggregate=all(m in aggregates['contended'] and aggregates['contended'][m]['bootstrap_95_pct'][1] <= -5 for m in ['user_equal_mean_ms', 'worst_user_p99_ms']),
        contended_latency_coverage=len(latency_ok) >= 9 and not latency_clear)
    return dict(representative_scope_qualified=all(checks.values()), checks=checks,
        pointwise_protected_count=len(pointwise), pointwise_protected_cells=pointwise,
        simultaneous_protected_count=len(simultaneous), simultaneous_protected_cells=simultaneous,
        strictly_nonincreasing_cpu_count=len(strict_cpu), strictly_nonincreasing_cpu_cells=strict_cpu,
        clear_primary_regressions=clear_regression, clear_latency_regressions=latency_clear,
        aggregate_protection=guard, broad_cpu_protection=broad_cpu,
        meaning='Enumerated same-host 100-Mbps grid only; +3% practical protection differs from strict zero CPU overhead.')


def collect(destination):
    if not (rig.PREP/'all-measurements-done.json').exists():
        raise RuntimeError('finish every measurement before heavy analysis')
    destination.mkdir(parents=True, exist_ok=True)
    results = {phase:cohort(phase) for phase in ['preflight', 'aa', 'ordinary', 'peak']}
    for phase, result in results.items():
        save(destination/(phase+'-summary.json'), result)
    ordinary = comparison(results['ordinary'])
    peak = comparison(results['peak'], peak=True)
    save(destination/'ordinary-comparisons.json', ordinary)
    save(destination/'peak-comparisons.json', peak)
    verdict = decisions(results['ordinary'], ordinary)
    aa = []
    r = results['aa']
    for case in sorted({x['case'] for x in r['rows']}):
        for rep in range(2):
            pair = sorted([x for x in r['rows'] if x['case'] == case and x['round'] == rep], key=lambda x:x['position'])
            aa.append(dict(case=case, round=rep, valid=all(x['strict_valid'] for x in pair),
                cpu_pct=100*(pair[1]['stack_cpu_s_per_gb']/pair[0]['stack_cpu_s_per_gb']-1),
                throughput_pct=100*(pair[1]['mbps']/pair[0]['mbps']-1)))
    preflight_ok = results['preflight']['strict_valid'] == results['preflight']['planned'] == 8 and results['preflight']['identity_valid']
    aa_ok = r['strict_valid'] == r['planned'] == 16 and r['identity_valid']
    extreme_ok = results['peak']['strict_valid'] == results['peak']['planned'] == 48 and results['peak']['identity_valid']
    peak_verdict = {k:dict(relaxed_10pct=extreme_ok and primary_protected(v['changes'], peak=True, margin=10),
        original_3pct=extreme_ok and primary_protected(v['changes'], peak=True),
        clear_worse_than_10pct=v['changes']['mbps']['bootstrap_95_pct'][1] < -10 or v['changes']['service_cpu_s_per_gb']['bootstrap_95_pct'][0] > 10) for k, v in peak['cells'].items()}
    verdict.update(preflight_valid=preflight_ok, aa_valid=aa_ok, extreme_valid=extreme_ok,
        extreme_verdict=peak_verdict, aa=aa,
        scoped_recommendation_supported=verdict['representative_scope_qualified'] and preflight_ok and aa_ok and extreme_ok and not any(v['clear_worse_than_10pct'] for v in peak_verdict.values()),
        unknown_capacity_default_supported=False, other_bandwidths_proven=False,
        no_literal_zero_overhead_claim=True, old_peak_3pct_verdicts_unchanged=True)
    save(destination/'verdict.json', verdict)
    counts = {k:dict(planned=v['planned'], completed=v['completed'], strict_valid=v['strict_valid'], observed_valid=v['observed_valid'], identity_valid=v['identity_valid']) for k, v in results.items()}
    save(destination/'cohorts.json', dict(cohorts=counts, total_planned=sum(v['planned'] for v in results.values()),
        throttled_events=sum(row.get('nr_throttled', 0) for v in results.values() for row in v['rows']),
        scheduled_requests=sum(row.get('planned_requests', 0) for v in results.values() for row in v['rows']),
        missing_requests=sum(row.get('missing_requests', 0) for v in results.values() for row in v['rows']),
        candidate_zero_progress=sum(row.get('heavy_zero_window_flows', 0) for row in results['ordinary']['rows'] if row['arm'] != 'off')))
    return dict(cohorts=counts, representative_scope_qualified=verdict['representative_scope_qualified'],
        checks=verdict['checks'], pointwise=verdict['pointwise_protected_count'], simultaneous=verdict['simultaneous_protected_count'], extreme=peak_verdict)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('destination', type=Path)
    a = p.parse_args()
    print(json.dumps(collect(a.destination.resolve()), indent=2))
