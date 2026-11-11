#!/usr/bin/env python3
"""Verify every archived byte before optional fresh extraction; never execute archive content."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

P = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--extract', type=Path)
    args = parser.parse_args()
    index = json.loads((P / 'collected-artifacts.index.json').read_text())
    archive = P / 'collected-artifacts.tar.gz'
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert digest == index['archive_sha256']
    assert archive.stat().st_size == index['archive_bytes']
    expected = {row['path']: row for row in index['files']}
    assert len(expected) == len(index['files'])
    if args.extract:
        args.extract.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        assert len(members) == len(expected)
        assert {member.name for member in members} == set(expected)
        for member in members:
            path = PurePosixPath(member.name)
            assert member.isfile() and not path.is_absolute() and '..' not in path.parts
            raw = stream.extractfile(member).read()
            assert len(raw) == expected[member.name]['size_bytes']
            assert hashlib.sha256(raw).hexdigest() == expected[member.name]['sha256']
            if args.extract:
                target = args.extract / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as output:
                    output.write(raw)
    print(json.dumps({'verified': True, 'files': len(expected), 'archive_sha256': digest,
                      'archive_bytes': archive.stat().st_size}, indent=2))


if __name__ == '__main__':
    main()
