#!/usr/bin/env python3
"""Offline per-frame analysis of the retained remote23 formal C16 fault arm.

Reads only retained artifacts (ledger, injector report, UDP raw samples) and
writes deterministic JSON. It does not re-grade the run: the formal verdict
stays FAIL/VALID as recomputed in ../2026-10-16-fifo-remote23.
"""
import argparse
import bisect
import collections
import gzip
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import fifo_ledger as ledger  # noqa: E402

DEFAULT = HERE.parent / "2026-10-16-fifo-remote23/formal-artifacts" / \
    "97fea515847c672e859bfd6fe4d1c746d6f4c08de7b30e437a76eb0917c29fea/fault/rig"
MS = 1_000_000
LATE_NS = MS
GAP_NS = 50 * MS
UDP_REQUEST_BYTES, UDP_REPLY_BYTES = 1074, 1134


def rel(ns, origin):
    return round((ns - origin) / 1e9, 6)


def quantiles(values):
    if not values:
        return None
    values = sorted(values)
    pick = lambda q: values[min(len(values) - 1, int(q * len(values)))]
    return {"n": len(values), "p50": pick(.5), "p99": pick(.99), "max": values[-1]}


def analyze(rig):
    report = json.loads((rig / "queue-control.json").read_text())
    rows = ledger.decode((rig / "queue-ledger.bin.gz").read_bytes())
    raw = json.loads(gzip.decompress((rig / "raw.json.gz").read_bytes()))
    planned = report["planned"]
    origin = planned["measurement_start_ns"]
    t_in = [row[6] for row in rows]
    due = [row[6] + row[7] for row in rows]
    out = [row[6] + row[8] if row[8] != ledger.NOT_SENT else None for row in rows]
    late = [None if out[i] is None else out[i] - due[i] for i in range(len(rows))]
    result = {"source": str(rig.relative_to(HERE.parent)), "frames": len(rows),
              "outcomes": {name: sum(row[2] == code for row in rows) for name, code in
                           (("delivered", ledger.DELIVERED), ("send_failed", ledger.SEND_FAILED),
                            ("overflow", ledger.OVERFLOW), ("unsent", ledger.UNSENT))},
              "planned_sec": {key: rel(value, origin) for key, value in planned.items()},
              "transparent_sec": rel(report["actual"]["transparent_ns"], origin)}

    by_phase = collections.defaultdict(list)
    for row, value in zip(rows, late):
        if value is not None:
            by_phase[ledger.PHASES[row[1]]].append(value)
    result["lateness_ns"] = {phase: dict(quantiles(values), over_limit=sum(v > ledger.LATENESS_LIMIT_NS for v in values))
                             for phase, values in sorted(by_phase.items())}

    # Single thread: while the FIFO head is late, the loop does nothing but
    # send, so the release rate in those windows is the send capacity.
    order = sorted((value, index) for index, value in enumerate(out) if value is not None)
    buckets = collections.defaultdict(list)
    for value, index in order:
        buckets[value // (10 * MS)].append(late[index])
    saturated = [len(values) * 100 for values in buckets.values() if len(values) >= 10 and min(values) > LATE_NS]
    gaps = [b[0] - a[0] for a, b in zip(order, order[1:]) if late[a[1]] > LATE_NS and late[b[1]] > LATE_NS]
    result["send_capacity"] = {"saturated_10ms_windows": len(saturated),
                               "frames_per_sec": quantiles(saturated),
                               "back_to_back_send_ns": quantiles(gaps)}

    loaded = [value for value in t_in if planned["start_ns"] <= value < planned["end_ns"]]
    arrivals = collections.Counter((value - planned["start_ns"]) // (100 * MS) for value in loaded)
    result["arrival"] = {"loaded_frames": len(loaded),
                         "mean_frames_per_sec": round(len(loaded) / ((planned["end_ns"] - planned["start_ns"]) / 1e9)),
                         "per_100ms": quantiles(list(arrivals.values()))}

    # A read gap is a pause between consecutive admissions. If the thread was
    # releasing late frames throughout it and the ring then yields a full
    # batch at once, the reader was starved rather than the path idle.
    out_sorted = [value for value, _ in order]
    late_sorted = [late[index] for _, index in order]
    gap_rows, gaps = [], []
    for index in range(1, len(rows)):
        lo, hi = t_in[index - 1], t_in[index]
        gap = hi - lo
        if gap < GAP_NS or not planned["start_ns"] <= hi < planned["measurement_end_ns"]:
            continue
        gaps.append((lo, hi))
        a, b = bisect.bisect_right(out_sorted, lo), bisect.bisect_left(out_sorted, hi)
        sent = late_sorted[a:b]
        burst = bisect.bisect_left(t_in, hi + MS, lo=index) - index
        gap_rows.append({"after_seq": index - 1, "start_sec": rel(lo, origin), "gap_ms": round(gap / MS, 3),
                         "released_in_gap": len(sent),
                         "released_late_fraction": round(sum(v > LATE_NS for v in sent) / len(sent), 4) if sent else None,
                         "release_rate_fps": round(len(sent) / (gap / 1e9)),
                         "admitted_within_1ms_after": burst})
    gap_rows.sort(key=lambda row: -row["gap_ms"])
    result["read_gaps"] = {"threshold_ms": GAP_NS / MS, "count": len(gap_rows), "largest": gap_rows[:12],
                           "busy_count": sum(1 for row in gap_rows if row["released_late_fraction"] and
                                             row["released_late_fraction"] > .99)}

    failed = [index for index, row in enumerate(rows) if row[2] == ledger.SEND_FAILED]
    if failed:
        times = [out[index] for index in failed]
        a, b = bisect.bisect_left(out_sorted, min(times) - 10 * MS), bisect.bisect_right(out_sorted, max(times) + 10 * MS)
        result["send_failed"] = {"count": len(failed), "seq": [rows[failed[0]][0], rows[failed[-1]][0]],
                                 "contiguous": failed == list(range(failed[0], failed[-1] + 1)),
                                 "released_sec": [rel(min(times), origin), rel(max(times), origin)],
                                 "bytes": dict(collections.Counter(rows[index][5] for index in failed).most_common()),
                                 "phases": dict(collections.Counter(ledger.PHASES[rows[index][1]] for index in failed)),
                                 "lateness_ns": quantiles([late[index] for index in failed]),
                                 "release_rate_fps_window_20ms": round((b - a) / ((max(times) - min(times) + 20 * MS) / 1e9))}

    result["udp"] = udp(raw, rows, out, origin, gaps)
    return result


def udp(raw, rows, out, origin, gaps):
    peers = sorted(key for key in raw if key.startswith("udp-"))
    samples = {key: {row["seq"]: row for row in raw[key]["samples"]} for key in peers}
    timed_out = sorted({seq for key in peers for seq, row in samples[key].items() if row["status"] == "timed_out"})
    requests = [index for index, row in enumerate(rows) if row[5] == UDP_REQUEST_BYTES]
    replies = sum(row[5] == UDP_REPLY_BYTES for row in rows)
    scheduled = sum(len(samples[key]) for key in peers)
    result = {"peers": len(peers), "scheduled": scheduled, "timed_out": sum(
        row["status"] == "timed_out" for key in peers for row in samples[key].values()),
        "request_frames": len(requests), "reply_frames": replies, "timed_out_seq": timed_out}
    if len(requests) != scheduled:
        result["mapping"] = "skipped: request frames do not match the schedule one to one"
        return result
    # All peers share one schedule; the k-th request frame belongs to seq k // peers.
    width = len(peers)
    offsets = [rows[index][6] / 1e9 - samples[peers[0]][position // width]["scheduled_at"]
               for position, index in enumerate(requests)]
    result["mapping"] = {"admission_minus_schedule_ms": {key: round(value * 1000, 3) for key, value in
                                                         quantiles(offsets).items() if key != "n"}}
    # Open-loop requests have a known send time, so admission minus schedule
    # bounds the wait before the injector read the frame; the ledger cannot
    # see that wait. A lag is attributed to a read gap only when the request
    # was due inside the gap and admitted in the batch that ended it.
    starts = [lo for lo, _ in gaps]
    lag = collections.defaultdict(list)
    attributed = collections.Counter()
    for position, index in enumerate(requests):
        sample = samples[peers[0]][position // width]
        scheduled_ns = round(sample["scheduled_at"] * 1e9)
        value = rows[index][6] - scheduled_ns
        lag[sample["phase"]].append(value)
        if value > ledger.LATENESS_LIMIT_NS:
            attributed["over_limit"] += 1
            slot = bisect.bisect_right(starts, scheduled_ns) - 1
            if slot >= 0 and scheduled_ns < gaps[slot][1] and rows[index][6] <= gaps[slot][1] + 5 * MS:
                attributed["inside_read_gap"] += 1
    result["admission_lag_ns"] = {phase: dict(quantiles(values),
                                              over_limit=sum(v > ledger.LATENESS_LIMIT_NS for v in values))
                                  for phase, values in sorted(lag.items())}
    result["admission_lag_over_limit"] = dict(attributed)
    release = sorted(out[index] for index in requests if out[index] is not None)
    windows = []
    for seq in timed_out:
        group = [requests[position] for position in range(seq * width, seq * width + width)]
        released = [out[index] for index in group if out[index] is not None]
        centre = statistics.median(released)
        dense = bisect.bisect_right(release, centre + MS) - bisect.bisect_left(release, centre - MS)
        windows.append({"seq": seq, "scheduled_sec": round(samples[peers[0]][seq]["scheduled_at"] - origin / 1e9, 6),
                        "admitted_sec": rel(min(rows[index][6] for index in group), origin),
                        "released_sec": rel(centre, origin),
                        "outcomes": sorted({rows[index][2] for index in group}),
                        "request_frames_released_within_1ms": dense})
    result["timed_out_requests"] = windows
    result["nominal_requests_per_2ms"] = round(width * 100 * .002, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rig", type=Path, default=DEFAULT)
    parser.add_argument("--output", type=Path, default=HERE / "formal-analysis.json")
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(args.rig.resolve()), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
