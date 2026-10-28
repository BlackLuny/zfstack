#!/usr/bin/env python3
"""Majority-objective decision, separate from the failed whole-matrix gate."""
import argparse
import json
from pathlib import Path

from common_collect import primary_protected
from run import save


def analyze(directory):
    audited = directory/'audited'
    result = json.loads((audited/'ordinary-comparisons.json').read_text())
    audit = json.loads((audited/'independent-audit.json').read_text())
    majority = json.loads((audited/'majority-with-unknowns.json').read_text())
    followup = json.loads((directory/'generator-confirmation-summary.json').read_text())
    cells = result['cells']
    complete = {k:v for k, v in cells.items() if v['observed_valid'] and v['candidate_strict_valid']}
    single = [v for v in complete.values() if v['heavy'] == 1]
    loaded = [v for v in complete.values() if v['heavy'] > 1]
    pointwise = [k for k, v in complete.items() if primary_protected(v['changes'])]
    simultaneous = [k for k, v in complete.items() if primary_protected(v['changes'], simultaneous=True)]
    latency_keys = ['user_equal_mean_ms', 'worst_user_p99_ms']
    latency = [v for v in loaded if all(m in v['changes'] and v['changes'][m]['bootstrap_95_pct'][1] <= 5 for m in latency_keys)]
    improved = [v for v in loaded if all(m in v['changes'] and v['changes'][m]['bootstrap_95_pct'][1] <= -5 for m in latency_keys)]
    clear_primary = [k for k, v in complete.items() if v['changes']['mbps']['bootstrap_95_pct'][1] < -3 or v['changes']['stack_cpu_s_per_gb']['bootstrap_95_pct'][0] > 3]
    clear_latency = [k for k, v in complete.items() if v['heavy'] > 1 and any(m in v['changes'] and v['changes'][m]['bootstrap_95_pct'][0] > 5 for m in latency_keys)]
    checks = dict(complete_original_cells=len(complete) >= 22,
        pointwise_primary=len(pointwise) >= 18, simultaneous_primary=len(simultaneous) >= 13,
        conservative_majority_sign=majority['majority_median_protection_supported'],
        single_throughput=len(single) == 12 and all(v['changes']['mbps']['bootstrap_95_pct'][0] >= -3 for v in single),
        single_cpu_scopes=all(result['aggregates']['single'][m]['bootstrap_95_pct'][1] <= 3 for m in ['stack_cpu_s_per_gb', 'stack_link_cpu_s_per_gb', 'process_cpu_s_per_gb']),
        no_clear_primary_regression=not clear_primary, latency_coverage=len(latency) >= 9,
        substantial_latency_coverage=len(improved) >= 6, no_clear_latency_regression=not clear_latency,
        independent_affected_cell=followup['protected'],
        extreme_observations=audit['other_cohorts_independently_valid'],
        extreme_relaxed_bounds=all(v['relaxed_10pct'] for v in audit['extreme_verdict'].values()))
    return dict(known_capacity_majority_default_supported=all(checks.values()), checks=checks,
        complete_original_cells=len(complete), unknown_original_cells=sorted(set(cells)-set(complete)),
        pointwise_protected_cells=pointwise, simultaneous_protected_cells=simultaneous,
        latency_nonregressing_cells=len(latency), latency_clearly_improved_cells=len(improved),
        clear_primary_regressions=clear_primary, clear_latency_regressions=clear_latency,
        exact_majority_p=majority['partial_conjunction_p'],
        original_whole_matrix_gate_passed=False, fresh_confirmation_pooled=False,
        unknown_capacity_default_supported=False,
        scope='Majority of enumerated same-host 100-Mbps cases, 1-500 ms; known per-iface IP capacity required. Preserve the failed original full-observation gate and separate fresh-cell confirmation.')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    result = analyze(a.directory.resolve())
    save(a.output.resolve(), result)
    print(json.dumps(result, indent=2))
