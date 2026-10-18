#!/usr/bin/env python3
"""Rebuild paired idle-retirement microbenchmark summary from the raw CSV."""
import collections
import csv
import random
import statistics
from pathlib import Path

data = Path(__file__).with_name("paired-micro.csv")
rows = list(csv.DictReader(data.open(encoding="utf-8", newline="")))
for n in [128, 4096, 16384]:
    blocks = collections.defaultdict(list)
    for row in rows:
        if int(row["cached"]) == n:
            blocks[int(row["block"])].append(row)
    baseline, candidate, paired, aa = [], [], [], []
    for block, group in sorted(blocks.items()):
        before = [int(row["median_ns"]) for row in group if row["arm"] == "base"]
        after = [int(row["median_ns"]) for row in group if row["arm"] == "cand"]
        assert len(before) == len(after) == 2, (n, block)
        baseline.extend(before)
        candidate.extend(after)
        paired.append(100 * (statistics.mean(after) / statistics.mean(before) - 1))
        aa.append(100 * (before[1] / before[0] - 1))
    assert len(blocks) == 12 and len(paired) == 12, n
    rng = random.Random(23 + n)
    boots = sorted(statistics.median(rng.choices(paired, k=len(paired))) for _ in range(10000))
    print(
        f"{n}: base median={statistics.median(baseline):.0f} ns; "
        f"candidate median={statistics.median(candidate):.0f} ns; "
        f"paired median={statistics.median(paired):+.2f}%; "
        f"95% bootstrap=[{boots[250]:+.2f}%, {boots[9749]:+.2f}%]; "
        f"base A/A median={statistics.median(aa):+.2f}%; "
        f"blocks={len(paired)}"
    )
