"""Read-only node evidence. A quiet snapshot does not certify a performance node."""
from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


def optional_text(path):
    try:
        return Path(path).read_text()[:65536]
    except (OSError, UnicodeError):
        return None


def collect_environment(pid=None):
    pid = pid or os.getpid()
    cgroups = []
    membership = optional_text(f"/proc/{pid}/cgroup")
    if membership:
        for line in membership.splitlines():
            fields = line.split(":", 2)
            if len(fields) == 3 and fields[0] == "0":
                base = Path("/sys/fs/cgroup")
                node = base / fields[2].lstrip("/")
                if node.resolve().is_relative_to(base):
                    while True:
                        cgroups.append({"path": str(node), "files": {name: optional_text(node / name) for name in
                                       ("cpu.max", "cpu.stat", "cpuset.cpus.effective", "memory.max", "memory.current",
                                        "memory.events", "cpu.pressure", "memory.pressure", "io.pressure")}})
                        if node == base:
                            break
                        node = node.parent
    return {"schema_version": 1, "system": platform.system(), "machine": platform.machine(),
            "kernel": platform.release(), "cpu_count": os.cpu_count(), "pid": pid,
            "affinity": sorted(os.sched_getaffinity(pid)) if hasattr(os, "sched_getaffinity") else None,
            "cpuinfo": optional_text("/proc/cpuinfo"), "proc_stat": optional_text("/proc/stat"),
            "cgroup_membership": membership, "cgroup_ancestors": cgroups,
            "pressure": {name: optional_text(f"/proc/pressure/{name}") for name in ("cpu", "memory", "io")},
            "performance_certified": False,
            "limitations": ["Snapshots alone cannot establish physical-domain exclusivity or absence of host interference."]}


def platform_id():
    arch = {"AMD64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    return f"{platform.system().lower()}-{arch}"


def capability_errors(kind, node):
    if kind == "l2-queue-control":
        kind = "l2"
    if kind not in ("l1", "l2", "external-performance"):
        return []
    errors = []
    if not node or node.get("isolated") is not True or node.get("kind") not in ("ephemeral-vm", "resettable-test-node"):
        errors.append("an explicitly designated isolated test node is required")
    if not node or not node.get("physical_domain") or not node.get("generation"):
        errors.append("physical-domain and generation identity are required")
    if platform.system() != "Linux":
        errors.append("native Linux execution required")
    if kind in ("l1", "l2"):
        if not Path("/dev/net/tun").exists() or not hasattr(os, "geteuid") or os.geteuid() != 0:
            errors.append("TUN and pre-granted root/CAP_NET_ADMIN test environment required")
        for name in ("ip", "tc") + (("wg",) if kind == "l2" else ()):
            if not shutil.which(name):
                errors.append(f"missing capability: {name}")
    if kind == "l2" and (not node or node.get("rig_lease_held") is not True):
        errors.append("existing zfc-rig node lease is required")
    return errors
