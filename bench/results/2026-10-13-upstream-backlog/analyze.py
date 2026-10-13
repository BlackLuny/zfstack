#!/usr/bin/env python3
"""Validate direct-kernel controls, RST provenance, and the fixed TUN matrix."""
import argparse
from collections import Counter
import json
from pathlib import Path
import socket
import struct


def packets(path):
    with path.open('rb') as f:
        header = f.read(24)
        assert header[:4] == b'\xd4\xc3\xb2\xa1', 'expected little-endian microsecond pcap'
        assert struct.unpack_from('<I', header, 20)[0] == 276, 'expected Linux SLL2'
        while frame := f.read(16):
            sec, usec, caplen, wirelen = struct.unpack('<IIII', frame)
            data = f.read(caplen)
            assert len(data) == caplen
            if len(data) < 60 or data[:2] != b'\x08\x00':
                continue
            ip = 20
            if data[ip] >> 4 != 4 or data[ip+9] != 6:
                continue
            tcp = ip + (data[ip] & 15) * 4
            if len(data) < tcp + 20:
                continue
            src, dst = (socket.inet_ntoa(data[ip+i:ip+i+4]) for i in [12, 16])
            sport, dport, seq, ack = struct.unpack_from('!HHII', data, tcp)
            yield {'unix_us': sec * 1000000 + usec, 'src': src, 'dst': dst,
                   'sport': sport, 'dport': dport, 'seq': seq, 'ack': ack,
                   'flags': data[tcp+13]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    a = p.parse_args()
    summary = {'direct': {}, 'rst_trace': {}, 'tun': []}
    for name, expected in [('default', 11), ('128', 0)]:
        rows = [json.loads(line) for line in (a.root / f'direct-{name}.jsonl').read_text().splitlines()]
        assert len(rows) == 8
        failures = sum(len(r['errors']) for r in rows)
        assert failures == expected
        assert all(not r['server_errors'] for r in rows)
        summary['direct'][name] = {'bursts': len(rows), 'flows': len(rows)*64,
                                   'errors': failures, 'backlog': rows[0]['backlog'],
                                   'error_types': dict(Counter(e for r in rows for e in r['errors']))}
    trace = a.root / 'runs' / 'trace-trace-1500-8-0-1'
    result = json.loads((trace / 'result.json').read_text())
    assert not result['ok'] and result['cleanup_ok']
    assert result['tcpdump']['dropped_by_kernel'] == 0
    data = list(packets(trace / 'capture.pcap'))
    rst = [r for r in data if r['flags'] & 4]
    upstream = [r for r in rst if r['src'] == '127.0.0.1' and r['sport'] == 5201]
    tun = [r for r in rst if r['src'] == '198.18.0.2' and r['sport'] == 5201]
    assert len(upstream) == len(tun) == 4
    assert upstream[0]['unix_us'] < tun[0]['unix_us']
    assert tun[0]['unix_us'] - upstream[0]['unix_us'] < 5000
    retries = []
    for reset in upstream:
        syn = [r for r in data if r['src'] == '127.0.0.1' and r['sport'] == reset['dport']
               and r['dport'] == 5201 and r['flags'] == 2]
        assert len(syn) >= 2
        retries.append({'client_port': reset['dport'], 'syns': len(syn),
                        'syn_retry_delay_ms': (syn[1]['unix_us']-syn[0]['unix_us'])/1000})
    summary['rst_trace'] = {'upstream_resets': upstream, 'tun_resets': tun,
                            'first_forwarding_delay_us': tun[0]['unix_us'] - upstream[0]['unix_us'],
                            'upstream_syn_retries': retries, 'tcpdump': result['tcpdump'],
                            'scope': 'upstream reset precedes propagation to TUN; not a claim about every historical timeout'}
    groups = Counter()
    for path in sorted((a.root / 'records').glob('*.json')):
        record = json.loads(path.read_text())
        r = record['result']
        summary['tun'].append({'name': path.stem, 'ok': r['ok'], 'errors': r['errors'],
                               'cleanup_ok': r['cleanup_ok']})
        if path.stem.startswith('confirmed-'):
            assert r['ok'] and r['cleanup_ok'] and r['children_reaped'] and r['namespace_removed']
            rows = [json.loads(line) for line in (a.root/'runs'/path.stem/'loader.jsonl').read_text().splitlines()]
            meta = next(x for x in rows if x['type'] == 'metadata')
            assert meta['upstream_backlog'] == 128
            assert meta['config']['flows'] == 64
            bulk = [x for x in rows if x['type'] == 'bulk']
            assert len(bulk) == 4 and all(len(x['per_flow']) == 64 for x in bulk)
            assert sum(x['verified_bytes'] for x in bulk) == 256 << 20
            groups['-'.join(path.stem.split('-')[1:-1])] += 1
    assert len(groups) == 8 and all(n == 3 for n in groups.values()), groups
    summary['confirmed_groups'] = dict(groups)
    summary['confirmed_bulk_bytes'] = sum(groups.values()) * (256 << 20)
    (a.root/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
