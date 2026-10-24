#!/usr/bin/env python3
"""Package retained RTT evidence, frozen executables and reanalysis tools."""
import argparse
import json
from pathlib import Path
import tarfile

from run import ROOT, save, sha


def bundle(destination):
    inventory = json.loads((destination / "cohorts.json").read_text())
    audits = json.loads((destination / "audit.json").read_text())
    if inventory["counts"]["completed"] != inventory["counts"]["planned"]:
        raise RuntimeError("incomplete planned evidence")
    if not all(a["identity_valid"] for a in audits):
        raise RuntimeError("identity audit failed")
    archive, manifest = destination / "evidence.tar.gz", destination / "evidence-files.json"
    if archive.exists() or manifest.exists():
        raise FileExistsError("evidence output already exists")
    files = set()
    for directory in sorted((ROOT / "qa/results").glob("rtt-20261023-*")):
        if directory.is_dir():
            files.update(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    files.update((ROOT / "target/release").glob("zfbench-rtt-*"))
    files.update((ROOT / "bench/capacity").glob("*.py"))
    files.update([ROOT / "bench/capacity/README.md", ROOT / "bench/capacity/Dockerfile", ROOT / "bench/tail69_exec.py"])
    files.update(p for p in destination.iterdir() if p.is_file() and p.suffix in (".md", ".json", ".png", ".svg"))
    files = sorted(files)
    entries = {str(p.relative_to(ROOT)): dict(sha256=sha(p), bytes=p.stat().st_size) for p in files}
    save(manifest, dict(format=1, files=entries, note="The file map excludes its own manifest; the external artifact index covers the manifest and archive."))
    def normalize(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info
    with tarfile.open(archive, "x:gz", compresslevel=6) as tar:
        for p in files + [manifest]:
            tar.add(p, arcname=str(p.relative_to(ROOT)), recursive=False, filter=normalize)
    return dict(archive=str(archive), sha256=sha(archive), bytes=archive.stat().st_size, indexed_files=len(entries))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("destination", type=Path)
    a = p.parse_args()
    print(json.dumps(bundle(a.destination.resolve()), indent=2))
