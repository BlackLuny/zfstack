#!/usr/bin/env python3
"""Frozen single/two-flow peak guard for the local-client TX-cap candidate."""
import argparse
import json
from pathlib import Path
import random
import time

from run import ARMS, CASES, ROOT, counters, execute, save, sha


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('out', type=Path)
    p.add_argument('--container', default='zfstack-capacity-20261023')
    a = p.parse_args()
    out = a.out.resolve()
    out.relative_to(ROOT)
    out.mkdir(parents=True, exist_ok=False)
    previous = json.loads((ROOT / 'qa/results/capacity-20261023/plan.json').read_text())
    binary = ROOT / 'target/release/zfbench'
    if sha(binary) != previous['binary_sha256']:
        raise RuntimeError('binary differs from screening')
    cases = {}
    for flows in (1, 2):
        args = list(CASES['client_down8'])
        args[args.index('--flows') + 1] = str(flows)
        args[args.index('--secs') + 1] = '20'
        args[args.index('--warmup') + 1] = '4'
        cases[f'client_down{flows}'] = args
    order = list(cases)
    random.Random(20261026).shuffle(order)
    rows = []
    for case in order:
        for block, arms in [('AA', 'AA'), ('B', 'ABBA')]:
            for position, arm in enumerate(arms):
                rows.append(dict(index=len(rows), case=case, block=block, position=position, arm=arm))
    sources = dict(previous['sources'])
    for path in ['bench/capacity/low_concurrency.py', 'bench/capacity/analyze.py',
                 'docs/experiments/2026-10-23-capacity/LOW-CONCURRENCY.md']:
        sources[path] = sha(ROOT / path)
    for path, expected in sources.items():
        if sha(ROOT / path) != expected:
            raise RuntimeError(f'frozen source differs: {path}')
    manifest = dict(base_sha=previous['base_sha'], binary_sha256=sha(binary), sources=sources,
                    rows=rows, cases=cases, arms={k:ARMS[k] for k in ('A', 'B')}, seed=20261026,
                    environment=counters(), screening_plan_sha256=sha(ROOT / 'qa/results/capacity-20261023/plan.json'))
    save(out / 'plan.json', manifest)
    for row in rows:
        name = f"{row['index']:03d}-{row['case']}-{row['block']}-{row['position']}-{row['arm']}"
        execute(out, name, row, a.container, binary, manifest['binary_sha256'], extra=cases[row['case']])
    save(out / 'complete.json', dict(attempts=12, end_unix=time.time()))


if __name__ == '__main__':
    main()
