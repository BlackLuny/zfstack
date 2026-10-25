#!/usr/bin/env python3
"""Conservative fixed-grid median test: unknown cells receive p=1."""
import argparse
import json
from pathlib import Path

from common_sign import sign_tail
from run import save


def analyze(directory):
    result = json.loads((directory/'ordinary-comparisons.json').read_text())
    cells = []
    for name, cell in sorted(result['cells'].items()):
        known = cell['observed_valid'] and cell['candidate_strict_valid'] and cell['pairs'] == 12
        cpu = cell['changes']['stack_cpu_s_per_gb']['paired_pct']
        throughput = cell['changes']['mbps']['paired_pct']
        if len(cpu) != 12 or len(throughput) != 12:
            raise ValueError('all enrolled pairs must remain present')
        pc = sign_tail(sum(v < 3-1e-12 for v in cpu), 12) if known else 1.
        pt = sign_tail(sum(v > -3+1e-12 for v in throughput), 12) if known else 1.
        cells.append(dict(case=name, complete=known, p_cpu=pc, p_throughput=pt, p_union=max(pc, pt)))
    if len(cells) != 24:
        raise ValueError('keep the full 24-cell hypothesis grid')
    value = min(1., 12*sorted(c['p_union'] for c in cells)[12])
    return dict(cells=cells, total_hypotheses=24, complete_cells=sum(c['complete'] for c in cells),
        tested_protected_cell_count=13, partial_conjunction_p=value,
        majority_median_protection_supported=value <= .05,
        note='Incomplete cells receive p=1, never disappear. Same sign-test assumptions/margins. Does not validate the original whole-matrix gate.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    result = analyze(a.directory.resolve())
    save(a.output.resolve(), result)
    print(json.dumps({k:v for k, v in result.items() if k != 'cells'}, indent=2))
