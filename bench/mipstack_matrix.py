#!/usr/bin/env python3
"""Same-machine proxy-client comparison of zfstack and mipstack.

Topology (zfbench --client-proxy):

    kernel TCP client -> 198.18.0.2:5201 -> TUN zfcT -> proxy -> 127.0.0.1:5201

Every proxy is its own process. Throughput is receiver goodput over the
post-warmup window. CPU is that process's utime+stime over the same window.
Memory is VmRSS / VmHWM from /proc. Bytes are pattern-checked by zfbench.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "target" / "release" / "zfbench"
MIPS = Path(os.environ.get("MIPS_BIN", "/tmp/mipstack-proxy"))
OUT = Path(os.environ.get("MIPS_OUT", ROOT / "bench" / "results" / "2026-10-26-mipstack"))

SECS = "8"
WARMUP = "2"


def stacks() -> dict[str, list[str]]:
    mips = ["--client-proxy", "mipstack", "--mipstack", str(MIPS)]
    zf = ["--client-proxy", "zfstack-client", "--splice", "--driver-thread"]
    return {
        "zf-offload": zf + ["--vnet-hdr"],
        "zf-plain": zf,
        "zf-tso": zf + ["--tso"],
        "mips": mips,
        "mips-1": mips + ["--proxy-workers", "1"],
        "mips-rx": mips + ["--mips-rx-offload"],
        "direct": ["--client-proxy", "direct"],
    }


def cases() -> list[tuple[str, str, list[str], int]]:
    """(name, stack, extra args, repetitions)."""
    bulk = [
        ("down1", ["--test", "down", "--flows", "1", "--secs", SECS, "--warmup", WARMUP]),
        ("down8", ["--test", "down", "--flows", "8", "--secs", SECS, "--warmup", WARMUP]),
        ("up1", ["--test", "up", "--flows", "1", "--secs", SECS, "--warmup", WARMUP]),
        ("up8", ["--test", "up", "--flows", "8", "--secs", SECS, "--warmup", WARMUP]),
    ]
    mixed = ["--test", "mixed", "--flows", "1", "--secs", SECS, "--warmup", WARMUP, "--rr-size", "1024", "--rr-interval-ms", "100", "--idle-rr-secs", "3"]
    connect = ["--test", "connect", "--conns", "2000", "--concurrency", "64"]
    idle = ["--test", "idle", "--conns", "2000"]
    plan: list[tuple[str, str, list[str], int]] = []
    for name, args in bulk:
        for stack in ("zf-offload", "mips"):
            plan.append((f"mtu65535-{stack}-{name}", stack, ["--tun-mtu", "65535", *args], 2))
    plan.append(("mtu65535-zf-offload-mixed", "zf-offload", ["--tun-mtu", "65535", *mixed], 2))
    plan.append(("mtu65535-mips-mixed", "mips", ["--tun-mtu", "65535", *mixed], 2))
    for stack in ("zf-offload", "mips"):
        plan.append((f"mtu65535-{stack}-connect", stack, ["--tun-mtu", "65535", *connect], 2))
        plan.append((f"mtu65535-{stack}-idle", stack, ["--tun-mtu", "65535", *idle], 2))
    for stack in ("zf-plain", "mips-1", "mips-rx"):
        for name, args in (("down1", bulk[0][1]), ("up1", bulk[2][1])):
            plan.append((f"mtu65535-{stack}-{name}", stack, ["--tun-mtu", "65535", *args], 2))
    for stack in ("zf-tso", "mips"):
        for name, args in bulk[:3]:
            plan.append((f"mtu1500-{stack}-{name}", stack, ["--tun-mtu", "1500", *args], 2))
    plan.append(("direct-down1", "direct", ["--test", "down", "--flows", "1", "--secs", SECS, "--warmup", WARMUP], 1))
    plan.append(("direct-up1", "direct", ["--test", "up", "--flows", "1", "--secs", SECS, "--warmup", WARMUP], 1))
    return plan


def run_one(name: str, args: list[str], rep: int) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{name}-r{rep}.json"
    err_path = OUT / f"{name}-r{rep}.err"
    cmd = ["sudo", "-n", str(BENCH), *args, "--label", f"{name}-r{rep}", "--json", str(dest)]
    started = time.time()
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=180)
    err_path.write_text(proc.stderr)
    record = {
        "name": name,
        "rep": rep,
        "exit": proc.returncode,
        "seconds": round(time.time() - started, 3),
        "json": str(dest),
    }
    if dest.is_file():
        record["result"] = json.loads(dest.read_text())
    else:
        record["stdout_tail"] = proc.stdout[-2000:]
        record["stderr_tail"] = proc.stderr[-2000:]
    print(f"{name} r{rep} exit={proc.returncode} {record['seconds']}s", flush=True)
    return record


def main() -> None:
    if not BENCH.is_file():
        raise SystemExit(f"missing {BENCH}")
    if not MIPS.is_file():
        raise SystemExit(f"missing {MIPS}")
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {
        "bench": str(BENCH),
        "mipstack_bin": str(MIPS),
        "mipstack_module": (ROOT / "bench" / "mipstack-proxy" / "go.mod").read_text(),
        "nproc": os.cpu_count(),
        "uname": subprocess.check_output(["uname", "-a"], text=True).strip(),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (OUT / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    known = stacks()
    runs = []
    for name, stack, args, reps in cases():
        for rep in range(1, reps + 1):
            runs.append(run_one(name, known[stack] + args, rep))
            (OUT / "runs.json").write_text(json.dumps(runs, indent=2) + "\n")
    print(f"wrote {OUT / 'runs.json'}", flush=True)


if __name__ == "__main__":
    main()
