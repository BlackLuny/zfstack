#!/usr/bin/env python3
"""Seal and independently replay all adaptive attempts and candidate identities."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

import adaptive_cases as rig
import adaptive_analyze as analyze
import adaptive_dynamic as dynamic
import adaptive_qualify as qualification
import adaptive_diagnostics as diagnostics
from run import ROOT,save,sha

MAIN_JSON=[p+'-'+suffix+'.json' for p in rig.PHASES for suffix in ['summary','comparisons']]+['cohorts.json','qualification.json','dynamic-transitions.json','dynamic-phase-latency.json','demand-transitions.json','demand-phase-latency.json']
EXPECTED=MAIN_JSON+[f'pilot-{c}/{name}.json' for c in ['a0','a1'] for name in ['pilot-summary','pilot-comparisons','cohorts']]+['pilot-a1/dynamic-transitions.json','pilot-a1/demand-transitions.json','diagnostics.json']


def done():
    marker=rig.PREP/'measurements-done.json'
    if not marker.exists():raise RuntimeError('finish the complete fixed A1 plan before heavy analysis')
    value=json.loads(marker.read_text())
    if value.get('candidate')!='a1' or value.get('attempts')!=312 or value.get('complete') is not True:
        raise RuntimeError('measurement completion identity differs')


def collect(directory):
    done();rig.CANDIDATE='a1';analyze.collect(directory,rig.PHASES)
    save(directory/'qualification.json',qualification.qualify(directory))
    for candidate in ['a0','a1']:
        rig.CANDIDATE=candidate;target=directory/('pilot-'+candidate);analyze.collect(target,['pilot'])
        if candidate=='a1':
            summary=json.loads((target/'pilot-summary.json').read_text())
            for phase in ['dynamic','demand']:
                subset=dict(rows=[r for r in summary['rows'] if r['scenario']==phase])
                save(target/(phase+'-transitions.json'),dynamic.analyze(phase,subset,out=rig.output('pilot')))
    rig.CANDIDATE='a1'
    save(directory/'diagnostics.json',diagnostics.collect(directory))
    return EXPECTED


def bundle(directory):
    done();manifest=directory/'evidence-files.json';archive=directory/'evidence.tar.gz'
    if manifest.exists() or archive.exists():raise FileExistsError('evidence already sealed')
    required=EXPECTED+['README.md','PROTOCOL.md','A1-CORRECTION.md','CODE-ARGUMENT.md']
    if any(not (directory/name).is_file() for name in required):raise RuntimeError('finish the complete report before sealing')
    files=set()
    parents=[rig.PREP,ROOT/'qa/results/adaptive-20261025-pilot',ROOT/'qa/results/adaptive-20261025-a1-pilot']+[ROOT/'qa/results'/('adaptive-20261025-a1-'+p) for p in rig.PHASES]
    for parent in parents:
        files.update(f for f in parent.rglob('*') if f.is_file() and '__pycache__' not in f.parts)
    for candidate in ['a0','a1']:
        build=json.loads((rig.PREP/f'build-{candidate}.json').read_text());files.add(ROOT/build['binary'])
        if candidate=='a1':files.update(ROOT/p for p in build['sources'])
    files.add(ROOT/rig.V5[0]);files.update((ROOT/'bench/capacity').glob('*.py'))
    files.update(ROOT/p for p in ['README.md','bench/capacity/README.md','bench/capacity/Dockerfile','bench/capacity/pacing_contract.rs','bench/tail69_exec.py'])
    files.update(f for f in directory.rglob('*') if f.is_file() and f.suffix in ['.md','.json','.png','.svg'] and f.name not in ['verification.json','evidence-files.json'])
    entries={str(f.relative_to(ROOT)):dict(bytes=f.stat().st_size,sha256=sha(f)) for f in sorted(files)}
    save(manifest,dict(format=1,files=entries,note='All336 attempts, original failures, A0/A1/V5 binaries and frozen sources. Verification/index outside archive avoid a hash cycle.'))
    def normalize(info):
        info.uid=info.gid=info.mtime=0;info.uname=info.gname='';return info
    with archive.open('xb') as raw,gzip.GzipFile(filename='',mode='wb',fileobj=raw,compresslevel=6,mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w|') as tar:
            for f in sorted(files)+[manifest]:tar.add(f,arcname=str(f.relative_to(ROOT)),recursive=False,filter=normalize)
    return dict(files=len(entries),bytes=archive.stat().st_size,sha256=sha(archive))


def identities():
    result={}
    for candidate in ['a0','a1']:
        build=json.loads((rig.PREP/f'build-{candidate}.json').read_text())
        if sha(ROOT/build['binary'])!=build['binary_sha256']:raise RuntimeError('candidate binary differs')
        phases=['pilot']+([] if candidate=='a0' else rig.PHASES);counts={};rig.CANDIDATE=candidate
        for phase in phases:
            parent=rig.output(phase);plan=json.loads((parent/'plan.json').read_text())
            if plan['rows']!=rig.sequence(phase):raise RuntimeError('frozen sequence differs')
            for path,digest in build['sources'].items():
                if sha(parent/'source'/path)!=digest:raise RuntimeError('candidate source snapshot differs: '+candidate+' '+phase+' '+path)
                if candidate=='a1' and sha(ROOT/path)!=digest:raise RuntimeError('current A1 Rust/Cargo source differs')
            for path,digest in plan['sources'].items():
                if sha(parent/'source'/path)!=digest:raise RuntimeError('frozen phase member differs')
            counts[phase]=dict(build_inputs=len(build['sources']),snapshot_inputs=len(plan['sources']))
        result[candidate]=dict(binary=build['binary'],sha256=build['binary_sha256'],phases=counts)
    v5=json.loads((rig.PREP/'v5-build.json').read_text())
    if sha(ROOT/rig.V5[0])!=rig.V5[1] or v5['binary_sha256']!=rig.V5[1]:raise RuntimeError('V5 executable differs')
    for path,digest in v5['sources'].items():
        if sha(rig.PREP/'v5-source'/path)!=digest:raise RuntimeError('V5 source differs')
    frozen=json.loads((rig.PREP/'confirmation-plan.json').read_text())
    for path,digest in frozen['runtime_sources'].items():
        if sha(ROOT/path)!=digest:raise RuntimeError('frozen confirmation runtime/protocol differs: '+path)
    result['v5']=dict(binary=rig.V5[0],sha256=rig.V5[1],source_inputs=len(v5['sources']))
    result['confirmation_runtime_inputs']=len(frozen['runtime_sources']);rig.CANDIDATE='a1'
    return result


def members(directory):
    manifest=directory/'evidence-files.json';expected=json.loads(manifest.read_text())['files']
    expected=dict(expected);expected[str(manifest.relative_to(ROOT))]=dict(bytes=manifest.stat().st_size,sha256=sha(manifest));seen=set()
    with tarfile.open(directory/'evidence.tar.gz','r:gz') as archive:
        for entry in archive:
            path=Path(entry.name)
            if not entry.isfile() or entry.name not in expected or entry.name in seen or path.is_absolute() or '..' in path.parts:raise RuntimeError('unknown/duplicate/unsafe archive member')
            h=hashlib.sha256();length=0;stream=archive.extractfile(entry)
            while block:=stream.read(1<<20):h.update(block);length+=len(block)
            value=expected[entry.name]
            if length!=value['bytes'] or h.hexdigest()!=value['sha256']:raise RuntimeError('archive content differs: '+entry.name)
            if (ROOT/path).stat().st_size!=length or sha(ROOT/path)!=value['sha256']:raise RuntimeError('workspace changed after sealing: '+entry.name)
            seen.add(entry.name)
    if seen!=set(expected):raise RuntimeError('missing archive members')
    return dict(manifest_entries=len(expected)-1,archive_members=len(seen),every_member_sha256_and_size_matches=True,archive_bytes=(directory/'evidence.tar.gz').stat().st_size,archive_sha256=sha(directory/'evidence.tar.gz'))


def verify(directory,fresh):
    done();fresh.mkdir(parents=True,exist_ok=False);identity=identities();collect(fresh)
    for relative in EXPECTED:
        if (directory/relative).read_bytes()!=(fresh/relative).read_bytes():raise RuntimeError('independent replay differs: '+relative)
    archive=members(directory)
    result=dict(passed=True,source_identity=identity,independent_replay_files=len(EXPECTED),independent_replay_byte_identical=EXPECTED,sealed_evidence=archive)
    save(directory/'verification.json',result);return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['collect','bundle','verify']);p.add_argument('directory',type=Path);p.add_argument('--fresh',type=Path);a=p.parse_args();directory=a.directory.resolve()
    if a.action=='collect':print(json.dumps(dict(files=collect(directory)),indent=2))
    elif a.action=='bundle':print(json.dumps(bundle(directory),indent=2))
    else:
        if a.fresh is None:p.error('--fresh is required for independent verification')
        print(json.dumps(verify(directory,a.fresh.resolve()),indent=2))
