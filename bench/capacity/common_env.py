#!/usr/bin/env python3
"""External native/peak placement audit for the representative confirmation."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from cubic50_env import ROLES, sample_thread
from fastpath_affinity import descendants


def wanted_mask(name, current, placement, interactive):
    if placement == 'original':
        return [4] if interactive and current == [4] else [0, 1, 2, 3]
    if name in ROLES:
        return [ROLES[name]]
    # The frozen executable explicitly assigns RR readers/senders to CPU 4.
    # Preserve that self-assignment, including threads born after warmup.
    return [4] if interactive and current == [4] else [3]


def run(kind, placement, interactive, ledger, resources, binary, args):
    start = time.monotonic()
    cpu = time.process_time()
    os.sched_setaffinity(0, {4})
    process = subprocess.Popen(['taskset', '-c', '0-3', 'python3',
        '/work/bench/tail69_exec.py', resources, binary] + args, start_new_session=True)
    errors, samples, roles, threads = [], [], {}, {}
    required = set(ROLES) if kind == 'native' else {'zfc-driver', 'zfc-tun-rx'}

    def interrupted(sig, frame):
        raise InterruptedError(f'observer received signal {sig}')

    signal.signal(signal.SIGTERM, interrupted)
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - start
            observed = []
            for pid in sorted(descendants(process.pid)):
                for task in sorted(Path(f'/proc/{pid}/task').glob('[0-9]*')):
                    try:
                        tid = int(task.name)
                        name = (task / 'comm').read_text().strip()
                        before = sample_thread(task)
                        wanted = wanted_mask(name, before['affinity'], placement, interactive)
                        if name in required and name not in roles:
                            roles[name] = dict(tid=tid, first_seen_sec=elapsed)
                        if before['affinity'] != wanted:
                            if elapsed > 2:
                                errors.append(f'late assignment {tid} {name}')
                            os.sched_setaffinity(tid, set(wanted))
                        value = sample_thread(task)
                        if value['affinity'] != wanted:
                            errors.append(f'actual mask differs {tid} {name}')
                        key = str(tid)
                        if key not in threads:
                            threads[key] = dict(pid=pid, tid=tid, name=name, first_seen_sec=elapsed, first=value)
                        threads[key].update(name=name, last_seen_sec=elapsed, last=value)
                        observed.append(dict(tid=tid, name=name, **value))
                    except (FileNotFoundError, ProcessLookupError):
                        continue
            if elapsed > 2:
                samples.append(dict(elapsed_sec=elapsed, threads=observed))
            time.sleep(.02 if elapsed < 2 else .5)
        rc = process.returncode
    except (OSError, InterruptedError, ValueError) as error:
        errors.append(str(error))
        rc = 1
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if set(roles) != required:
            errors.append('required native/proxy roles missing')
        if any(r['first_seen_sec'] > 2 for r in roles.values()):
            errors.append('required role first observed after 2 s')
        if not samples:
            errors.append('no post-assignment samples')
        result = dict(valid=rc == 0 and not errors, returncode=rc, errors=sorted(set(errors)),
            kind=kind, placement=placement, interactive=interactive, required_roles=roles,
            threads=threads, samples=samples, observer_affinity=sorted(os.sched_getaffinity(0)),
            observer_cpu_sec=time.process_time()-cpu, wall_sec=time.monotonic()-start,
            scope='Observed lifetime counters; exact measured-window CPU comes from the frozen executable.')
        with Path(ledger).open('x') as f:
            json.dump(result, f, indent=2)
            f.write('\n')
    return rc if rc else int(bool(errors))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('kind', choices=['native', 'peak'])
    p.add_argument('placement', choices=['original', 'fixed'])
    p.add_argument('interactive', type=int, choices=[0, 1])
    p.add_argument('ledger')
    p.add_argument('resources')
    p.add_argument('binary')
    p.add_argument('args', nargs=argparse.REMAINDER)
    a = p.parse_args()
    raise SystemExit(run(a.kind, a.placement, bool(a.interactive), a.ledger, a.resources, a.binary, a.args))
