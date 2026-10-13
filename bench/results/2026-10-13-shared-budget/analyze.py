#!/usr/bin/env python3
"""Revalidate recorded shared-budget runs with the archived admission checker."""
import argparse
import importlib.util
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('recorded_case', args.root / 'shared_budget_case.py')
    case = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(case)
    summary = {'cases': [], 'matrix_rounds': 0, 'matrix_verified_bytes': 0}
    combinations = set()
    for path in sorted((args.root / 'runs').glob('*/result.json')):
        result = json.loads(path.read_text())
        assert result['ok'] and not result['errors'] and result['exit_code'] == 0
        assert result['cleanup_ok'] and result['namespace_removed'] and result['children_reaped']
        assert result['links_after_exit'] == ['lo']
        rows = [json.loads(line) for line in (path.parent / 'probe.jsonl').read_text().splitlines()]
        config = next(row for row in rows if row['type'] == 'config')
        validation = case.validate(rows, config['rounds'])
        assert validation == result['validation']
        samples = [row for row in rows if row['type'] == 'sample']
        done = [row for row in rows if row['type'] == 'round_done']
        natural = [row for row in rows if row['type'] == 'natural_done']
        release = [row for row in rows if row['type'] == 'release_done']
        entry = {'name': path.parent.name, 'source_sha': result['source_sha'], 'binary_sha256': result['binary_sha256'],
                 'config': config, 'validation': validation,
                 'peak_sampled_global_bytes': max(row['global_bytes'] for row in samples),
                 'natural_global_failures': [row['global_failures'] for row in natural],
                 'reader_queue_drops': [row['ingress_queue_drops'] for row in done],
                 'port_a_survived_cancel_bytes': [row['peer_a_survived_bytes'] for row in done],
                 'injected_host_bytes': [row['injected_host_bytes'] for row in release],
                 'held_response_bytes': [row['bytes'] for row in rows if row['type'] == 'held'],
                 'elapsed_sec': result['elapsed_sec'], 'cleanup_ok': True}
        summary['cases'].append(entry)
        if path.parent.name.startswith('matrix-'):
            combination = (config['mtu'], config['global_bytes'], config['trim'])
            assert combination not in combinations and config['rounds'] == 3
            assert config['flows_per_port'] == 8 and config['bytes_per_flow'] == 1 << 20
            combinations.add(combination)
            summary['matrix_rounds'] += config['rounds']
            summary['matrix_verified_bytes'] += validation['natural_verified_bytes'] + validation['release_verified_bytes']
    expected = {(mtu, mib << 20, trim) for mtu in [512, 1500] for mib in [1, 2] for trim in [False, True]}
    assert combinations == expected
    assert summary['matrix_rounds'] == 24
    assert summary['matrix_verified_bytes'] == 408 << 20
    summary['scope'] = 'real kernel TCP; two Tokio driver OS threads; injected host-buffer gate is separate from natural traffic; ledger is not total RSS'
    (args.root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
