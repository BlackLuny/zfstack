#!/usr/bin/env python3
"""Sequential paired TUN measurements; run with an exclusively owned Docker rig.

Mount repo at /repo (read-only) and ZF_PACKET_EVIDENCE at /evidence. Place the
fat-LTO binaries in /evidence/bin/{before,after}. No case output is overwritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(os.environ.get('ZF_PACKET_EVIDENCE', '/workspace/scratch/packetpool-evidence'))
RIG = os.environ.get('ZF_PACKET_RIG', 'zfstack-packetpool-rig')
SHA = {'before': 'b170affa14e70d73f3d0834b2e06579dde70ca85',
       'after': 'db1f6a50a6cb26b5185a0d9ece6c602e2a005e43'}
FLAGS = {'rust': '1.94.1', 'profile': 'release', 'lto': 'fat',
         'codegen_units': 1, 'panic': 'abort', 'features': ['packet-pool-idle-trim']}
ENV = {'ZFBENCH_PACKET_POOL_TRIM_IDLE_MS': 3000, 'ZFBENCH_PACKET_POOL_TRIM_TARGET': 0,
       'ZFBENCH_TX_CACHE_TRIM_CHECK_MS': 1000, 'ZFBENCH_TX_CACHE_TRIM_WARM_MS': 3000,
       'ZFBENCH_TX_CACHE_TRIM_COLD_MS': 10000, 'ZFBENCH_TX_CACHE_TRIM_LARGE': 2,
       'ZFBENCH_TX_CACHE_TRIM_SMALL': 2}


def cgroup():
    return {name: Path('/sys/fs/cgroup/' + name).read_text() for name in ['cpu.stat', 'cpu.max']}


def run(group, variant, mtu, pair=0, position=0, diagnostic=False, extra=()):
    name = f'{group}-{mtu}-{pair:02}-{position}-{variant}'
    order = 'AB' if pair % 2 == 0 else 'BA'
    cmd = ['env', '-u', 'DOCKER_HOST', '-u', 'DOCKER_CONTEXT', '-u', 'DOCKER_TLS',
           '-u', 'DOCKER_TLS_VERIFY', '-u', 'DOCKER_CERT_PATH', 'docker',
           '--host=unix:///var/run/docker.sock', 'exec', RIG,
           'python3', '-B', '/repo/bench/pareto/issue23_tun_case.py',
           '--binary', '/evidence/bin/' + variant,
           '--loader', '/repo/bench/pareto/issue23_tun_load.py',
           '--outdir', '/evidence/runs/' + name, '--label', group, '--variant', variant,
           '--source-sha', SHA[variant], '--pair', str(pair), '--pair-position', str(position),
           '--pair-order', order, '--build-flags', json.dumps(FLAGS), '--loader-cpus', '2,3,4',
           '--mtu', str(mtu), '--idle-secs', '1', '--flows', '8', '--rr-samples', '200',
           '--rr-sizes', '64,512,1400,4096', '--bulk-bytes-per-flow', str(8 << 20),
           '--mixed-rr-interval-ms', '1']
    env = dict(ENV)
    if diagnostic:
        cmd += ['--diagnostic']
        env['ZFBENCH_TX_CACHE_SAMPLE_MS'] = 1000
    for key, value in env.items():
        cmd += ['--env', f'{key}={value}']
    cmd += list(extra)
    before = cgroup()
    start = time.monotonic()
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=255)
    path = ROOT / 'runs' / name / 'result.json'
    result = json.loads(path.read_text()) if path.exists() else None
    record = dict(name=name, group=group, variant=variant, mtu=mtu, pair=pair, position=position,
                  command=cmd, elapsed=time.monotonic()-start, exit_code=p.returncode,
                  before=before, after=cgroup(), result=result, stdout=p.stdout, stderr=p.stderr)
    (ROOT / 'records').mkdir(exist_ok=True)
    with (ROOT / 'records' / (name + '.json')).open('x') as f:
        json.dump(record, f, indent=2)
    ok = p.returncode == 0 and result and result['ok']
    print(name, 'PASS' if ok else 'FAIL', round(record['elapsed'], 2), flush=True)
    if not ok:
        raise RuntimeError(f'{name}: {result or p.stderr}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['smoke', 'diagnostics', 'sustained', 'performance', 'confirm'])
    args = parser.parse_args()
    (ROOT / 'runs').mkdir(exist_ok=True)
    if args.mode == 'smoke':
        run('smoke', 'after', 1500, diagnostic=True)
    elif args.mode == 'sustained':
        for mtu in [1500, 512]:
            run('sustained', 'after', mtu, diagnostic=True,
                extra=['--bulk-bytes-per-flow', str(64 << 20)])
    elif args.mode == 'confirm':
        for pair in range(12):
            order = ['before', 'after'] if pair % 2 == 0 else ['after', 'before']
            for position, variant in enumerate(order):
                run('confirm', variant, 512, pair, position)
    elif args.mode == 'diagnostics':
        for mtu in [1500, 512]:
            for variant in ['before', 'after']:
                run('low-rate', variant, mtu, diagnostic=True,
                    extra=['--idle-secs', '0', '--low-secs', '20', '--low-interval-ms', '250'])
            run('cold-reburst', 'after', mtu, diagnostic=True, extra=['--idle-secs', '15'])
            run('budget', 'after', mtu, diagnostic=True,
                extra=['--idle-count', '100', '--env', 'ZFBENCH_GLOBAL_BYTES=8388608',
                       '--env', 'ZFBENCH_PORT_BYTES=4194304', '--env', 'ZFBENCH_PEER_BYTES=4194304'])
    else:
        for mtu in [1500, 512]:
            for pair in range(6):
                for position in range(2):
                    run('aa', 'before', mtu, pair, position)
            for pair in range(12):
                order = ['before', 'after'] if pair % 2 == 0 else ['after', 'before']
                for position, variant in enumerate(order):
                    run('ab', variant, mtu, pair, position)


if __name__ == '__main__':
    main()
