#!/usr/bin/env python3
"""Physical-core isolation control: untraced baseline arms, interleaved, decided by a frozen test.

Arms (PLAN.md):
  A  default, no affinity (as the formal attempt);
  B  the whole arm confined to physical cores 1-3 with both SMT siblings (CPUs 1-3,5-7);
  C  as B, and every eno1 IRQ moved to physical core 0 (CPUs 0,4) for the run only. C runs
     only with --allow-irq-affinity and an inactive irqbalance; it restores and verifies the
     original affinities after each run, and any restore failure stops the sequence.
Each round runs every arm once, in an order rotated by round. Every run is retained; none is
repeated or replaced. The public lease is held for the whole sequence.
"""
import argparse
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))
from qa.lease import Lease  # noqa: E402

ROUNDS = 5
ARMS = ("A", "B", "C")
CPUS = {"A": None, "B": "1-3,5-7", "C": "1-3,5-7"}
IRQ_CORE0 = "0,4"
ALPHA = 0.05
MISS_MS = 20.0


def schedule(arms, rounds=ROUNDS):
    return [(index + 1, arms[(position + index) % len(arms)])
            for index in range(rounds) for position in range(len(arms))]


def nic_irqs(text):
    """IRQ numbers whose /proc/interrupts action names an eno1 queue or vector."""
    found = []
    for line in text.splitlines()[1:]:
        head, _, rest = line.partition(":")
        if head.strip().isdigit() and any(word.startswith("eno1") for word in rest.split()):
            found.append(head.strip())
    return found


def affinity(irq):
    base = Path("/proc/irq", irq)
    return {name: (base / name).read_text().strip() for name in ("smp_affinity_list", "effective_affinity_list")}


def cpu_set(text):
    result = set()
    for part in text.split(","):
        low, _, high = part.partition("-")
        result.update(range(int(low), int(high or low) + 1))
    return result


def restore_affinities(original):
    # Restore every vector even if one write fails. A broad configured mask alone
    # does not restore the previous effective CPU (IRQ 128 is 0-7, effective 6).
    errors = []
    for irq, value in original.items():
        try:
            path = Path("/proc/irq", irq, "smp_affinity_list")
            path.write_text(value["effective_affinity_list"] + "\n")
            path.write_text(value["smp_affinity_list"] + "\n")
        except OSError as error:
            errors.append(f"IRQ {irq}: {error}")
    restored = {irq: affinity(irq) for irq in original}
    for irq, value in original.items():
        if restored[irq] != value:
            errors.append(f"IRQ {irq}: configured/effective affinity differs from original")
    return restored, errors


def run_metric(directory):
    """Per-run maximum UDP generator lag and miss count from the arm's own samples."""
    lags = []
    for path in sorted(Path(directory).rglob("udp-*.json")):
        if path.stem.split("-")[1].isdigit():
            lags += [row["generator_lag_ms"] for row in json.loads(path.read_text())["samples"]
                     if row.get("generator_lag_ms") is not None]
    if not lags:
        return None
    lags.sort()
    return {"n": len(lags), "max_ms": round(lags[-1], 3), "p999_ms": round(lags[min(len(lags) - 1, int(.999 * len(lags)))], 3),
            "over_5ms": sum(v >= 5 for v in lags), "misses": sum(v > MISS_MS for v in lags)}


def exact_greater(x, y):
    """One-sided exact Mann-Whitney p-value for x tending to exceed y (ties count one half)."""
    def u(first, second):
        return sum(1.0 if a > b else .5 if a == b else 0.0 for a in first for b in second)
    observed, pooled, count, extreme = u(x, y), list(x) + list(y), 0, 0
    for chosen in itertools.combinations(range(len(pooled)), len(x)):
        picked = set(chosen)
        first = [pooled[i] for i in picked]
        second = [pooled[i] for i in range(len(pooled)) if i not in picked]
        count += 1
        extreme += u(first, second) >= observed - 1e-9
    return extreme / count


