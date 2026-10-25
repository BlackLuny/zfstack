#!/usr/bin/env python3
"""Descriptive audits after sampling; never alter the frozen qualification."""
import json
import statistics

import adaptive_cases as rig
from adaptive_dynamic import recovery
from run import ROOT


def collect(directory):
    cohorts = {}
    fairness = {}
    for prefix, phases, candidate in [('', rig.PHASES, 'a1'),
                                      ('pilot-a0/', ['pilot'], 'a0'),
                                      ('pilot-a1/', ['pilot'], 'a1')]:
        for phase in phases:
            summary = json.loads((directory / (prefix + phase + '-summary.json')).read_text())
            name = prefix + phase
            base = ROOT / 'qa/results' / ('adaptive-20261025-' +
                    ('' if candidate == 'a0' else 'a1-') + phase)
            counters = dict(attempts=len(summary['rows']), planned_requests=0,
                            completed_requests=0, missing_requests=0,
                            generator_late_requests=0, completed_over_3s=0,
                            throttled_events=summary['throttled_events'])
            failures = []
            maximum_latency = 0
            for row in summary['rows']:
                path = base / rig.name(row)
                arrivals = path / 'arrivals.json'
                streams = json.loads(arrivals.read_text()) if arrivals.exists() else []
                for stream in streams:
                    if len(stream['arrivals']) != stream['count']:
                        raise ValueError('incomplete ledger: ' + str(path))
                    counters['planned_requests'] += stream['count']
                    for arrival in stream['arrivals']:
                        complete = arrival['outcome'] == 'completed'
                        counters['completed_requests' if complete else 'missing_requests'] += 1
                        if arrival['started_ns'] is not None:
                            counters['generator_late_requests'] += (
                                arrival['started_ns'] - arrival['planned_ns'] > 20_000_000)
                        if complete:
                            latency = (arrival['completed_ns'] - arrival['planned_ns']) / 1e6
                            maximum_latency = max(maximum_latency, latency)
                            counters['completed_over_3s'] += latency > 3000
                if row['original_outer_returncode'] != 0 or not row['observation_valid']:
                    failures.append(dict(name=path.name,
                        outer_returncode=row['original_outer_returncode'],
                        original_strict_valid=row['original_strict_valid'],
                        observation_valid=row['observation_valid'],
                        original_observer_errors=row['original_observer_errors'],
                        certified_observer_errors=row['certified_observer_errors'],
                        audit_errors=row['audit_errors'], missing_requests=row.get('missing_requests', 0)))
            cohorts[name] = dict(**counters, max_completed_latency_ms=maximum_latency,
                                 original_strict_valid=summary['original_strict_valid'],
                                 observed_valid=summary['observed_valid'], failures=failures)
            if not prefix and phase in ['primary', 'bandwidth', 'heavy']:
                cells = {}
                for case in sorted({r['case'] for r in summary['rows']}):
                    arms = {}
                    for arm in ['off', 'auto']:
                        rows = [r for r in summary['rows'] if r['case'] == case and r['arm'] == arm]
                        gaps = [r['max_observed_no_read_sec'] for r in rows]
                        arms[arm] = dict(attempts=len(rows),
                            median_jain=statistics.median(r['heavy_jain'] for r in rows),
                            median_worst_flow_no_read_sec=statistics.median(gaps),
                            maximum_no_read_sec=max(gaps),
                            zero_entire_window_flows=sum(r['heavy_zero_window_flows'] for r in rows))
                    cells[case] = arms
                fairness[phase] = cells

    summary = json.loads((directory / 'dynamic-summary.json').read_text())
    transitions = json.loads((directory / 'dynamic-transitions.json').read_text())
    control_recovery = []
    for pair in transitions['pairs']:
        entry = dict(case=pair['case'], round=pair['round'], stages={})
        for label, at, end in [('lower', 10, 25), ('restored', 25, 50)]:
            reference = pair['stages'][label]['baseline_plateau_mbps']
            arms = {}
            for arm in ['off', 'auto']:
                row = next(r for r in summary['rows'] if r['case'] == pair['case']
                           and r['round'] == pair['round'] and r['arm'] == arm)
                raw = json.loads((ROOT / 'qa/results/adaptive-20261025-a1-dynamic' /
                                  rig.name(row) / 'stdout.json').read_text())
                progress = raw['results']['heavy']['progress_250ms']
                arms[arm] = dict(
                    original_time_bound=recovery(progress, at, pair['recovery_limit_sec'], reference),
                    whole_phase_descriptive=recovery(progress, at, end - at, reference))
            entry['stages'][label] = arms
        control_recovery.append(entry)
    keys = ['attempts', 'planned_requests', 'completed_requests', 'missing_requests',
            'generator_late_requests', 'completed_over_3s', 'throttled_events',
            'original_strict_valid', 'observed_valid']
    totals = {k: sum(v[k] for v in cohorts.values()) for k in keys}
    primary = json.loads((directory / 'primary-summary.json').read_text())['rows']
    environment_pairs = []
    for case in sorted({r['case'] for r in primary}):
        for rep in range(8):
            arms = {arm: next(r for r in primary if r['case'] == case
                    and r['round'] == rep and r['arm'] == arm) for arm in ['off', 'auto']}
            environment_pairs.append(dict(case=case, round=rep,
                order='AB' if arms['off']['position'] < arms['auto']['position'] else 'BA',
                throughput_change_pct=100 * (arms['auto']['mbps'] / arms['off']['mbps'] - 1),
                arms={arm: dict(global_window_steal_seconds=r['window_steal_seconds'],
                    global_window_softirq_seconds=r['window_softirq_seconds'],
                    driver_runqueue_ns=r['telemetry']['driver']['schedstat_delta']['runqueue_ns'],
                    driver_migrations=r['telemetry']['driver']['scheduler_delta']['se.nr_migrations'],
                    throttled_events=r['nr_throttled']) for arm, r in arms.items()}))
    return dict(cohorts=cohorts, totals_including_both_pilots=totals,
                bulk_fairness_descriptive=fairness,
                primary_environment_pairs_descriptive=environment_pairs,
                dynamic_control_recovery_descriptive=control_recovery,
                note='Post-sampling descriptive audit. Original qualification and time/plateau bounds unchanged. '
                     'Whole-phase recovery is explanatory, not a replacement guard. Jain covers bulk flows of '
                     'one peer; 250-ms read-progress silence is neither exact packet starvation nor a latency theorem.')
