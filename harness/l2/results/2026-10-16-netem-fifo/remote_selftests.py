#!/usr/bin/env python3
"""Real-host gate for netem-fifo: capability probe, then one run of each self-test mode.

Run as root from a clean checkout of the frozen SHA. It writes its plan before
executing, holds the public QA lease for the whole sequence, runs every
privileged step inside qa/isolate-linux.sh, never repeats a run, and records
the gate verdict: PASS only if the probe and all three modes pass and the
oracle verdicts recompute identically. If any mode fails receiver UDP order,
it also runs exactly three plain-netem controls.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness/l2"))
from qa.lease import Lease  # noqa: E402
import netem_fifo_ledger as ledger  # noqa: E402
from analyze_order import analyze  # noqa: E402

MODES = ("full", "cancel", "overflow")
CONTROLS = 3


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lease-dir", default="/var/lock/zfstack-qa")
    a = parser.parse_args()
    head = git("rev-parse", "HEAD")
    if head != a.expected_sha or git("status", "--porcelain", "--untracked-files=all"):
        raise SystemExit(f"checkout is not a clean {a.expected_sha} (HEAD {head})")
    if not a.output.is_absolute() or a.output.exists():
        raise SystemExit("--output must be a fresh absolute directory")
    a.output.mkdir(parents=True)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    plan = {"written_before_execution_unix_ns": time.time_ns(), "tested_sha": head, "kernel": os.uname().release,
            "boot_id": boot, "domain": a.domain, "run_id": a.run_id, "order": ["capabilities", *MODES],
            "runs_each": 1, "kind": ledger.KIND, "lateness_limit_ns": ledger.LATENESS_LIMIT_NS,
            "conditional": f"only if any mode fails udp_exactly_once_in_order: exactly {CONTROLS} plain-netem controls; order analysis is always recomputed",
            "gate": "PASS only if capabilities and all three modes pass and recomputed oracle verdicts match",
            "policy": "no run is repeated; every outcome is retained; no threshold, case or workload change"}
    (a.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    records = []

    def isolated(label, argv, timeout):
        started = time.time_ns()
        with (a.output / (label + ".log")).open("xb") as log:
            try:
                code = subprocess.run(["timeout", "--kill-after=5", str(timeout), "bash", str(ROOT / "qa/isolate-linux.sh"), *argv],
                                      cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout + 15).returncode
            except subprocess.TimeoutExpired:
                code = "timeout"
        records.append({"label": label, "argv": argv, "returncode": code, "started_unix_ns": started,
                        "finished_unix_ns": time.time_ns(), "proc_modules": Path("/proc/modules").read_text()})
        (a.output / "execution.json").write_text(json.dumps(records, indent=2) + "\n")
        print(label, "exit", code, flush=True)
        return code

    lease = Lease(a.lease_dir, a.domain, a.run_id, boot)
    verdict = {"gate": "FAIL"}
    with lease:
        probe = isolated("capabilities", [sys.executable, str(HERE / "netem_capabilities.py"), str(a.output / "capabilities.json")], 120)
        lease.check()
        if probe == 0:
            for mode in MODES:
                isolated(mode, [sys.executable, str(ROOT / "harness/l2/fifo_selftest.py"), "--kind", ledger.KIND,
                                "--mode", mode, "--output", str(a.output / (mode + ".json"))], 300)
                lease.check()
            summaries = [a.output / (mode + ".json") for mode in MODES]
            if any(path.is_file() and json.loads(path.read_text())["checks"].get("udp_exactly_once_in_order") is False
                   for path in summaries):
                for index in range(1, CONTROLS + 1):
                    isolated(f"control-{index}", [sys.executable, str(HERE / "netem_order_control.py"),
                                                  "--output", str(a.output / f"control-{index}.json")], 300)
                    lease.check()
        verdict["lease"] = {**lease.value, "owned_until_release": lease.owned}
    recomputed = {}
    for mode in MODES:
        summary, directory = a.output / (mode + ".json"), a.output / mode
        if not summary.is_file() or not (directory / "queue-control.json").is_file():
            recomputed[mode] = {"missing": True}
            continue
        report = json.loads((directory / "queue-control.json").read_text())
        blob = (directory / "queue-ledger.bin.gz").read_bytes() if (directory / "queue-ledger.bin.gz").is_file() else None
        value = json.loads(summary.read_text())
        errors = ledger.validate(report, report.get("config"), blob)
        recomputed[mode] = {"selftest_ok": value["ok"], "failed_checks": sorted(k for k, v in value["checks"].items() if not v),
                            "oracle_errors": errors, "oracle_matches_selftest": errors == value["oracle_errors"]}
        try:
            recomputed[mode]["order"] = analyze(directory, None)
        except SystemExit as error:
            recomputed[mode]["order"] = str(error)
    verdict["recomputed"] = recomputed
    passed = (probe == 0 and all(r.get("selftest_ok") and r.get("oracle_matches_selftest") for r in recomputed.values())
              and all(r["returncode"] == 0 for r in records if r["label"] in MODES))
    verdict["gate"] = "PASS" if passed else "FAIL"
    (a.output / "gate.json").write_text(json.dumps(verdict, indent=2) + "\n")
    print("gate", verdict["gate"], flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
