#!/usr/bin/env python3
"""Recompute summaries from an extracted raw-evidence archive using stdlib only."""
import argparse
import json
from pathlib import Path
import random
import statistics as st


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sampler(path):
    return [json.loads(line.split(' ', 1)[1]) for line in path.read_text().splitlines()
            if line.startswith('ZFBENCH_TX_CACHE ')]


def metrics(data):
    result = {}
    for row in data:
        kind = row['type']
        if kind == 'rr':
            prefix = f"rr/{row['stage']}/{row['size']}"
            for key in ['p50_us', 'p95_us', 'p99_us']:
                result[prefix + '/' + key] = row['latency'][key]
            result[prefix + '/first_request_us'] = row['first_request']['rtt_us']
        elif kind == 'bulk':
            prefix = f"bulk/{row['stage']}/{row['direction']}"
            for key in ['goodput_mbps', 'proxy_cpu_sec_per_GB']:
                result[prefix + '/' + key] = row[key]
            packets = row['tun_delta']['rx_packets'] + row['tun_delta']['tx_packets']
            result[prefix + '/cpu_ns_per_tun_packet'] = row['proxy_cpu_sec'] * 1e9 / packets
            result[prefix + '/tun_pps'] = packets / row['window_wall_sec']
            result[prefix + '/rss_bytes'] = row['after']['proxy']['rss_bytes']
            result[prefix + '/hwm_bytes'] = row['after']['proxy']['hwm_bytes']
            result[prefix + '/minor_faults'] = row['after']['proxy']['minor_faults'] - row['before']['proxy']['minor_faults']
    return result


def compare(pairs):
    result = {}
    for metric in sorted(set.intersection(*(set(a) & set(b) for a, b in pairs))):
        vals = [(a[metric], b[metric]) for a, b in pairs
                if a[metric] is not None and b[metric] is not None and a[metric] > 0]
        if not vals:
            continue
        ratios = [b / a for a, b in vals]
        rng = random.Random(23)
        boots = sorted(st.median(rng.choices(ratios, k=len(ratios))) for _ in range(10000))
        result[metric] = {'n': len(vals), 'before_median': st.median(a for a, b in vals),
                          'after_median': st.median(b for a, b in vals),
                          'paired_median_delta_pct': (st.median(ratios) - 1) * 100,
                          'bootstrap_95_pct': [(boots[i] - 1) * 100 for i in [249, 9749]],
                          'paired_min_max_pct': [(f(ratios) - 1) * 100 for f in [min, max]]}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path, nargs='?', default=Path('.'))
    args = parser.parse_args()
    records = [json.loads(p.read_text()) for p in sorted((args.root / 'records').glob('*.json'))]
    summary = {'cases': [], 'low_rate': {}, 'cold': {}, 'performance': {},
               'limitations': ['whole proxy CPU sampled at kernel tick resolution',
                               'Python endpoints and shared VM can limit throughput',
                               'pool snapshots are separate reads, not atomic',
                               'no PMU, allocator alloc/free hooks or cache hit/miss counters',
                               'single shard/PeerId(0), no real multi-peer or zfc acceptance',
                               'diagnostic cases are excluded from performance statistics']}
    for record in records:
        result = record['result'] or {}
        summary['cases'].append({key: record[key] for key in ['name', 'group', 'variant', 'mtu', 'exit_code']}
                                | {'ok': result.get('ok', False), 'errors': result.get('errors', [])})
        if not result.get('ok'):
            continue
        path = args.root / 'runs' / record['name']
        data = rows(path / 'loader.jsonl')
        for sample in sampler(path / 'proxy.stderr'):
            for used, limit in [('global_reserved_bytes', 'global_limit_bytes'),
                                ('port_physical_bytes', 'port_limit_bytes'),
                                ('peer_physical_bytes', 'peer_limit_bytes')]:
                assert sample[used] <= sample[limit], (record['name'], used, sample)
        if record['group'] == 'low-rate':
            low = next(row for row in data if row['type'] == 'low_traffic')
            meta = next(row for row in data if row['type'] == 'metadata')
            start = low['before']['proxy']['start_ticks'] / meta['cpu_tick_hz']
            begin = low['before']['monotonic_ns'] / 1e9 - start
            end = low['after']['monotonic_ns'] / 1e9 - start
            samples = [r for r in sampler(path / 'proxy.stderr') if begin + 8 <= r['elapsed_ms'] / 1000 < end - 1]
            assert len(samples) >= 3, record['name']
            cached = [r['packet_cached'] for r in samples]
            in_use = [r['packet_in_use'] for r in samples]
            expected = 128 if record['variant'] == 'before' else 1
            assert all(v == expected for v in cached), (record['name'], cached)
            assert all(v == 1 for v in in_use), (record['name'], in_use)
            summary['low_rate'][record['name']] = {
                'samples': len(samples), 'cached_range': [min(cached), max(cached)],
                'in_use_range': [min(in_use), max(in_use)], 'buf_size': samples[0]['packet_buf_size'],
                'free_lease_bytes': samples[-1]['packet_free_lease_bytes_estimate'],
                'proxy_cpu_sec': low['changes']['proxy_cpu_sec'],
                'rss_before': low['before']['proxy']['rss_bytes'], 'rss_after': low['after']['proxy']['rss_bytes'],
                'requests': low['requests'], 'requested_sec': low['requested_sec']}
        elif record['group'] == 'cold-reburst':
            quiet = next(row for row in data if row['type'] == 'quiet')
            assert quiet['complete_wire_silence'], record['name']
            zero = [r for r in sampler(path / 'proxy.stderr')
                    if r['packet_cached'] == 0 and r['packet_in_use'] == 0]
            assert zero, record['name']
            summary['cold'][record['name']] = {'zero_packet_pool_samples': len(zero), 'wire_silence': True}
    for mtu in [1500, 512]:
        for group, count in [('aa', 6), ('ab', 12), ('confirm', 12)]:
            selected = [r for r in records if r['group'] == group and r['mtu'] == mtu]
            if not selected:
                continue
            assert len(selected) == count * 2
            pairs = []
            for pair in range(count):
                both = [r for r in selected if r['pair'] == pair]
                assert len(both) == 2 and all(r['result']['ok'] and r['result']['eligible_for_performance'] for r in both)
                both.sort(key=(lambda r: r['position']) if group == 'aa' else (lambda r: r['variant'] != 'before'))
                pairs.append(tuple(metrics(rows(args.root / 'runs' / r['name'] / 'loader.jsonl')) for r in both))
            summary['performance'][f'{group}/{mtu}'] = compare(pairs)
    (args.root / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print('cases', len(records), 'passed', sum(c['ok'] for c in summary['cases']))
    print(json.dumps(summary['low_rate'], indent=2))
    for group, values in summary['performance'].items():
        for key, value in values.items():
            if key.startswith('bulk/') and key.endswith(('goodput_mbps', 'proxy_cpu_sec_per_GB')):
                print(group, key, round(value['paired_median_delta_pct'], 1), [round(v, 1) for v in value['bootstrap_95_pct']])


if __name__ == '__main__':
    main()
