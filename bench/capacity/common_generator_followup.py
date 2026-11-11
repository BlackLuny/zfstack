#!/usr/bin/env python3
"""One independent full-cell confirmation after the blinded generator miss."""
import argparse
import json
import math
from pathlib import Path
import random

import common_cases as rig
import common_collect as collect
from common_reaudit import certify
from run import ROOT, save, sha

OUT = ROOT/'qa/results/common-20261025-generator-confirmation'


def sequence():
    orders = ['AB']*6 + ['BA']*6
    random.Random(2026102504).shuffle(orders)
    rows = []
    for rep, order in enumerate(orders):
        for position, letter in enumerate(order):
            arm = 'off' if letter == 'A' else 'p99'
            rows.append(dict(index=len(rows), kind='native', case='cubic-r100-n8-fixed', cc='cubic', rtt=100,
                heavy=8, lights=3, placement='fixed', round=rep, position=position,
                block=f'generator-confirm-r{rep:02d}', arm=arm, binary='v5', tokio_stack=True, splice=True,
                warmup=8, measure=20, retry_budget=1, egress_rate=0 if arm == 'off' else 12_375_000))
    return rows


def run():
    if not (rig.PREP/'done.json').exists():
        raise RuntimeError('finish original 648 attempts first')
    rows = sequence()
    original = rig.sequence
    rig.sequence = lambda phase:rows
    try:
        rig.run(OUT, 'generator-confirmation')
    finally:
        rig.sequence = original
    save(rig.PREP/'all-measurements-done.json', dict(original_plan=648, independent_followup=24, complete=True))


def analyze():
    if not (rig.PREP/'all-measurements-done.json').exists():
        raise RuntimeError('finish both measurement cohorts first')
    plan = json.loads((OUT/'plan.json').read_text())
    errors = []
    if plan['rows'] != sequence() or not (OUT/'complete.json').exists():
        errors.append('followup incomplete or sequence differs')
    for path, digest in plan['sources'].items():
        if sha(OUT/'source'/path) != digest:
            errors.append('source snapshot differs: '+path)
    rows, certs = [], []
    for row in plan['rows']:
        rows.append(rig.inspect(OUT, row, plan['container']))
        certs.append(certify(OUT, row, plan['container']))
    pairs = []
    for rep in range(12):
        group = [r for r in rows if r['round'] == rep]
        pairs.append((next(r for r in group if r['arm'] == 'off'), next(r for r in group if r['arm'] == 'p99')))
    series = {}
    for metric in collect.METRICS:
        if all(a.get(metric) is not None and b.get(metric) is not None and a[metric] > 0 and b[metric] > 0 for a, b in pairs):
            series[metric] = [math.log(b[metric]/a[metric]) for a, b in pairs]
    stats = collect.bootstrap(series)
    changes = stats['series']
    observed = not errors and all(c['independently_observed_valid'] for c in certs)
    candidates = [c for c, row in zip(certs, rows) if row['arm'] == 'p99']
    checks = dict(complete=observed and len(rows) == 24,
        candidate_service=all(c['independently_service_valid'] for c in candidates),
        candidate_bulk_progress=all(r['heavy_zero_window_flows'] == 0 for r in rows if r['arm'] == 'p99'),
        primary=collect.primary_protected(changes),
        latency=all(m in changes and changes[m]['bootstrap_95_pct'][1] <= 5 for m in ['user_equal_mean_ms', 'worst_user_p99_ms']))
    return dict(planned=24, completed=len(rows), original_strict_valid=sum(r['strict_valid'] for r in rows),
        independently_observed_valid=sum(c['independently_observed_valid'] for c in certs),
        identity_valid=not errors, errors=errors, rows=rows, certificates=certs,
        changes=changes, statistics={k:v for k, v in stats.items() if k != 'series'},
        checks=checks, protected=all(checks.values()),
        no_pooling_with_original=True, original_matrix_remains_unqualified=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--analyze', type=Path)
    a = p.parse_args()
    if a.analyze:
        result = analyze()
        save(a.analyze.resolve(), result)
        print(json.dumps({k:v for k, v in result.items() if k not in ['rows', 'certificates', 'changes']}, indent=2))
    else:
        run()
