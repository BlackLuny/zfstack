"""Read-only native macOS vCPU accounting; stop with SIGTERM, no live file IO."""
import ctypes as C
import gzip
import json
import signal
import sys
import time
from pathlib import Path

pid, out = int(sys.argv[1]), Path(sys.argv[2])
lib = C.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
lib.proc_pidinfo.argtypes = [C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int]


class ThreadInfo(C.Structure):
    _fields_ = [("user", C.c_uint64), ("system", C.c_uint64), ("cpu", C.c_int),
                ("policy", C.c_int), ("state", C.c_int), ("flags", C.c_int),
                ("sleep", C.c_int), ("curpri", C.c_int), ("priority", C.c_int),
                ("maxpriority", C.c_int), ("name", C.c_char * 64)]


assert C.sizeof(ThreadInfo) == 112
handles = (C.c_uint64 * 1024)()
n = lib.proc_pidinfo(pid, 6, 0, handles, C.sizeof(handles))
assert n > 0 and n < C.sizeof(handles)
threads = []
for tid in handles[:n // 8]:
    t = ThreadInfo()
    if lib.proc_pidinfo(pid, 5, tid, C.byref(t), C.sizeof(t)) != C.sizeof(t):
        continue
    name = t.name.decode()
    if ".thread.cpu-" in name:
        threads.append({"tid": tid, "vcpu": int(name.rsplit("-", 1)[1]),
                        "name": name, "policy": t.policy, "priority": t.priority,
                        "curpri": t.curpri, "maxpriority": t.maxpriority})
assert threads
stop = False


def terminate(*_):
    global stop
    stop = True


signal.signal(signal.SIGTERM, terminate)
signal.signal(signal.SIGINT, terminate)
rows = []
print(json.dumps({"ready": True, "pid": pid, "threads": threads}), flush=True)
deadline = time.monotonic() + 90
try:
    while not stop and time.monotonic() < deadline:
        b = time.monotonic_ns()
        values = []
        for thread in threads:
            t = ThreadInfo()
            got = lib.proc_pidinfo(pid, 5, thread["tid"], C.byref(t), C.sizeof(t))
            if got != C.sizeof(t) or t.name.decode() != thread["name"]:
                raise RuntimeError("VM thread identity changed")
            values.append([thread["vcpu"], t.user, t.system, t.state, t.curpri])
        rows.append([b, time.monotonic_ns(), values])
        time.sleep(.001)
finally:
    out.write_bytes(gzip.compress(json.dumps({"pid": pid, "threads": threads,
        "rows": rows, "cpu_times_unit": "nanoseconds",
        "clock": "host time.monotonic_ns", "sample_period_requested_ns": 1_000_000,
    }).encode(), mtime=0))
