#!/usr/bin/env python3
"""Revalidate all stages, retaining overload controls and rejected acceptance."""
import importlib.util
import json
from pathlib import Path
import sys


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def analyze(root):
    legacy = module(root / 'runner-before.py', 'legacy')
    current = module(root / 'runner-final.py', 'current')
    cases = []
    for path in sorted((root / 'runs').glob('*/result.json')):
        result = json.loads(path.read_text())
        rows = [json.loads(line) for line in path.with_name('probe.jsonl').read_text().splitlines()]
        config = rows[0]
        phase = path.parent.name.split('-')[0]
        assert result['exit_code'] == 0 and result['cleanup_ok'], path
        if phase == 'before':
            validation = legacy.validate(rows, config['rounds'])
            assert result['ok']
        else:
            validation = current.validate(rows, config['rounds'], allow_ingress_drops=True)
            for row in rows:
                if row['type'] == 'round_done':
                    assert sum(row['ingress_drops_by_port']) == row['ingress_queue_drops']
                    assert all(a <= b for a, b in zip(row['ingress_ack_only_drops'], row['ingress_drops_by_port']))
                    assert max(row['ingress_high_water']) <= config['ingress_capacity']
            # Stage 1 discovered a second loss layer; preserve those five rejects.
            if not result['ok']:
                assert phase == 'after' and result['errors'] == [
                    'ingress drops invalidate clean acceptance; use explicit diagnostic override']
                assert not validation['clean_ingress']
        clean_candidate = phase == 'final' and config['ingress_capacity'] == config['tun_queue_len'] == 4096
        if clean_candidate:
            current.validate(rows, config['rounds'])
            assert result['ok'] and not result['validation']['allow_ingress_drops']
        completed = [row for row in rows if row['type'] == 'round_done']
        cases.append({
            'name': path.parent.name, 'config': config, 'source_sha': result['source_sha'],
            'binary_sha256': result['binary_sha256'], 'runner_ok': result['ok'],
            'rounds': len(completed), 'verified_bytes': validation['natural_verified_bytes'] + validation['release_verified_bytes'],
            'clean_candidate': clean_candidate,
            'queue_drops': [row['ingress_queue_drops'] for row in completed],
            'kernel_drops': None if phase == 'before' else [row['tun_tx_dropped'] for row in completed],
            'ack_only_drops': None if phase == 'before' else sum(sum(row['ingress_ack_only_drops']) for row in completed),
            'max_queue_occupancy': None if phase == 'before' else max(max(row['ingress_high_water']) for row in completed),
            'kernel_drops_after_natural': [row['tun_tx_dropped'] for row in rows if row['type'] == 'natural_done' and 'tun_tx_dropped' in row],
            'release_observed_us': validation['release_observed_us'],
        })
    assert len(cases) == 40, len(cases)
    clean = [case for case in cases if case['clean_candidate']]
    assert len(clean) == 10 and sum(case['rounds'] for case in clean) == 100
    normal = [case['config'] for case in clean if case['name'].endswith('normal')]
    assert {(c['mtu'], c['global_bytes'], c['trim']) for c in normal} == {
        (mtu, mib << 20, trim) for mtu in [512, 1500] for mib in [1, 2] for trim in [False, True]}
    assert sum(not case['runner_ok'] for case in cases) == 5
    return {'cases': cases, 'total_rounds': sum(case['rounds'] for case in cases),
            'total_verified_bytes': sum(case['verified_bytes'] for case in cases),
            'clean_rounds': sum(case['rounds'] for case in clean),
            'clean_verified_bytes': sum(case['verified_bytes'] for case in clean),
            'scope': 'queue-overflow diagnosis; cancellation discards after a receiver closes are counted separately; not universal zero-loss or trim-performance proof'}


if __name__ == '__main__':
    print(json.dumps(analyze(Path(sys.argv[1])), indent=2))
