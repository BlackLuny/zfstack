#!/usr/bin/env python3
"""Export all completed cohorts without pooling distinct fixture versions."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics

import rtt_analyze
import rtt_audit
import rtt_final
import rtt_guard
import rtt_load
import rtt_memory
import rtt_queue
import rtt_retry
from run import ROOT, save

COHORTS = [
    ("pilot-v1", "pilot", rtt_analyze.analyze),
    ("factorial-v1", "screen", rtt_analyze.analyze),
    ("memory-v1", "memory", rtt_memory.analyze),
    ("headroom-v1", "headroom", rtt_memory.analyze),
    ("guard-v1", "guard", rtt_guard.analyze),
    ("wan-v1", "wan", rtt_analyze.analyze),
    ("host-v2", "retry-proof", rtt_retry.analyze),
    ("host-v3", "retry-proof-v3", rtt_retry.analyze),
    ("host-v4", "retry-proof-v4", rtt_retry.analyze),
    ("final-screen", "final-screen", rtt_final.analyze),
    ("final-depth", "final-depth", rtt_queue.analyze),
    ("final-floor", "final-floor", rtt_load.analyze),
    ("final-policy", "final-policy", rtt_queue.analyze),
    ("final-loaded", "final-loaded", rtt_queue.analyze),
    ("final-confirm", "final-confirm", rtt_queue.analyze),
    ("final-aa", "final-aa", rtt_final.analyze),
    ("final-guard", "final-guard", rtt_final.analyze),
]


def policy_table(result):
    output = []
    for c in result["comparisons"]:
        group = [r for r in result["rows"] if (r["cc"],r["rtt"],r["heavy"]) == (c["cc"],c["rtt"],c["heavy"])]
        absolute = {}
        for arm in ("q0s0p0", "q1s0p0"):
            rows = [r for r in group if r["arm"] == arm]
            metrics = rtt_analyze.KEYS + ["idle_user_mean_ms", "heavy_zero_window_flows", "generator_max_lag_ms"]
            absolute[arm] = {k: statistics.median([r[k] for r in rows if r.get(k) is not None])
                             for k in metrics if any(r.get(k) is not None for r in rows)}
            absolute[arm]["inactive_flow_counts"] = [r["heavy_zero_window_flows"] for r in rows]
            absolute[arm]["missing_requests"] = sum(r.get("missing_requests",0) for r in rows)
            absolute[arm]["valid_runs"] = sum(r["valid"] for r in rows)
        output.append(dict(**c, absolute=absolute))
    return output


def collect(destination):
    destination.mkdir(parents=True, exist_ok=True)
    inventory, audits, results = [], [], {}
    for key, directory, analyzer in COHORTS:
        out = ROOT / "qa/results" / ("rtt-20261023-" + directory)
        plan = json.loads((out / "plan.json").read_text())
        if not (out / "complete.json").exists():
            raise RuntimeError(f"incomplete cohort: {out.name}")
        result = analyzer(out)
        results[key] = result
        save(destination / (key + "-summary.json"), result)
        if result["completed"] != len(plan["rows"]):
            raise RuntimeError(f"missing attempts: {out.name}")
        identity = None
        if key.startswith("final-"):
            audit = rtt_audit.audit(out)
            audits.append(audit)
            identity = audit["identity_valid"]
        rows = result["rows"]
        inventory.append(dict(key=key, directory=str(out.relative_to(ROOT)),
            binary_sha256=plan["binary_sha256"], planned=len(plan["rows"]), completed=len(rows),
            measurement_valid=sum(r["valid"] for r in rows), identity_valid=identity,
            planned_requests=sum(r.get("planned_requests",0) for r in rows),
            missing_requests=sum(r.get("missing_requests",0) for r in rows),
            throttled_events=sum(r.get("nr_throttled",0) for r in rows),
            invalid_runs=[dict(name=r["name"], errors=r["errors"]) for r in rows if not r["valid"]]))
        if key == "final-policy":
            save(destination / "policy-table.json", policy_table(result))
    # Only unchanged configurations are reused. Changed loaded cells come from
    # their new direct FIFO comparisons, never a ratio between separate stages.
    def unchanged(row):
        return row["heavy"] == 1 or row["rtt"] in (1,10)
    selected_rows = [dict(r, source_cohort="final-policy") for r in results["final-policy"]["rows"] if unchanged(r)]
    selected_rows += [dict(r, source_cohort="final-loaded") for r in results["final-loaded"]["rows"]]
    selected_comparisons = [dict(c, source_cohort="final-policy") for c in results["final-policy"]["comparisons"] if unchanged(c)]
    selected_comparisons += [dict(c, source_cohort="final-loaded") for c in results["final-loaded"]["comparisons"]]
    selected = dict(phase="configured-regimes", planned=96, completed=len(selected_rows),
        valid=sum(r["valid"] for r in selected_rows), all_valid=len(selected_rows)==96 and all(r["valid"] for r in selected_rows),
        rows=selected_rows, comparisons=selected_comparisons,
        candidate_label="Peer / 1 BDP loaded, 2 BDP single", candidate_arm="q1s0p0",
        interpretation="64 unchanged attempts reused from final-policy plus 32 new final-loaded attempts; no online transition policy was tested.")
    save(destination / "final-regimes-summary.json", selected)
    save(destination / "regimes-table.json", policy_table(selected))
    save(destination / "audit.json", audits)
    total = Counter()
    for phase in inventory:
        for k in ("planned", "completed", "measurement_valid", "planned_requests", "missing_requests", "throttled_events"):
            total[k] += phase[k]
    save(destination / "cohorts.json", dict(cohorts=inventory, counts=dict(total),
        interpretation="Counts describe retained evidence; V1 and diagnostic versions are not pooled into final estimates."))
    return dict(counts=dict(total), final_identity_valid=all(a["identity_valid"] for a in audits))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("destination", type=Path)
    a = p.parse_args()
    print(json.dumps(collect(a.destination.resolve()), indent=2))
