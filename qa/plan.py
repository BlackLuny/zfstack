"""Expand reviewed policy into a frozen, complete case-instance set."""
from __future__ import annotations

import copy
from pathlib import Path

from .common import EvidenceError, case_identity, policy_hash, read_json
from .validate import validate_plan


def make_plan(policy_dir, suite, tested_sha, base_sha=None, head_sha=None, platform="linux-x86_64", seed=1):
    policy_dir = Path(policy_dir)
    definitions = read_json(policy_dir / "cases.json")
    required = read_json(policy_dir / "required.json")
    if suite not in required["suites"]:
        raise EvidenceError("unknown suite")
    entries = {c["case_id"]: c for c in definitions["cases"]}
    if len(entries) != len(definitions["cases"]):
        raise EvidenceError("duplicate policy case")
    cases = []
    selected = required.get("platform_suites", {}).get(suite, {}).get(platform, required["suites"][suite])
    for ident in selected:
        if ident not in entries:
            raise EvidenceError(f"unknown required policy case: {ident}")
        case = copy.deepcopy(entries[ident])
        case["platform"] = platform
        case["requiredness"] = "REQUIRED"
        if case["runner"] in ("l0", "replay"):
            case["config"].setdefault("seed", seed)
        case["case_instance_id"] = case_identity(case)
        cases.append(case)
    plan = {"schema_version": 1, "suite": suite, "tested_sha": tested_sha,
            "base_sha": base_sha or tested_sha, "head_sha": head_sha or tested_sha,
            "policy_hash": policy_hash(policy_dir), "toolchain": required["toolchain"],
            "seed": seed, "cases": cases}
    return validate_plan(plan)


def check_trusted_plan(plan, policy_dir):
    validate_plan(plan)
    platforms = {c["platform"] for c in plan["cases"]}
    if len(platforms) != 1:
        raise EvidenceError("one native platform per plan; aggregate release platforms separately")
    expected = make_plan(policy_dir, plan["suite"], plan["tested_sha"], plan["base_sha"], plan["head_sha"],
                         next(iter(platforms)), plan["seed"])
    if plan != expected:
        raise EvidenceError("plan differs from trusted policy (case removal/threshold/requiredness change)")
    return expected
