"""One fail-closed gate for local, CI and release evidence.

FAIL is an observed contract violation. BLOCKED means required evidence is
missing/invalid/inconclusive. Both return nonzero. Attribution never waives a
failure, and performance contamination never erases a functional failure.
"""
from __future__ import annotations

from pathlib import Path

from .common import EvidenceError, digest, file_hash, read_json, safe_path
from .plan import check_trusted_plan
from .validate import validate_exemption, validate_result
from .performance import verify_performance
from .build_manifest import verify_manifest
from .integration import verify_integration


def verify_artifacts(result, directory):
    seen = set()
    for artifact in result["artifacts"]:
        if artifact["path"] in seen:
            raise EvidenceError("duplicate artifact")
        seen.add(artifact["path"])
        path = safe_path(directory, artifact["path"])
        if not path.is_file() or path.stat().st_size != artifact["size_bytes"] or file_hash(path) != artifact["sha256"]:
            raise EvidenceError(f"artifact missing/tampered: {artifact['path']}")
    if not seen:
        raise EvidenceError("empty artifact inventory")
    for name, field in (("build-manifest.json", "build_manifest_hash"), ("environment.json", "environment_hash")):
        value = read_json(safe_path(directory, name))
        if digest(value) != result[field]:
            raise EvidenceError(f"{name} identity mismatch")
    manifest = read_json(directory / "build-manifest.json")
    source = manifest.get("source", {})
    if source.get("sha") != result["tested_sha"]:
        raise EvidenceError("build manifest has wrong tested SHA")
    if source.get("dirty") is not False or manifest.get("identity_error"):
        raise EvidenceError("build is dirty or its identity is incomplete")
    return manifest


def verify_execution_evidence(result, case, directory, policy_dir):
    """A selected test name / runner PASS is not evidence that any test ran."""
    if case["runner"] == "external-performance":
        return  # Its raw frozen slots are checked by the performance verifier.
    inventory = {a["path"] for a in result["artifacts"]}
    safety_required = {"fuzz": {"fuzz-seeds.json", "fuzz-artifacts.json", "safety-capability.json"},
                       "sanitizer": {"safety-capability.json"}}.get(case["runner"], set())
    missing = safety_required - inventory
    if missing:
        raise EvidenceError("missing hashed safety artifacts: " + ", ".join(sorted(missing)))
    if "execution.json" not in inventory:
        raise EvidenceError("hashed execution lifecycle artifact is required")
    execution = read_json(directory / "execution.json")
    records = execution.get("commands")
    if not isinstance(records, list):
        raise EvidenceError("missing typed command execution records")
    if case["runner"] == "coverage":
        if "coverage.json" not in inventory:
            raise EvidenceError("missing typed coverage audit")
        from .coverage import verify_audit
        verify_audit(read_json(directory / "coverage.json"), policy_dir)
    elif not records:
        raise EvidenceError("no executed commands")
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not record.get("argv") or not isinstance(record["argv"], list):
            raise EvidenceError("invalid command identity")
        if f"command-{index}.log" not in inventory:
            raise EvidenceError("missing hashed command output")
        if result["functional"]["status"] == "PASS" and (record.get("status") != "completed" or record.get("exit_code") != 0
                or record.get("cleanup_status") != "PASS"):
            raise EvidenceError("reported PASS contradicts child lifecycle / cleanup")
    if case["runner"] not in ("l1", "l2", "l2-queue-control"):
        from .run import _functional_evidence
        actual, _ = _functional_evidence(case, directory, records)
        if actual != result["functional"]["status"] or actual == "NOT_RUN":
            raise EvidenceError("independent execution / nonempty test evidence validation failed")


