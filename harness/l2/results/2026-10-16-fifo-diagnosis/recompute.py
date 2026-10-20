#!/usr/bin/env python3
"""Recompute raw-ledger timing, outcomes, payload counts and link deltas."""
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import fifo_ledger as ledger


def recompute():
    identity = json.loads((HERE / "source-identity.json").read_text())
    oracle = HERE.parents[1] / "fifo_ledger.py"
    assert hashlib.sha256(oracle.read_bytes()).hexdigest() == identity["files"]["harness/l2/fifo_ledger.py"]
    assert ledger.LATENESS_LIMIT_NS == 20_000_000
    result = {}
    for name in ("before-full-1", "before-full-2", "before-cancel-1", "before-full-3",
                 "after-full", "after-cancel", "after-overflow"):
        directory = HERE / name
        report = json.loads((directory / "queue-control.json").read_text())
        blob = (directory / "queue-ledger.bin.gz").read_bytes()
        rows = ledger.decode(blob)
        sender = json.loads((directory / "sender.json").read_text())
        receiver = json.loads((directory / "receiver.json").read_text())
        sequence = [row[0] for row in receiver["udp"]]
        phases = {}
        for phase, label in enumerate(ledger.PHASES):
            values = [row[8] - row[7] for row in rows
                      if row[1] == phase and row[2] == ledger.DELIVERED]
            phases[label] = {"frames": len(values), "early": sum(x < 0 for x in values),
                             "late": sum(x > ledger.LATENESS_LIMIT_NS for x in values),
                             "max_lateness_ns": max(values) if values else None}
        value = {"frames": len(rows), "ledger_sha256": hashlib.sha256(blob).hexdigest(),
                 "oracle_errors": ledger.validate(report, report["config"], blob), "phases": phases,
                 "outcomes": {str(code): sum(row[2] == code for row in rows)
                              for code in (ledger.DELIVERED, ledger.OVERFLOW, ledger.SEND_FAILED, ledger.UNSENT)},
                 "udp_sent": sender["sent"], "udp_received": len(sequence),
                 "udp_exactly_once_in_order": sequence == list(range(sender["sent"])),
                 "udp_payload_intact": all(row[3] for row in receiver["udp"]),
                 "sender_eagain": sender["blocked"],
                 "tcp_bytes_match": receiver["tcp_bytes"] == sender["tcp_bytes"] and not receiver["tcp_errors"]}
        if (directory / "link-counters.json").is_file():
            counters = json.loads((directory / "link-counters.json").read_text())
            value["link_drop_deltas"] = {
                node: {direction: counters["after"][node][direction]["dropped"] - before[direction]["dropped"]
                       for direction in ("rx", "tx")} for node, before in counters["before"].items()}
            lifecycle = json.loads((directory / "lifecycle.json").read_text())
            value["path_removed_after_clients_and_measurement"] = report["actual"]["removed_ns"] >= max(
                lifecycle["sender_reaped_ns"], lifecycle["measurement_end_ns"])
        saved = json.loads((HERE / (name + ".json")).read_text())
        assert value["oracle_errors"] == saved["oracle_errors"], name
        result[name] = value
    return result


if __name__ == "__main__":
    print(json.dumps(recompute(), indent=2))
