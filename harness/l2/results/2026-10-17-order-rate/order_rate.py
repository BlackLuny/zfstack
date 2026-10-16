#!/usr/bin/env python3
"""Receiver-order rate: netem-fifo full self-tests against plain-netem controls, alternated.

Diagnostic only (PLAN.md). Runs RUNS full self-tests of the netem-fifo injector (arm F) and
RUNS plain-netem controls with the historical v3 timeline (arm P) under the same load,
strictly alternating F, P, F, P, ... Every run executes once inside qa/isolate-linux.sh under
the public QA lease and is retained; there is no early stop. The primary endpoint is whether
a run's receiver saw any overtaken UDP datagram; arms are compared with a one-sided exact
Fisher test. Nothing here grades C16.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))

RUNS = 30
ALPHA = 0.05
MIN_VALID = 25
SENT = 18475
# The injector, self-test and control must be byte-identical to the Canada attempt 02 gate.
GATE_SHA = "ca9b597621614a371826866f515f6f5f77da4103"
UNCHANGED = (":(glob)harness/l2/*.py", ":(glob)harness/l2/*.sh", "qa/isolate-linux.sh",
             "harness/l2/results/2026-10-16-netem-fifo/netem_order_control.py", ":(exclude,glob)harness/l2/test_*.py")
CONTROL = "harness/l2/results/2026-10-16-netem-fifo/netem_order_control.py"


def schedule(runs=RUNS):
    return [(index // 2 + 1, "FP"[index % 2]) for index in range(2 * runs)]


def receiver_stats(path):
    """Overtaken, unique and duplicate datagram counts from one run's raw receiver samples."""
    try:
        rows = json.loads(Path(path).read_text())["udp"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    best, overtaken, seen = -1, 0, set()
    for row in rows:
        seq = row[0]
        overtaken += seq < best
        best = max(best, seq)
        seen.add(seq)
    return {"received": len(rows), "unique": len(seen), "duplicates": len(rows) - len(seen), "overtaken": overtaken}


def fisher_greater(a_hits, a_n, b_hits, b_n):
    """One-sided exact Fisher p for arm A having a higher hit rate than arm B."""
    hits, total = a_hits + b_hits, a_n + b_n
    denominator = math.comb(total, hits)
    return sum(math.comb(a_n, k) * math.comb(b_n, hits - k) for k in range(a_hits, min(a_n, hits) + 1)) / denominator


def binomial_cdf(k, n, p):
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))


def clopper_pearson(hits, n, level=.95):
    if n == 0:
        return [0.0, 1.0]
    tail = (1 - level) / 2

    def solve(target, k):
        low, high = 0.0, 1.0
        for _ in range(60):
            middle = (low + high) / 2
            if binomial_cdf(k, n, middle) > target:
                low = middle
            else:
                high = middle
        return (low + high) / 2
    lower = 0.0 if hits == 0 else solve(1 - tail, hits - 1)
    upper = 1.0 if hits == n else solve(tail, hits)
    return [round(lower, 4), round(upper, 4)]


def valid(stats):
    return stats is not None and stats["unique"] == SENT and stats["duplicates"] == 0


def decide(rows):
    arms = {}
    for arm in "FP":
        usable = [row for row in rows if row["arm"] == arm and valid(row["receiver"])]
        hits = sum(1 for row in usable if row["receiver"]["overtaken"])
        arms[arm] = {"runs": sum(1 for row in rows if row["arm"] == arm), "valid": len(usable), "overtaking_runs": hits,
                     "overtaken_datagrams": sum(row["receiver"]["overtaken"] for row in usable),
                     "rate_ci95": clopper_pearson(hits, len(usable))}
    result = {"arms": arms, "alpha": ALPHA, "min_valid": MIN_VALID}
    if min(arms["F"]["valid"], arms["P"]["valid"]) < MIN_VALID:
        result["verdict"] = "INCONCLUSIVE"
        result["reason"] = f"fewer than {MIN_VALID} valid runs in an arm"
        return result
    p = fisher_greater(arms["F"]["overtaking_runs"], arms["F"]["valid"], arms["P"]["overtaking_runs"], arms["P"]["valid"])
    result["fisher_one_sided_p"] = round(p, 6)
    if p <= ALPHA:
        result["verdict"] = "F_HIGHER"
    elif arms["P"]["overtaking_runs"]:
        result["verdict"] = "NOT_SHOWN_SHARED"
    else:
        result["verdict"] = "NOT_SHOWN"
    return result


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def main():
    from qa.lease import Lease
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lease-dir", default="/var/lock/zfstack-qa")
    parser.add_argument("--runs", type=int, default=RUNS, help="per arm; anything but the frozen value is a smoke run")
    a = parser.parse_args()
    head = git("rev-parse", "HEAD")
    if head != a.expected_sha or git("status", "--porcelain", "--untracked-files=all"):
        raise SystemExit(f"checkout is not a clean {a.expected_sha} (HEAD {head})")
    changed = git("diff", "--name-only", GATE_SHA, head, "--", *UNCHANGED)
    if changed:
        raise SystemExit("injector, self-test or control differ from the attempt 02 gate: " + changed.replace("\n", ", "))
    if not a.output.is_absolute() or a.output.exists():
        raise SystemExit("--output must be a fresh absolute directory")
    a.output.mkdir(parents=True)
    order = schedule(a.runs)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    plan = {"written_before_execution_unix_ns": time.time_ns(), "tested_sha": head, "gate_sha": GATE_SHA,
            "kernel": os.uname().release, "boot_id": boot, "runs_per_arm": a.runs, "frozen_runs_per_arm": RUNS,
            "smoke": a.runs != RUNS, "order": order, "endpoint": "run has any receiver-overtaken UDP datagram",
            "test": f"one-sided exact Fisher F>P, alpha {ALPHA}; fewer than {MIN_VALID} valid runs per arm is INCONCLUSIVE",
            "valid_run": f"receiver has all {SENT} datagrams exactly once", "qualification": "none"}
    (a.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    rows = []
    with Lease(a.lease_dir, a.domain, a.run_id, boot) as lease:
        for number, arm in order:
            label = f"{arm}-{number:02d}"
            target = a.output / (label + ".json")
            argv = ([sys.executable, str(ROOT / "harness/l2/fifo_selftest.py"), "--kind", "netem-fifo", "--mode", "full",
                     "--output", str(target)] if arm == "F" else [sys.executable, str(ROOT / CONTROL), "--output", str(target)])
            before = {name: Path("/proc", name).read_text() for name in ("stat", "softirqs")}
            started = time.time_ns()
            with (a.output / (label + ".log")).open("xb") as log:
                try:
                    code = subprocess.run(["timeout", "--kill-after=5", "300", "bash", str(ROOT / "qa/isolate-linux.sh"), *argv],
                                          cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=315).returncode
                except subprocess.TimeoutExpired:
                    code = "timeout"
            row = {"label": label, "arm": arm, "round": number, "returncode": code, "started_unix_ns": started,
                   "finished_unix_ns": time.time_ns(), "receiver": receiver_stats(a.output / label / "receiver.json"),
                   "proc_before": before, "proc_after": {name: Path("/proc", name).read_text() for name in before}}
            rows.append(row)
            (a.output / "execution.json").write_text(json.dumps(rows, indent=2) + "\n")
            lease.check()
            print(label, "exit", code, "receiver", row["receiver"], flush=True)
    summary = decide(rows)
    summary["smoke"] = a.runs != RUNS
    (a.output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
