#!/usr/bin/env python3
"""Verify every sealed member, frozen source and independent JSON replay."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

import common_cases as rig
import common_collect
import common_generator_followup
import common_majority
import common_reaudit
import common_recommendation
import common_sign
from run import ROOT, save, sha

ORIGINAL_JSON = ['preflight-summary.json', 'aa-summary.json', 'ordinary-summary.json', 'peak-summary.json',
    'ordinary-comparisons.json', 'peak-comparisons.json', 'verdict.json', 'cohorts.json', 'majority-median.json',
    'generator-confirmation-summary.json', 'engineering-decision.json']
AUDITED_JSON = ['preflight-summary.json', 'aa-summary.json', 'ordinary-summary.json', 'peak-summary.json',
    'ordinary-comparisons.json', 'peak-comparisons.json', 'majority-median.json', 'independent-audit.json', 'majority-with-unknowns.json']
RUNTIME_PYTHON = ['common_cases.py', 'common_env.py', 'cubic50_env.py', 'fastpath_affinity.py',
    'production_pacing.py', 'rtt_matrix.py', 'rtt_analyze.py', 'analyze.py', 'run.py', 'confirm.py']


def replay(directory, fresh):
    fresh.mkdir(parents=True, exist_ok=False)
    common_collect.collect(fresh)
    save(fresh/'majority-median.json', common_sign.analyze(fresh))
    common_reaudit.reaudit(fresh, fresh/'audited')
    save(fresh/'audited/majority-with-unknowns.json', common_majority.analyze(fresh/'audited'))
    save(fresh/'generator-confirmation-summary.json', common_generator_followup.analyze())
    save(fresh/'engineering-decision.json', common_recommendation.analyze(fresh))
    checked = []
    for parent, names in [(Path('.'), ORIGINAL_JSON), (Path('audited'), AUDITED_JSON)]:
        for name in names:
            rel = parent/name
            if (directory/rel).read_bytes() != (fresh/rel).read_bytes():
                raise RuntimeError('independent replay differs: '+str(rel))
            checked.append(str(rel))
    return checked


def source_identity():
    build = json.loads((ROOT/'qa/results/fastpath-20261024-build/build.json').read_text())
    if build['binary_sha256'] != rig.BINARIES['v5'][1]:
        raise RuntimeError('candidate build differs')
    counts = {}
    helper = ROOT/'bench/tail69_exec.py'
    helper_digest = sha(helper)
    for phase in ['preflight', 'aa', 'ordinary', 'peak', 'generator-confirmation']:
        out = ROOT/'qa/results'/('common-20261025-'+phase)
        plan = json.loads((out/'plan.json').read_text())
        for path, digest in build['sources'].items():
            if sha(ROOT/path) != digest or sha(out/'source'/path) != digest:
                raise RuntimeError('Rust/Cargo source differs: '+phase+' '+path)
        for path, digest in plan['sources'].items():
            if sha(out/'source'/path) != digest:
                raise RuntimeError('frozen snapshot differs: '+phase+' '+path)
        for name in RUNTIME_PYTHON:
            path = 'bench/capacity/'+name
            if sha(ROOT/path) != plan['sources'][path]:
                raise RuntimeError('runtime Python changed: '+phase+' '+path)
        previous = subprocess.check_output(['git', 'show', plan['base_sha']+':bench/tail69_exec.py'], cwd=ROOT)
        if hashlib.sha256(previous).hexdigest() != helper_digest:
            raise RuntimeError('resource wrapper differs from phase starting commit')
        counts[phase] = dict(rust_cargo_inputs=len(build['sources']), frozen_snapshot_files=len(plan['sources']),
            runtime_python_files=len(RUNTIME_PYTHON), resource_wrapper_matches_starting_git_tree=True)
    for name, (path, digest) in rig.BINARIES.items():
        if sha(ROOT/path) != digest:
            raise RuntimeError('executable differs: '+name)
    return dict(candidate_inputs=len(build['sources']), phases=counts, resource_wrapper_sha256=helper_digest,
        binaries={k:dict(path=p, sha256=h) for k, (p, h) in rig.BINARIES.items()})


def verify_members(directory):
    manifest_path = directory/'evidence-files.json'
    manifest = json.loads(manifest_path.read_text())['files']
    expected = dict(manifest)
    expected[str(manifest_path.relative_to(ROOT))] = dict(bytes=manifest_path.stat().st_size, sha256=sha(manifest_path))
    observed = set()
    with tarfile.open(directory/'evidence.tar.gz', 'r:gz') as archive:
        for member in archive:
            if not member.isfile() or member.name not in expected or member.name in observed:
                raise RuntimeError('unknown/non-file/duplicate archive member: '+member.name)
            relative = Path(member.name)
            if relative.is_absolute() or '..' in relative.parts:
                raise RuntimeError('unsafe member path')
            digest = hashlib.sha256()
            stream = archive.extractfile(member)
            size = 0
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
            value = expected[member.name]
            if size != value['bytes'] or digest.hexdigest() != value['sha256']:
                raise RuntimeError('archive member differs: '+member.name)
            current = ROOT/relative
            if current.stat().st_size != size or sha(current) != value['sha256']:
                raise RuntimeError('workspace member changed after sealing: '+member.name)
            observed.add(member.name)
    if observed != set(expected):
        raise RuntimeError('missing archive members')
    return dict(manifest_entries=len(manifest), archive_members=len(observed), every_member_sha256_and_size_matches=True,
        archive_bytes=(directory/'evidence.tar.gz').stat().st_size, archive_sha256=sha(directory/'evidence.tar.gz'))


def verify(directory, fresh):
    if not (rig.PREP/'all-measurements-done.json').exists():
        raise RuntimeError('measurements incomplete')
    identities = source_identity()
    files = replay(directory, fresh)
    members = verify_members(directory)
    result = dict(passed=True, source_identity=identities, independent_replay_byte_identical=files,
        independent_replay_files=len(files), sealed_evidence=members,
        note='Original strict failures retained; independent audited view uses identical numerical estimates, pairs, thresholds and p-values.')
    save(directory/'verification.json', result)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('fresh_replay_directory', type=Path)
    a = p.parse_args()
    print(json.dumps(verify(a.directory.resolve(), a.fresh_replay_directory.resolve()), indent=2))
