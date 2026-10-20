"""Serial experiment driver: preserved logs, independent oracle, paired clocks."""
import argparse
import gzip
import json
import os
import signal
import subprocess as S
import sys
import time
from pathlib import Path

P = Path(__file__).resolve().parent
CONTAINER = "zfstack-pr75-sched"
parser = argparse.ArgumentParser()
parser.add_argument("name")
parser.add_argument("--injector-cpu", type=int)
parser.add_argument("--load-cpus")
parser.add_argument("--fifo", type=int)
parser.add_argument("--plain", action="store_true")
parser.add_argument("--mode", default="full", choices=["full", "cancel", "overflow"])
a = parser.parse_args()
out = P / a.name
out.mkdir()  # Existing results are never overwritten.


def docker(args, *, check=True):
    return S.run(["docker", "exec", CONTAINER, *args], check=check, capture_output=True)


def mapping(label):
    code = ('import json,sys,time\nprint("ready",flush=True)\n'
            'for line in sys.stdin: print(json.dumps([time.monotonic_ns(),time.time_ns()]),flush=True)')
    p = S.Popen(["docker", "exec", "-i", CONTAINER, "python3", "-u", "-c", code],
                stdin=S.PIPE, stdout=S.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    rows = []
    for _ in range(80):
        b = time.monotonic_ns()
        p.stdin.write("ping\n")
        p.stdin.flush()
        g, real = json.loads(p.stdout.readline())
        end = time.monotonic_ns()
        rows.append({"host_before": b, "host_after": end, "guest_mono": g,
                     "guest_real": real, "offset_low": b-g, "offset_high": end-g})
    p.stdin.close()
    assert p.wait() == 0
    (out / (label + ".json")).write_text(json.dumps(rows, indent=2) + "\n")


def snapshot(label):
    result = docker(["sh", "-c", "cat /sys/fs/cgroup/cpu.stat; cat /sys/fs/cgroup/cpu.max; "
                     "cat /sys/fs/cgroup/cpuset.cpus.effective; cat /sys/fs/cgroup/memory.events; "
                     "cat /proc/pressure/cpu; cat /proc/pressure/memory; cat /proc/pressure/io"])
    (out / (label + "-guest.txt")).write_bytes(result.stdout + result.stderr)
    result = S.run(["ps", "-axo", "pid,ppid,ni,%cpu,cputime,comm"], capture_output=True)
    (out / (label + "-host.txt")).write_bytes(result.stdout + result.stderr)


ps = S.check_output(["ps", "-axo", "pid,comm"], text=True).splitlines()
vm = [int(line.split()[0]) for line in ps if line.strip().endswith(
    "/com.apple.Virtualization.VirtualMachine")]
assert len(vm) == 1, vm
diagnostic = not a.plain
trace = "/tracing/instances/qa75"
if diagnostic:
    docker(["sh", "-ec", f"mkdir -p {trace}; echo 0 > {trace}/tracing_on; "
            f"echo mono > {trace}/trace_clock; echo 16384 > {trace}/buffer_size_kb; "
            f"echo 0 > {trace}/events/enable; echo nop > {trace}/current_tracer; "
            f"echo 'prev_comm == \"qa75-inject\" || next_comm == \"qa75-inject\"' > {trace}/events/sched/sched_switch/filter; "
            f"echo 'comm == \"qa75-inject\"' > {trace}/events/sched/sched_wakeup/filter; "
            f"echo 1 > {trace}/events/sched/sched_switch/enable; "
            f"echo 1 > {trace}/events/sched/sched_wakeup/enable; "
            f"echo > {trace}/trace; echo 1 > {trace}/tracing_on"])
snapshot("before")
mapping("clock-before")
observer = None
if diagnostic:
    observer = S.Popen([sys.executable, str(P / "host_probe.py"), str(vm[0]), str(out / "host-vcpu.json.gz")],
                       stdout=S.PIPE, stderr=(out / "observer.stderr").open("wb"), text=True)
    ready = json.loads(observer.stdout.readline())
    (out / "observer-ready.json").write_text(json.dumps(ready, indent=2) + "\n")
env = ["env", "QA_DIAGNOSTIC=" + str(int(diagnostic))]
if a.injector_cpu is not None:
    env.append("QA_INJECTOR_CPU=" + str(a.injector_cpu))
if a.fifo is not None:
    env.append("QA_INJECTOR_FIFO=" + str(a.fifo))
cmd = [*env, "bash", "/work/fixed/qa/isolate-linux.sh"]
if a.load_cpus:
    cmd.extend(["taskset", "-c", a.load_cpus])
cmd.extend(["python3", "/work/diagnostic/fifo_selftest.py", "--mode", a.mode,
            "--output", "/evidence/" + a.name + ".json"])
start = time.time_ns()
with (out / "run.log").open("wb") as log:
    result = S.run(["docker", "exec", CONTAINER, *cmd], stdout=log, stderr=S.STDOUT)
if observer:
    observer.send_signal(signal.SIGTERM)
    assert observer.wait(timeout=20) == 0
mapping("clock-after")
snapshot("after")
if diagnostic:
    docker(["sh", "-ec", f"echo 0 > {trace}/tracing_on"])
    raw = docker(["cat", trace + "/trace"]).stdout
    (out / "guest.ftrace.gz").write_bytes(gzip.compress(raw, mtime=0))
    stats = docker(["sh", "-ec", f"for p in {trace}/per_cpu/cpu*/stats; do echo \"$p\"; cat \"$p\"; done"])
    (out / "trace-stats.txt").write_bytes(stats.stdout)
    docker(["sh", "-ec", f"echo 0 > {trace}/events/enable; rmdir {trace}"])
# Only this run's uniquely named paths are copied, never another attempt's.
for path in [a.name, a.name + ".json"]:
    S.run(["docker", "cp", CONTAINER + ":/evidence/" + path, str(out / path)], check=True)
(out / "execution.json").write_text(json.dumps({"command": cmd, "started_unix_ns": start,
    "finished_unix_ns": time.time_ns(), "returncode": result.returncode, "vm_pid": vm[0],
    "diagnostic": diagnostic, "args": vars(a)}, indent=2) + "\n")
print(a.name, "exit", result.returncode, flush=True)
