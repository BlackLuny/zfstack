"""Recompute all runs from ledgers/counters and conservatively align CPU clocks."""
import bisect
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

P = Path(__file__).resolve().parent
sys.path.insert(0, str(P.parents[1]))
import fifo_ledger as L


def read(path):
    return json.loads(path.read_text())


def ns(decimal):
    seconds, fraction = decimal.split(".")
    return int(seconds) * 10**9 + int(fraction.ljust(9, "0"))


assert L.LATENESS_LIMIT_NS == 20_000_000
plan = read(P / "experiment-plan.json")
for path, sha in plan["source_files"].items():
    assert hashlib.sha256((P.parents[3] / path).read_bytes()).hexdigest() == sha, path
summary = {}
for run in sorted(P.iterdir()):
    if not run.is_dir() or not (run / "execution.json").exists():
        continue
    name = run.name
    raw = run / name
    report = read(raw / "queue-control.json")
    rows = L.decode((raw / "queue-ledger.bin.gz").read_bytes())
    errors = L.validate(report, report["config"], (raw / "queue-ledger.bin.gz").read_bytes())
    delivered = [r for r in rows if r[2] == L.DELIVERED]
    worst = max(delivered, key=lambda r: r[8] - r[7])
    seq, phase = worst[:2]
    due, emitted = worst[6] + worst[7], worst[6] + worst[8]
    links = read(raw / "link-counters.json")
    delta = {node: {d: links["after"][node][d]["dropped"] -
                        links["before"][node][d]["dropped"] for d in ("rx", "tx")}
             for node in links["before"]}
    sent, received = read(raw / "sender.json"), read(raw / "receiver.json")
    recovery_latency = [r[2]-r[1] for r in received["udp"] if r[1] >= report["planned"]["recovery_start_ns"]]
    result = {"oracle_errors": errors, "frames": len(rows),
        "delivered": len(delivered), "overflows": sum(r[2] == L.OVERFLOW for r in rows),
        "late_frames": sum(r[8]-r[7] > L.LATENESS_LIMIT_NS for r in delivered),
        "max_lateness_by_phase_ns": {str(p): max((r[8]-r[7] for r in delivered if r[1] == p), default=None) for p in range(4)},
        "worst": {"row": worst, "seq": seq, "phase": phase,
                  "due_ns": due, "emitted_ns": emitted, "lateness_ns": emitted-due},
        "sender": sent, "udp_received": len(received["udp"]),
        "udp_in_order_complete": [r[0] for r in received["udp"]] == list(range(sent["sent"])),
        "tcp_matches": received["tcp_bytes"] == sent["tcp_bytes"] and not received["tcp_errors"],
        "receiver_recovery_max_ns": max(recovery_latency, default=None),
        "receiver_recovery_lt_125ms": bool(recovery_latency) and max(recovery_latency) < 125_000_000,
        "raw_link_drop_delta": delta, "cpu_policy": read(raw / "cpu-policy.json")}
    if (run / "host-vcpu.json.gz").exists():
        maps = read(run / "clock-before.json") + read(run / "clock-after.json")
        low, high = max(r["offset_low"] for r in maps), min(r["offset_high"] for r in maps)
        assert low <= high, (name, low, high)
        host = json.loads(gzip.decompress((run / "host-vcpu.json.gz").read_bytes()))
        samples = host["rows"]
        starts, ends = [r[0] for r in samples], [r[1] for r in samples]
        cpu_ids = [t["vcpu"] for t in host["threads"]]
        values = {cpu: [next(v for v in s[2] if v[0] == cpu) for s in samples] for cpu in cpu_ids}
        # XNU recount reports accrued lifetime counters, not a live current-CPU
        # snapshot. Extend each endpoint through two consecutive WAITING reads
        # with equal counters. Never call an arbitrary RUNNING read a bound.
        settled = {cpu: [i for i in range(1, len(samples))
                        if values[cpu][i][3] == values[cpu][i-1][3] == 3
                        and values[cpu][i][1:3] == values[cpu][i-1][1:3]] for cpu in cpu_ids}

        def bound(begin, end):
            i = bisect.bisect_left(ends, begin+low)-1
            j = bisect.bisect_right(starts, end+high)
            if i < 0 or j >= len(samples):
                return None
            before = {r[0]: r[1]+r[2] for r in samples[i][2]}
            after = {r[0]: r[1]+r[2] for r in samples[j][2]}
            assert before.keys() == after.keys()
            cost = {cpu: after[cpu]-before[cpu] for cpu in before}
            assert min(cost.values()) >= 0
            closed = {}
            for cpu in cpu_ids:
                candidates = settled[cpu]
                pos = bisect.bisect_left(candidates, j+1)
                if pos >= len(candidates):
                    closed = None
                    break
                k = candidates[pos]
                v = values[cpu][k]
                closed[cpu] = {"sample_index": k, "sample_window_ns": samples[k][:2],
                               "cpu_delta_ns": v[1]+v[2]-before[cpu]+1000,
                               "state": v[3], "stable_waiting_reads": 2}
            closed_total = sum(v["cpu_delta_ns"] for v in closed.values()) if closed else None
            return {"host_sample_window_ns": [samples[i][0], samples[j][1]],
                    "edge_slack_ns": begin+low-samples[i][0] + samples[j][1]-end-high,
                    "unsettled_all_vcpu_cpu_delta_ns": sum(cost.values()),
                    "unsettled_per_vcpu_cpu_delta_ns": cost,
                    "settled_endpoints": closed,
                    "all_vcpu_cpu_upper_bound_ns": closed_total,
                    "wall_ns": end-begin,
                    "minimum_wall_not_executing_any_vcpu_ns": max(0, end-begin-closed_total) if closed_total is not None else None}

        events = json.loads(gzip.decompress((raw / "diagnostic-events.json.gz").read_bytes()))["events"]
        slow = []
        for e in events:
            b, end = e["begin"], e["end"]
            # Idle waits before traffic is admitted cannot explain late frames.
            if end[0]-b[0] < 5_000_000 or end[0] < report["planned"]["measurement_start_ns"]:
                continue
            e = dict(e)
            e["wall_ns"], e["guest_cpu_ns"] = end[0]-b[0], end[1]-b[1]
            e["host_bound"] = bound(b[0], end[0])
            slow.append(e)
        lines = gzip.decompress((run / "guest.ftrace.gz").read_bytes()).decode().splitlines()
        trace = []
        for line in lines:
            m = re.search(r"\[(\d+)\].*? (\d+\.\d+): (sched_switch|sched_wakeup): (.*)", line)
            if m:
                trace.append({"ns": ns(m[2]), "cpu": int(m[1]), "event": m[3], "text": m[4], "line": line})
        trace.sort(key=lambda r: r["ns"])
        waits, pending = [], None
        for t in trace:
            text = t["text"]
            if t["event"] == "sched_wakeup":
                pending = t
            elif "next_comm=qa75-inject " in text:
                if pending:
                    gap = t["ns"] - pending["ns"]
                    if gap > 1_000_000:
                        waits.append({"begin_ns": pending["ns"], "end_ns": t["ns"], "wait_ns": gap,
                                      "begin_event": pending, "end_event": t, "host_bound": bound(pending["ns"], t["ns"])})
                pending = None
            elif "prev_comm=qa75-inject " in text:
                pending = t if "prev_state=R " in text else None
        result["clock_offset_interval_ns"] = [low, high]
        result["clock_offset_width_ns"] = high-low
        result["host_vcpu_count"] = len(host["threads"])
        result["worst"]["host_bound"] = bound(due, emitted)
        result["worst"]["overlapping_operations"] = [e for e in slow if e["begin"][0] < emitted and e["end"][0] > due]
        result["worst"]["overlapping_runnable_waits"] = [w for w in waits if w["begin_ns"] < emitted and w["end_ns"] > due]
        result["long_operations"] = sorted(slow, key=lambda e: e["wall_ns"], reverse=True)[:30]
        result["long_runnable_waits"] = sorted(waits, key=lambda w: w["wait_ns"], reverse=True)[:20]
        nearby = [t for t in trace if due-100_000 <= t["ns"] <= emitted+100_000]
        before = [t for t in trace if t["ns"] < due-100_000]
        after = [t for t in trace if t["ns"] > emitted+100_000]
        bracket = before[-2:] + nearby + after[:2]
        (run / "worst-frame-kernel.txt").write_text("\n".join(t["line"] for t in bracket)+"\n")
        stats = (run / "trace-stats.txt").read_text()
        result["trace_overruns"] = [int(n) for n in re.findall(r"(?m)^overrun: (\d+)$", stats)]
        result["trace_commit_overruns"] = [int(n) for n in re.findall(r"(?m)^commit overrun: (\d+)$", stats)]
        result["trace_dropped_events"] = [int(n) for n in re.findall(r"(?m)^dropped events: (\d+)$", stats)]
        assert not any(result["trace_overruns"] + result["trace_commit_overruns"] + result["trace_dropped_events"]), name
        (run / "correlation.json").write_text(json.dumps(result, indent=2)+"\n")
    summary[name] = result
