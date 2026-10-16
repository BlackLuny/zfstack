"""GitHub Actions conclusion for nightly when L2 may be unprovisioned.

The trusted evaluator stays fail-closed: missing L2 evidence remains BLOCKED in
the summary. Release recomputes the full nightly suite and still requires L2
artifacts. This helper only decides whether the hosted nightly check may be green.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

L2_RUNNERS = frozenset({"l2", "l2-queue-control"})


def l2_enabled_value(raw):
    return raw == "true"


def l2_case_ids(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("cases"), list):
        raise ValueError("plan is missing cases")
    ids = set()
    for case in plan["cases"]:
        if not isinstance(case, dict):
            raise ValueError("invalid plan case")
        if case.get("runner") in L2_RUNNERS:
            ident = case.get("case_id")
            if not isinstance(ident, str) or not ident:
                raise ValueError("L2 case is missing case_id")
            ids.add(ident)
    return ids


def is_expected_l2_skip_block(block, l2_cases):
    if not isinstance(block, str):
        return False
    if block == "job l2: skipped":
        return True
    for case_id in l2_cases:
        if block == f"{case_id}: required case NOT_RUN":
            return True
        if block.startswith(f"{case_id}: functional evidence NOT_RUN"):
            return True
    return False


def hosted_nightly_decision(summary, plan, jobs, *, l2_enabled):
    """Return (exit_code, message). 0 means the GitHub job may pass."""
    if not isinstance(summary, dict) or not isinstance(jobs, dict):
        return 2, "summary or jobs JSON is invalid"
    try:
        l2_cases = l2_case_ids(plan)
    except ValueError as error:
        return 2, str(error)
    status = summary.get("status")
    failures = summary.get("failures")
    blocked = summary.get("blocked")
    if not isinstance(failures, list) or not isinstance(blocked, list):
        return 2, "summary is missing failures/blocked lists"
    if any(not isinstance(item, str) or not item for item in failures + blocked):
        return 2, "summary failures/blocked must be nonempty strings"
    if failures:
        return 1, "functional failures remain"
    l2_job = jobs.get("l2")
    l2_result = l2_job.get("result") if isinstance(l2_job, dict) else None
    if l2_enabled:
        if l2_result != "success":
            return 2, f"L2 enabled; job l2 result is {l2_result!r}"
        if status != "PASS":
            return 2, f"L2 enabled; nightly must be PASS, got {status!r}"
        if blocked:
            return 2, "L2 enabled; blocked evidence remains"
        return 0, "full nightly passed"
    if l2_result != "skipped":
        return 2, f"L2 not provisioned; expected job l2 skipped, got {l2_result!r}"
    if not l2_cases:
        return 2, "plan has no L2 cases; hosted skip allowance does not apply"
    unexpected = [item for item in blocked if not is_expected_l2_skip_block(item, l2_cases)]
    if unexpected:
        return 2, "blocked beyond skipped L2: " + "; ".join(unexpected)
    if status not in {"BLOCKED", "PASS"}:
        return 2, f"unexpected nightly status {status!r}"
    return 0, "hosted nightly passed; L2 not certified"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Hosted nightly GitHub conclusion; evaluate remains fail-closed")
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--l2-enabled", default="")
    args = parser.parse_args(argv)
    try:
        summary = json.loads(args.summary.read_text())
        plan = json.loads(args.plan.read_text())
        jobs = json.loads(args.jobs.read_text())
    except (OSError, json.JSONDecodeError) as error:
        print(f"QA BLOCKED: {error}", file=sys.stderr)
        return 2
    code, message = hosted_nightly_decision(summary, plan, jobs, l2_enabled=l2_enabled_value(args.l2_enabled))
    stream = sys.stdout if code == 0 else sys.stderr
    print(("QA PASS: " if code == 0 else "QA BLOCKED: ") + message, file=stream)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
