#!/usr/bin/env python3
"""Run the shared-budget example inside one fresh owned netns; bounded cleanup."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


def digest(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate(rows, rounds, allow_ingress_drops=False):
    configs = [row for row in rows if row.get('type') == 'config']
    summaries = [row for row in rows if row.get('type') == 'summary']
    if len(configs) != 1 or len(summaries) != 1 or rows[-1] != summaries[0]:
        raise ValueError('missing unique config/final summary')
    config = configs[0]
    if not summaries[0].get('ok') or not summaries[0].get('ledger_zero') or summaries[0]['rounds'] != rounds:
        raise ValueError('probe did not complete all rounds with zero ledger')
    expected = set(range(rounds))
    groups = {}
    for kind in ['natural_done', 'release_done', 'round_done']:
        group = [row for row in rows if row.get('type') == kind]
        if len(group) != rounds or {row['round'] for row in group} != expected:
            raise ValueError(f'incomplete {kind}')
        groups[kind] = group
    samples = [row for row in rows if row.get('type') == 'sample']
    for row in samples:
        if not 0 <= row['global_bytes'] <= config['global_bytes']:
            raise ValueError('global ceiling exceeded')
        if not 0 <= row['port_bytes'] <= config['port_bytes'] or not 0 <= row['peer_bytes'] <= config['peer_bytes']:
            raise ValueError('port/peer ceiling exceeded')
    for round_id in expected:
        if {row['port'] for row in samples if row['round'] == round_id and row['phase'] == 'natural'} != {0, 1}:
            raise ValueError('both real driver snapshots required')
        for phase in ['release', 'cancel']:
            held = [row for row in rows if row.get('type') == 'held' and row['round'] == round_id and row['phase'] == phase]
            if len(held) != 1 or held[0]['quiet_ms'] < 300 or held[0]['bytes'] >= config['bytes_per_flow']:
                raise ValueError('missing incomplete transfer held without control requests')
            witnesses = [row for row in samples if row['round'] == round_id and row['phase'] == phase
                         and (row['tx_waiters'] > 0 or row['core_waiting'] or row['record_waiters'] > 0)]
            if not witnesses:
                raise ValueError('no observed parked writer')
    for row in groups['natural_done']:
        if row['verified_bytes'] != 2 * config['flows_per_port'] * config['bytes_per_flow'] or sum(row['global_failures']) == 0:
            raise ValueError('natural payload/global-pressure evidence missing')
    for row in groups['release_done']:
        if row['injected_host_bytes'] <= 0 or row['epoch_after'] <= row['epoch_before']:
            raise ValueError('no cross-port release observed')
        if row['verified_bytes'] != config['bytes_per_flow'] or row['wake_observed_us'] >= 2_100_000:
            raise ValueError('release did not recover complete payload within bound')
    ingress_drops = 0
    kernel_drops = 0
    for row in groups['round_done']:
        drops = row['ingress_queue_drops']
        device_drops = row['tun_tx_dropped']
        if drops < 0 or device_drops < 0:
            raise ValueError('invalid ingress drop counters')
        ingress_drops += drops
        kernel_drops += device_drops
        if not allow_ingress_drops and (drops or device_drops):
            raise ValueError('ingress drops invalidate clean acceptance; use explicit diagnostic override')
        if any(row[name] != 0 for name in ['peer_b_after_cancel', 'global_reserved', 'global_cached', 'time_wait_bytes']):
            raise ValueError('lifecycle ledger not zero')
        if row['connection_counts'] != [0, 0] or row['cancelled_app_writers'] < 1 or row['peer_a_survived_bytes'] <= 0:
            raise ValueError('cancellation crossed port identity or left resources')
    return {'rounds': rounds, 'natural_verified_bytes': sum(row['verified_bytes'] for row in groups['natural_done']),
            'release_verified_bytes': sum(row['verified_bytes'] for row in groups['release_done']),
            'release_observed_us': [row['wake_observed_us'] for row in groups['release_done']],
            'ledger_zero_every_round': True, 'ingress_queue_drops': ingress_drops,
            'tun_tx_dropped': kernel_drops, 'clean_ingress': ingress_drops == kernel_drops == 0,
            'allow_ingress_drops': allow_ingress_drops}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--outdir', required=True, type=Path)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--mtu', type=int, choices=[512, 1500], default=1500)
    parser.add_argument('--global-mib', type=int, choices=[1, 2, 4, 8], default=1)
    parser.add_argument('--rounds', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--flows', type=int, choices=range(2, 33), default=8)
    parser.add_argument('--trim', action='store_true')
    parser.add_argument('--ingress-capacity', type=int, choices=range(1, 65537), default=4096, metavar='1..65536')
    parser.add_argument('--allow-ingress-drops', action='store_true',
                        help='diagnostic overload controls only; never clean acceptance')
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch('[0-9a-f]{40}', args.source_sha):
        raise ValueError('requires root in dedicated rig and full source SHA')
    binary = args.binary.resolve(strict=True)
    args.outdir.mkdir(parents=True, exist_ok=False)
    ns = 'zf-shared-' + uuid.uuid4().hex[:12]
    result = {'ok': False, 'errors': [], 'namespace': ns, 'source_sha': args.source_sha,
              'binary_sha256': digest(binary), 'runner_sha256': digest(__file__),
              'namespace_removed': False, 'children_reaped': False}
    proc = None
    created = False
    started = time.monotonic()

    def ip(*items):
        return subprocess.run(['ip', *items], capture_output=True, text=True, check=True, timeout=5)

    try:
        ip('netns', 'add', ns)
        created = True
        ip('-n', ns, 'link', 'set', 'lo', 'up')
        argv = ['ip', 'netns', 'exec', ns, str(binary), '--mtu', str(args.mtu), '--global-mib', str(args.global_mib),
                '--rounds', str(args.rounds), '--flows', str(args.flows),
                '--ingress-capacity', str(args.ingress_capacity)]
        if args.trim:
            argv.append('--trim')
        result['command'] = argv
        with (args.outdir / 'probe.jsonl').open('x') as stdout, (args.outdir / 'probe.stderr').open('x') as stderr:
            proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, start_new_session=True)
            result['pid'] = proc.pid
            code = proc.wait(timeout=210)
        result['exit_code'] = code
        if code != 0:
            raise ValueError(f'probe exited {code}')
        rows = [json.loads(line) for line in (args.outdir / 'probe.jsonl').read_text().splitlines()]
        result['validation'] = validate(rows, args.rounds, args.allow_ingress_drops)
        if digest(binary) != result['binary_sha256']:
            raise ValueError('binary changed during run')
        # Nonpersistent TUN must disappear through normal owned-fd release.
        links = json.loads(ip('-n', ns, '-j', 'link', 'show').stdout)
        result['links_after_exit'] = [link['ifname'] for link in links]
        if result['links_after_exit'] != ['lo']:
            raise ValueError('probe left a TUN device behind')
        result['ok'] = True
    except Exception as error:
        result['errors'].append(str(error))
    finally:
        try:
            if proc is not None:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait(timeout=3)
                result['children_reaped'] = proc.poll() is not None
            if created:
                pids = ip('netns', 'pids', ns).stdout.split()
                if pids:
                    raise ValueError(f'owned namespace still has child PIDs: {pids}')
                ip('netns', 'del', ns)
                result['namespace_removed'] = not Path('/run/netns', ns).exists()
        except Exception as error:
            result['errors'].append('cleanup: ' + str(error))
        result['cleanup_ok'] = result['namespace_removed'] and result['children_reaped']
        result['ok'] = result['ok'] and result['cleanup_ok'] and not result['errors']
        result['elapsed_sec'] = time.monotonic() - started
        (args.outdir / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
