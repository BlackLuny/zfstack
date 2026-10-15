#!/usr/bin/env python3
"""Export any L2 case ID to QA result v2. Missing evidence stays unknown/invalid.

Identity and lifecycle come from trusted orchestration, never from a legacy
``ok`` bit. A single run has no statistical performance conclusion. Feed its
observations to the separately frozen ABBA/BAAB experiment plan.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from evidence import canonical, digest, inspect_case, read_json


IDENTITY = ("run_id", "block_id", "arm", "case_id", "case_version", "case_instance_id",
            "tested_sha", "plan_hash", "build_manifest_hash", "environment_hash", "config_hash")


def export_case(path, identity):
    missing = [key for key in (*IDENTITY, "execution") if key not in identity]
    if missing:
        raise ValueError("orchestrator identity is missing: " + ", ".join(missing))
    if identity["arm"] not in ("A", "B"):
        raise ValueError("arm must be A or B (same binary is permitted for an explicit A/A plan)")
    execution = identity["execution"]
    if not isinstance(execution, dict) or any(key not in execution for key in
                                            ("status", "exit_code", "start_ns", "end_ns", "clock", "cleanup")):
        raise ValueError("orchestrator execution lifecycle is incomplete")
    if (type(execution["start_ns"]) is not int or type(execution["end_ns"]) is not int
            or execution["start_ns"] < 0 or execution["end_ns"] < execution["start_ns"]
            or not isinstance(execution["clock"], str) or not execution["clock"]):
        raise ValueError("orchestrator execution timestamps / clock are invalid")
    case = inspect_case(path)
    result = case["result"]
    errors = list(case["performance_errors"])
    if identity["config_hash"] != digest(case["config"]):
        errors.append("effective configuration differs from the orchestrator config hash")
    expected_binary = identity.get("binary_sha256")
    if expected_binary is None or expected_binary != case["binary_sha256"]:
        errors.append("binary identity is missing or differs from the build manifest")
    known_failure = bool(case["functional_errors"])
    identity_mismatch = identity["config_hash"] != digest(case["config"]) or expected_binary != case["binary_sha256"]
    complete = (isinstance(result.get("ok"), bool) and bool(case["raw"]) and
                not case["functional_evidence_errors"] and not identity_mismatch)
    functional_status = "FAIL" if known_failure else "PASS" if complete else "NOT_RUN"
    assertions = [{"id": f"L2.observed_failure.{i}", "status": "FAIL", "kind": "execution", "detail": error}
                  for i, error in enumerate(case["functional_errors"])]
    if not assertions:
        assertions = [{"id": "L2.load_contract", "status": functional_status, "kind": "execution",
                       "detail": "Recorded workload completion and payload checks" if complete else "Missing workload completion evidence"}]
    artifacts = []
    for name in ("result.json", "raw.json.gz", "analysis.json", "worker.sha256", "build-info.json", "qdisc.json"):
        item = Path(path) / name
        if item.is_file():
            hasher, size = hashlib.sha256(), 0
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk); size += len(chunk)
            artifacts.append({"path": name, "sha256": hasher.hexdigest(), "size_bytes": size})
    envelope = {key: identity[key] for key in IDENTITY}
    envelope.update(schema_version=2,
                    validity={"functional": "VALID" if complete or known_failure else "UNKNOWN",
                              "performance": "INVALID" if errors else "VALID", "reasons": errors},
                    functional={"status": functional_status, "assertions": assertions},
                    performance={"status": "INCONCLUSIVE", "metrics": case["metrics"], "analysis_hash": None},
                    attribution=identity.get("attribution", "UNATTRIBUTED"),
                    execution=identity["execution"], artifacts=artifacts,
                    observations={"source_format": "l2-legacy" if not case["config_complete"] else "l2-effective-config",
                                  "original_ok": result.get("ok"), "effective_config": case["config"],
                                  "binary_sha256": case["binary_sha256"], "position": identity.get("position"), "worker": result.get("worker"),
                                  "heavy_bytes": result.get("heavy_bytes"), "light_users": result.get("light_users"),
                                  "udp": result.get("udp"), "qdisc": result.get("qdisc"),
                                  "path_bytes": result.get("path_bytes"), "load_counters": result.get("load_counters"),
                                  "startup_anomalies": result.get("startup_anomalies"),
                                  "endpoint_diagnostics": case["endpoint_diagnostics"],
                                  "performance_contaminated": result.get("performance_contaminated")})
    canonical(envelope)
    return envelope


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("case", type=Path)
    p.add_argument("--identity", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    try:
        result = export_case(a.case, read_json(a.identity))
        a.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"L2 export rejected: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
