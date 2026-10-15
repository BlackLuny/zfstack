import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
from datetime import datetime, timezone

root = Path('/Users/lulin/zfstack-qa-evidence-20261015')
archive = root / 'qa-results-20261015.tar.zst'
index_path = Path('/tmp/zfstack-review-materials-20261015/qa/review-evidence/artifact-index.json')
index_bytes = index_path.read_bytes()
index = json.loads(index_bytes)
expected = {a['path']: a for a in index['artifacts']}
assert len(expected) == len(index['artifacts'])
seen = set()
content = {}
links = {}
total = 0
errors = []
with (root / 'archive-verification-zstd.log').open('wb') as stderr:
    process = subprocess.Popen(['zstd', '-d', '-c', str(archive)], stdout=subprocess.PIPE, stderr=stderr)
    with tarfile.open(fileobj=process.stdout, mode='r|') as stream:
        for member in stream:
            p = PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts:
                errors.append({'unsafe_path': member.name})
                continue
            if member.isdir():
                continue
            if not member.isfile() and not member.islnk():
                errors.append({'unexpected_type': member.name, 'type': repr(member.type)})
                continue
            if not member.name.startswith('qa/results/'):
                errors.append({'unexpected_prefix': member.name})
                continue
            rel = member.name[len('qa/results/'):]
            if rel in seen:
                errors.append({'duplicate': rel})
            seen.add(rel)
            if member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or '..' in target.parts or not member.linkname.startswith('qa/results/'):
                    errors.append({'unsafe_hardlink': rel, 'target': member.linkname})
                else:
                    links[rel] = member.linkname[len('qa/results/'):]
                continue
            digest = hashlib.sha256()
            size = 0
            f = stream.extractfile(member)
            while chunk := f.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            total += size
            content[rel] = (size, digest.hexdigest())
            want = expected.get(rel)
            if want is None or size != want['size_bytes'] or digest.hexdigest() != want['sha256']:
                errors.append({'mismatch': rel, 'size_bytes': size, 'sha256': digest.hexdigest()})
    # Consume remaining tar padding so zstd can verify its frame checksum.
    while process.stdout.read(1024 * 1024):
        pass
    rc = process.wait()
pending = dict(links)
while pending:
    resolved = []
    for rel, target in pending.items():
        if target not in content:
            continue
        size, digest = content[target]
        content[rel] = (size, digest)
        total += size
        want = expected.get(rel)
        if want is None or size != want['size_bytes'] or digest != want['sha256']:
            errors.append({'hardlink_mismatch': rel, 'target': target, 'size_bytes': size, 'sha256': digest})
        resolved.append(rel)
    if not resolved:
        errors.append({'unresolved_or_cyclic_hardlinks': pending})
        break
    for rel in resolved:
        del pending[rel]
missing = sorted(set(expected) - seen)
sha = hashlib.sha256()
with archive.open('rb') as f:
    while chunk := f.read(1024 * 1024):
        sha.update(chunk)
result = {
    'verified_utc': datetime.now(timezone.utc).isoformat(),
    'archive_path': str(archive),
    'archive_size_bytes': archive.stat().st_size,
    'archive_sha256': sha.hexdigest(),
    'artifact_index_sha256': hashlib.sha256(index_bytes).hexdigest(),
    'method': 'Streaming zstd decompression and tar member size/SHA-256 comparison; archive hardlinks resolved to verified in-archive content, including cycle/missing/unsafe-target rejection; no extraction.',
    'member_root': 'qa/results',
    'regular_files': len(seen),
    'hardlink_members': len(links),
    'logical_bytes': total,
    'missing': missing,
    'errors': errors,
    'zstd_exit_code': rc,
    'status': 'PASS' if not errors and not missing and rc == 0 and total == index['totals']['logical_bytes'] else 'FAIL',
}
(root / 'archive-verification.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({k:v for k,v in result.items() if k not in ('missing', 'errors')}, indent=2))
print(json.dumps({'missing_count':len(missing), 'error_count':len(errors)}))
raise SystemExit(0 if result['status'] == 'PASS' else 1)
