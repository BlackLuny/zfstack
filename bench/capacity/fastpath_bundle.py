#!/usr/bin/env python3
"""Seal all V5 continuation attempts and their exact old/V4/V5 identities."""
import argparse
import json
from pathlib import Path
import tarfile

import fastpath
from run import ROOT,save,sha


def bundle(destination):
    cohorts=json.loads((destination/'cohorts.json').read_text())['cohorts']
    if not all(c['identity_valid'] for c in cohorts):raise RuntimeError('identity audit failed')
    manifest=destination/'evidence-files.json';archive=destination/'evidence.tar.gz'
    if manifest.exists() or archive.exists():raise FileExistsError('evidence already sealed')
    files=set()
    for folder in (ROOT/'qa/results').glob('fastpath-20261024-*'):
        files.update(f for f in folder.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    files.update(f for f in (ROOT/'qa/results/production-20261024-build').rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    for name in ['rtt-20261023-final-aa','production-20261024-review-wan-long']:
        parent=ROOT/'qa/results'/name;source=parent/('frozen_source' if name.startswith('rtt-') else 'source')
        files.update(f for f in source.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
        files.add(parent/'plan.json')
    files.update(fastpath.BINARIES.values())
    files.update((ROOT/'bench/capacity').glob('*.py'))
    files.update([ROOT/'README.md',ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'bench/Cargo.toml',
        ROOT/'bench/capacity/README.md',ROOT/'bench/capacity/Dockerfile',ROOT/'bench/tail69_exec.py',ROOT/'docs/design/0011-contention-egress-pacing.md'])
    files.update(f for f in destination.iterdir() if f.is_file() and f.suffix in ['.md','.json','.png','.svg'])
    entries={str(f.relative_to(ROOT)):dict(bytes=f.stat().st_size,sha256=sha(f)) for f in sorted(files)}
    save(manifest,dict(format=1,files=entries,note='Manifest excludes itself; the external artifact index covers it and the archive.'))
    def normalize(info):
        info.uid=info.gid=0;info.uname=info.gname='';return info
    with tarfile.open(archive,'x:gz',compresslevel=6) as tar:
        for f in sorted(files)+[manifest]:tar.add(f,arcname=str(f.relative_to(ROOT)),recursive=False,filter=normalize)
    return dict(sha256=sha(archive),bytes=archive.stat().st_size,files=len(entries))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('destination',type=Path);a=p.parse_args()
    print(json.dumps(bundle(a.destination.resolve()),indent=2))