def decide(metrics, arms):
    values = {arm: [m["max_ms"] for m in metrics.get(arm, []) if m] for arm in arms}
    result = {"per_run_max_ms": values, "runs_with_miss": {arm: sum(1 for m in metrics.get(arm, []) if m and m["misses"])
                                                           for arm in arms}}
    for high, low in (("A", "B"), ("B", "C")):
        if high not in arms or low not in arms:
            continue
        key = f"{high}>{low}"
        if min(len(values[high]), len(values[low])) < ROUNDS - 1:
            result[key] = {"verdict": "INCONCLUSIVE", "reason": "fewer than four valid runs in an arm"}
            continue
        p = exact_greater(values[high], values[low])
        result[key] = {"p": round(p, 4), "verdict": "LOWER" if p <= ALPHA else "NOT SHOWN"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lease-dir", default="/var/lock/zfstack-qa")
    parser.add_argument("--allow-irq-affinity", action="store_true")
    a = parser.parse_args()
    if not a.output.is_absolute() or a.output.exists():
        raise SystemExit("--output must be a fresh absolute directory")
    irqbalance = subprocess.run(["systemctl", "is-active", "irqbalance"], capture_output=True, text=True).stdout.strip()
    arms = ARMS if a.allow_irq_affinity and irqbalance != "active" else ARMS[:2]
    irqs = nic_irqs(Path("/proc/interrupts").read_text())
    original = {irq: affinity(irq) for irq in irqs}
    order = schedule(arms)
    a.output.mkdir(parents=True)
    plan = {"written_before_execution_unix_ns": time.time_ns(), "arms": arms, "cpus": CPUS, "order": order,
            "irq_core0": IRQ_CORE0, "nic_irqs": original, "irqbalance": irqbalance,
            "arm_c": "run" if "C" in arms else "NOT_RUN: needs --allow-irq-affinity and inactive irqbalance",
            "metric": "per-run maximum UDP generator lag over all four flows and all phases of the baseline arm",
            "test": f"one-sided exact Mann-Whitney, A>B and B>C, alpha {ALPHA}; fewer than four valid runs is INCONCLUSIVE",
            "qualification": "none; C16 stays NOT_RUN and known-BLOCKED"}
    (a.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    records, metrics = [], {arm: [] for arm in arms}
    with Lease(a.lease_dir, a.domain, a.run_id, boot) as lease:
        for round_number, arm in order:
            label = f"r{round_number}-{arm}"
            record = {"label": label, "arm": arm, "started_unix_ns": time.time_ns()}
            moved = False
            try:
                if arm == "C":
                    moved = True
                    for irq in irqs:
                        Path("/proc/irq", irq, "smp_affinity_list").write_text(IRQ_CORE0 + "\n")
                    record["irq_during"] = {irq: affinity(irq) for irq in irqs}
                    if not irqs or any(not cpu_set(v["effective_affinity_list"])
                                       or not cpu_set(v["effective_affinity_list"]) <= {0, 4}
                                       for v in record["irq_during"].values()):
                        raise RuntimeError("eno1 IRQ effective affinity is not confined to core 0")
                argv = [sys.executable, str(HERE / "arm.py"), "--expected-sha", a.expected_sha, "--task-dir", str(a.task_dir),
                        "--output", str(a.output / label), "--label", label, "--domain", a.domain, "--run-id", a.run_id,
                        "--lease-dir", a.lease_dir, "--parent-lease"]
                if CPUS[arm]:
                    argv += ["--cpus", CPUS[arm]]
                with (a.output / (label + ".log")).open("xb") as log:
                    record["returncode"] = subprocess.run(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1")).returncode
            finally:
                if moved:
                    record["irq_restored"], record["restore_errors"] = restore_affinities(original)
                records.append(record)
                (a.output / "execution.json").write_text(json.dumps(records, indent=2) + "\n")
            if moved and record["restore_errors"]:
                raise SystemExit("IRQ affinity was not restored; sequence stopped")
            record["metric"] = run_metric(a.output / label)
            metrics[arm].append(record["metric"])
            (a.output / "execution.json").write_text(json.dumps(records, indent=2) + "\n")
            lease.check()
            print(label, "returncode", record["returncode"], "metric", record["metric"], flush=True)
    summary = decide(metrics, arms)
    (a.output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
