#!/usr/bin/env python3
"""Locate guest-wide pauses in the retained real-host gate, from raw files only.

The UDP sender follows an absolute 2 ms schedule, so a gap in its send
timestamps means the sender did not run. For netem-fifo runs the egress
ledger and the injector's 100 ms clock samples show whether the queue and
the injector stopped over the same interval. This does not re-grade any run.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent / "2026-10-16-netem-fifo"))
import netem_fifo_ledger as ledger  # noqa: E402
from analyze_order import inversions  # noqa: E402

STAGE = HERE / "evidence/stage1"
PAUSE_NS = 5_000_000
MS = 1_000_000


def gaps(values, origin, limit):
    return [{"at_sec": round((a - origin) / 1e9, 4), "ms": round((b - a) / MS, 3)}
            for a, b in zip(values, values[1:]) if b - a > limit]


def run(name):
    directory = STAGE / name
    udp = json.loads((directory / "receiver.json").read_text())["udp"]
    if name.startswith("control"):
        planned = json.loads((STAGE / (name + ".json")).read_text())["planned_ns"]
        start, end, recovery = planned["start"], planned["end"], planned["recovery"]
    else:
        report = json.loads((directory / "queue-control.json").read_text())
        planned = report["planned"]
        start, end, recovery = planned["start_ns"], planned["end_ns"], planned["recovery_start_ns"]
    result = {"sender_pauses": gaps(sorted(row[1] for row in udp), start, PAUSE_NS)}
    late = set(inversions([row[0] for row in udp]))
    sent = {row[0]: row[1] for row in udp}
    result["overtaken"] = {"count": len(late), "sent_at_sec": sorted({round((sent[s] - start) / 1e9, 3) for s in late})}
    if not name.startswith("control"):
        in_rows, out_rows = ledger.decode((directory / "queue-ledger.bin.gz").read_bytes())
        offset = ledger.clock_offset(report["clock_samples"])[0]
        pairs, _, _ = ledger.match(in_rows, out_rows)
        released = sorted(out_rows[b][0] - offset for _, b in pairs)
        result["egress_pauses_in_loaded"] = [g for g in gaps(released, start, PAUSE_NS)
                                             if 0.5 < g["at_sec"] < (recovery - start) / 1e9]
        samples = sorted((s[0] + s[2]) // 2 for s in report["clock_samples"])
        result["injector_sample_gaps_over_110ms"] = gaps(samples, start, 110 * MS)
        excess = [(out_rows[b][0] - in_rows[a][0] - 500 * MS, in_rows[a][0] - offset) for a, b in pairs
                  if start + MS < in_rows[a][0] - offset < end - MS]
        worst = max(excess)
        result["max_active_excess_ms"] = {"ms": round(worst[0] / MS, 3), "arrived_at_sec": round((worst[1] - start) / 1e9, 4),
                                          "limit_ms": ledger.LATENESS_LIMIT_NS / MS}
    return result


def main():
    names = ["full", "cancel", "overflow", "control-1", "control-2", "control-3"]
    result = {"pause_threshold_ms": PAUSE_NS / MS, "runs": {name: run(name) for name in names},
              "note": "send times come from received datagrams, so overflow gaps mostly mark datagrams dropped by the forced overflow, and its egress gaps are the overflowed queue, not pauses"}
    (HERE / "pause-analysis.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
