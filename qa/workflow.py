"""GitHub glue; untrusted execution and trusted policy/reporting stay distinct."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from .common import ROOT, EvidenceError, digest, read_json, write_json
from .plan import make_plan
from .run import run_plan
from .archive import stage

GROUPS = {
    "lint": {"lint", "coverage", "feature-audit"},
    "test": {"core-build", "core-test", "adapter-test", "adapter-qa", "budget-qa"},
    "bench": {"harness-test", "bench-build", "bench-test", "bench-all-test"},
    "l0": {"l0", "replay"},
    "tools": {"python-qa", "python-l2", "python-perf"},
    "extended": {"l0", "replay", "ignored", "loom", "core-build", "core-test", "adapter-test", "adapter-qa", "budget-qa",
                 "harness-test", "bench-build", "bench-test", "bench-all-test", "lint", "coverage", "feature-audit", "python-qa", "python-l2", "python-perf"},
    "l1": {"l1"}, "l2": {"l2", "l2-queue-control"}, "performance": {"external-performance"},
}


def prepare_policy(ref, destination):
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise EvidenceError("trusted ref must be a full immutable commit SHA")
    names = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, "qa/policy"], cwd=ROOT,
                           capture_output=True, text=True, check=True).stdout.splitlines()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    if not names:
        # Allows testing the first implementation. Reporter does NOT accept this
        # as trust: qa/gate remains BLOCKED until a reviewed QA ref is configured.
        for file in (ROOT / "qa/policy").glob("*.json"):
            write_json(destination / file.name, read_json(file), exclusive=True)
        return False
    for name in names:
        relative = Path(name).relative_to("qa/policy")
        if len(relative.parts) != 1 or relative.suffix != ".json":
            raise EvidenceError("unexpected trusted policy path")
        text = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout
        write_json(destination / relative, json.loads(text), exclusive=True)
    return True


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare-policy")
    prepare.add_argument("--ref", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    group = sub.add_parser("run-group")
    group.add_argument("group", choices=sorted(GROUPS))
    group.add_argument("--plan", type=Path, required=True)
    group.add_argument("--policy", type=Path, required=True)
    group.add_argument("--output", type=Path, required=True)
    group.add_argument("--node", type=Path)
    group.add_argument("--lease-dir", type=Path)
    upload = sub.add_parser("stage-upload")
    upload.add_argument("--source", type=Path, required=True)
    upload.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if args.command == "stage-upload":
        stage(args.source, args.output)
        return 0
    if args.command == "prepare-policy":
        trusted = prepare_policy(args.ref, args.output)
        print("trusted base policy" if trusted else "BOOTSTRAP: execution-only candidate policy; trusted gate unavailable")
        return 0
    plan = read_json(args.plan)
    ids = [c["case_id"] for c in plan["cases"] if c["runner"] in GROUPS[args.group]]
    if not ids:
        raise EvidenceError("empty CI group")
    results = run_plan(plan, ROOT, args.output, args.policy, ids, read_json(args.node) if args.node else None, args.lease_dir)
    return 0 if all(r["functional"]["status"] == "PASS" and r["validity"]["functional"] == "VALID" for r in results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(f"QA BLOCKED: {error}")
