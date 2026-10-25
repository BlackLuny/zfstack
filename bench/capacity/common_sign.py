#!/usr/bin/env python3
"""Blinded supplemental median sign/partial-conjunction test; no new samples."""
import argparse
import json
import math
from pathlib import Path

from run import save


def sign_tail(successes, trials):
    if not 0 <= successes <= trials:
        raise ValueError('invalid sign counts')
    return sum(math.comb(trials, k) for k in range(successes, trials+1))/2**trials


def analyze(directory):
    summary = json.loads((directory/'ordinary-summary.json').read_text())
    result = json.loads((directory/'ordinary-comparisons.json').read_text())
    cells = []
    for name, cell in sorted(result['cells'].items()):
        cpu = cell['changes']['stack_cpu_s_per_gb']['paired_pct']
        throughput = cell['changes']['mbps']['paired_pct']
        if len(cpu) != 12 or len(throughput) != 12:
            raise ValueError('every enrolled pair must remain present')
        good_cpu = sum(v < 3-1e-12 for v in cpu)
        good_throughput = sum(v > -3+1e-12 for v in throughput)
        p_cpu = sign_tail(good_cpu, 12)
        p_throughput = sign_tail(good_throughput, 12)
        cells.append(dict(case=name, pairs=12, cpu_successes=good_cpu, throughput_successes=good_throughput,
            p_cpu=p_cpu, p_throughput=p_throughput, p_union=max(p_cpu, p_throughput)))
    if len(cells) != 24:
        raise ValueError('the frozen question requires exactly 24 cells')
    ordered = sorted(c['p_union'] for c in cells)
    majority = min(1., 12*ordered[12])
    valid = summary['identity_valid'] and summary['observed_valid'] == summary['planned'] == 576 and all(c['candidate_strict_valid'] for c in result['cells'].values())
    return dict(valid_observations=valid, cells=cells, tested_protected_cell_count=13,
        thirteenth_ordered_p=ordered[12], partial_conjunction_p=majority,
        majority_median_protection_supported=valid and majority <= .05, alpha=.05,
        estimand='Within-cell median paired CPU/GB < +3% and throughput > -3%; same enumerated 24-cell grid.',
        assumptions='Independent run-pair signs within each cell; arbitrary between-cell/endpoint dependence. No strict-zero CPU, all-networks or primary-gate override claim.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    result = analyze(a.directory.resolve())
    save(a.output.resolve(), result)
    print(json.dumps({k:v for k, v in result.items() if k != 'cells'}, indent=2))
