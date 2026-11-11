#!/usr/bin/env python3
"""Re-run the netem FIFO oracle and the order analysis on every retained run."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
import netem_fifo_ledger as ledger  # noqa: E402
from analyze_order import analyze  # noqa: E402

RUNS = {"full": "selftests/full", "cancel": "selftests/cancel", "overflow": "selftests/overflow",
        "full-heavy": "selftests/full-heavy", "order-trace": "diagnostic/order-trace"}


def main():
    result = {}
    for name, path in RUNS.items():
        directory = HERE / path
        summary = json.loads(directory.with_suffix(".json").read_text())
        report = json.loads((directory / "queue-control.json").read_text())
        blob = (directory / "queue-ledger.bin.gz").read_bytes()
        config = dict(report["config"])
        trace = directory.with_suffix(".ftrace.gz")
        result[name] = {"selftest_ok": summary["ok"],
                        "selftest_failed_checks": sorted(key for key, value in summary["checks"].items() if not value),
                        "oracle_errors": ledger.validate(report, config, blob),
                        "order": analyze(directory, trace if trace.is_file() else None)}
        result[name]["order"].pop("source")
    (HERE / "recomputed.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
