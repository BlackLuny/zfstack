#!/usr/bin/env python3
"""Seal the targeted retest and historical two-pair evidence without pooling."""
import argparse
import json
from pathlib import Path
import tarfile

import cubic50_retest as rig
from run import ROOT,save,sha


def bundle(destination):
    cohorts=json.loads((destination/'cohorts.json').read_text())['cohorts']
    if not all(c['identity']['valid'] and c['observer_audit']['valid'] for c in cohorts):raise RuntimeError('identity/observer audit failed')
    manifest=destination/'evidence-files.json';archive=destination/'evidence.tar.gz'
    if manifest.exists() or archive.exists():raise FileExistsError('evidence already sealed')
    files=set()
    for p in (ROOT/'qa/results').glob('cubic50-20261024-*'):
        files.update(f for f in p.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    prior=ROOT/'qa/results/fastpath-20261024-wan'
    files.add(prior/'plan.json')
    files.update(f for f in (prior/'source').rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    for folder in prior.glob('05[2-5]-cubic-r50-h1-*'):
        files.update(f for f in folder.rglob('*') if f.is_file())
    files.add(ROOT/'docs/experiments/2026-10-24-pacing-fast-path/wan-summary.json')
    files.update(ROOT/'qa/results/fastpath-20261024-build'/name for name in ['build.json','container.json','sysctls.json'])
    files.add(rig.BINARY)
    files.update((ROOT/'bench/capacity').glob('*.py'))
    files.update([ROOT/'README.md',ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'bench/Cargo.toml',ROOT/'bench/capacity/README.md',ROOT/'bench/capacity/Dockerfile',ROOT/'bench/tail69_exec.py'])
    files.update(f for f in destination.iterdir() if f.is_file() and f.suffix in ['.md','.json','.png','.svg'])
    entries={str(f.relative_to(ROOT)):dict(bytes=f.stat().st_size,sha256=sha(f)) for f in sorted(files)}
    save(manifest,dict(format=1,files=entries,note='Historical four runs are separate and never pooled. The external artifact index covers manifest and archive.'))
    def normalize(info):
        info.uid=info.gid=0;info.uname=info.gname='';return info
    with tarfile.open(archive,'x:gz',compresslevel=6) as tar:
        for f in sorted(files)+[manifest]:tar.add(f,arcname=str(f.relative_to(ROOT)),recursive=False,filter=normalize)
    return dict(files=len(entries),bytes=archive.stat().st_size,sha256=sha(archive))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args();print(json.dumps(bundle(a.directory.resolve()),indent=2))
