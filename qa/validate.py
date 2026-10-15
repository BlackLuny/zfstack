"""JSON Schema plus cross-field contracts. Missing evidence never means zero."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from jsonschema import Draft202012Validator

from .common import ROOT, EvidenceError, case_identity, finite, read_json


def schema_validate(value, name):
    finite(value)
    schema = read_json(ROOT / "qa/schema" / f"{name}.json")
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        raise EvidenceError("; ".join(f"{list(e.path)}: {e.message}" for e in errors[:10]))
    return value


def validate_plan(plan):
    schema_validate(plan, "plan-v1")
    ids = set()
    for case in plan["cases"]:
        if "service_contract" in case:
            if case["runner"] not in ("l2", "l2-queue-control"):
                raise EvidenceError("service contract requires an L2 runner with raw per-peer evidence")
            from .service import validate_contract
            validate_contract(case["service_contract"])
        ident = case["case_instance_id"]
        if ident != case_identity(case):
            raise EvidenceError(f"{case['case_id']}: case identity mismatch")
        if ident in ids:
            raise EvidenceError("duplicate case instance")
        ids.add(ident)
        performance = case["performance"]
        if not set(performance.get("absolute_slos", {})).issubset(performance["metrics"]):
            raise EvidenceError("absolute service contract refers to an unregistered primary metric")
        if performance.get("absolute_slos") and case["runner"] != "external-performance":
            raise EvidenceError("absolute metric contracts require an audited performance experiment")
        if performance["mode"] == "enforce" and (not performance.get("calibration_id") or not performance.get("experiment_plan_hash")):
            raise EvidenceError("enforcement requires frozen calibration and experiment plan identities")
        if performance["mode"] == "not_applicable" and performance["metrics"]:
            raise EvidenceError("N/A cannot declare performance metrics")
        if case["requiredness"] == "EXEMPTED" and not case.get("exemption_id"):
            raise EvidenceError("exempted case needs a policy exemption")
    if not any(c["requiredness"] == "REQUIRED" for c in plan["cases"]):
        raise EvidenceError("plan requires at least one required case")
    return plan


def validate_result(result):
    schema_validate(result, "result-v2")
    execution = result["execution"]
    if execution["end_ns"] < execution["start_ns"]:
        raise EvidenceError("monotonic interval goes backwards")
    assertions = result["functional"]["assertions"]
    ids = [a["id"] for a in assertions]
    if len(ids) != len(set(ids)):
        raise EvidenceError("duplicate assertion")
    if result["functional"]["status"] == "PASS":
        if not assertions or any(a["status"] != "PASS" for a in assertions):
            raise EvidenceError("functional PASS needs nonempty passing assertions")
        if execution["status"] != "completed" or execution["exit_code"] != 0:
            raise EvidenceError("functional PASS contradicts execution")
    if result["performance"]["status"] in ("PASS", "FAIL"):
        if not result["performance"]["analysis_hash"]:
            raise EvidenceError("performance verdict requires analysis evidence")
    return result


def validate_exemption(exemption, case, now=None):
    """Only exact, reviewed, time-limited assertions; safety failures are ineligible."""
    expected = {"id", "case_instance_id", "assertion_id", "expires", "owner", "approval", "reason"}
    if not isinstance(exemption, dict) or set(exemption) != expected:
        raise EvidenceError("invalid exemption contract")
    if any(not isinstance(v, str) or not v for v in exemption.values()):
        raise EvidenceError("empty exemption fields")
    if exemption["case_instance_id"] != case["case_instance_id"]:
        raise EvidenceError("exemption scope mismatch")
    try:
        expiry = dt.datetime.fromisoformat(exemption["expires"].replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError("invalid exemption expiry") from error
    if expiry.tzinfo is None or expiry <= (now or dt.datetime.now(dt.timezone.utc)):
        raise EvidenceError("expired or timezone-free exemption")
    return exemption