def evaluate(plan, result_paths, policy_dir, *, expected_sha, jobs=None, ci=False, now=None):
    failures, blocked, notes, rows = [], [], [], []
    try:
        check_trusted_plan(plan, policy_dir)
        if expected_sha != plan["tested_sha"]:
            raise EvidenceError("expected/tested SHA mismatch")
    except EvidenceError as error:
        return {"schema_version": 1, "status": "BLOCKED", "tested_sha": expected_sha,
                "failures": [], "blocked": [str(error)], "cases": [], "notes": [],
                "coverage": {"required": 0, "completed": 0}, "plan_hash": digest(plan)}
    required_policy = read_json(Path(policy_dir) / "required.json")
    if ci:
        required_jobs = set(required_policy.get("jobs", {}).get(plan["suite"], []))
        if not required_jobs:
            blocked.append("CI suite has no registered job set")
        if not isinstance(jobs, dict) or set(jobs) != required_jobs:
            blocked.append("required CI job set missing or different")
        for name in required_jobs:
            value = (jobs or {}).get(name, {})
            if not isinstance(value, dict) or value.get("result") != "success":
                blocked.append(f"job {name}: {value.get('result', 'missing') if isinstance(value, dict) else 'invalid'}")
    expected = {case["case_instance_id"]: case for case in plan["cases"]}
    received = {}
    for path in result_paths:
        path = Path(path)
        try:
            result = validate_result(read_json(path))
            ident = result["case_instance_id"]
            if ident in received:
                raise EvidenceError("duplicate case-instance result; retries must not replace or mask earlier evidence")
            if ident not in expected:
                raise EvidenceError("unexpected case-instance")
            case = expected[ident]
            if any(result[key] != value for key, value in
                   (("tested_sha", expected_sha), ("plan_hash", digest(plan)),
                    ("case_id", case["case_id"]), ("case_version", case["case_version"]),
                    ("config_hash", digest(case["config"])))):
                raise EvidenceError("case/plan/config/source identity mismatch")
            received[ident] = result
            try:
                manifest = verify_artifacts(result, path.parent)
                verify_manifest(manifest, case, plan, read_json(path.parent / "environment.json"))
                verify_execution_evidence(result, case, path.parent, policy_dir)
                verify_integration(result, case, path.parent)
                if case["runner"] == "external-performance" or case["performance"]["mode"] == "enforce":
                    verify_performance(result, case, path.parent, policy_dir=policy_dir)
            except (OSError, EvidenceError, KeyError, TypeError, ValueError) as error:
                blocked.append(f"{case['case_id']}: {error}")
        except (OSError, EvidenceError) as error:
            blocked.append(f"{path.name}: {error}")
    exemptions = read_json(Path(policy_dir) / "known-failures.json").get("exemptions", [])
    completed = 0
    for ident, case in expected.items():
        name = case["case_id"]
        result = received.get(ident)
        if result is None:
            if case["requiredness"] != "OPTIONAL":
                blocked.append(f"{name}: required case NOT_RUN")
            rows.append({"case_id": name, "layer": case["layer"], "functional": "NOT_RUN", "validity": "UNKNOWN",
                         "performance": "INCONCLUSIVE", "requiredness": case["requiredness"]})
            continue
        functional = result["functional"]["status"]
        if functional != "NOT_RUN":
            completed += 1
        invalid = result["validity"]["functional"] != "VALID"
        assertions = [a for a in result["functional"]["assertions"] if a["status"] == "FAIL"]
        if functional == "FAIL":
            unwaived = []
            for assertion in assertions or [{"id": "functional", "kind": "safety"}]:
                match = next((e for e in exemptions if e.get("case_instance_id") == ident and e.get("assertion_id") == assertion["id"]), None)
                if match and assertion["kind"] != "safety":
                    try:
                        validate_exemption(match, case, now)
                        notes.append(f"{name}/{assertion['id']}: reviewed exemption {match['id']} until {match['expires']}")
                        continue
                    except EvidenceError as error:
                        notes.append(f"rejected exemption: {error}")
                unwaived.append(assertion["id"])
            if unwaived:
                failures.append(f"{name}: functional FAIL ({', '.join(unwaived)}); attribution={result['attribution']}")
        if case["requiredness"] != "OPTIONAL":
            if functional == "NOT_RUN" or invalid:
                blocked.append(f"{name}: functional evidence {functional}/{result['validity']['functional']}")
            execution = result["execution"]
            if execution["status"] != "completed" or execution["exit_code"] != 0 or execution["cleanup"] != "PASS":
                blocked.append(f"{name}: execution/cleanup incomplete ({execution['status']}, {execution['cleanup']})")
        mode, performance = case["performance"]["mode"], result["performance"]["status"]
        if mode == "not_applicable" and performance != "NOT_APPLICABLE":
            blocked.append(f"{name}: performance applicability differs from trusted plan")
        if mode != "not_applicable" and performance == "NOT_APPLICABLE":
            blocked.append(f"{name}: runner cannot declare planned performance N/A")
        if mode == "enforce":
            if performance == "FAIL":
                failures.append(f"{name}: performance FAIL")
            if performance == "INCONCLUSIVE" or result["validity"]["performance"] != "VALID":
                blocked.append(f"{name}: required performance inconclusive/invalid")
            if not set(case["performance"]["metrics"]).issubset(result["performance"]["metrics"]):
                blocked.append(f"{name}: missing primary metric")
            # A bare self-reported PASS is never sufficient: evaluator versions
            # must explicitly support a calibrated analysis contract.
            analysis = result["performance"]["analysis_hash"]
            if analysis is None or analysis not in {a["sha256"] for a in result["artifacts"]}:
                blocked.append(f"{name}: calibrated analysis artifact missing")
        elif mode == "shadow":
            notes.append(f"{name}: performance SHADOW ({performance}); no enforced non-inferiority claim")
        rows.append({"case_id": name, "layer": case["layer"], "functional": functional,
                     "validity": result["validity"], "performance": performance,
                     "requiredness": case["requiredness"], "attribution": result["attribution"],
                     "run_id": result["run_id"], "case_instance_id": ident,
                     "build_manifest_hash": result["build_manifest_hash"], "environment_hash": result["environment_hash"],
                     "config_hash": result["config_hash"], "performance_metrics": result["performance"]["metrics"],
                     "assertion_details": [a["detail"] for a in result["functional"]["assertions"] if a["status"] != "PASS"]})
    return {"schema_version": 1, "status": "FAIL" if failures else "BLOCKED" if blocked else "PASS",
            "tested_sha": expected_sha, "plan_hash": digest(plan), "policy_hash": plan["policy_hash"],
            "coverage": {"required": sum(c["requiredness"] != "OPTIONAL" for c in plan["cases"]), "completed": completed},
            "failures": failures, "blocked": blocked, "notes": notes, "cases": rows,
            "scope": "Only the declared case instances and native platform; shadow metrics are not performance certification."}
