#!/usr/bin/env python3
"""Run one scoped validation under the public physical-domain lease."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

REPO = Path('/root/work/zfstack-issue23-20261018')
sys.path.insert(0, str(REPO))
from qa.lease import Lease

parser = argparse.ArgumentParser()
parser.add_argument('--log', required=True)
parser.add_argument('--timeout', type=int, default=900)
parser.add_argument('command', nargs=argparse.REMAINDER)
args = parser.parse_args()
command = args.command
if command and command[0] == '--':
    command = command[1:]
if not command:
    parser.error('command is required')
log = Path(args.log)
log.parent.mkdir(parents=True, exist_ok=True)
meta_path = log.with_suffix(log.suffix + '.json')

def source_identity():
    paths = ['src/pktpool.rs', 'tests/packet_pool_release.rs']
    return {
        'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
        'files': {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in paths if (REPO / name).exists()},
    }

metadata = {'command': command, 'cwd': str(REPO), 'started_ns': time.time_ns(), 'source_before': source_identity()}
env = os.environ.copy()
env['CARGO_BUILD_JOBS'] = '4'
env['CARGO_TERM_COLOR'] = 'never'
process = None
try:
    with log.open('xb') as output:
        with Lease('/var/lock/zfstack-qa', socket.gethostname(), log.stem, 'dev-20261018') as lease:
            metadata['lease'] = dict(lease.value)
            process = subprocess.Popen(command, cwd=REPO, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + args.timeout
            while process.poll() is None:
                lease.check()
                if time.monotonic() >= deadline:
                    raise TimeoutError('validation command timeout')
                time.sleep(0.25)
            lease.check()
            metadata['exit_code'] = process.returncode
    metadata['lease_released'] = True
except BaseException as error:
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    metadata['error'] = repr(error)
    metadata['exit_code'] = process.returncode if process is not None else None
    raise
finally:
    metadata['finished_ns'] = time.time_ns()
    metadata['source_after'] = source_identity()
    if log.exists():
        metadata['log_sha256'] = hashlib.sha256(log.read_bytes()).hexdigest()
    meta_path.write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(metadata, indent=2), flush=True)
    if log.exists():
        print('\n'.join(log.read_text(errors='replace').splitlines()[-32:]), flush=True)
raise SystemExit(metadata['exit_code'])
