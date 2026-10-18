#!/usr/bin/env python3
"""Describe every retained sample; no performance admission or E2E inference."""
import argparse
import json
from pathlib import Path
import random
import statistics

def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    left = int(position)
    right = min(left + 1, len(values) - 1)
    return values[left] + (values[right] - values[left]) * (position - left)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.raw.read_text().splitlines()]
    report = {"rows": len(rows), "nonpassing": [row for row in rows if row["status"] != "PASS"], "cases": []}
    if rows and rows[0]["phase"] == "counts":
        report["cases"] = [{"label": row["label"], "case": row["case"], **row.get("measurement", {})} for row in rows]
    else:
        rng = random.Random(53)
        for phase in ("AA", "AB"):
            for case in ("idle1", "idle64"):
                matching = [row for row in rows if row["phase"] == phase and row["case"] == case and row["status"] == "PASS"]
                ratios = []
                for pair in sorted({row["pair"] for row in matching}):
                    paired = {row["label"]: row["measurement"] for row in matching if row["pair"] == pair}
                    other = "A2" if phase == "AA" else "B"
                    if {"A1", other} <= paired.keys():
                        ratios.append(paired[other]["thread_cpu_ns"] / paired["A1"]["thread_cpu_ns"])
                if not ratios:
                    continue
                boot = [statistics.median(rng.choices(ratios, k=len(ratios))) for _ in range(10000)]
                report["cases"].append({"phase": phase, "case": case, "complete_pairs": len(ratios),
                    "paired_cpu_ratios": ratios, "median_cpu_ratio": statistics.median(ratios),
                    "descriptive_bootstrap_95pct": [percentile(boot, .025), percentile(boot, .975)],
                    "complete_planned_window": len(ratios) == (4 if phase == "AA" else 12)})
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
