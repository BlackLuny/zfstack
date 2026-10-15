"""Read-only Linux performance observations; unsupported fields stay unknown."""
import os
from pathlib import Path
import platform
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from qa.environment import collect_environment
except ModuleNotFoundError:
    collect_environment = None  # Historical standalone checkout compatibility.


def _read(path):
    try: return Path(path).read_text().strip()
    except OSError: return None


def cgroup_snapshot(pid):
    """Resolve the DUT's own cgroup-v2 path and retain every ancestor limit."""
    if collect_environment is not None:
        try:
            observed = collect_environment(pid)
        except (ProcessLookupError, PermissionError):
            return {"path": None, "error": "DUT cgroup unavailable"}
        membership = observed["cgroup_membership"] or ""
        paths = [line[3:] for line in membership.splitlines() if line.startswith("0::")]
        if len(paths) != 1 or not observed["cgroup_ancestors"]:
            return {"path": None, "error": "cgroup v2 unavailable"}
        return {"path": paths[0], "ancestors": [{"path": row["path"], **{key: value.strip() if isinstance(value, str) else value for key, value in row["files"].items()}} for row in observed["cgroup_ancestors"]]}
    text = _read(f"/proc/{pid}/cgroup")
    if text is None: return {"path": None, "error": "DUT cgroup unavailable"}
    paths = [line[3:] for line in text.splitlines() if line.startswith("0::")]
    if len(paths) != 1: return {"path": None, "error": "cgroup v2 unavailable"}
    mount = Path("/sys/fs/cgroup")
    group = mount / paths[0].lstrip("/")
    ancestors = []
    current = group
    while current == mount or mount in current.parents:
        ancestors.append({"path": str(current), **{name: _read(current / name) for name in
                          ("cpu.max", "cpu.stat", "cpuset.cpus.effective", "memory.max", "memory.current", "cpu.pressure", "memory.pressure", "io.pressure")}})
        if current == mount: break
        current = current.parent
    return {"path": paths[0], "ancestors": ancestors}


def snapshot(pid=None):
    pid = pid or os.getpid()
    compilers = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        name = _read(path)
        if name in ("cargo", "rustc", "cc1", "cc1plus", "rust-lld", "ld.lld"):
            compilers.append({"pid": int(path.parent.name), "name": name})
    group = cgroup_snapshot(pid)
    leaf = (group.get("ancestors") or [{}])[0]
    cpu = _read("/proc/cpuinfo")
    model = next((line.split(":", 1)[1].strip() for line in (cpu or "").splitlines() if line.startswith("model name")), None)
    try: affinity = sorted(os.sched_getaffinity(pid))
    except (AttributeError, ProcessLookupError, PermissionError): affinity = None
    return {"at_ns": time.monotonic_ns(), "pid": pid, "kernel": platform.release(), "cpu_model": model,
            "affinity": affinity, "compilers": compilers, "cgroup": group,
            # Compatibility for the historical descriptive analyzer.
            "cpu_stat": leaf.get("cpu.stat"), "cpu_max": leaf.get("cpu.max"),
            "loadavg": _read("/proc/loadavg"), "proc_stat": _read("/proc/stat"),
            "cpu_pressure": _read("/proc/pressure/cpu"), "memory_pressure": _read("/proc/pressure/memory"),
            "io_pressure": _read("/proc/pressure/io"), "memory": _read("/proc/meminfo"),
            "process_status": _read(f"/proc/{pid}/status"),
            "governors": {p.parent.parent.name: _read(p) for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor")}}


def validity(samples, expected):
    errors = []
    if not samples: return ["missing environment samples"]
    for sample in samples:
        if sample["compilers"]: errors.append("concurrent compiler detected")
        for key in ("kernel", "cpu_model", "affinity", "governors"):
            if key not in expected or sample.get(key) != expected[key]: errors.append(f"environment fingerprint missing/changed: {key}")
        if sample["cgroup"].get("error"): errors.append(sample["cgroup"]["error"])
        group = sample["cgroup"]
        ancestors = group.get("ancestors", [])
        if (not isinstance(group.get("path"), str) or not group["path"].startswith("/")
                or not ancestors or any(not isinstance(row.get(k), str) or not row[k].strip()
                    for row in ancestors for k in ("path", "cpu.max", "cpuset.cpus.effective", "memory.max"))):
            errors.append("DUT cgroup v2 ancestor limits unavailable")
    # Throttling and PSI are retained as observations, not automatically
    # discarded: they may be caused by the candidate under a fixed quota.
    groups = [s["cgroup"] for s in samples]
    if any(g.get("path") != groups[0].get("path") for g in groups): errors.append("DUT cgroup changed")
    def limits(g):
        return [{k: row.get(k) for k in ("path", "cpu.max", "cpuset.cpus.effective", "memory.max")} for row in g.get("ancestors", [])]
    if any(limits(g) != limits(groups[0]) for g in groups): errors.append("DUT or ancestor cgroup limits changed")
    if expected.get("cgroup_limits") != limits(groups[0]): errors.append("cgroup limits differ from frozen environment")
    return sorted(set(errors))
