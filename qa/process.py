"""Bounded children with explicit platform containment scope.

Linux commands run under a dedicated subreaper: only that supervisor's adopted
children can be killed/reaped. macOS provides process-group containment and
reports its detached-descendant limitation. Windows uses a kill-on-close job.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_OUTPUT = 64 * 1024 * 1024
MIN_DISK = 64 * 1024 * 1024


def _budget_error(output):
    size = output.stat().st_size
    if size > MAX_OUTPUT:
        return f"output byte budget exceeded: {size} > {MAX_OUTPUT}"
    free = shutil.disk_usage(output.parent).free
    if free < MIN_DISK:
        return f"free disk budget exhausted: {free} < {MIN_DISK}"
    return ""


def _budget(output):
    return not _budget_error(output)


def _linux_children():
    path = Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children")
    return [int(pid) for pid in path.read_text().split()]


def _linux_reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if not pid:
            return


def _supervise(argv, timeout, status_fd, output):
    """Runs in a fresh Python process, never in the orchestrator's process."""
    start = time.monotonic_ns()
    status, process, leaked, cleanup = "completed", None, False, "PASS"
    detail = ""
    cancelled = False
    def cancel(_number, _frame):
        nonlocal cancelled
        cancelled = True
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "cannot enable child subreaper")
        # Parent cancellation is held until this frame: a SIGTERM before
        # Python installed its handler would otherwise discard cleanup proof.
        os.write(status_fd, b'{"supervisor_ready":true}\n')
        if cancelled:
            status = "cancelled"
        else:
            process = subprocess.Popen(argv, close_fds=True)
        deadline = time.monotonic() + timeout
        while process is not None and process.poll() is None:
            if cancelled:
                status = "cancelled"
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                break
            if not _budget(output):
                status = "failed"
                detail = _budget_error(output)
                break
            time.sleep(0.025)
        if process is not None and process.poll() is not None:
            # poll reaps the primary child; remaining children are ours by
            # subreaper adoption even if they created a fresh session.
            # Adopted descendants that already exited (e.g. git's detached
            # auto-maintenance) are zombies, not live leaks: reap them first.
            _linux_reap()
            leaked = bool(_linux_children())
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        deadline = time.monotonic() + 5
        while True:
            _linux_reap()
            children = _linux_children()
            if not children:
                break
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if time.monotonic() >= deadline:
                cleanup = "UNKNOWN"
                break
            time.sleep(0.025)
        if status == "completed" and (process is None or process.returncode != 0 or leaked or not _budget(output)):
            status = "failed"
        detail = detail or _budget_error(output)
    except BaseException as error:
        status, cleanup = "failed", "UNKNOWN"
        detail = f"{type(error).__name__}: {error}"[:1000]
    else:
        if leaked:
            detail = "; ".join(filter(None, (detail, "detached/background descendants detected")))
    record = {"argv": argv, "status": status, "exit_code": process.returncode if process else None,
              "start_ns": start, "end_ns": time.monotonic_ns(), "cleanup_scope": "linux-subreaper",
              "cleanup_status": cleanup, "cleanup_limits": [], "lifecycle_detail": detail}
    if cleanup != "PASS":
        # Exceptional paths still clean only this dedicated supervisor's
        # children. Never leave a successfully spawned primary suspended/live.
        deadline = time.monotonic() + 5
        while True:
            if process is not None:
                try:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=.1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            try:
                _linux_reap()
                children = _linux_children()
            except OSError:
                break
            if not children:
                cleanup = "PASS"
                break
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if time.monotonic() >= deadline:
                break
            time.sleep(.025)
        record["cleanup_status"] = cleanup
        record["end_ns"] = time.monotonic_ns()
    with os.fdopen(status_fd, "w") as channel:
        channel.write(json.dumps(record) + "\n")


