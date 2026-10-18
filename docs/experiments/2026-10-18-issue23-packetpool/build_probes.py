#!/usr/bin/env python3
"""Export immutable inputs and independently build ordinary PacketPool probes."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import time


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--probe', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--candidate', required=True)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    probe = Path(args.probe).read_bytes()
    env = os.environ.copy()
    env.update(CARGO_BUILD_JOBS='4', CARGO_TERM_COLOR='never', CARGO_INCREMENTAL='0',
               CARGO_PROFILE_RELEASE_LTO='fat', CARGO_PROFILE_RELEASE_CODEGEN_UNITS='1',
               CARGO_PROFILE_RELEASE_PANIC='abort', CARGO_PROFILE_RELEASE_INCREMENTAL='false')
    env.pop('RUSTFLAGS', None)
    env.pop('CARGO_ENCODED_RUSTFLAGS', None)
    env.pop('CARGO_TARGET_DIR', None)
    manifest = {
        'started_ns': time.time_ns(), 'probe_sha256': hashlib.sha256(probe).hexdigest(),
        'toolchain': subprocess.check_output(['rustc', '+1.94.1', '-vV'], text=True),
        'profile': {k: v for k, v in env.items() if k.startswith('CARGO_PROFILE_') or k in ['CARGO_INCREMENTAL', 'CARGO_BUILD_JOBS']},
        'arms': [],
    }
    paths = ['Cargo.toml', 'Cargo.lock', 'rustfmt.toml', 'src', 'bench/Cargo.toml',
             'bench/src', 'bench/examples', 'harness/Cargo.toml', 'harness/src']
    for name, ref in [('A1', args.base), ('A2', args.base), ('B', args.candidate)]:
        commit = subprocess.check_output(['git', 'rev-parse', ref + '^{commit}'], cwd=repo, text=True).strip()
        if commit != ref:
            raise ValueError('full immutable commit SHAs are required')
        archive = subprocess.check_output(['git', 'archive', ref, '--', *paths], cwd=repo)
        dest = out / name
        dest.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            for entry in stream.getmembers():
                resolved = (dest / entry.name).resolve()
                if not resolved.is_relative_to(dest) or not (entry.isfile() or entry.isdir()):
                    raise ValueError('unsafe source archive entry: ' + entry.name)
            stream.extractall(dest)
        examples = dest / 'examples'
        examples.mkdir(exist_ok=True)
        (examples / 'issue23_packetpool_probe.rs').write_bytes(probe)
        command = ['cargo', '+1.94.1', 'build', '--locked', '--release', '-p', 'zfstack', '--example', 'issue23_packetpool_probe']
        entry = {
            'name': name, 'commit': commit, 'cwd': str(dest), 'command': command,
            'archive_sha256': hashlib.sha256(archive).hexdigest(),
            'pktpool_sha256': sha(dest / 'src/pktpool.rs'), 'lock_sha256': sha(dest / 'Cargo.lock'),
            'started_ns': time.time_ns(),
        }
        log = out / (name + '-build.log')
        with log.open('xb') as output:
            result = subprocess.run(command, cwd=dest, env=env, stdout=output, stderr=subprocess.STDOUT)
        entry.update(finished_ns=time.time_ns(), exit_code=result.returncode, log_sha256=sha(log))
        if result.returncode == 0:
            binary = dest / 'target/release/examples/issue23_packetpool_probe'
            entry.update(binary=str(binary), binary_sha256=sha(binary))
        manifest['arms'].append(entry)
        (out / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(json.dumps(entry), flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)
    manifest['finished_ns'] = time.time_ns()
    (out / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
