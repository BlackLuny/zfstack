#!/usr/bin/env python3
"""Attribute late UDP generator sends in one traced arm to scheduling, IRQ or runtime.

Reads only the arm's own files: trace.txt.gz (tracefs, monotonic clock),
trace-stats.json, processes.json, host-before.json and the generator samples.
For every UDP sample sent or skipped at least LATE_MS after its scheduled time,
the window [scheduled, observed] of that generator's single thread is split into
sleeping, runnable (by what occupied the CPU: idle, other tasks, IRQ/softirq)
and running (generator vs IRQ/softirq time on its CPU). The largest component
labels the window. The frozen rules are in PLAN.md; nothing here grades C16.
"""
import argparse
import bisect
import collections
import gzip
import json
import re
from pathlib import Path

LATE_MS = 5.0
MISS_MS = 20.0
WAKE_TIMER_LOOKBACK_NS = 200_000
LINE = re.compile(r"^\s*(?P<task>.+)-(?P<pid>\d+)\s+\[(?P<cpu>\d+)\]\s+\S+\s+(?P<sec>\d+)\.(?P<frac>\d+):\s+(?P<event>\w+):\s?(?P<body>.*)$")
SWITCH = re.compile(r"prev_comm=(?P<pc>.*) prev_pid=(?P<pp>-?\d+) prev_prio=\S+ prev_state=(?P<ps>\S+) ==> "
                    r"next_comm=(?P<nc>.*) next_pid=(?P<np>-?\d+) next_prio=\S+")
WAKE = re.compile(r"comm=(?P<comm>.*) pid=(?P<pid>\d+) prio=\S+(?: success=\d+)? target_cpu=(?P<cpu>\d+)")
MIGRATE = re.compile(r"comm=(?P<comm>.*) pid=(?P<pid>\d+) prio=\S+ orig_cpu=(?P<orig>\d+) dest_cpu=(?P<dest>\d+)")
KV = re.compile(r"(\w+)=(\S+)")
CLASSES = ("sleeping (late wakeup)", "runnable, CPU idle", "runnable behind other tasks",
           "IRQ/softirq on generator CPU", "generator on CPU")


class Intervals:
    """Per-CPU closed intervals with labels, queried for overlap with a window."""

    def __init__(self):
        self.rows, self.longest = collections.defaultdict(list), collections.Counter()

    def add(self, cpu, start, end, label):
        if end > start:
            self.rows[cpu].append((start, end, label))
            self.longest[cpu] = max(self.longest[cpu], end - start)

    def finish(self):
        for rows in self.rows.values():
            rows.sort()
        self.starts = {cpu: [row[0] for row in rows] for cpu, rows in self.rows.items()}

    def overlapping(self, cpu, start, end):
        rows, starts = self.rows.get(cpu, []), self.starts.get(cpu, [])
        index = bisect.bisect_left(starts, start - self.longest[cpu])
        while index < len(rows) and rows[index][0] < end:
            low, high = max(rows[index][0], start), min(rows[index][1], end)
            if high > low:
                yield low, high, rows[index][2]
            index += 1

    def union(self, cpu, start, end):
        total, reach = 0, start
        for low, high, _ in self.overlapping(cpu, start, end):
            low = max(low, reach)
            if high > low:
                total, reach = total + high - low, high
        return total