# The standalone supervisor imports no candidate package and has no shared
# state with the parent or other QA tasks. -I ignores interpreter env hooks.
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--supervise":
    _supervise(json.loads(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5]))
    raise SystemExit(0)

from .common import EvidenceError


def _linux_run(argv, root, output, timeout, env, lease):
    start = time.monotonic_ns()
    reader, writer = os.pipe()
    process = None
    cancellation = None
    ready, terminated = False, False
    buffer, frames = b"", []

    def receive():
        nonlocal buffer, ready
        while True:
            try:
                chunk = os.read(reader, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            buffer += chunk
            if len(buffer) > 1024 * 1024:
                raise EvidenceError("oversized child supervisor record")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    frame = json.loads(line)
                except (UnicodeError, ValueError) as error:
                    raise EvidenceError("invalid child supervisor record") from error
                if frame == {"supervisor_ready": True}:
                    ready = True
                else:
                    frames.append(frame)

    def unavailable(context):
        # Cancellation never erases the original ownership failure. Absence
        # of a final supervisor record remains honestly UNKNOWN cleanup.
        if cancellation is None:
            raise EvidenceError(context)
        return {"argv": argv, "status": "cancelled", "exit_code": process.returncode if process else None,
                "start_ns": start, "end_ns": time.monotonic_ns(), "cleanup_scope": "linux-subreaper",
                "cleanup_status": "UNKNOWN", "cleanup_limits": ["supervisor did not return a cleanup record"],
                "lifecycle_detail": cancellation + "; " + context}

    try:
        with output.open("xb") as stream:
            # Already-lost ownership needs no child process and has no cleanup
            # work. This is a real no-start record, not a fabricated reap PASS.
            try:
                if lease:
                    lease.check()
            except (EvidenceError, KeyboardInterrupt) as error:
                return {"argv": argv, "status": "cancelled", "exit_code": None,
                        "start_ns": start, "end_ns": time.monotonic_ns(), "cleanup_scope": "not-started",
                        "cleanup_status": "PASS", "cleanup_limits": [],
                        "lifecycle_detail": repr(error) + "; child supervisor not started: lease preflight rejected"}
            process = subprocess.Popen([sys.executable, "-I", str(Path(__file__).resolve()), "--supervise",
                                        json.dumps(argv), str(timeout), str(writer), str(output)], cwd=root,
                                       stdout=stream, stderr=subprocess.STDOUT, env=env,
                                       pass_fds=(writer,), start_new_session=True)
            os.close(writer)
            writer = None
            os.set_blocking(reader, False)
            deadline = time.monotonic() + timeout + 8
            while process.poll() is None:
                try:
                    receive()
                except EvidenceError as error:
                    return unavailable(str(error))
                if cancellation is None:
                    try:
                        if lease:
                            lease.check()
                    except (EvidenceError, KeyboardInterrupt) as error:
                        cancellation = repr(error)
                if cancellation is not None and ready and not terminated:
                    process.terminate()
                    terminated = True
                if time.monotonic() >= deadline:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
                    return unavailable("child supervisor failed to finish; cleanup UNKNOWN")
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            try:
                receive()
            except EvidenceError as error:
                return unavailable(str(error))
            if process.returncode or len(frames) != 1 or buffer:
                return unavailable("child supervisor failed or final record unavailable; cleanup UNKNOWN")
            result = frames[0]
            if (not isinstance(result, dict) or result.get("cleanup_status") not in ("PASS", "UNKNOWN")
                    or result.get("cleanup_scope") != "linux-subreaper"):
                return unavailable("invalid child supervisor lifecycle record; cleanup UNKNOWN")
            if cancellation is not None:
                result["status"] = "cancelled"
                context = result.get("lifecycle_detail") or "supervisor completed owned-child cleanup"
                result["lifecycle_detail"] = cancellation + "; " + context
            result["start_ns"] = start
            return result
    finally:
        if writer is not None:
            os.close(writer)
        os.close(reader)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)


