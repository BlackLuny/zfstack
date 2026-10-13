#!/usr/bin/env python3
"""Recheck source identities, budget ceilings, payloads and concurrent progress."""
import argparse
from collections import Counter
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    groups = Counter()
    summary = {'cases': [], 'scope': 'diagnostic real-TUN four-peer progress and sampled hard ceilings, not bandwidth fairness or WG integration'}
    for record_path in sorted((args.root / 'records').glob('*.json')):
        record = json.loads(record_path.read_text())
        result = record['result']
        assert record['exit_code'] == 0 and result['ok'], record_path
        assert result['cleanup_ok'] and result['children_reaped'] and result['namespace_removed']
        assert result['peer_validation']['traffic_peers'] == [1, 2, 3, 4]
        root = args.root / 'runs' / record_path.stem
        rows = [json.loads(line) for line in (root / 'loader.jsonl').read_text().splitlines()]
        meta = next(row for row in rows if row['type'] == 'metadata')
        config = meta['config']
        assert meta['upstream_backlog'] == 128 and config['flows'] == 64
        assert config['timeout_secs'] == 10 and config['idle_count'] == 16
        bulk = [row for row in rows if row['type'] == 'bulk']
        assert len(bulk) == 4
        progress, latencies = [], []
        for row in bulk:
            expected_sources = {f'172.19.1.{i}' for i in range(1, 5)} if config['peer_mode'] == 'fanout' else {'172.19.1.1'}
            assert {flow['source'] for flow in row['per_flow']} == expected_sources
            assert len(row['per_flow']) == 64
            assert all(flow['verified_bytes'] == 1 << 20 for flow in row['per_flow'])
            assert row['verified_bytes'] == 64 << 20
            probes = row['mixed_peers']
            assert len(probes) == 3
            assert {probe['source'] for probe in probes} == {'172.19.1.2', '172.19.1.3', '172.19.1.4'}
            assert all(probe['fully_overlapping_samples'] > 0 for probe in probes)
            progress += [probe['fully_overlapping_samples'] for probe in probes]
            latencies += [probe['latency']['max_us'] for probe in probes]
        logs = (root / 'proxy.stderr').read_text().splitlines()
        def samples(prefix):
            return [json.loads(line[len(prefix):]) for line in logs if line.startswith(prefix)]
        peers, cache = samples('ZFBENCH_PEERS '), samples('ZFBENCH_TX_CACHE ')
        assert peers and cache
        peaks = [0] * 5
        observed = set()
        for sample in peers:
            assert len(sample['peers']) == 5
            for i, peer in enumerate(sample['peers']):
                assert peer['peer_id'] == i
                assert 0 <= peer['physical_bytes'] <= sample['peer_limit_bytes'] == 2 << 20
                if i == 0:
                    assert peer['egress_packets'] == 0
                elif peer['physical_bytes'] and peer['egress_packets']:
                    observed.add(i)
                peaks[i] = max(peaks[i], peer['physical_bytes'])
        assert observed == {1, 2, 3, 4}
        for row in cache:
            assert row['global_reserved_bytes'] <= row['global_limit_bytes'] == 32 << 20
            assert row['port_physical_bytes'] <= row['port_limit_bytes'] == 16 << 20
        refused = [max(row['reserve_failures_by_level'][i] for row in cache) for i in range(3)]
        if config['peer_mode'] == 'noisy':
            assert peaks[1] > 0.9 * (2 << 20), 'peer 1 must actually approach its ceiling'
            assert refused[2] > 0 and refused[:2] == [0, 0], 'isolate peer pressure from global/port pressure'
        last = next(row for row in rows if row['type'] == 'summary')
        assert last['ok'] and not last['errors']
        entry = {
            'name': record_path.stem, 'mode': config['peer_mode'], 'mtu': config['mtu'],
            'trim_enabled': config['variant'] == '1', 'verified_bytes': sum(row['verified_bytes'] for row in bulk),
            'peak_peer_physical_bytes': peaks, 'peak_global_reserved_bytes': max(row['global_reserved_bytes'] for row in cache),
            'peak_port_physical_bytes': max(row['port_physical_bytes'] for row in cache),
            'reserve_failures_by_level': refused, 'minimum_light_completions_per_bulk_peer': min(progress),
            'total_light_completions_during_bulk': sum(progress), 'max_mixed_rtt_us': max(latencies),
            'idle_trimmed_blocks': max(row['idle_trimmed_blocks'] for row in cache),
            'cleanup_ok': result['cleanup_ok'], 'wall_sec': last['wall_sec'],
        }
        if record_path.stem.startswith('decay-'):
            quiet = next(row for row in rows if row['type'] == 'quiet')
            low = next(row for row in rows if row['type'] == 'low_traffic')
            assert quiet['requested_sec'] == low['requested_sec'] == 12
            assert quiet['actual_idle_connections'] == 16 and low['requests'] >= 10
            # Pair each cache sample with its adjacent timestamped peer sample.
            assert len(peers) in (len(cache), len(cache) + 1)
            assert all(0 <= row['elapsed_ms'] - peer['elapsed_ms'] < 100 for row, peer in zip(cache, peers))
            peer_by_elapsed = {row['elapsed_ms']: peer for row, peer in zip(cache, peers)}
            phases = {}
            for name, phase in [('quiet', quiet), ('low', low)]:
                selected = [row for row in cache if row['elapsed_ms'] in peer_by_elapsed
                            and phase['before']['unix_ns'] / 1e6 <= peer_by_elapsed[row['elapsed_ms']]['unix_ms']
                            <= phase['after']['unix_ns'] / 1e6]
                assert len(selected) >= 9
                assert all(all(peer['physical_bytes'] > 0 for peer in peer_by_elapsed[row['elapsed_ms']]['peers'][1:])
                           for row in selected), 'idle traffic peers must remain alive during decay'
                phases[name] = {'samples': len(selected),
                                'first_large_cached': selected[0]['large_cached'],
                                'last_large_cached': selected[-1]['large_cached'],
                                'trimmed_blocks_delta': selected[-1]['idle_trimmed_blocks'] - selected[0]['idle_trimmed_blocks']}
            entry['decay_phases'] = phases
            if entry['trim_enabled']:
                assert entry['idle_trimmed_blocks'] > 0
                assert phases['quiet']['trimmed_blocks_delta'] > 0
            else:
                assert entry['idle_trimmed_blocks'] == 0
        summary['cases'].append(entry)
        if record_path.stem.startswith('matrix-'):
            groups[f"{config['peer_mode']}-{config['mtu']}-{config['variant']}"] += 1
    assert len(groups) == 8 and all(count == 2 for count in groups.values()), groups
    summary['matrix_groups'] = dict(groups)
    summary['matrix_cases'] = sum(groups.values())
    summary['matrix_verified_bytes'] = summary['matrix_cases'] * (256 << 20)
    summary['total_cases'] = len(summary['cases'])
    summary['total_verified_bytes'] = sum(row['verified_bytes'] for row in summary['cases'])
    compatibility = args.root / 'compat-record.json'
    if compatibility.exists():
        record = json.loads(compatibility.read_text())
        result = record['result']
        assert record['exit_code'] == 0 and result['ok'] and result['cleanup_ok']
        assert result['children_reaped'] and result['namespace_removed']
        rows = [json.loads(line) for line in (args.root / 'runs/compat-single/loader.jsonl').read_text().splitlines()]
        assert next(row for row in rows if row['type'] == 'metadata')['config']['peer_mode'] == 'single'
        assert not any(line.startswith('ZFBENCH_PEERS ') for line in (args.root / 'runs/compat-single/proxy.stderr').read_text().splitlines())
        bulk = [row for row in rows if row['type'] == 'bulk']
        assert len(bulk) == 4 and sum(row['verified_bytes'] for row in bulk) == 256 << 20
        assert all(flow['source'] is None for row in bulk for flow in row['per_flow'])
        summary['single_peer_compatibility'] = {'ok': True, 'verified_bytes': 256 << 20, 'cleanup_ok': True}
    (args.root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
