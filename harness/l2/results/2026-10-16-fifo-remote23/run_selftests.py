"""One serial three-mode qualification on the assigned VM, all outcomes retained."""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "source"))
from qa.lease import Lease

out = ROOT / "evidence"
plan = json.loads((out / "selftest-plan.json").read_text())
for name, sha in plan["source_files"].items():
    assert hashlib.sha256((ROOT / "source" / name).read_bytes()).hexdigest() == sha, name
boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
domain = "zfstack-qa-vps-23.136.204.72-23826"
lease = Lease("/var/lock/zfstack-qa", domain, "fifo-selftests-20261016-pr75", boot)
results = []
with lease:
    for mode in plan["order"]:
        path = out / (mode + ".json")
        assert not path.exists() and not path.with_suffix("").exists()
        cmd = ["timeout", "--kill-after=5", "180", "bash", str(ROOT / "source/qa/isolate-linux.sh"),
               "python3", str(ROOT / "source/harness/l2/fifo_selftest.py"), "--mode", mode,
               "--output", str(path)]
        before = Path("/proc/stat").read_text()
        begin = time.time_ns()
        with (out / (mode + ".log")).open("xb") as log:
            r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=190)
        record = {"mode": mode, "command": cmd, "returncode": r.returncode,
                  "started_unix_ns": begin, "finished_unix_ns": time.time_ns(),
                  "proc_stat_before": before, "proc_stat_after": Path("/proc/stat").read_text()}
        results.append(record)
        (out / (mode + ".execution.json")).write_text(json.dumps(record, indent=2)+"\n")
        lease.check()
        print(mode, "exit", r.returncode, flush=True)
(out / "selftest-execution.json").write_text(json.dumps({
    "results": results, "lease": {**lease.value, "release_status": "PASS", "owned": lease.owned},
    "physical_domain_scope": "assigned guest VM; physical hypervisor exclusivity unproven",
}, indent=2)+"\n")
print("selftest sequence finished; lease released", flush=True)
