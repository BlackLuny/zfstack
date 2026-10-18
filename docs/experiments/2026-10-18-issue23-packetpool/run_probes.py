#!/usr/bin/env python3
"""Execute a fixed, paired diagnostic window; never replace failed slots."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import resource
import socket
import subprocess
import time


def validate_probe(slot, probe):
    if not isinstance(probe, dict):
        raise ValueError('probe JSON is not an object')
    cached = 0 if slot['case'] == 'cache0' else 32
    per_iteration = 33 if slot['case'] == 'overflow33' else 1
    packets = slot['iterations'] * per_iteration
    checksum = sum((23 + 37 * i) % 256 for i in range(64))
    expected = {
        'schema': 'zfstack-issue23-packetpool-v1', 'status': 'PASS', 'case': slot['case'],
        'buf_size': slot['size'], 'iterations': slot['iterations'], 'seed': 23,
        'packets_per_iteration': per_iteration, 'packets': packets, 'bytes_per_packet': 64,
        'bytes': packets * 64, 'capacity_bytes': packets * slot['size'],
        'checksum': checksum * packets, 'checksum_expected': checksum * packets,
        'priming_packets': cached, 'priming_checksum': checksum * cached,
        'max_cached': cached, 'budget_high': 64 * 1024 * 1024, 'cached': cached, 'in_use': 0,
        'reserved': cached * slot['size'], 'peer_reserved': cached * slot['size'],
        'port_reserved': cached * slot['size'], 'trimmed': cached, 'post_trim_cached': 0,
        'post_trim_in_use': 0, 'final_reserved': 0, 'final_peer_reserved': 0, 'final_port_reserved': 0,
    }
    for name, value in expected.items():
        if type(probe.get(name)) is not type(value) or probe[name] != value:
            raise ValueError('probe/plan mismatch for ' + name)
    if type(probe.get('loop_wall_ns')) is not int or probe['loop_wall_ns'] <= 0:
        raise ValueError('missing or nonpositive loop wall time')


def output_text(value):
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return value or ''


def environment():
    compilers = []
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                comm = (entry / 'comm').read_text().strip()
                if comm in {'rustc', 'cargo', 'cc1', 'cc1plus', 'clang', 'gcc', 'zig', 'ld.lld', 'lld'}:
                    compilers.append({'pid': int(entry.name), 'comm': comm})
            except (OSError, ProcessLookupError):
                pass
    return {'time_ns': time.time_ns(), 'load': os.getloadavg(), 'compilers': compilers,
            'compiler_inventory_scope': 'non-exhaustive process comm snapshot',
            'proc_stat': Path('/proc/stat').read_text(), 'meminfo': Path('/proc/meminfo').read_text()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--build-manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--cpu', type=int, default=2)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    build = json.loads(Path(args.build_manifest).read_text())
    arms = {a['name']: a for a in build['arms']}
    for arm in arms.values():
        actual = hashlib.sha256(Path(arm['binary']).read_bytes()).hexdigest()
        if actual != arm['binary_sha256'] or arm['exit_code'] != 0:
            raise ValueError('binary identity mismatch')
    if args.cpu not in os.sched_getaffinity(0):
        raise ValueError('requested CPU is unavailable')
    rng = random.Random(23)
    cases = [(case, size) for case in ['cache0', 'cache32', 'overflow33'] for size in [64, 1500, 65536]]
    plan = []
    for phase, pair_count, names in [('AA', 4, ['A1', 'A2']), ('AB', 12, ['A1', 'B'])]:
        order_by_case = {}
        for case, size in cases:
            directions = [False, True] * (pair_count // 2)
            rng.shuffle(directions)
            order_by_case[(case, size)] = directions
            for arm in names:
                if phase == 'AB' and arm == 'A1':
                    continue  # A1 already has its separate warmup process.
                plan.append({'phase': phase, 'warmup': True, 'case': case, 'size': size,
                             'arm': arm, 'pair': -1, 'iterations': 304 if case == 'overflow33' else 10000})
        for pair in range(pair_count):
            shuffled = list(cases)
            rng.shuffle(shuffled)
            for case, size in shuffled:
                order = list(reversed(names)) if order_by_case[(case, size)][pair] else list(names)
                for arm in order:
                    plan.append({'phase': phase, 'warmup': False, 'case': case, 'size': size,
                                 'arm': arm, 'pair': pair, 'iterations': 30304 if case == 'overflow33' else 1000000})
    protocol = {'seed': 23, 'cpu': args.cpu, 'host': socket.gethostname(), 'kernel': os.uname().release,
                'build_manifest': build, 'slots': plan, 'cpu_measurement': 'per-process RUSAGE_CHILDREN delta',
                'performance_qualification': 'diagnostic only; shared host, not calibrated or isolated'}
    (output / 'frozen-plan.json').write_text(json.dumps(protocol, indent=2) + '\n')
    env_records = {'before': environment(), 'cpuinfo': Path('/proc/cpuinfo').read_text(),
                   'admission_policy': 'Diagnostic on shared host; record foreign compilers without excluding samples. Own builds finish before this command.'}
    cpu_root = Path('/sys/devices/system/cpu') / ('cpu' + str(args.cpu))
    env_records['cpu_settings'] = {}
    for name in ['topology/thread_siblings_list', 'topology/core_id', 'cpufreq/scaling_governor',
                 'cpufreq/scaling_cur_freq', 'cpufreq/scaling_min_freq', 'cpufreq/scaling_max_freq']:
        path = cpu_root / name
        env_records['cpu_settings'][name] = path.read_text().strip() if path.exists() else None
    (output / 'environment.json').write_text(json.dumps(env_records, indent=2) + '\n')
    failure = None
    try:
        with (output / 'samples.jsonl').open('x') as sink:
            for index, slot in enumerate(plan):
                arm = arms[slot['arm']]
                command = [arm['binary'], slot['case'], str(slot['size']), str(slot['iterations'])]
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                start = time.monotonic_ns()
                record = dict(slot, index=index, command=command, returncode=None, timed_out=False)
                try:
                    completed = subprocess.run(command, capture_output=True, text=True, timeout=30,
                                               preexec_fn=lambda: os.sched_setaffinity(0, {args.cpu}))
                    record.update(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
                except subprocess.TimeoutExpired as error:
                    record.update(timed_out=True, error=repr(error), stdout=output_text(error.stdout), stderr=output_text(error.stderr))
                except (OSError, subprocess.SubprocessError) as error:
                    record.update(error=repr(error), stdout='', stderr='')
                finish = time.monotonic_ns()
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                record.update(cpu_user_sec=after.ru_utime - before.ru_utime,
                              cpu_system_sec=after.ru_stime - before.ru_stime,
                              cpu_sec=(after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime),
                              process_wall_ns=finish - start)
                try:
                    record['probe'] = json.loads(record['stdout'])
                    validate_probe(slot, record['probe'])
                except (json.JSONDecodeError, ValueError) as error:
                    record['validation_error'] = str(error)
                sink.write(json.dumps(record) + '\n')
                sink.flush()
                if record['returncode'] != 0 or record['timed_out'] or 'error' in record or 'validation_error' in record:
                    raise RuntimeError('failed slot retained; stopping without replacement: ' + str(index))
                if index % 36 == 0:
                    print(json.dumps({'completed': index + 1, 'total': len(plan)}), flush=True)
    except BaseException as error:
        failure = error
        env_records['failure'] = repr(error)
    finally:
        env_records['after'] = environment()
        env_records['binary_hashes_after'] = {}
        for name, arm in arms.items():
            try:
                actual = hashlib.sha256(Path(arm['binary']).read_bytes()).hexdigest()
                env_records['binary_hashes_after'][name] = actual
                if actual != arm['binary_sha256']:
                    env_records.setdefault('identity_errors', []).append(name + ': binary changed')
            except OSError as error:
                env_records.setdefault('identity_errors', []).append(name + ': ' + repr(error))
        (output / 'environment.json').write_text(json.dumps(env_records, indent=2) + '\n')
    if failure is not None:
        raise failure
    if env_records.get('identity_errors'):
        raise ValueError('binary identity failure; observations retained in environment.json')
    print(json.dumps({'completed': len(plan), 'measurement_samples': sum(not s['warmup'] for s in plan),
                      'warmup_samples': sum(s['warmup'] for s in plan)}), flush=True)


if __name__ == '__main__':
    main()
