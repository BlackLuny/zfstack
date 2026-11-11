#!/usr/bin/env python3
"""Independent, longer confirmation; no screening samples enter its estimates."""
import argparse
import json
from pathlib import Path
import random
import statistics
import time

from analyze import summarize_run
from run import ARMS, CASES, ROOT, counters, execute, save, sha

CASE = 'isolate200_rtt10'


def sequence():
    order = ['AC'] * 6 + ['CA'] * 6
    random.Random(20261024).shuffle(order)
    return [dict(index=2*i+j, case=CASE, block=f'pair{i:02d}', position=j, arm=arm)
            for i, pair in enumerate(order) for j, arm in enumerate(pair)]


def run(out, container):
    out.mkdir(parents=True, exist_ok=False)
    previous = json.loads((ROOT / 'qa/results/capacity-20261023/plan.json').read_text())
    binary = ROOT / 'target/release/zfbench'
    if sha(binary) != previous['binary_sha256']:
        raise RuntimeError('binary differs from screening')
    args = list(CASES[CASE])
    args[args.index('--secs') + 1] = '20'
    args[args.index('--warmup') + 1] = '4'
    sources = dict(previous['sources'])
    for path in ['bench/capacity/confirm.py', 'bench/capacity/analyze.py',
                 'docs/experiments/2026-10-23-capacity/CONFIRMATION.md']:
        sources[path] = sha(ROOT / path)
    for path, expected in sources.items():
        if sha(ROOT / path) != expected:
            raise RuntimeError(f'frozen source differs: {path}')
    manifest = dict(base_sha=previous['base_sha'], binary_sha256=sha(binary), sources=sources,
                    rows=sequence(), cases={CASE: args}, arms={k:ARMS[k] for k in ('A', 'C')},
                    pair_order_seed=20261024, bootstrap_seed=20261025, environment=counters(),
                    screening_plan_sha256=sha(ROOT / 'qa/results/capacity-20261023/plan.json'))
    save(out / 'plan.json', manifest)
    for row in manifest['rows']:
        name = f"{row['index']:03d}-{row['case']}-{row['block']}-{row['position']}-{row['arm']}"
        execute(out, name, row, container, binary, manifest['binary_sha256'], extra=args)
    save(out / 'complete.json', dict(attempts=24, end_unix=time.time()))


def interval(values):
    rng = random.Random(20261025)
    samples = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(10000))
    return dict(paired_pct=values, median_pct=statistics.median(values),
                bootstrap_95_pct=[samples[249], samples[9749]])


def analyze(out):
    rows = [summarize_run(p, expected_loaded=400) for p in sorted(out.glob('[0-9][0-9][0-9]-*'))
            if (p / 'end.json').exists()]
    pairs = []
    for i in range(12):
        pair = [r for r in rows if r['block'] == f'pair{i:02d}']
        if len(pair) == 2:
            pair.sort(key=lambda r:r['arm'])
            pairs.append(pair)
    keys = ['mbps', 'service_cpu_s_per_gb', 'service_cpu_s_per_s', 'rss_hwm_kib',
            'user_equal_mean_ms', 'worst_user_p99_ms']
    changes = {}
    for key in keys:
        values = [100*(c[key]/a[key]-1) for a, c in pairs
                  if a.get(key) is not None and c.get(key) is not None and a[key] > 0]
        if len(values) == 12:
            changes[key] = interval(values)
    return dict(planned=24, completed=len(rows), valid=sum(r['valid'] for r in rows),
                all_valid=len(rows) == 24 and all(r['valid'] for r in rows),
                changes_descriptive=changes, rows=rows,
                scope='Independent selected-hypothesis local cell, 12 pairs; invalid runs preclude confirmation; no production claim')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('out', type=Path)
    p.add_argument('--container', default='zfstack-capacity-20261023')
    p.add_argument('--analyze', action='store_true')
    a = p.parse_args()
    out = a.out.resolve()
    out.relative_to(ROOT)
    if a.analyze:
        print(json.dumps(analyze(out), indent=2, sort_keys=True, allow_nan=False))
    else:
        run(out, a.container)


if __name__ == '__main__':
    main()
