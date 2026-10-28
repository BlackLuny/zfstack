#!/usr/bin/env python3
"""Seal all common-case attempts, original failures and independent re-audit."""
import argparse
import gzip
import json
from pathlib import Path
import tarfile

import common_cases as rig
from run import ROOT, save, sha


def bundle(directory):
    if not (rig.PREP/'all-measurements-done.json').exists():
        raise RuntimeError('finish every measurement before sealing')
    manifest = directory/'evidence-files.json'
    archive = directory/'evidence.tar.gz'
    if manifest.exists() or archive.exists():
        raise FileExistsError('already sealed')
    files = set()
    for phase in ['preparation', 'preflight', 'aa', 'ordinary', 'peak', 'generator-confirmation']:
        parent = ROOT/'qa/results'/('common-20261025-'+phase)
        files.update(f for f in parent.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    files.update(ROOT/binary for binary, _ in rig.BINARIES.values())
    files.update(f for f in (ROOT/'qa/results/fastpath-20261024-build').rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    # Retained original pre-change build identity/source, without pooling runs.
    files.add(ROOT/'qa/results/capacity-20261023-preparation/build.json')
    old_source = ROOT/'qa/results/rtt-20261023-final-aa'
    files.add(old_source/'plan.json')
    files.update(f for f in (old_source/'frozen_source').rglob('*') if f.is_file())
    files.update((ROOT/'bench/capacity').glob('*.py'))
    files.add(ROOT/'bench/capacity/pacing_contract.rs')
    files.update(ROOT/name for name in ['README.md', 'Cargo.toml', 'Cargo.lock', 'bench/Cargo.toml',
        'bench/capacity/README.md', 'bench/capacity/Dockerfile', 'bench/tail69_exec.py',
        'docs/design/0011-contention-egress-pacing.md'])
    files.update(f for f in directory.rglob('*') if f.is_file() and f.suffix in ['.md', '.json', '.png', '.svg']
        and f.name not in ['verification.json', 'evidence-files.json'])
    if any(not f.exists() or not f.is_file() for f in files):
        raise RuntimeError('missing required evidence member')
    entries = {str(f.relative_to(ROOT)):dict(bytes=f.stat().st_size, sha256=sha(f)) for f in sorted(files)}
    save(manifest, dict(format=1, files=entries,
        note='All attempts and original failures retained. The re-audit changes validity classification only; no numerical estimate or pair changes. Manifest excludes itself; external index covers manifest/archive/verification.'))
    def normalize(info):
        info.uid = info.gid = info.mtime = 0
        info.uname = info.gname = ''
        return info
    with archive.open('xb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, compresslevel=6, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|') as tar:
            for f in sorted(files)+[manifest]:
                tar.add(f, arcname=str(f.relative_to(ROOT)), recursive=False, filter=normalize)
    return dict(files=len(entries), bytes=archive.stat().st_size, sha256=sha(archive))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    a = p.parse_args()
    print(json.dumps(bundle(a.directory.resolve()), indent=2))