class _WindowsJob:
    """Create a Job before resuming the suspended child; never a race to assign."""
    def __init__(self):
        from ctypes import wintypes as w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, w.LPCWSTR)
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
        self.kernel.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
        self.kernel.QueryInformationJobObject.argtypes = (w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p)
        self.kernel.TerminateJobObject.argtypes = (w.HANDLE, w.UINT)
        self.kernel.CloseHandle.argtypes = (w.HANDLE,)
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", w.DWORD),
                        ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("active_limit", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ro", "wo", "oo", "rb", "wb", "ob")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, w.DWORD) for name in ("page_faults", "total", "active", "terminated")]
        self.Accounting = Accounting
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise EvidenceError("cannot create Windows containment job")
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; breakaway prohibited.
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise EvidenceError("cannot configure Windows containment job")
    def assign_resume(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise EvidenceError("cannot assign suspended child to Windows containment job")
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = (ctypes.c_void_p,)
        resume.restype = ctypes.c_long
        if resume(int(process._handle)) != 0:
            raise EvidenceError("cannot resume contained Windows child")
    def active(self):
        value = self.Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(value), ctypes.sizeof(value), None):
            raise EvidenceError("cannot query Windows child containment")
        return value.active
    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise EvidenceError("cannot terminate Windows child containment")
    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _portable_run(argv, root, output, timeout, env, lease):
    start = time.monotonic_ns()
    status, cleanup, process, job = "completed", "PASS", None, None
    detail = ""
    windows = os.name == "nt"
    try:
        job = _WindowsJob() if windows else None
        with output.open("xb") as stream:
            process = subprocess.Popen(argv, cwd=root, stdout=stream, stderr=subprocess.STDOUT, env=env,
                                       start_new_session=not windows,
                                       creationflags=0x4 if windows else 0)  # CREATE_SUSPENDED
            if job:
                job.assign_resume(process)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if lease:
                    lease.check()
                if not _budget(output):
                    status = "failed"
                    detail = _budget_error(output)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    status = "timeout"
                    break
                try:
                    process.wait(timeout=min(.05, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if process.poll() is None:
                if job:
                    job.terminate()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            if job:
                if job.active():
                    if status == "completed":
                        status = "failed"
                        detail = "; ".join(filter(None, (detail, "detached/background descendants detected")))
                    job.terminate()
                    deadline = time.monotonic() + 5
                    while job.active() and time.monotonic() < deadline:
                        time.sleep(.025)
                    if job.active():
                        cleanup = "UNKNOWN"
            else:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                    if status == "completed":
                        status = "failed"
                        detail = "; ".join(filter(None, (detail, "detached/background descendants detected")))
            if status == "completed" and (process.returncode != 0 or not _budget(output)):
                status = "failed"
            detail = detail or _budget_error(output)
    except (KeyboardInterrupt, EvidenceError):
        status = "cancelled"
        if process is not None and process.poll() is None:
            if job:
                job.terminate()
                if process.poll() is None:
                    process.kill()  # Also covers failure before job assignment.
            else:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        if process is None:
            raise
    finally:
        if job:
            job.close()
    return {"argv": argv, "status": status, "exit_code": process.returncode,
            "start_ns": start, "end_ns": time.monotonic_ns(), "cleanup_status": cleanup,
            "cleanup_scope": "windows-job" if windows else "process-group",
            "cleanup_limits": [] if windows else ["detached descendants outside the original process group are not contained"],
            "lifecycle_detail": detail}


def run_child(argv, root, output, timeout, env, lease=None):
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) for x in argv):
        raise EvidenceError("invalid child argv")
    if timeout <= 0:
        raise EvidenceError("child timeout must be positive")
    root, output = Path(root).resolve(), Path(output).resolve()
    if sys.platform.startswith("linux"):
        return _linux_run(argv, root, output, timeout, env, lease)
    return _portable_run(argv, root, output, timeout, env, lease)
