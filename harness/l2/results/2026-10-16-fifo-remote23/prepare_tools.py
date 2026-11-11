"""Prepare QA helpers and retain apt transactions, including dependency upgrades."""
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "source"))
from qa.lease import Lease

boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
lease = Lease("/var/lock/zfstack-qa", "zfstack-qa-vps-23.136.204.72-23826",
              "prepare-tools-pr75-20261016", boot)
records = []


def run(argv):
    before = time.time_ns()
    path = ROOT / ("evidence/preparation-" + str(len(records)) + ".log")
    with path.open("xb") as stream:
        r = subprocess.run(argv, stdout=stream, stderr=subprocess.STDOUT, timeout=600)
    records.append({"argv": argv, "returncode": r.returncode,
                    "started_unix_ns": before, "finished_unix_ns": time.time_ns(), "log": path.name})
    lease.check()
    if r.returncode:
        raise RuntimeError("preparation command failed; original log retained")


try:
    with lease:
        packages = []
        if not shutil.which("cmake"):
            packages.append("cmake")
        if importlib.util.find_spec("ensurepip") is None:
            packages.append("python3-venv")
        if packages:
            run(["nice", "-n", "10", "apt-get", "update"])
            run(["nice", "-n", "10", "apt-get", "install", "-y", "--no-install-recommends", "--no-upgrade", *packages])
        run(["python3", "-m", "venv", str(ROOT / "qa-venv")])
finally:
    (ROOT / "evidence/preparation-execution.json").write_text(json.dumps({
        "commands": records, "lease": {**lease.value, "owned": lease.owned},
        "installed_packages": locals().get("packages", []),
    }, indent=2)+"\n")
