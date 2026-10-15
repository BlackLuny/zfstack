#!/usr/bin/env python3
"""Hold peer-1 idle sockets while the existing four-peer L2 load runs (#37)."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import time

import load


async def open_idle(host, port, count, concurrency, timeout):
    sem = asyncio.Semaphore(concurrency)
    sockets, errors = [], []
    loop = asyncio.get_running_loop()

    async def connect():
        async with sem:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setblocking(False)
            try:
                await asyncio.wait_for(loop.sock_connect(sock, (host, port)), timeout)
            except (OSError, asyncio.TimeoutError) as exc:
                errors.append(type(exc).__name__)
                sock.close()
            else:
                sockets.append(sock)

    await asyncio.gather(*(connect() for _ in range(count)))
    return sockets, errors


def alive(sock):
    try:
        sock.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        return True  # No payload is sent; the target still waits for its header.
    except OSError:
        pass
    return False  # EOF, reset, or unexpected application bytes.


def probe(a):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, max(soft, a.idle_conns + 128)), hard))
    sockets, errors = asyncio.run(open_idle(a.host, a.port, a.idle_conns, a.concurrency, a.connect_timeout))
    result = {"requested": a.idle_conns, "established": len(sockets), "connect_errors": errors}
    Path(a.out + ".ready").write_text(json.dumps(result) + "\n")
    try:
        stop = Path(a.out + ".stop")
        while not stop.exists() and time.monotonic() < a.deadline:
            time.sleep(.1)
        result["alive_at_end"] = sum(alive(sock) for sock in sockets)
        result["controller_stopped"] = stop.exists()
        Path(a.out).write_text(json.dumps(result, indent=2) + "\n")
    finally:
        for sock in sockets:
            sock.close()
    return bool(errors) or result["alive_at_end"] != a.idle_conns or not result["controller_stopped"]


def run(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    idle_file = out / "idle.json"
    stop = Path(str(idle_file) + ".stop")
    ready = Path(str(idle_file) + ".ready")
    if any(p.exists() for p in (idle_file, stop, ready)):
        raise ValueError("idle probe requires fresh output files")
    errors = []
    deadline = time.monotonic() + a.ramp_budget
    command = ["ip", "netns", "exec", a.clients[1], sys.executable, __file__, "probe",
               "--host", a.host, "--port", str(a.port), "--idle-conns", str(a.idle_conns),
               "--concurrency", str(a.concurrency), "--connect-timeout", str(a.connect_timeout),
               "--deadline", str(deadline + a.baseline + a.loaded + a.recovery + 30),
               "--out", str(idle_file)]
    with open(out / "idle.log", "w") as log:
        child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.1)
            if not ready.exists():
                errors.append("idle ramp did not finish before deadline")
                child.kill()
                child.wait()
            # Keep the normal light requests, byte validation, resource sampling
            # and path checks, including the other peer on the saturated port.
            previous_pid = os.environ.get("L2_IDLE_GENERATOR_PID")
            os.environ["L2_IDLE_GENERATOR_PID"] = str(child.pid)
            try:
                load.run(a)
            finally:
                if previous_pid is None:
                    os.environ.pop("L2_IDLE_GENERATOR_PID", None)
                else:
                    os.environ["L2_IDLE_GENERATOR_PID"] = previous_pid
        finally:
            stop.touch()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    result_file = out / "result.json"
    result = json.loads(result_file.read_text())
    idle = json.loads(idle_file.read_text()) if idle_file.exists() else {}
    if child.returncode or idle.get("alive_at_end") != a.idle_conns:
        errors.append("idle capacity or liveness check failed")
    result["errors"].extend(errors)
    result.update(idle=idle, ok=not result["errors"])
    result_file.write_text(json.dumps(result, indent=2) + "\n")
    return not result["ok"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("probe", "run"))
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=19001)
    p.add_argument("--out", required=True)
    p.add_argument("--clients", nargs=4)
    p.add_argument("--worker-pid", type=int)
    p.add_argument("--conns", type=int, default=4)
    p.add_argument("--idle-conns", type=int, default=1500)
    p.add_argument("--concurrency", type=int, default=64)
    p.add_argument("--connect-timeout", type=float, default=4)
    p.add_argument("--ramp-budget", type=float, default=120)
    p.add_argument("--deadline", type=float, default=0)
    p.add_argument("--baseline", type=float, default=8)
    p.add_argument("--loaded", type=float, default=16)
    p.add_argument("--recovery", type=float, default=8)
    p.add_argument("--request-timeout", type=float, default=3)
    a = p.parse_args()
    if min(a.conns, a.idle_conns, a.concurrency, a.connect_timeout, a.ramp_budget, a.request_timeout) <= 0:
        p.error("counts and timeouts must be positive")
    if min(a.baseline, a.loaded, a.recovery) <= 1:
        p.error("phases must exceed 1s")
    if a.mode == "run" and (not a.clients or not a.worker_pid):
        p.error("run requires --clients and --worker-pid")
    return {"probe": probe, "run": run}[a.mode](a)


if __name__ == "__main__":
    sys.exit(main())
