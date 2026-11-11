"""Read-only inventory of the explicitly assigned remote VM; no app arguments."""
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
rows = []
commands = [["uname", "-a"], ["lscpu"], ["systemd-detect-virt"],
            ["df", "-h", "/"], ["free", "-h"], ["uptime"],
            ["python3", "--version"], ["rustup", "toolchain", "list"],
            ["ip", "-Version"], ["tc", "-Version"],
            ["docker", "ps", "--format", "{{.Names}} {{.Status}}"],
            ["ps", "-eo", "pid,ppid,pcpu,pmem,comm", "--sort=-pcpu"],
            ["ip", "-j", "link", "show"], ["ip", "netns", "list"]]
for cmd in commands:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        rows.append({"argv": cmd, "rc": r.returncode, "stdout": r.stdout, "stderr": r.stderr})
    except (OSError, subprocess.SubprocessError) as e:
        rows.append({"argv": cmd, "error": str(e)})
files = {}
for p in ["/proc/self/cgroup", "/proc/stat", "/proc/pressure/cpu", "/proc/pressure/memory",
          "/proc/pressure/io", "/proc/sys/kernel/random/boot_id", "/sys/fs/cgroup/cgroup.controllers"]:
    try:
        files[p] = Path(p).read_text()
    except OSError as e:
        files[p] = {"unavailable": str(e)}
membership = Path("/proc/self/cgroup").read_text().strip().split("::")[-1]
node = Path("/sys/fs/cgroup") / membership.lstrip("/")
while True:
    for name in ("cpu.max", "cpu.stat", "cpuset.cpus.effective", "memory.max", "memory.events"):
        p = node / name
        try:
            files[str(p)] = p.read_text()
        except OSError:
            files[str(p)] = None
    if node == Path("/sys/fs/cgroup"):
        break
    node = node.parent
result = {"observed_unix_ns": time.time_ns(), "commands": rows, "files": files,
          "affinity": sorted(os.sched_getaffinity(0)),
          "namespaces": {n: os.readlink("/proc/self/ns/"+n) for n in ("net", "mnt", "pid")},
          "physical_cpu_exclusivity": "UNPROVEN: KVM VPS with other services"}
(ROOT / "evidence/host-before.json").write_text(json.dumps(result, indent=2)+"\n")
print("preflight inventory saved")
