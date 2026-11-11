#!/usr/bin/env python3
"""Post-run descriptive facts from raw receivers/timings; no changed verdict or new traffic."""
import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo


def read(path):
    return json.loads(path.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--recomputed', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    directory = args.evidence / 'order-rate'
    plan = read(directory / 'plan.json')
    execution = read(directory / 'execution.json')
    recomputed = read(args.recomputed)
    assert recomputed['matches_remote'] is True
    assert [(r['round'], r['arm']) for r in execution] == [tuple(r) for r in plan['order']]
    assert len(execution) == 60 and len({r['label'] for r in execution}) == 60
    validity = {(r['round'], r['arm']): r for r in recomputed['validity']}
    result_rows, changes = [], []
    outcomes = {}
    for row in execution:
        label, arm = row['label'], row['arm']
        sample = read(directory / label / 'receiver.json')['udp']
        sent = read(directory / label / 'sender.json')
        summary = read(directory / (label + '.json'))
        phases = summary['planned'] if arm == 'F' else summary['planned_ns']
        start, end, recovery = [phases[key] for key in
                               (('start_ns', 'end_ns', 'recovery_start_ns') if arm == 'F' else ('start', 'end', 'recovery'))]
        best, incidents = -1, []
        for seq, send_ns, recv_ns, _ in sample:
            if seq < best:
                phase = ('baseline' if send_ns < start else 'loaded' if send_ns < end else
                         'drain-or-guard' if send_ns < recovery else 'recovery')
                incidents.append({'seq': seq, 'overtaker': best, 'send_phase': phase,
                                  'send_offset_from_loaded_ms': (send_ns - start) / 1e6})
            best = max(best, seq)
        seqs = [s[0] for s in sample]
        missing = sorted(set(range(sent['sent'])) - set(seqs))
        valid = validity[(row['round'], arm)]
        outcomes[(row['round'], arm)] = bool(incidents) if valid['valid'] else None
        facts = {'label': label, 'round': row['round'], 'arm': arm, 'valid': valid['valid'],
                 'invalid_reasons': valid['reasons'], 'sent': sent['sent'], 'received': len(sample),
                 'duplicates': len(seqs) - len(set(seqs)), 'missing': missing,
                 'overtaken': len(incidents), 'incidents': incidents,
                 'child_status': row['child']['status'], 'child_exit_code': row['child']['exit_code'],
                 'owned_process_cleanup': row['child']['cleanup_status'], 'cleanup_safe': row['cleanup_safe']}
        if arm == 'F':
            for change in summary['actual']['changes']:
                changes.append({'run': label, 'phase': change['phase'],
                                'duration_ms': (change['t1_ns'] - change['t0_ns']) / 1e6,
                                'start_offset_from_loaded_ms': (change['t0_ns'] - start) / 1e6})
            early = [s for s in sample if start + 50_000_000 <= s[1] < end and s[2] - s[1] < 500_000_000]
            facts['active_interior_under_500ms'] = len(early)
            if early:
                active_change = next(c for c in summary['actual']['changes'] if c['phase'] == 'active')
                during = [s for s in sample if active_change['t0_ns'] <= s[1] <= s[2] <= active_change['t1_ns']]
                facts['sent_and_received_within_active_change'] = len(during)
                facts['early_active_samples'] = {'first': early[0], 'last': early[-1],
                                                'min_latency_ms': min(s[2]-s[1] for s in early)/1e6}
        elif missing:
            facts['neighbor_samples'] = [s for s in sample if any(abs(s[0] - m) < 3 for m in missing)]
            facts['control_actual_ns'] = summary['actual_ns']
            facts['control_planned_ns'] = phases
        result_rows.append(facts)
    # Separate calculation of the registered paired statistics; no imported driver math.
    x = y = wx = wy = 0
    for number in range(1, 31):
        f, p = outcomes[(number, 'F')], outcomes[(number, 'P')]
        if f is not None and p is not None:
            x += int(f and not p)
            y += int(p and not f)
        f, p = (False if f is None else f), (True if p is None else p)
        wx += int(f and not p)
        wy += int(p and not f)
    tail = lambda a, b: sum(math.comb(a+b, k) for k in range(a, a+b+1)) / 2**(a+b)
    assert tail(x, y) == recomputed['summary']['paired_one_sided_p']
    assert tail(wx, wy) == recomputed['summary']['worst_case_p']
    launch = read(args.evidence / 'launch-result.json')
    local = lambda ns: datetime.fromtimestamp(ns / 1e9, ZoneInfo('Asia/Ho_Chi_Minh')).isoformat()
    result = {'source_sha': plan['tested_sha'], 'registered_verdict_unchanged': recomputed['summary']['verdict'],
              'started_local': local(launch['measurement_started_unix_ns']),
              'finished_local': local(launch['measurement_finished_unix_ns']),
              'measurement_elapsed_seconds': (launch['measurement_finished_unix_ns'] - launch['measurement_started_unix_ns']) / 1e9,
              'all_60_once_in_frozen_order': True, 'all_child_cleanup_pass': all(r['owned_process_cleanup']=='PASS' for r in result_rows),
              'complete_pair_discordance_F_P': [x, y], 'paired_p': tail(x, y),
              'worst_case_discordance_F_P': [wx, wy], 'worst_case_p': tail(wx, wy),
              'overtaken_datagrams_by_arm': dict(Counter({arm: sum(r['overtaken'] for r in result_rows if r['arm']==arm) for arm in 'FP'})),
              'longest_phase_changes': sorted(changes, key=lambda r:r['duration_ms'], reverse=True)[:6],
              'runs': result_rows,
              'limits': 'Descriptive post-run timing only. Send phase is not the phase of receiver delivery. No tracing, CPU/path attribution, shared-cause proof, equivalence claim, or regrade.'}
    with args.output.open('x') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('runs','longest_phase_changes')}, indent=2))


if __name__ == '__main__':
    main()
