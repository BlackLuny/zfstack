"""Experiment entry point. fifo_injector_impl.py remains byte-for-byte frozen.

CPU policy applies before installation. QA_DIAGNOSTIC=0 leaves the hot path
unwrapped; diagnostic timings are never substituted for the frozen oracle.
"""
import ctypes
import gzip
import json
import os
import resource
import sys
import time
from pathlib import Path

import fifo_injector_impl as I

libc = ctypes.CDLL(None)
libc.prctl(15, b"qa75-inject", 0, 0, 0)
diagnostic = os.environ.get("QA_DIAGNOSTIC") == "1"
events = []
spec = json.loads(Path(sys.argv[2]).read_text())
output = Path(spec["report"]).parent
policy = {"diagnostic": diagnostic, "pid": os.getpid(),
          "affinity_before": sorted(os.sched_getaffinity(0)),
          "scheduler_before": os.sched_getscheduler(0)}
if os.environ.get("QA_INJECTOR_CPU"):
    os.sched_setaffinity(0, {int(os.environ["QA_INJECTOR_CPU"])})
if os.environ.get("QA_INJECTOR_FIFO"):
    os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(int(os.environ["QA_INJECTOR_FIFO"])))
policy.update(affinity_after=sorted(os.sched_getaffinity(0)),
              scheduler_after=os.sched_getscheduler(0),
              priority_after=os.sched_getparam(0).sched_priority)


def stamp():
    r = resource.getrusage(resource.RUSAGE_THREAD)
    return [time.monotonic_ns(), time.thread_time_ns(), r.ru_nvcsw,
            r.ru_nivcsw, libc.sched_getcpu()]


def event(label, begin, end, extra=None):
    if end[0] - begin[0] >= 1_000_000:
        events.append({"op": label, "begin": begin, "end": end, "extra": extra})


if diagnostic:
    for name in ("emit", "admit", "read_batch"):
        old = getattr(I.Injector, name)

        def wrapped(self, *args, _old=old, _name=name, **kwargs):
            b = stamp()
            extra = {"seq_before": self.seq, "queued": len(self.fifo),
                     "head_due": self.fifo[0][0] if self.fifo else None}
            try:
                return _old(self, *args, **kwargs)
            finally:
                extra["seq_after"] = self.seq
                event(_name, b, stamp(), extra)

        setattr(I.Injector, name, wrapped)

    old_epoll = I.select.epoll

    class Poll:
        def __init__(self, *args, **kwargs):
            self.inner = old_epoll(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def poll(self, *args, **kwargs):
            b = stamp()
            try:
                return self.inner.poll(*args, **kwargs)
            finally:
                event("epoll", b, stamp(), {"timeout": args[0] if args else None})

    I.select.epoll = Poll
    old_install = I.Injector.install

    class Sender:
        def __init__(self, sender):
            self.sender = sender

        def __getattr__(self, name):
            return getattr(self.sender, name)

        def send(self, data):
            b = stamp()
            try:
                return self.sender.send(data)
            finally:
                event("send", b, stamp(), {"bytes": len(data)})

    def install(self):
        old_install(self)
        self.sender = Sender(self.sender)
        Path("/tracing/instances/qa75/trace_marker").write_text("injector-installed\n")

    I.Injector.install = install

try:
    raise SystemExit(I.main())
finally:
    (output / "cpu-policy.json").write_text(json.dumps(policy, indent=2) + "\n")
    if diagnostic:
        (output / "diagnostic-events.json.gz").write_bytes(gzip.compress(json.dumps({
            "clock_fields": ["monotonic_ns", "thread_cpu_ns", "voluntary_switches",
                             "involuntary_switches", "guest_cpu"], "events": events,
        }).encode(), mtime=0))
