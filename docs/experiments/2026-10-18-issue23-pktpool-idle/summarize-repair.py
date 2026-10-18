#!/usr/bin/env python3
"""Summarize the full 12-block ABBA/BAAB base/main vs fixed PR #92 comparison.

Block effect: (mean of 2 fixed subprocess medians) /
              (mean of 2 baseline subprocess medians) - 1.
Reported effect: median across blocks; percentile bootstrap resamples whole
blocks 10,000 times with fixed seeds. A/A reflects the baseline arms only.
"""
from collections import defaultdict
import csv
import random
import statistics
from pathlib import Path

root = Path(__file__).parent
for label, filename, group_columns in [
    ("idle-trim", "repair-paired-trim.csv", ("cached", "hot_count", "floor")),
    ("LIFO-get-drop", "repair-paired-hotpath.csv", ("cached", "iterations")),
]:
    data = defaultdict(lambda: defaultdict(list))
    with (root / filename).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = tuple(int(row[name]) for name in group_columns)
            data[key][int(row["block"])].append(row)
    print(f"\n{label}")
    for key, blocks in sorted(data.items()):
        paired = []
        aa = []
        b_times = []
        f_times = []
        for block, rows in sorted(blocks.items()):
            baseline = [int(r["median_ns"]) for r in rows if r["arm"] == "base"]
            fixed = [int(r["median_ns"]) for r in rows if r["arm"] == "fix"]
            assert len(baseline) == len(fixed) == 2, (key, block)
            b_times.extend(baseline)
            f_times.extend(fixed)
            paired.append(100.0 * (statistics.mean(fixed) / statistics.mean(baseline) - 1))
            aa.append(100.0 * (baseline[1] / baseline[0] - 1))
        assert len(blocks) == len(paired) == 12, key
        rng = random.Random(sum(key) * 31 + 17)
        boots = sorted(statistics.median(rng.choices(paired, k=len(paired))) for _ in range(10000))
        print(
            f"{key}: baseline_ns={statistics.median(b_times):.0f} fixed_ns={statistics.median(f_times):.0f} "
            f"paired_pct={statistics.median(paired):+.2f} "
            f"paired_95_ci=[{boots[250]:+.2f},{boots[9749]:+.2f}] "
            f"baseline_AA_pct={statistics.median(aa):+.2f}"
        )
