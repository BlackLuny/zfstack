from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .build_manifest import build_manifest, command
from .common import ROOT, EvidenceError, read_json, write_json
from .coverage import audit
from .environment import platform_id
from .evaluate import evaluate
from .plan import make_plan
from .render_report import render
from .run import run_plan
from .validate import validate_plan, validate_result


def main(argv=None):
    parser = argparse.ArgumentParser(description="zfstack QA: immutable plans, typed runners, fail-closed evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("make-plan")
    plan.add_argument("--suite", default="pr")
    plan.add_argument("--tested-sha")
    plan.add_argument("--base-sha")
    plan.add_argument("--head-sha")
    plan.add_argument("--platform", default=platform_id())
    plan.add_argument("--seed", type=int, default=1)
    plan.add_argument("--policy", type=Path, default=ROOT / "qa/policy")
    plan.add_argument("--output", type=Path, required=True)
    for kind in ("validate-plan", "validate-result"):
        item = sub.add_parser(kind)
        item.add_argument("path", type=Path)
    build = sub.add_parser("build-manifest")
    build.add_argument("--toolchain", default="1.94.1")
    build.add_argument("--profile", default="release")
    build.add_argument("--feature", action="append", default=[])
    build.add_argument("--binary", action="append", default=[])
    build.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run-plan")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--case", action="append", default=[])
    run.add_argument("--policy", type=Path, default=ROOT / "qa/policy")
    run.add_argument("--node", type=Path)
    run.add_argument("--lease-dir", type=Path)
    gate = sub.add_parser("evaluate")
    gate.add_argument("--plan", type=Path, required=True)
    gate.add_argument("--results", type=Path, required=True)
    gate.add_argument("--policy", type=Path, default=ROOT / "qa/policy")
    gate.add_argument("--expected-sha", required=True)
    gate.add_argument("--jobs", type=Path)
    gate.add_argument("--ci", action="store_true")
    gate.add_argument("--output", type=Path, required=True)
    gate.add_argument("--report", type=Path)
    report = sub.add_parser("report")
    report.add_argument("path", type=Path)
    report.add_argument("--output", type=Path)
    sub.add_parser("audit-coverage")
    args = parser.parse_args(argv)
    try:
        if args.command == "make-plan":
            sha = args.tested_sha or command(["git", "rev-parse", "HEAD"], ROOT)
            value = make_plan(args.policy, args.suite, sha, args.base_sha, args.head_sha, args.platform, args.seed)
            write_json(args.output, value, exclusive=True)
        elif args.command.startswith("validate-"):
            (validate_plan if args.command == "validate-plan" else validate_result)(read_json(args.path))
            print("valid")
        elif args.command == "build-manifest":
            write_json(args.output, build_manifest(ROOT, args.toolchain, args.profile, args.feature, args.binary), exclusive=True)
        elif args.command == "run-plan":
            values = run_plan(read_json(args.plan), ROOT, args.output, args.policy, args.case,
                              read_json(args.node) if args.node else None, args.lease_dir)
            return 0 if all(v["functional"]["status"] == "PASS" and v["validity"]["functional"] == "VALID" for v in values) else 1
        elif args.command == "evaluate":
            value = evaluate(read_json(args.plan), sorted(args.results.rglob("result.json")), args.policy,
                             expected_sha=args.expected_sha, jobs=read_json(args.jobs) if args.jobs else None, ci=args.ci)
            write_json(args.output, value)
            text = render(value)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(text)
            print(text)
            return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[value["status"]]
        elif args.command == "report":
            text = render(read_json(args.path))
            if args.output:
                args.output.write_text(text)
            else:
                print(text)
        elif args.command == "audit-coverage":
            print(json.dumps(audit(ROOT), indent=2))
    except (EvidenceError, OSError, KeyError, ValueError) as error:
        print(f"QA BLOCKED: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
