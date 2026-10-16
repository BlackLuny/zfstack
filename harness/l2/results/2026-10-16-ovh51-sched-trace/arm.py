#!/usr/bin/env python3
"""One diagnostic baseline arm of C16 v3 on the OVH host, optionally traced or CPU-confined.

Runs exactly the formal baseline command and environment (qa.run.commands and
the runner's environment) against the frozen worker, under the public QA lease
and the runner's subreaper. Optional: a private tracefs instance recording
scheduler, IRQ, softirq and hrtimer events with the monotonic clock, and/or a
`taskset` CPU list applied to the whole arm. Records host IRQ/topology state
before and after and maps the arm's own processes (never others) to host PIDs.
This is not a qualification run; C16 stays NOT_RUN and known-BLOCKED.
"""
import argparse
import contextlib
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))

CASE_ID = "C16-l2-queue-control"
FORMAL_SHA = "604a61834db76a1b94583932585fb24a0265964b"
WORKER_SHA256 = "d921130672b975fb48f646e7ff159301ea5089d51a8fdd06b16cd0a08477a551"
# Files the baseline arm executes or imports; they must be byte-identical to the formal attempt.
UNCHANGED = (":(glob)harness/l2/*.py", ":(glob)harness/l2/*.sh", ":(glob)qa/*.py", ":(glob)qa/*.sh",
             "qa/policy/cases.json", ":(exclude,glob)harness/l2/test_*.py")
TRACEFS = Path("/sys/kernel/tracing")
INSTANCE = "qa44sched"
EVENTS = ("sched/sched_switch", "sched/sched_wakeup", "sched/sched_wakeup_new", "sched/sched_waking",
          "sched/sched_migrate_task", "sched/sched_process_fork", "sched/sched_process_exit",
          "irq/irq_handler_entry", "irq/irq_handler_exit", "irq/softirq_entry", "irq/softirq_exit",
          "timer/hrtimer_start", "timer/hrtimer_expire_entry", "timer/hrtimer_expire_exit")
BUFFER_KB_PER_CPU = 262144
MIN_AVAILABLE_BYTES = 8 << 30
SAMPLE_SEC = .25


def read(path):
    try:
        return Path(path).read_text()
    except OSError as error:
        return f"<unreadable: {error.strerror}>"


def host_snapshot():
    irqs = {}
    for directory in sorted(Path("/proc/irq").iterdir(), key=lambda p: (len(p.name), p.name)):
        if directory.name.isdigit():
            irqs[directory.name] = {name: read(directory / name).strip()
                                    for name in ("smp_affinity_list", "effective_affinity_list")}
    cpus = {}
    for cpu in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*"), key=lambda p: int(p.name[3:])):
        cpus[cpu.name] = {"siblings": read(cpu / "topology/thread_siblings_list").strip(),
                          "core_id": read(cpu / "topology/core_id").strip(),
                          "governor": read(cpu / "cpufreq/scaling_governor").strip()}
    irqbalance = subprocess.run(["systemctl", "is-active", "irqbalance"], capture_output=True, text=True)
    return {"monotonic_ns": time.monotonic_ns(), "unix_ns": time.time_ns(),
            "files": {name: read(name) for name in ("/proc/interrupts", "/proc/softirqs", "/proc/stat",
                                                    "/proc/net/softnet_stat", "/proc/loadavg", "/proc/meminfo")},
            "irq_affinity": irqs, "cpus": cpus, "irqbalance": irqbalance.stdout.strip() or irqbalance.stderr.strip(),
            "compilers": [name for name in ("cargo", "rustc")
                          if subprocess.run(["pgrep", "-x", name], stdout=subprocess.DEVNULL).returncode == 0]}


class ProcessMap(threading.Thread):
    """Host PID, namespace PID and command line of this driver's descendants only."""

    def __init__(self):
        super().__init__(daemon=True)
        self.stop, self.seen, self.root = threading.Event(), {}, os.getpid()

    def scan(self):
        parents, status = {}, {}
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                fields = dict(line.split(":\t", 1) for line in Path(entry.path, "status").read_text().splitlines() if ":\t" in line)
            except (OSError, ValueError):
                continue
            pid = int(entry.name)
            parents[pid], status[pid] = int(fields.get("PPid", "0")), fields
        now = time.monotonic_ns()
        for pid in status:
            ancestor, depth = parents.get(pid), 0
            while ancestor and ancestor != self.root and depth < 64:
                ancestor, depth = parents.get(ancestor), depth + 1
            if ancestor != self.root:
                continue
            try:
                start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            except (OSError, IndexError):
                continue
            key = f"{pid}:{start}"
            row = self.seen.get(key)
            if row is None:
                try:
                    cmdline = [part.decode(errors="replace") for part in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if part]
                except OSError:
                    cmdline = []
                row = self.seen[key] = {"pid": pid, "ppid": parents[pid], "nspid": status[pid].get("NSpid", "").split(),
                                        "comm": status[pid].get("Name", "").strip(), "cmdline": cmdline,
                                        "first_seen_ns": now}
            row["last_seen_ns"] = now

    def run(self):
        while not self.stop.is_set():
            self.scan()
            self.stop.wait(SAMPLE_SEC)


