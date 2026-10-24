#!/usr/bin/env python3
"""Seal production-pacing raw evidence, source snapshots and frozen binaries."""
import argparse
import json
from pathlib import Path
import tarfile

from run import ROOT,save,sha


def bundle(destination):
    audits=json.loads((destination/'audit.json').read_text())
    if not all(a['identity_valid'] for a in audits):raise RuntimeError('identity audit failed')
    files=set()
    for folder in (ROOT/'qa/results').glob('production-20261024-*'):
        files.update(f for f in folder.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    # Preserve the exact old library/host sources for cross-binary comparisons.
    prior=ROOT/'qa/results/rtt-20261023-final-aa/frozen_source'
    if not prior.is_dir():raise FileNotFoundError(prior)
    files.update(f for f in prior.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    files.add(prior.parent/'plan.json')
    files.add(prior.parent/'source.patch')
    files.update((ROOT/'target/release').glob('zfbench-production-v*-20261024'))
    files.add(ROOT/'target/release/zfbench-rtt-final-20261023')
    files.update((ROOT/'bench/capacity').glob('*.py'))
    files.update([ROOT/'README.md',ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'bench/Cargo.toml',
        ROOT/'bench/capacity/README.md',ROOT/'bench/capacity/Dockerfile',ROOT/'bench/tail69_exec.py',ROOT/'docs/design/0011-contention-egress-pacing.md'])
    files.update(f for f in destination.iterdir() if f.is_file() and f.suffix in ['.md','.json','.png','.svg'])
    manifest=destination/'evidence-files.json';archive=destination/'evidence.tar.gz'
    if manifest.exists() or archive.exists():raise FileExistsError('evidence already sealed')
    entries={str(f.relative_to(ROOT)):dict(bytes=f.stat().st_size,sha256=sha(f)) for f in sorted(files)}
    save(manifest,dict(format=1,files=entries,note='Manifest excludes itself; external artifact index covers it and the archive.'))
    def normalize(info):
        info.uid=info.gid=0;info.uname=info.gname='';return info
    with tarfile.open(archive,'x:gz',compresslevel=6) as tar:
        for f in sorted(files)+[manifest]:tar.add(f,arcname=str(f.relative_to(ROOT)),recursive=False,filter=normalize)
    return dict(sha256=sha(archive),bytes=archive.stat().st_size,files=len(entries))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path);a=p.parse_args()
    print(json.dumps(bundle(a.destination.resolve()),indent=2))
