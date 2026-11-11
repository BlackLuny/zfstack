#!/usr/bin/env python3
"""Frozen independent representative WAN and relaxed extreme confirmation."""
import argparse
import json
from pathlib import Path
import random
import subprocess
import time

import production_pacing  # Installs the explicit native arguments, unchanged.
import rtt_matrix as native
from analyze import summarize_run
from common_env import wanted_mask
from cubic50_env import ROLES
from rtt_analyze import summarize
from run import ROOT, counters, save, sha

DIRECTORY = ROOT / 'docs/experiments/2026-10-25-common-cases'
PREP = ROOT / 'qa/results/common-20261025-preparation'
CONTAINER = 'zfstack-common-20261025'
BINARIES = {
    'v5': ('target/release/zfbench-production-v5-20261024', '96695f880dd600172e3dfa5b32304da0fa296579357785b5f83dbb87ea1a26c5'),
    'old': ('target/release/zfbench-rtt-final-20261023', '74a5d0f81bea350c2a0dfd2f217dc4f373a4c0fd52979c0aba7ea0e4a7bfb518'),
}


def sequence(phase):
    rng = random.Random(2026102501)
    rows = []
    if phase == 'peak':
        orders = {n: ['AB']*6 + ['BA']*6 for n in [1, 2]}
        for order in orders.values():
            rng.shuffle(order)
        for rep in range(12):
            cells = [1, 2]
            rng.shuffle(cells)
            for n in cells:
                for position, letter in enumerate(orders[n][rep]):
                    binary = 'old' if letter == 'A' else 'v5'
                    rows.append(dict(kind='peak', case=f'peak-down{n}', round=rep, position=position,
                        block=f'peak-down{n}-r{rep:02d}', arm=binary, binary=binary, flows=n,
                        placement='original', warmup=8, measure=30,
                        egress_rate=0 if binary == 'old' else 12_375_000))
    else:
        if phase == 'preflight':
            cells = [('cubic', 1, 1, 'fixed'), ('bbr', 500, 1, 'fixed'),
                ('cubic', 10, 8, 'fixed'), ('bbr', 250, 8, 'fixed')]
            rounds = 1
        elif phase == 'aa':
            cells = [(cc, rtt, 1, placement) for cc, rtt in [('cubic', 50), ('bbr', 500)]
                for placement in ['original', 'fixed']]
            rounds = 2
        else:
            cells = [(cc, rtt, n, 'fixed') for cc in ['cubic', 'bbr']
                for rtt in [1, 10, 50, 100, 250, 500] for n in [1, 8]]
            rounds = 12
        orders = {cell: ['AB']*(rounds//2) + ['BA']*(rounds//2) if rounds > 1 else ['AB'] for cell in cells}
        for order in orders.values():
            rng.shuffle(order)
        for rep in range(rounds):
            ordered = list(cells)
            rng.shuffle(ordered)
            for cc, rtt, n, placement in ordered:
                case = f'{cc}-r{rtt}-n{n}-{placement}'
                for position, letter in enumerate(orders[cc, rtt, n, placement][rep]):
                    arm = 'off' if letter == 'A' or phase == 'aa' else 'p99'
                    rows.append(dict(kind='native', case=case, cc=cc, rtt=rtt, heavy=n,
                        lights=0 if n == 1 else 3, placement=placement, round=rep, position=position,
                        block=f'{case}-r{rep:02d}', arm=arm, binary='v5', tokio_stack=True, splice=True,
                        warmup=max(8, round(40*rtt/1000)), measure=20, retry_budget=1,
                        egress_rate=0 if arm == 'off' else 12_375_000))
    return [dict(index=i, **row) for i, row in enumerate(rows)]


def name(row):
    return f"{row['index']:04d}-{row['block']}-{row['position']}-{row['arm']}"


def command(out, row, container):
    rel = (out / name(row)).relative_to(ROOT)
    env = {}
    if row['kind'] == 'native':
        env = dict(ZFBENCH_QUEUE_DISCIPLINE='fifo', ZFBENCH_RR_RAW='1', ZFBENCH_RR_OPEN_HZ='25',
            ZFBENCH_RR_ARRIVALS=f'/work/{rel}/arrivals.json', ZFBENCH_CONNECT_PARALLEL='32',
            ZFBENCH_HEAVY_PROGRESS='1', ZFBENCH_RETRY_BUDGET_WRITERS='1')
        if row['lights']:
            env['ZFBENCH_RR_CPU'] = '4'
        args = native.arguments(row)
    else:
        args = ['--client-proxy', 'zfstack-client', '--splice', '--driver-thread', '--proxy-workers', '2',
            '--tun-mtu', '65535', '--secs', str(row['warmup']+row['measure']), '--warmup', str(row['warmup']),
            '--flows', str(row['flows']), '--test', 'down']
        if row['binary'] != 'old':
            args += ['--iface-egress-rate', str(row['egress_rate'])]
    argv = ['docker', 'exec', container, 'env'] + [k+'='+v for k, v in env.items()]
    argv += ['timeout', '-k', '5', '180', 'python3', '/work/bench/capacity/common_env.py',
        row['kind'], row['placement'], str(int(bool(row.get('lights', 0)))),
        f'/work/{rel}/environment.json', f'/work/{rel}/resources.json', '/work/'+BINARIES[row['binary']][0]]
    return argv+args+['--label', name(row)], env


def observer_audit(a, row):
    errors = list(a['errors'])
    required = set(ROLES) if row['kind'] == 'native' else {'zfc-driver', 'zfc-tun-rx'}
    if a['observer_affinity'] != [4] or set(a['required_roles']) != required:
        errors.append('observer affinity or required roles differ')
    if a['placement'] != row['placement'] or a['kind'] != row['kind'] or a['interactive'] != bool(row.get('lights', 0)):
        errors.append('observer mode differs')
    seen = {key: set() for key in required}
    for sample in a['samples']:
        if sample['elapsed_sec'] <= 2:
            errors.append('sample before assignment deadline')
        for t in sample['threads']:
            if t['affinity'] != wanted_mask(t['name'], t['affinity'], row['placement'], bool(row.get('lights', 0))):
                errors.append('sampled thread mask differs')
            if t['name'] in seen:
                seen[t['name']].add(t['tid'])
    if not a['samples'] or any(r['first_seen_sec'] > 2 for r in a['required_roles'].values()):
        errors.append('required role/sample late or absent')
    for role, tids in seen.items():
        if role not in a['required_roles'] or tids != {a['required_roles'][role]['tid']}:
            errors.append('missing/ambiguous role: '+role)
    driver = 'zfb-stack' if row['kind'] == 'native' else 'zfc-driver'
    diagnostic = {}
    if driver in a['required_roles']:
        tid = a['required_roles'][driver]['tid']
        observations = [(s['elapsed_sec'], t) for s in a['samples'] for t in s['threads'] if t['tid'] == tid]
        if len(observations) >= 2:
            first, last = observations[0][1], observations[-1][1]
            diagnostic['observed_span_sec'] = observations[-1][0]-observations[0][0]
            diagnostic['cpu_locations'] = sorted({t['last_cpu'] for _, t in observations})
            diagnostic['scheduler_delta'] = {k:last['scheduler_counts'][k]-v for k, v in first['scheduler_counts'].items() if k in last['scheduler_counts']}
            if first['schedstat'] and last['schedstat']:
                diagnostic['schedstat_delta'] = {k:last['schedstat'][k]-v for k, v in first['schedstat'].items()}
            if row['placement'] == 'fixed' and diagnostic['scheduler_delta'].get('se.nr_migrations') != 0:
                errors.append('fixed driver migrated or migration count unavailable')
        else:
            errors.append('insufficient driver samples')
    return dict(valid=a['valid'] and not errors, errors=sorted(set(errors)), driver=diagnostic,
        observer_cpu_sec=a['observer_cpu_sec'])


def inspect(out, row, container):
    dest = out/name(row)
    errors = []
    result = (summarize if row['kind'] == 'native' else summarize_run)(dest)
    raw = json.loads((dest/'stdout.json').read_text())
    start = json.loads((dest/'start.json').read_text())
    end = json.loads((dest/'end.json').read_text())
    argv, env = command(out, row, container)
    binary, digest = BINARIES[row['binary']]
    if start['argv'] != argv or start['env'] != env or start['row'] != row or start['binary_sha256'] != digest or not end['binary_unchanged'] or sha(ROOT/binary) != digest:
        errors.append('invocation/binary identity differs')
    audit = observer_audit(json.loads((dest/'environment.json').read_text()), row)
    if not audit['valid']:
        errors += audit['errors']
    cfg = raw['config']
    expected = dict(warmup=row['warmup'], secs=row['warmup']+row['measure'])
    if row['kind'] == 'native':
        expected.update(stack='zfstack-'+row['cc'], test='isolate', ports=1, heavy_conns=row['heavy'], lights=row['lights'],
            rtt_ms=row['rtt'], rate_mbps=100, rate_up_mbps=100, mtu=1420, sock_buf_kb=16384, seed=23,
            loss=0, loss_up=0, iface_egress_rate=row['egress_rate'], tokio_stack=True, tokio_splice=True)
        stats = raw['stack_thread']['stack_stats']
        if stats['impl'] != 'zfstack-tokio' or not stats['production_splice'] or stats['reserved_after_shutdown'] or stats['fixture_app_errors'] or stats['kernel_upstream_threads_after_shutdown']:
            errors.append('native relay/teardown failed')
        paced = sum(s['egress_paced_bytes'] for s in stats['shards'])
        result['egress_paced_bytes'] = paced
        result['egress_pacing_pauses'] = sum(s['egress_pacing_pauses'] for s in stats['shards'])
        if row['heavy'] == 1 and paced:
            errors.append('single peer did not bypass')
        for direction in ['up', 'down']:
            link = raw['link'][direction]
            params = link['params']
            if link['queue_discipline'] != 'fifo-service' or params['queue_limit_bytes'] != 250000 or params['rate_bps'] != 100_000_000 or params['delay_us'] != row['rtt']*500 or params['loss'] != 0:
                errors.append('FIFO/rate/RTT identity differs')
        if row['lights']:
            streams = json.loads((dest/'arrivals.json').read_text())
            if len(streams) != 6 or any(s.get(k) != 4 for s in streams for k in ['requested_cpu', 'observer_cpu', 'sender_cpu']):
                errors.append('actual RR affinity differs')
        cpu = raw['cpu']
        result.update(window_steal_seconds=cpu['system_window']['steal_sec'],
            window_softirq_seconds=cpu['system_window']['softirq_sec'],
            stack_lifetime_packets_out=raw['stack_thread']['pkts_out'])
    else:
        expected.update(proxy='zfstack-client', test='down', flows=row['flows'], splice=True,
            driver_thread=True, proxy_workers=2, tun_mtu=65535)
        if cfg.get('iface_egress_rate', 0) != row['egress_rate']:
            errors.append('local peak rate differs')
    for key, value in expected.items():
        if cfg.get(key) != value:
            errors.append('configuration differs: '+key)
    if result['nr_throttled'] or result['throttled_usec']:
        errors.append('cgroup throttling')
    result['strict_valid'] = result['valid'] and not errors
    result['audit_errors'] = sorted(set(errors))
    result['telemetry'] = audit
    result['valid'] = result['strict_valid']
    # Preserve actual latency of fully completed late controls only.
    observation_valid = result['strict_valid']
    if row['kind'] == 'native' and row['lights'] and not errors:
        streams = json.loads((dest/'arrivals.json').read_text())
        arrivals = [a for s in streams for a in s['arrivals']]
        late = 'invalid completion time or deadline exceeded'
        complete = bool(arrivals) and all(a['outcome'] == 'completed' and a['started_ns'] is not None and a['completed_ns'] is not None and
            a['planned_ns'] <= a['started_ns'] <= a['completed_ns'] and a['started_ns']-a['planned_ns'] <= 20_000_000 for a in arrivals)
        observation_valid |= complete and result['errors'] == [late]
        result['late_completed'] = sum(a['outcome'] == 'completed' and a['completed_ns'] is not None and a['completed_ns']-a['planned_ns'] > 3_000_000_000 for a in arrivals)
    result['observation_valid'] = observation_valid
    return result


def run(out, phase, container=CONTAINER):
    out.relative_to(ROOT)
    out.mkdir(parents=True, exist_ok=False)
    for binary, digest in BINARIES.values():
        if sha(ROOT/binary) != digest:
            raise RuntimeError('frozen binary differs')
    sources = {str(f.relative_to(ROOT)):sha(f) for parent, glob in [(ROOT/'src', '*.rs'), (ROOT/'bench/src', '*.rs'),
        (ROOT/'bench/capacity', '*.py'), (DIRECTORY, '*.md')] for f in sorted(parent.rglob(glob))}
    for f in [ROOT/'Cargo.toml', ROOT/'Cargo.lock', ROOT/'bench/Cargo.toml', ROOT/'bench/capacity/pacing_contract.rs']:
        sources[str(f.relative_to(ROOT))] = sha(f)
    for source in sorted(sources):
        dest = out/'source'/source
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT/source).read_bytes())
    rows = sequence(phase)
    save(out/'plan.json', dict(phase=phase, rows=rows, sources=sources, binaries=BINARIES, container=container,
        base_sha=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        environment=counters(), numpy_not_required_for_execution=True))
    for row in rows:
        dest = out/name(row)
        dest.mkdir()
        argv, env = command(out, row, container)
        binary, digest = BINARIES[row['binary']]
        if sha(ROOT/binary) != digest:
            raise RuntimeError('frozen binary changed')
        save(dest/'start.json', dict(row=row, argv=argv, env=env, binary_sha256=digest,
            start_unix=time.time(), cgroup=counters()))
        with (dest/'stdout.json').open('x') as stdout, (dest/'stderr.txt').open('x') as stderr:
            try:
                rc = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=195).returncode
            except subprocess.TimeoutExpired:
                rc = 'outer_timeout'
        save(dest/'end.json', dict(returncode=rc, end_unix=time.time(), cgroup=counters(), binary_unchanged=sha(ROOT/binary) == digest))
        print(json.dumps(dict(name=dest.name, returncode=rc)), flush=True)
        if rc in [124, 137, 'outer_timeout'] or sha(ROOT/binary) != digest:
            raise RuntimeError('bounded/identity failure; no retry')
    save(out/'complete.json', dict(end_unix=time.time(), planned=len(rows)))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase', choices=['preflight', 'aa', 'ordinary', 'peak'])
    p.add_argument('out', type=Path)
    p.add_argument('--container', default=CONTAINER)
    a = p.parse_args()
    run(a.out.resolve(), a.phase, a.container)