(P / "recomputed.json").write_text(json.dumps(summary, indent=2)+"\n")
for name, r in summary.items():
    print(name, "phase maxima ms", [round(x/1e6, 6) if x is not None else None for x in r["max_lateness_by_phase_ns"].values()],
          "late", r["late_frames"], "oracle", r["oracle_errors"][:1])

calibration = P / "accounting-calibration"
if calibration.exists():
    maps = read(calibration / "clock-before.json") + read(calibration / "clock-after.json")
    low, high = max(r["offset_low"] for r in maps), min(r["offset_high"] for r in maps)
    assert low <= high
    host = json.loads(gzip.decompress((calibration / "host-vcpu.json.gz").read_bytes()))["rows"]
    starts, ends = [r[0] for r in host], [r[1] for r in host]
    response = []
    for phase in read(calibration / "guest-phases.json"):
        if phase["phase"] != "spin":
            continue
        i = bisect.bisect_left(ends, phase["begin_ns"]+low)-1
        # The next idle phase is 500ms. Deliberately include at least 100ms
        # afterward, and require two stable WAITING reads on every vCPU.
        j = bisect.bisect_right(starts, phase["end_ns"]+high+100_000_000)
        while j < len(host):
            if all(a[3] == b[3] == 3 and a[1:3] == b[1:3]
                   for a, b in zip(host[j-1][2], host[j][2])):
                break
            j += 1
        assert i >= 0 and j < len(host)
        cost = {a[0]: b[1]+b[2]-a[1]-a[2]+1000 for a, b in zip(host[i][2], host[j][2])}
        response.append({"phase": phase, "host_sample_window_ns": [host[i][0], host[j][1]],
                         "per_vcpu_cpu_upper_bound_ns": cost,
                         "all_vcpu_cpu_upper_bound_ns": sum(cost.values())})
    (P / "calibration-recomputed.json").write_text(json.dumps({
        "clock_offset_width_ns": high-low, "spin_phases": response,
        "meaning": "Native CPU counters respond to known guest work and settle during subsequent idle. This is a counter sanity check, not FIFO qualification.",
    }, indent=2)+"\n")