def parse(path, generators):
    """Scheduler state of generator threads plus per-CPU occupancy and IRQ/softirq intervals."""
    irq, occupancy = Intervals(), Intervals()
    stacks, current = collections.defaultdict(list), {}
    state = {pid: None for pid in generators}
    timeline = {pid: [] for pid in generators}
    residency = {pid: collections.Counter() for pid in generators}
    starts, expiries, wakings = {}, collections.defaultdict(list), {pid: [] for pid in generators}
    first = last = None
    with gzip.open(path, "rt", errors="replace") as stream:
        for line in stream:
            match = LINE.match(line)
            if not match:
                continue
            cpu, event, body = int(match["cpu"]), match["event"], match["body"]
            t = int(match["sec"]) * 1_000_000_000 + int(match["frac"].ljust(9, "0")[:9])
            first = t if first is None else first
            last = t

            def move(pid, new, where):
                old = state[pid]
                if old is not None:
                    timeline[pid].append((old[1], t, old[0], old[2]))
                    if old[0] == "running":
                        residency[pid][old[2]] += t - old[1]
                state[pid] = (new, t, where)

            if event == "sched_switch":
                fields = SWITCH.search(body)
                if not fields:
                    continue
                previous, following = int(fields["pp"]), int(fields["np"])
                if cpu in current:
                    occupancy.add(cpu, current[cpu][0], t, current[cpu][1])
                current[cpu] = (t, "idle" if following == 0 else fields["nc"])
                if previous in state:
                    move(previous, "runnable" if fields["ps"].startswith("R") else "sleeping", cpu)
                if following in state:
                    move(following, "running", cpu)
            elif event in ("sched_wakeup", "sched_wakeup_new"):
                fields = WAKE.search(body)
                if fields and int(fields["pid"]) in state:
                    pid = int(fields["pid"])
                    if state[pid] is None or state[pid][0] == "sleeping":
                        move(pid, "runnable", int(fields["cpu"]))
            elif event == "sched_waking":
                fields = WAKE.search(body)
                if fields and int(fields["pid"]) in wakings:
                    wakings[int(fields["pid"])].append((t, cpu))
            elif event == "sched_migrate_task":
                fields = MIGRATE.search(body)
                if fields and int(fields["pid"]) in state and state[int(fields["pid"])] and state[int(fields["pid"])][0] == "runnable":
                    move(int(fields["pid"]), "runnable", int(fields["dest"]))
            elif event in ("irq_handler_entry", "softirq_entry"):
                if event == "irq_handler_entry":
                    fields = dict(KV.findall(body))
                    label = f"irq {fields.get('irq')} {fields.get('name', '')}".strip()
                else:
                    label = "softirq " + (body.split("action=")[-1].rstrip("]") if "action=" in body else "?")
                stacks[cpu].append((t, label))
            elif event in ("irq_handler_exit", "softirq_exit"):
                if stacks[cpu]:
                    start, label = stacks[cpu].pop()
                    irq.add(cpu, start, t, label)
            elif event == "hrtimer_start":
                fields = dict(KV.findall(body))
                if "hrtimer" in fields and fields.get("expires", "").isdigit():
                    starts.setdefault(fields["hrtimer"], []).append((t, int(fields["expires"])))
            elif event == "hrtimer_expire_entry":
                fields = dict(KV.findall(body))
                if fields.get("function") == "hrtimer_wakeup" and fields.get("now", "").isdigit():
                    expiries[cpu].append((t, fields["hrtimer"], int(fields["now"])))
    for cpu, (start, label) in current.items():
        occupancy.add(cpu, start, last, label)
    for pid in state:
        if state[pid] is not None:
            timeline[pid].append((state[pid][1], last, state[pid][0], state[pid][2]))
    irq.finish()
    occupancy.finish()
    return {"irq": irq, "occupancy": occupancy, "timeline": timeline, "residency": residency, "hrtimer_starts": starts,
            "expiries": expiries, "wakings": wakings, "first_ns": first, "last_ns": last}


def timer_lateness(model, pid, start, end):
    """Largest observed delay of a select/poll hrtimer that woke this generator within the window."""
    worst = None
    for t, cpu in model["wakings"][pid]:
        if not start <= t <= end:
            continue
        rows = model["expiries"].get(cpu, [])
        index = bisect.bisect_right(rows, (t, "\uffff", 0)) - 1
        if index < 0 or t - rows[index][0] > WAKE_TIMER_LOOKBACK_NS:
            continue
        _, pointer, now = rows[index]
        armed = [expires for when, expires in model["hrtimer_starts"].get(pointer, []) if when <= rows[index][0]]
        if armed:
            late = now - armed[-1]
            worst = late if worst is None else max(worst, late)
    return worst


