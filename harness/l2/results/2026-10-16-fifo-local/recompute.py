#!/usr/bin/env python3
"""Recompute every retained oracle verdict and phase timing from raw ledgers."""
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import fifo_ledger as ledger


def recompute():
    result = {}
    for name in ("full", "cancel", "overflow", "full-2", "full-3"):
        directory = HERE / "local-selftest" / name
        report = json.loads((directory / "queue-control.json").read_text())
        blob = (directory / "queue-ledger.bin.gz").read_bytes()
        rows = ledger.decode(blob)
        phases = {}
        for phase, label in enumerate(ledger.PHASES):
            values = [row[8] - row[7] for row in rows
                      if row[1] == phase and row[2] == ledger.DELIVERED]
            phases[label] = {
                "delivered_frames": len(values),
                "early_frames": sum(value < 0 for value in values),
                "late_frames": sum(value > ledger.LATENESS_LIMIT_NS for value in values),
                "max_lateness_ns": max(values) if values else None,
            }
        result[name] = {
            "ledger_sha256": hashlib.sha256(blob).hexdigest(),
            "frames": len(rows),
            "outcomes": {str(outcome): sum(row[2] == outcome for row in rows)
                         for outcome in (ledger.DELIVERED, ledger.OVERFLOW,
                                         ledger.SEND_FAILED, ledger.UNSENT)},
            "oracle_errors": ledger.validate(report, report["config"], blob),
            "phases": phases,
        }
    return result


if __name__ == "__main__":
    print(json.dumps(recompute(), indent=2))
