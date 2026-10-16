"""Collect owned evidence/receipts only; never include runtime WG keys/configs."""
import hashlib
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
E = ROOT / "evidence"
commands = [["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
            ["ip", "-j", "link", "show"], ["ip", "netns", "list"],
            ["df", "-h", "/"], ["free", "-h"], ["dpkg-query", "-W", "python3.11", "cmake", "libclang-dev"]]
observations = []
for cmd in commands:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    observations.append({"argv": cmd, "returncode": result.returncode,
                         "stdout": result.stdout, "stderr": result.stderr})
leases = []
for path in Path("/var/lock/zfstack-qa").glob("*"):
    leases.append({"name": path.name, "size_bytes": path.stat().st_size})
(E / "host-final.json").write_text(json.dumps({"observed_unix_ns": time.time_ns(),
    "commands": observations, "public_lease_directory_entries": leases}, indent=2)+"\n")
selected = [(p, "evidence/" + str(p.relative_to(E))) for p in sorted(E.rglob("*")) if p.is_file()]
for build_name in ("build", "build-2", "build-3", "build-4"):
    build = ROOT / build_name
    if not build.is_dir():
        continue
    for path in sorted(build.glob("**/*")):
        # Do not descend into repos or copy binaries/target/cache/config data.
        relative = path.relative_to(build)
        if path.is_file() and len(relative.parts) <= 2 and path.suffix in (".json", ".log", ".lock"):
            selected.append((path, "build-receipts/" + build_name + "/" + str(relative)))
index = [{"path": name, "size_bytes": path.stat().st_size,
          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path, name in selected]
destination = ROOT / "collected-artifacts.tar.gz"
with destination.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as archive:
    for path, name in selected:
        archive.add(path, arcname=name, recursive=False)
(ROOT / "collected-artifacts.index.json").write_text(json.dumps({"files": index}, indent=2)+"\n")
print("collected", len(index), "files; bytes", destination.stat().st_size)