def attribute(model, pid, start, end, siblings):
    parts = collections.Counter()
    irq_labels, tasks, cpus = collections.Counter(), collections.Counter(), set()
    for low, high, kind, cpu in model["timeline"][pid]:
        low, high = max(low, start), min(high, end)
        if high <= low:
            continue
        if kind == "sleeping":
            parts["sleeping (late wakeup)"] += high - low
            continue
        cpus.add(cpu)
        if kind == "running":
            interrupted = model["irq"].union(cpu, low, high)
            parts["IRQ/softirq on generator CPU"] += interrupted
            parts["generator on CPU"] += high - low - interrupted
        else:
            for a, b, occupant in model["occupancy"].overlapping(cpu, low, high):
                interrupted = model["irq"].union(cpu, a, b)
                parts["IRQ/softirq on generator CPU"] += interrupted
                key = "runnable, CPU idle" if occupant == "idle" else "runnable behind other tasks"
                parts[key] += b - a - interrupted
                if occupant != "idle":
                    tasks[occupant] += b - a - interrupted
        for a, b, label in model["irq"].overlapping(cpu, low, high):
            irq_labels[label] += b - a
    covered = sum(parts.values())
    sibling_irq = {str(s): model["irq"].union(s, start, end) for cpu in cpus for s in siblings.get(cpu, ()) if s != cpu}
    dominant = max(CLASSES, key=lambda key: parts[key]) if covered else None
    return {"class": dominant, "components_ms": {key: round(parts[key] / 1e6, 3) for key in CLASSES},
            "covered_ms": round(covered / 1e6, 3), "uncovered_ns": end - start - covered, "cpus": sorted(cpus),
            "top_irq_softirq_ms": {k: round(v / 1e6, 3) for k, v in irq_labels.most_common(5)},
            "top_other_tasks_ms": {k: round(v / 1e6, 3) for k, v in tasks.most_common(5)},
            "sibling_irq_softirq_ms": {k: round(v / 1e6, 3) for k, v in sibling_irq.items()},
            "timer_late_ms": None if (late := timer_lateness(model, pid, start, end)) is None else round(late / 1e6, 3)}


def generator_samples(directory):
    found = {}
    for path in sorted(directory.rglob("udp-*.json")):
        flow = path.stem.split("-")[1]
        if flow.isdigit() and path.is_file():
            found[int(flow)] = json.loads(path.read_text())["samples"]
    if not found:
        for path in directory.rglob("raw.json.gz"):
            raw = json.loads(gzip.decompress(path.read_bytes()))
            found = {int(k[4:-5]): v["samples"] for k, v in raw.items() if k.startswith("udp-") and k.endswith(".json")}
    return found


def generator_pids(processes):
    pids = {}
    for row in processes:
        line = row["cmdline"]
        if any(part.endswith("udp_probe.py") for part in line) and "--flow" in line:
            pids.setdefault(int(line[line.index("--flow") + 1]), set()).add(row["pid"])
    return pids


def siblings_of(snapshot):
    result = {}
    for name, row in snapshot["cpus"].items():
        members = set()
        for part in row["siblings"].split(","):
            low, _, high = part.partition("-")
            if low.isdigit():
                members.update(range(int(low), int(high or low) + 1))
        result[int(name[3:])] = sorted(members)
    return result


def trace_drops(stats):
    drops = {}
    for cpu, text in stats.items():
        fields = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
        drops[cpu] = {key: int(fields.get(key, "0").strip() or 0) for key in ("overrun", "dropped events", "commit overrun")}
    return drops


