"""Recompute the three archived kernel selftests; does not run new traffic."""
import hashlib
import json
import sys
from pathlib import Path

P = Path(__file__).resolve().parent
sys.path.insert(0, str(P.parents[1]))
import fifo_ledger as L

assert L.LATENESS_LIMIT_NS == 20_000_000
source = json.loads((P / "selftest-plan.json").read_text())
root = P.parents[3]
for name, sha in source["source_files"].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == sha, name
results = {}
for mode in ("full", "cancel", "overflow"):
    d = P / "selftests/evidence" / mode
    report = json.loads((d / "queue-control.json").read_text())
    blob = (d / "queue-ledger.bin.gz").read_bytes()
    rows = L.decode(blob)
    errors = L.validate(report, report["config"], blob)
    sender = json.loads((d / "sender.json").read_text())
    receiver = json.loads((d / "receiver.json").read_text())
    counters = json.loads((d / "link-counters.json").read_text())
    delta = {n: {side: counters["after"][n][side]["dropped"]-counters["before"][n][side]["dropped"]
                 for side in ("rx", "tx")} for n in counters["before"]}
    summary = json.loads(d.with_suffix(".json").read_text())
    results[mode] = {"oracle_errors": errors,
        "max_lateness_by_phase_ns": {str(p): max((r[8]-r[7] for r in rows if r[1] == p and r[2] == L.DELIVERED), default=None)
                                     for p in range(4)},
        "frames": len(rows), "delivered": sum(r[2] == L.DELIVERED for r in rows),
        "overflows": sum(r[2] == L.OVERFLOW for r in rows),
        "udp_overflows": sum(r[2] == L.OVERFLOW and r[10] == 42 and r[5] == 1074 for r in rows),
        "sender": sender, "udp_received": len(receiver["udp"]),
        "tcp_matches": receiver["tcp_bytes"] == sender["tcp_bytes"] and not receiver["tcp_errors"],
        "link_drop_delta": delta, "selftest_checks": summary["checks"]}
    assert not any(v for directions in delta.values() for v in directions.values()), mode
    assert (not errors if mode == "full" else errors), mode
    if mode != "overflow":
        assert [r[0] for r in receiver["udp"]] == list(range(sender["sent"])), mode
        assert results[mode]["tcp_matches"], mode
    else:
        assert sender["sent"]-len(receiver["udp"]) == results[mode]["udp_overflows"]
    if mode == "cancel":
        life = json.loads((d / "lifecycle.json").read_text())
        assert report["actual"]["removed_ns"] >= max(life["sender_reaped_ns"], life["measurement_end_ns"])
    print(mode, "phase maxima ms", [None if t is None else round(t/1e6, 6)
                                     for t in results[mode]["max_lateness_by_phase_ns"].values()])
(P / "selftests-recomputed.json").write_text(json.dumps(results, indent=2)+"\n")
