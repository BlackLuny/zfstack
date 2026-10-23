#!/usr/bin/env python3
"""Independent identity/configuration audit for frozen corrected-host cohorts."""
import argparse
import json
from pathlib import Path

from run import ROOT, sha
from rtt_analyze import summarize


def audit(out):
    plan = json.loads((out / "plan.json").read_text())
    errors, observations = [], []
    for rel, digest in plan["sources"].items():
        p = out / "frozen_source" / rel
        if not p.is_file() or sha(p) != digest:
            errors.append(f"frozen source mismatch: {rel}")
    paths = sorted(out.glob("[0-9][0-9][0-9]-*"))
    planned = {r["index"]: r for r in plan["rows"]}
    seen = set()
    for p in paths:
        faults = []
        try:
            start, end, raw = [json.loads((p / name).read_text()) for name in ("start.json", "end.json", "stdout.json")]
            row, config, env, argv = start["row"], raw["config"], start["env"], start["argv"]
            if row != planned.get(row["index"]) or row["index"] in seen:
                faults.append("row identity")
            seen.add(row["index"])
            if start["binary_sha256"] != plan["binary_sha256"] or not end["binary_unchanged"]:
                faults.append("binary identity")
            binary = [t for t in argv if t.startswith("/work/target/release/zfbench")]
            if len(binary) != 1 or sha(ROOT / binary[0].removeprefix("/work/")) != plan["binary_sha256"]:
                faults.append("retained executable identity")
            arm = plan["arms"][row["arm"]]
            if any(env.get(k) != v for k, v in arm.items()):
                faults.append("arm environment")
            if env.get("ZFBENCH_RETRY_BUDGET_WRITERS") != "1" or not raw["trial_tuning"].get("retry_budget_writers"):
                faults.append("host correction not enabled")
            expected = dict(stack="zfstack-" + row["cc"], test="isolate", ports=1, lights=row["lights"],
                heavy_conns=row["heavy"], rtt_ms=row["rtt"], rate_mbps=10000 if plan["phase"] == "guard" else 100,
                queue_bytes_up=row.get("queue_bytes", 250000), queue_bytes_down=row.get("queue_bytes", 250000),
                sock_buf_kb=16384, secs=row["warmup"] + row["measure"], warmup=row["warmup"],
                idle_rr_secs=2 if row["lights"] else 0, rr_size=256, rr_interval_ms=40, seed=23,
                connect_timeout_ms=20000, budget_mb=128 if row["arm"].endswith("p2") else 0)
            for k, v in expected.items():
                if config.get(k) != v:
                    faults.append(f"configuration {k}")
            if raw["stack_thread"]["stack_stats"]["sock_buf"] != 16777216:
                faults.append("core send/receive window")
            def value(flag):
                return argv[argv.index(flag) + 1]
            for flag, expected_value in (("--rtt-ms", row["rtt"]), ("--heavy-conns", row["heavy"]),
                                         ("--secs", row["warmup"] + row["measure"]),
                                         ("--warmup", row["warmup"]),
                                         ("--queue-bytes", row.get("queue_bytes", 250000))):
                if value(flag) != str(expected_value): faults.append(f"argv {flag}")
            observed = summarize(p)
            observations.append(dict(name=p.name, identity_valid=not faults, measurement_valid=observed["valid"],
                nr_throttled=observed["nr_throttled"], throttled_usec=observed["throttled_usec"],
                missing_requests=observed.get("missing_requests", 0), planned_requests=observed.get("planned_requests", 0),
                inactive_bulk_flows=observed.get("heavy_zero_window_flows"),
                generator_max_lag_ms=observed.get("generator_max_lag_ms"),
                system_steal_sec=raw["cpu"]["system_window"].get("steal_sec"), faults=faults))
        except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
            faults.append(f"audit input: {e}")
        errors.extend(f"{p.name}: {f}" for f in faults)
    if seen != set(planned):
        errors.append("incomplete or extra attempts")
    complete = out / "complete.json"
    if not complete.exists() or json.loads(complete.read_text())["attempts"] != len(planned):
        errors.append("completion record missing/mismatch")
    return dict(cohort=out.name, planned=len(planned), audited=len(observations), source_files=len(plan["sources"]),
                binary_sha256=plan["binary_sha256"], identity_valid=not errors, errors=errors, observations=observations)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    a = p.parse_args()
    print(json.dumps(audit(a.out.resolve()), indent=2, sort_keys=True, allow_nan=False))
