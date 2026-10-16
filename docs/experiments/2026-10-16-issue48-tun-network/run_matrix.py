#!/usr/bin/env python3
"""Drive unmodified issue23 TUN cases for the frozen issue48 plan.

This is an experiment orchestrator, not a new QA gate. It only invokes
bench/pareto/issue23_tun_case.py / issue23_tun_load.py and records outcomes.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
REPO = pathlib.Path(os.environ.get("ZF48_REPO", str(ROOT.parents[2])))
EVID = pathlib.Path(os.environ.get("ZF48_EVIDENCE_DIR", "/tmp/zf48"))
CASE = REPO / "bench/pareto/issue23_tun_case.py"
LOADER = REPO / "bench/pareto/issue23_tun_load.py"
PLAN = json.loads((ROOT / "plan.json").read_text())

SHA = {
    "baseline": PLAN["baseline_sha"],
    "candidate": PLAN["candidate_sha"],
}
FLAGS = PLAN["build"]
CASE_CFG = PLAN["case"]
BUILD_FLAGS = {
    "rust": FLAGS["rustc"],
    "profile": FLAGS["profile"],
    "lto": FLAGS["lto"],
    "codegen_units": FLAGS["codegen_units"],
    "panic": FLAGS["panic"],
    "features": FLAGS["features"],
}


def snapshot():
    def read(path):
        p = pathlib.Path(path)
        return p.read_text() if p.exists() else ""

    return {
        "cpu_stat": read("/sys/fs/cgroup/cpu.stat"),
        "cpu_max": read("/sys/fs/cgroup/cpu.max"),
        "loadavg": read("/proc/loadavg"),
        "unix_ns": time.time_ns(),
    }


def run(group, variant, pair, position, order, extra=None, diagnostic=False):
    name = f"{group}-{pair:02}-{position}-{variant}"
    out = EVID / "runs" / name
    binary = EVID / "bin" / variant
    cmd = [
        "python3", "-B", str(CASE),
        "--binary", str(binary),
        "--loader", str(LOADER),
        "--outdir", str(out),
        "--label", group,
        "--variant", variant,
        "--source-sha", SHA[variant],
        "--pair", str(pair),
        "--pair-order", order,
        "--pair-position", str(position),
        "--build-flags", json.dumps(BUILD_FLAGS, separators=(",", ":")),
        "--proxy-cpus", ",".join(str(c) for c in CASE_CFG["proxy_cpus"]),
        "--loader-cpus", ",".join(str(c) for c in CASE_CFG["loader_cpus"]),
        "--mtu", str(CASE_CFG["mtu"]),
        "--idle-secs", str(CASE_CFG["idle_secs"]),
        "--flows", str(CASE_CFG["flows"]),
        "--rr-samples", str(CASE_CFG["rr_samples"]),
        "--rr-sizes", CASE_CFG["rr_sizes"],
        "--bulk-bytes-per-flow", str(CASE_CFG["bulk_bytes_per_flow"]),
        "--mixed-rr-interval-ms", str(CASE_CFG["mixed_rr_interval_ms"]),
        "--direction", CASE_CFG["direction"],
        "--low-secs", str(CASE_CFG["low_secs"]),
        "--settle-secs", str(CASE_CFG["settle_secs"]),
        "--case-timeout-secs", str(CASE_CFG["case_timeout_secs"]),
    ]
    if diagnostic:
        cmd.append("--diagnostic")
    cmd.extend(extra or [])
    before = snapshot()
    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CASE_CFG["case_timeout_secs"] + 20)
    result_path = out / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    record = {
        "name": name,
        "group": group,
        "variant": variant,
        "pair": pair,
        "position": position,
        "order": order,
        "command": cmd,
        "exit_code": proc.returncode,
        "elapsed": time.monotonic() - start,
        "before": before,
        "after": snapshot(),
        "result": result,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "diagnostic": diagnostic,
    }
    rec_dir = EVID / "records"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
    ok = bool(result and result.get("ok"))
    print(name, "PASS" if ok else "FAIL", round(record["elapsed"], 2),
          "exit", proc.returncode, flush=True)
    if proc.stderr:
        tail = "\n".join(proc.stderr.splitlines()[-20:])
        print(tail, flush=True)
    return record


def require(record):
    if record["exit_code"] or not record["result"] or not record["result"].get("ok"):
        raise SystemExit(record["name"] + ": " + str((record.get("result") or {}).get("errors") or record["stderr"][-2000:]))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "help"
    (EVID / "runs").mkdir(parents=True, exist_ok=True)
    (EVID / "records").mkdir(parents=True, exist_ok=True)
    if os.geteuid() != 0:
        raise SystemExit("run as root so issue23_tun_case.py can create netns/TUN")
    if mode == "smoke":
        extra = ["--rr-samples", "100", "--bulk-bytes-per-flow", "1048576"]
        require(run("smoke", "candidate", 0, 0, "AB", extra, diagnostic=True))
    elif mode == "aa":
        orders = PLAN["protocol"]["aa_orders"]
        for pair, order in enumerate(orders):
            for position in range(2):
                require(run("aa", "baseline", pair, position, order))
    elif mode == "ab":
        n = PLAN["protocol"]["ab_pairs"]
        for pair in range(n):
            order = "AB" if pair % 2 == 0 else "BA"
            variants = ["baseline", "candidate"] if order == "AB" else ["candidate", "baseline"]
            for position, variant in enumerate(variants):
                require(run("ab", variant, pair, position, order))
    else:
        raise SystemExit("usage: run_matrix.py smoke|aa|ab")


if __name__ == "__main__":
    main()