def analyze(directory):
    directory = Path(directory)
    processes = json.loads((directory / "processes.json").read_text())
    before = json.loads((directory / "host-before.json").read_text())
    siblings = siblings_of(before)
    samples = generator_samples(directory)
    pids = generator_pids(processes)
    reasons = []
    if sorted(samples) != sorted(pids) or any(len(v) != 1 for v in pids.values()):
        reasons.append("generator processes could not be mapped one to one onto sample files")
    stats = json.loads((directory / "trace-stats.json").read_text())
    drops = trace_drops(stats)
    if any(value for row in drops.values() for value in row.values()):
        reasons.append("trace buffer overran or dropped events")
    mapping = {flow: next(iter(found)) for flow, found in pids.items() if len(found) == 1}
    model = parse(directory / "trace.txt.gz", set(mapping.values()))
    late, lags = [], {}
    for flow, rows in sorted(samples.items()):
        values = [row["generator_lag_ms"] for row in rows if row.get("generator_lag_ms") is not None]
        lags[str(flow)] = {"n": len(values), "max_ms": round(max(values), 3) if values else None,
                           "over_late_ms": sum(v >= LATE_MS for v in values), "over_miss_ms": sum(v > MISS_MS for v in values)}
        for row in rows:
            lag = row.get("generator_lag_ms")
            if lag is None or lag < LATE_MS or flow not in mapping:
                continue
            start = round(row["scheduled_at"] * 1e9)
            end = round((row["started_at"] if row.get("started_at") is not None else row["scheduled_at"] + lag / 1000) * 1e9)
            entry = {"flow": flow, "seq": row["seq"], "phase": row["phase"], "status": row["status"],
                     "lag_ms": round(lag, 3), "miss": lag > MISS_MS}
            if model["first_ns"] is None or start < model["first_ns"] or end > model["last_ns"]:
                entry["class"] = None
                reasons.append(f"trace does not cover late sample flow={flow} seq={row['seq']}")
            else:
                entry.update(attribute(model, mapping[flow], start, end, siblings))
                if entry["uncovered_ns"] != 0:
                    reasons.append(f"scheduler state does not cover late sample flow={flow} seq={row['seq']}")
                core0 = set(siblings.get(0, [0]))
                spans = [(label, b - a) for cpu in core0 for a, b, label in model["irq"].overlapping(cpu, start, end)]
                entry["core0_irq130_ms"] = round(sum(d for label, d in spans if label.split()[:2] == ["irq", "130"]) / 1e6, 3)
                entry["core0_net_rx_ms"] = round(sum(d for label, d in spans if label == "softirq NET_RX") / 1e6, 3)
                entry["generator_on_core0"] = bool(set(entry["cpus"]) & core0)
            late.append(entry)
    totals = collections.Counter()
    for cpu, rows in model["irq"].rows.items():
        for start, end, label in rows:
            if label.startswith("irq "):
                totals[(cpu, label)] += end - start
    return {"valid": not reasons, "invalid_reasons": reasons, "late_ms": LATE_MS, "miss_ms": MISS_MS,
            "trace_drops": drops, "trace_span_ns": [model["first_ns"], model["last_ns"]],
            "generator_pids": {str(k): v for k, v in mapping.items()},
            "generator_cpu_residency_ms": {str(flow): {str(c): round(v / 1e6, 1) for c, v in sorted(model["residency"][pid].items())}
                                           for flow, pid in mapping.items()},
            "generator_lag": lags, "late_samples": late,
            "class_counts": dict(collections.Counter(entry.get("class") for entry in late)),
            "miss_class_counts": dict(collections.Counter(entry.get("class") for entry in late if entry["miss"])),
            "irq_handler_ms_by_cpu": {f"{cpu}:{label}": round(v / 1e6, 3) for (cpu, label), v in totals.most_common(20)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("arm", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    a = parser.parse_args()
    a.output.write_text(json.dumps(analyze(a.arm), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
