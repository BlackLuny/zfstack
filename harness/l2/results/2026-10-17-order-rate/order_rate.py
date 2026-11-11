#!/usr/bin/env python3
"""Pre-registered paired receiver-order diagnostic; never a C16 qualification.

See PLAN.md for frozen allocation, validity, missing-result sensitivity and stop rules.
The measured injector/self-test/control remain byte-identical to Canada attempt 02.
"""
import argparse
import json
import hashlib
import math
import os
import signal
import subprocess
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT))

RUNS = 30
ALPHA = 0.05
MIN_VALID = 25
PLAN_VERSION = 2
SEED = "28f8082271b2eafc0070c6e013ac939976b571c094ddf3b0aa28f882c1a6bbc6"
SENT = 18475
# The injector, self-test and control must be byte-identical to the Canada attempt 02 gate.
GATE_SHA = "ca9b597621614a371826866f515f6f5f77da4103"
UNCHANGED = (":(glob)harness/l2/*.py", ":(glob)harness/l2/*.sh", "qa/isolate-linux.sh", "qa/process.py", "qa/lease.py",
             "harness/l2/results/2026-10-16-netem-fifo/netem_order_control.py", ":(exclude,glob)harness/l2/test_*.py")
CONTROL = "harness/l2/results/2026-10-16-netem-fifo/netem_order_control.py"


def schedule(runs=RUNS):
    # Independent frozen coin per pair. No forced 15/15 balance: the exact paired
    # sign test below uses independent 1/2 assignment probabilities.
    return [(number, arm) for number in range(1, runs + 1)
            for arm in ("FP" if hashlib.sha256(bytes.fromhex(SEED) + number.to_bytes(4, "big")).digest()[0] & 1 else "PF")]


def read_json(path):
    return json.loads(Path(path).read_text())