class Tracer:
    """A private tracefs instance; created and removed by this run, never shared state."""

    def __init__(self, output):
        self.output, self.path, self.created = output, TRACEFS / "instances" / INSTANCE, False

    def write(self, name, value):
        (self.path / name).write_text(str(value) + "\n")

    def start(self):
        if not (TRACEFS / "instances").is_dir():
            raise SystemExit("tracefs is not mounted at /sys/kernel/tracing; refusing to mount it")
        if self.path.exists():
            raise SystemExit(f"tracefs instance {INSTANCE} already exists; refusing to touch it")
        self.path.mkdir()
        self.created = True
        self.write("trace_clock", "mono")
        self.write("buffer_size_kb", BUFFER_KB_PER_CPU)
        self.write("options/overwrite", 0)
        for event in EVENTS:
            self.write(f"events/{event}/enable", 1)
        self.write("tracing_on", 1)

    def stop(self):
        self.write("tracing_on", 0)
        stats = {cpu.name: read(cpu / "stats") for cpu in sorted(self.path.glob("per_cpu/cpu*"), key=lambda p: int(p.name[3:]))}
        (self.output / "trace-stats.json").write_text(json.dumps(stats, indent=2) + "\n")
        with (self.path / "trace").open("rb") as source, gzip.open(self.output / "trace.txt.gz", "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target, 1 << 20)
        return stats

    def remove(self):
        if self.created and self.path.exists():
            try:
                self.write("tracing_on", 0)
            finally:
                self.path.rmdir()


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    from qa.integration import l2_environment
    from qa.lease import Lease
    from qa.process import run_child
    from qa.run import commands
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lease-dir", default="/var/lock/zfstack-qa")
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--cpus", help="taskset CPU list applied to the whole arm")
    parser.add_argument("--parent-lease", action="store_true",
                        help="the calling sequence already holds the public lease for --domain/--run-id")
    a = parser.parse_args()
    head = git("rev-parse", "HEAD")
    if head != a.expected_sha or git("status", "--porcelain", "--untracked-files=all"):
        raise SystemExit(f"checkout is not a clean {a.expected_sha} (HEAD {head})")
    changed = git("diff", "--name-only", FORMAL_SHA, head, "--", *UNCHANGED)
    if changed:
        raise SystemExit("baseline arm code differs from the formal attempt: " + changed.replace("\n", ", "))
    task = a.task_dir.resolve()
    if not Path(sys.executable).resolve().is_relative_to(task):
        raise SystemExit("run with the task's QA venv interpreter, as the formal runner did")
    node = json.loads((task / "node.json").read_text())
    if sha256(node["worker_binary"]) != WORKER_SHA256:
        raise SystemExit("worker binary differs from the frozen formal build")
    if not a.output.is_absolute() or a.output.exists():
        raise SystemExit("--output must be a fresh absolute directory")
    available = int(next(line.split()[1] for line in read("/proc/meminfo").splitlines() if line.startswith("MemAvailable:"))) * 1024
    if a.trace and available < MIN_AVAILABLE_BYTES:
        raise SystemExit("not enough free memory for the trace buffers")
    case = next(c for c in json.loads((ROOT / "qa/policy/cases.json").read_text())["cases"] if c["case_id"] == CASE_ID)
    if case["case_version"] != 3 or case["config"]["queue_control"]["kind"] != "netem-fifo":
        raise SystemExit("C16 is not the registered case v3")
    a.output.mkdir(parents=True)
    argv = commands(case, case["toolchain"], ROOT, a.output, node, ROOT / "qa/policy")[0]
    if a.cpus:
        argv = ["taskset", "-c", a.cpus, *argv]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(l2_environment(case["config"]))
    env["RIG_BUILD"] = str(Path(node["rig_build"]).resolve())
    env["PYTHON"] = sys.executable
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    boot = read("/proc/sys/kernel/random/boot_id").strip()
    plan = {"written_before_execution_unix_ns": time.time_ns(), "label": a.label, "tested_sha": head,
            "formal_sha": FORMAL_SHA, "worker_sha256": WORKER_SHA256, "argv": argv, "trace": a.trace,
            "trace_events": list(EVENTS) if a.trace else [], "buffer_kb_per_cpu": BUFFER_KB_PER_CPU if a.trace else 0,
            "cpus": a.cpus, "env_l2": {k: v for k, v in env.items() if k.startswith("L2_") or k in ("RIG_BUILD", "PYTHON")},
            "kernel": os.uname().release, "boot_id": boot, "qualification": "none; diagnostic arm only"}
    (a.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    tracer, sampler, record, stats = Tracer(a.output) if a.trace else None, ProcessMap(), None, None
    lease = Lease(a.lease_dir, a.domain, a.run_id, boot)
    if a.parent_lease:
        held = json.loads(lease.path.read_text()) if lease.path.is_file() else {}
        if held.get("run_id") != a.run_id or held.get("generation") != boot:
            raise SystemExit("--parent-lease given but the public lease is not held by this run id")
        lease = contextlib.nullcontext()
    with lease as owned:
        before = host_snapshot()
        (a.output / "host-before.json").write_text(json.dumps(before, indent=2) + "\n")
        if before["compilers"]:
            raise SystemExit("competing compiler is running; not starting the arm")
        try:
            sampler.start()
            if tracer:
                tracer.start()
            record = run_child(argv, ROOT, a.output / "command.log", case["timeout_seconds"], env, owned)
        finally:
            try:
                if tracer and tracer.created:
                    stats = tracer.stop()
            finally:
                if tracer:
                    tracer.remove()
                sampler.stop.set()
                sampler.join()
        (a.output / "processes.json").write_text(json.dumps(sorted(sampler.seen.values(), key=lambda r: r["first_seen_ns"]), indent=2) + "\n")
        (a.output / "host-after.json").write_text(json.dumps(host_snapshot(), indent=2) + "\n")
        if owned is not None:
            owned.check()
    (a.output / "execution.json").write_text(json.dumps({"record": record, "trace_stats": stats,
                                                         "instance_removed": not (TRACEFS / "instances" / INSTANCE).exists()},
                                                        indent=2) + "\n")
    print(a.label, "arm", record["status"], "exit", record["exit_code"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
