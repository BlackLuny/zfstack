"""Materialize the versioned raw evidence archive and verify every original file."""
import hashlib
import json
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def evidence_root():
    index = json.loads((HERE / 'evidence-index.json').read_text())
    archive = HERE / 'evidence.tar.gz'
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == index['archive_sha256']
    destination = HERE / 'evidence'
    if not destination.exists():
        destination.mkdir()
        with tarfile.open(archive) as source:
            source.extractall(destination, filter='data')
    for row in index['files']:
        path = destination / row['path']
        assert path.stat().st_size == row['size_bytes'], path
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], path
    return destination