def receiver_stats(path):
    """Reject malformed samples; a count alone does not prove the exact sequence set."""
    try:
        rows = read_json(path)["udp"]
        if not isinstance(rows, list):
            return None
        for row in rows:
            if (not isinstance(row, list) or len(row) != 4
                    or any(type(v) is not int for v in row[:3])
                    or not 0 <= row[0] < SENT or not 0 < row[1] <= row[2]
                    or type(row[3]) is not bool):
                return None
        if any(a[2] > b[2] for a, b in zip(rows, rows[1:])):
            return None
        best, overtaken, seen = -1, 0, set()
        for seq, _, _, _ in rows:
            overtaken += seq < best
            best = max(best, seq)
            seen.add(seq)
        return {"received": len(rows), "unique": len(seen), "duplicates": len(rows) - len(seen),
                "overtaken": overtaken, "exact_sequences": seen == set(range(SENT)),
                "payload_intact": all(row[3] for row in rows)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


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
    return (stats is not None and stats.get("exact_sequences") is True
            and stats.get("payload_intact") is True and stats["received"] == SENT
            and stats["unique"] == SENT and stats["duplicates"] == 0)


def paired_p(f_only, p_only):
    """One-sided exact McNemar/sign randomization test for independently randomized pairs."""
    n = f_only + p_only
    return sum(math.comb(n, k) for k in range(f_only, n + 1)) / 2 ** n


def decide(rows, runs=RUNS, aborted=None):
    expected = schedule(runs)
    structural_error = [(r["round"], r["arm"]) for r in rows] != expected
    by_pair = {(r["round"], r["arm"]): r for r in rows}

    def outcome(row):
        if row and row.get("validity", {}).get("valid") is True and valid(row.get("receiver")):
            return bool(row["receiver"]["overtaken"])
        return None

    arms = {}
    for arm in "FP":
        values = [outcome(by_pair.get((i, arm))) for i in range(1, runs + 1)]
        n = sum(v is not None for v in values)
        hits = sum(v is True for v in values)
        arms[arm] = {"planned": runs, "valid": n, "unknown": runs - n, "overtaking_runs": hits,
                     "observed_rate_ci95": clopper_pearson(hits, n),
                     "all_runs_rate_ci95_envelope": [clopper_pearson(hits, runs)[0],
                                                     clopper_pearson(hits + runs - n, runs)[1]]}
    observed = [0, 0]
    worst = [0, 0]
    complete = 0
    for i in range(1, runs + 1):
        f, p = (outcome(by_pair.get((i, arm))) for arm in "FP")
        if f is not None and p is not None:
            complete += 1
            observed[0] += f and not p
            observed[1] += p and not f
        # The least favorable completion for F>P: unknown F=0, unknown P=1.
        f, p = (False if f is None else f), (True if p is None else p)
        worst[0] += f and not p
        worst[1] += p and not f
    p_observed, p_worst = paired_p(*observed), paired_p(*worst)
    result = {"plan_version": PLAN_VERSION, "arms": arms, "alpha": ALPHA, "min_valid": MIN_VALID,
              "complete_pairs": complete, "observed_discordant_F_P": observed,
              "worst_case_discordant_F_P": worst, "paired_one_sided_p": p_observed,
              "worst_case_p": p_worst, "secondary_fisher_one_sided_p": fisher_greater(
                  arms["F"]["overtaking_runs"], arms["F"]["valid"],
                  arms["P"]["overtaking_runs"], arms["P"]["valid"])}
    if aborted or structural_error or runs != RUNS or min(a["valid"] for a in arms.values()) < MIN_VALID:
        result.update(verdict="INCONCLUSIVE", reason=aborted or
                      ("incomplete, duplicated or out-of-order schedule" if structural_error else
                       "smoke run" if runs != RUNS else "fewer than 25 valid runs in an arm"))
    elif p_worst <= ALPHA:
        result["verdict"] = "F_HIGHER"
    elif any(a["unknown"] for a in arms.values()):
        result.update(verdict="INCONCLUSIVE", reason="F superiority does not survive missing-result sensitivity")
    else:
        result["verdict"] = "NOT_SHOWN_P_OBSERVED" if arms["P"]["overtaking_runs"] else "NOT_SHOWN"
    return result


def assess(target, arm, lifecycle):
    """Recompute validity from raw evidence, never accept the summary's check booleans.

    An order-only F failure remains valid. Any other failure is an unknown endpoint,
    retained in the least-favorable completion; it is not a replacement opportunity.
    """
    directory = target.with_suffix("")
    stats = receiver_stats(directory / "receiver.json")
    reasons, checks = [], {}
    cleanup_safe = (lifecycle.get("cleanup_status") == "PASS"
                    and lifecycle.get("cleanup_scope") == "linux-subreaper")
    if not cleanup_safe:
        reasons.append("owned-process cleanup not proven")
    if not (lifecycle.get("status") == "completed" or
            (arm == "F" and lifecycle.get("status") == "failed" and lifecycle.get("exit_code") == 1
             and not lifecycle.get("lifecycle_detail"))):
        reasons.append("child did not complete normally: " + str(lifecycle.get("status")))
    if not valid(stats):
        reasons.append("receiver must contain exactly 0..18474 once each, intact, with valid timestamps")
    try:
        sent = read_json(directory / "sender.json")
        received = read_json(directory / "receiver.json")
        summary = read_json(target)
        checks.update(sender_count=type(sent["sent"]) is int and sent["sent"] == SENT,
                      no_sender_backpressure=sent["blocked"] == 0,
                      tcp_stream_intact=received["tcp_bytes"] == sent["tcp_bytes"] > 0 and not received["tcp_errors"],
                      receiver_socket_no_loss=all(received["udp_mib_delta"][k] == 0 for k in ("RcvbufErrors", "InErrors")))
        rows = received["udp"] if stats is not None else []
        if arm == "F":
            sys.path.insert(0, str(ROOT / "harness/l2"))
            import netem_fifo_ledger as ledger
            report = read_json(directory / "queue-control.json")
            config = {"kind": "netem-fifo", "node": "worker", "interface": "eth0", "phase": "loaded",
                      "delay_ms": 500.0, "limit_packets": 100000}
            blob = (directory / "queue-ledger.bin.gz").read_bytes()
            errors = ledger.validate(report, config, blob)
            ins, outs = ledger.decode(blob)
            _, lost, extra = ledger.match(ins, outs)
            qdisc_restored = [q.get("kind") for q in report["kernel"]["qdisc_restored"]] == ["noqueue"]
            cleanup_safe = cleanup_safe and report.get("cleanup") == "PASS" and qdisc_restored
            plan = report["planned"]
            start, end, recovery = (plan[k] for k in ("start_ns", "end_ns", "recovery_start_ns"))
            life = read_json(directory / "lifecycle.json")
            counters = read_json(directory / "link-counters.json")
            checks.update(identity=summary["kind"] == "netem-fifo" and summary["mode"] == "full",
                          oracle_pass=not errors, all_frames_delivered=bool(ins) and not lost and not extra,
                          gso_frames_preserved=any(r[3] & ledger.GSO_TYPE_MASK for r in ins),
                          qdisc_restored=qdisc_restored, cleanup_pass=report.get("cleanup") == "PASS",
                          removed_after_sender=report["actual"]["removed_ns"] >= max(
                              life["sender_reaped_ns"], life["measurement_end_ns"]))
            reasons.extend("oracle: " + error for error in errors)
        else:
            # The pinned control writes actual_ns only after each checked tc command
            # succeeds. It has no mirror/ledger or option readback: do not claim one.
            plan, actual = summary["planned_ns"], summary["actual_ns"]
            start, end, recovery = (plan[k] for k in ("start", "end", "recovery"))
            install, drain, remove = (actual[k] for k in ("install", "drain", "remove"))
            checks.update(exit_status=lifecycle.get("exit_code") == 0,
                          treatment_commands_completed=start <= install[0] <= install[1] < end <= drain[0]
                              <= drain[1] <= remove[0] <= remove[1],
                          qdisc_zero_drops=actual["final_drops"] == 0)
            # Historical remove-after-recovery is measured, never silently corrected.
            checks["historical_timeline"] = abs(end - start - 15_250_000_000) <= 1 and abs(recovery - end - 750_000_000) <= 1
            counters = {"before": summary["link_before"], "after": summary["link_after"]}
        before, after = counters["before"], counters["after"]
        checks["link_zero_drops"] = len(before) == 2 and before.keys() == after.keys() and all(
            after[node][direction]["dropped"] - before[node][direction]["dropped"] == 0
            for node in before for direction in ("rx", "tx"))
        # Avoid classifying the plain control's delayed tc install as failure: use
        # only the interior after its observed installation completed.
        active_start = max(start + 50_000_000, summary["actual_ns"]["install"][1] if arm == "P" else start)
        active = [r[2] - r[1] for r in rows if active_start <= r[1] < end]
        recovering = [r[2] - r[1] for r in rows if r[1] >= recovery]
        checks.update(active_delay_applied=bool(active) and min(active) >= 500_000_000,
                      recovery_transparent=bool(recovering) and max(recovering) < 125_000_000)
        if arm == "F":
            expected_code = 0 if all(checks.values()) and valid(stats) and not stats["overtaken"] else 1
            checks["exit_status"] = lifecycle.get("exit_code") == expected_code
        reasons.extend(key for key, passed in checks.items() if not passed)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, EOFError, zlib.error) as error:
        reasons.append("evidence unavailable/malformed: " + repr(error))
        # F has an explicit injector cleanup record; absent or unreadable evidence
        # must not authorize the next measurement, even if containment succeeded.
        if arm == "F":
            cleanup_safe = False
    return {"receiver": stats, "validity": {"valid": not reasons, "reasons": reasons, "checks": checks},
            "cleanup_safe": cleanup_safe}


def host_state(require_services=True):
    """Read-only between-run checks; never inspect process arguments or change services."""
    compilers = []
    for path in Path("/proc").glob("[0-9]*/comm"):
        try:
            if path.read_text().strip() in ("cargo", "rustc", "rustc-real"):
                compilers.append(int(path.parent.name))
        except FileNotFoundError:
            pass
    state = {"compiler_pids": compilers, "services": {}, "rig_scopes": []}
    if require_services:
        for unit in ("rc-worker.service", "zfc-rig-watch.service"):
            value = subprocess.check_output(["systemctl", "show", unit, "-p", "ActiveState", "--value"],
                                            text=True, timeout=10).strip()
            state["services"][unit] = value
        scopes = subprocess.check_output(["systemctl", "list-units", "--type=scope", "--state=active",
                                          "--no-legend", "--plain"], text=True, timeout=10)
        state["rig_scopes"] = [line.split()[0] for line in scopes.splitlines() if "rig-watch-" in line]
    state["quiet"] = not compilers and not state["rig_scopes"] and all(v == "inactive" for v in state["services"].values())
    return state


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def main():
    from qa.lease import Lease
    from qa.process import run_child
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lease-dir", default="/var/lock/zfstack-qa")
    parser.add_argument("--runs", type=int, default=RUNS, help="per arm; anything but 30 is smoke only")
    a = parser.parse_args()
    if sys.platform != "linux" or not 1 <= a.runs <= RUNS:
        raise SystemExit("requires Linux and 1..30 pairs")
    head = git("rev-parse", "HEAD")
    if head != a.expected_sha or git("status", "--porcelain", "--untracked-files=all"):
        raise SystemExit(f"checkout is not a clean {a.expected_sha} (HEAD {head})")
    changed = git("diff", "--name-only", GATE_SHA, head, "--", *UNCHANGED)
    if changed:
        raise SystemExit("measured code or isolation/supervision differs from attempt 02: " + changed.replace("\n", ", "))
    if not a.output.is_absolute() or a.output.exists():
        raise SystemExit("--output must be a fresh absolute directory")
    allocation = {"plan_version": PLAN_VERSION, "seed": SEED, "order": [list(pair) for pair in schedule()]}
    if read_json(HERE / "allocation.json") != allocation:
        raise SystemExit("allocation file differs from the frozen schedule")
    a.output.mkdir(parents=True)
    order = schedule(a.runs)
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    plan = {"plan_version": PLAN_VERSION, "written_before_execution_unix_ns": time.time_ns(),
            "tested_sha": head, "gate_sha": GATE_SHA, "kernel": os.uname().release, "boot_id": boot,
            "runs_per_arm": a.runs, "frozen_runs_per_arm": RUNS, "smoke": a.runs != RUNS,
            "seed": SEED, "order": order, "endpoint": "any receiver-overtaken UDP datagram",
            "test": "one-sided paired exact test, alpha .05; least-favorable completion of unknowns",
            "qualification": "none"}
    (a.output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    rows, aborted = [], None
    try:
        with Lease(a.lease_dir, a.domain, a.run_id, boot) as lease:
            for number, arm in order:
                lease.check()
                label = f"{arm}-{number:02d}"
                state = host_state(require_services=a.runs == RUNS)
                (a.output / (label + ".host-before.json")).write_text(json.dumps(state, indent=2) + "\n")
                if not state["quiet"]:
                    raise RuntimeError("competing compiler/rig task or paused service restarted")
                if Path("/proc/sys/kernel/random/boot_id").read_text().strip() != boot:
                    raise RuntimeError("host boot identity changed")
                target = a.output / (label + ".json")
                argv = ([sys.executable, str(ROOT / "harness/l2/fifo_selftest.py"), "--kind", "netem-fifo", "--mode", "full",
                         "--output", str(target)] if arm == "F" else [sys.executable, str(ROOT / CONTROL), "--output", str(target)])
                before = {name: Path("/proc", name).read_text() for name in ("stat", "softirqs")}
                started = time.time_ns()
                command = ["bash", str(ROOT / "qa/isolate-linux.sh"), *argv]
                try:
                    child = run_child(command, ROOT, a.output / (label + ".log"), 300, env, lease)
                except (Exception, KeyboardInterrupt) as error:
                    child = {"argv": command, "status": "cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                             "exit_code": None, "cleanup_scope": "unknown", "cleanup_status": "UNKNOWN",
                             "lifecycle_detail": "runner returned no cleanup proof: " + repr(error)}
                # Persist lifecycle before analysis, including exceptions/malformed evidence.
                (a.output / (label + ".child.json")).write_text(json.dumps(child, indent=2) + "\n")
                row = {"label": label, "arm": arm, "round": number, "returncode": child.get("exit_code"),
                       "child": child, "started_unix_ns": started, "finished_unix_ns": time.time_ns(),
                       "proc_before": before, "proc_after": {name: Path("/proc", name).read_text() for name in before},
                       **assess(target, arm, child)}
                rows.append(row)
                (a.output / "execution.json").write_text(json.dumps(rows, indent=2) + "\n")
                lease.check()
                if not row["cleanup_safe"]:
                    lease.quarantine("order-rate cleanup unproven; inspect before any further measurement")
                    raise RuntimeError("cleanup failed or unknown")
                state = host_state(require_services=a.runs == RUNS)
                (a.output / (label + ".host-after.json")).write_text(json.dumps(state, indent=2) + "\n")
                if not state["quiet"]:
                    raise RuntimeError("competing compiler/rig task or paused service restarted")
                if child["status"] == "cancelled":
                    raise RuntimeError("child cancelled")
                # Progress only; do not expose accumulating statistical results.
                print(label, "recorded", flush=True)
    except Exception as error:
        aborted = repr(error)
    except KeyboardInterrupt:
        aborted = "operator interruption"
    if aborted:
        (a.output / "abort.json").write_text(json.dumps({"reason": aborted}, indent=2) + "\n")
    summary = decide(rows, a.runs, aborted)
    summary.update(smoke=a.runs != RUNS, aborted=aborted)
    (a.output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 2 if aborted else 0


def interrupt(_number, _frame):
    raise KeyboardInterrupt("termination requested")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    raise SystemExit(main())
