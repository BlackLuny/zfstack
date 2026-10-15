"""Run prebuilt frozen performance experiments and publish a QA result-v2."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid

from .build_manifest import source_identity
from .archive import calibration_paths
from .common import EvidenceError, digest, file_hash, read_json, safe_path, write_json
from .environment import capability_errors, collect_environment, platform_id
from .performance import check_experiment, engine, inspect_rows, read_experiment_rows
from .process import run_child
from .validate import validate_result


def _input(node, name):
    value = node.get(name)
    if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
        raise EvidenceError(f"missing absolute node input: {name}")
    return Path(value).resolve()


def _inventory(output):
    items = []
    for path in sorted(output.rglob("*")):
        if path.is_symlink():
            raise EvidenceError("symlink cannot be sealed as performance evidence")
        if path.is_file() and path.name != "result.json":
            relative = path.relative_to(output).as_posix()
            safe_path(output, relative)
            items.append({"path": relative, "sha256": file_hash(path), "size_bytes": path.stat().st_size})
    return items


def _copy_calibration(source, output):
    if not isinstance(source, str) or not Path(source).is_absolute() or not Path(source).is_dir():
        raise EvidenceError("enforce requires an absolute calibration_directory")
    source = Path(source)
    spec = read_json(safe_path(source, "calibration-spec.json"))
    paths = calibration_paths(spec)
    if len(paths) > 4096:
        raise EvidenceError("calibration tree exceeds artifact count budget")
    total = 0
    for relative in sorted(paths):
        original, destination = safe_path(source, relative), safe_path(output, relative)
        total += original.stat().st_size
        if original.stat().st_size > 64 * 1024 * 1024 or total > 2 * 1024 ** 3:
            raise EvidenceError("calibration tree exceeds bounded import budget")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with original.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
    return read_json(output / "calibration.json")


def _verify_staged_calibration(output, result, case, policy_dir, generation):
    """The public verifier requires a sealed result; never publish provisional PASS."""
    from .release import verify_calibration
    with tempfile.TemporaryDirectory(prefix=".calibration-verify-", dir=output.parent) as temporary:
        stage = Path(temporary)
        for artifact in result["artifacts"]:
            path = safe_path(stage, artifact["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            os.link(safe_path(output, artifact["path"]), path)
        write_json(stage / "result.json", result, exclusive=True)
        return verify_calibration(stage, case, policy_dir, generation=generation)


def run_performance_case(plan, case, root, output, node=None, lease_dir=None, policy_dir=None):
    """No builds or nested lease: the fixed runner acquires the physical domain."""
    root, output, node = Path(root).resolve(), Path(output).resolve(), node or {}
    output.mkdir(parents=True, exist_ok=False)
    start, run_id = time.monotonic_ns(), str(uuid.uuid4())
    outer_environment = collect_environment()
    manifest = {"schema_version": 1, "identity_error": "original prebuilt manifest unavailable"}
    experiment, perf_environment, rows, analysis, audit = None, None, [], None, None
    before, after, runner, calibration = None, None, None, None
    reasons, status, exit_code, cleanup = [], "capability_missing", None, "PASS"
    functional, functional_validity, performance_validity = "NOT_RUN", "UNKNOWN", "UNKNOWN"
    observations = {"scope": case["layer"], "node_generation": node.get("generation"),
                    "physical_domain": node.get("physical_domain"), "phases": ["PREPARE"]}
    try:
        # Retain available immutable inputs even if a later capability is absent.
        manifest = read_json(_input(node, "build_manifest"))
        experiment = read_json(_input(node, "performance_plan"))
        perf_environment = read_json(_input(node, "performance_environment"))
        for name, value in (("perf-plan.json", experiment), ("perf-environment.json", perf_environment)):
            write_json(output / name, value, exclusive=True)
        errors = capability_errors("external-performance", node)
        if case["platform"] != platform_id():
            errors.append("native platform differs from the requested performance case")
        if not lease_dir or not Path(lease_dir).is_absolute():
            errors.append("absolute physical-domain lease directory is required")
        if not isinstance(node.get("expected_plan_hash"), str):
            errors.append("expected_plan_hash must come from trusted node configuration")
        if errors:
            raise EvidenceError("; ".join(errors))
        check_experiment(experiment, case, manifest, outer_environment, perf_environment, plan["tested_sha"],
                         expected_hash=node["expected_plan_hash"])
        before = source_identity(root)
        if before != manifest["source"]:
            raise EvidenceError("working source differs from original prebuilt manifest")
        binaries = {"A": _input(node, "baseline_binary"), "B": _input(node, "candidate_binary")}
        for arm, binary in binaries.items():
            if file_hash(binary) != experiment["binary_sha256"][arm]:
                raise EvidenceError(f"{arm}: actual binary differs from frozen plan")
        candidate = binaries["B"]
        if not any(item == {"name": candidate.name, "size_bytes": candidate.stat().st_size,
                            "sha256": experiment["binary_sha256"]["B"]} for item in manifest["binaries"]):
            raise EvidenceError("actual candidate file differs from original build manifest")
        if case["performance"]["mode"] == "enforce":
            calibration = _copy_calibration(node.get("calibration_directory"), output)
        command = [sys.executable, str(root / "bench/pareto/run_ab.py"), "--plan", str(output / "perf-plan.json"),
                   "--expected-plan-hash", node["expected_plan_hash"], "--baseline", str(binaries["A"]),
                   "--candidate", str(candidate), "--environment", str(output / "perf-environment.json"),
                   "--lease-directory", str(Path(lease_dir).resolve()), "--physical-domain", node["physical_domain"],
                   "--generation", node["generation"], "--outdir", str(output / "runs")]
        observations["phases"].append("MEASURE")
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        runner = run_child(command, root, output / "runner.log", case["timeout_seconds"], environment)
        status, exit_code, cleanup = runner["status"], runner["exit_code"], runner["cleanup_status"]
        if runner.get("cleanup_scope") != "linux-subreaper" or runner.get("cleanup_limits"):
            reasons.append("outer performance runner lacks full Linux descendant containment")
        after = source_identity(root)
        if after != before:
            reasons.append("source changed during performance execution")
        if any(file_hash(binary) != experiment["binary_sha256"][arm] for arm, binary in binaries.items()):
            reasons.append("immutable binary changed during performance execution")
        for name, expected in (("plan.json", experiment), ("environment.json", perf_environment)):
            if read_json(output / "runs" / name) != expected:
                reasons.append("runner frozen input differs from admitted input: " + name)
        source_rows = safe_path(output, "runs/runs.jsonl")
        if source_rows.stat().st_size > 64 * 1024 * 1024:
            raise EvidenceError("raw run rows exceed analysis byte budget")
        with source_rows.open("rb") as reader, (output / "perf-rows.jsonl").open("xb") as writer:
            shutil.copyfileobj(reader, writer, 1024 * 1024)
        rows, parse_errors = read_experiment_rows(output / "perf-rows.jsonl")
        audit = inspect_rows(experiment, rows, output, perf_environment)
        audit["errors"].extend(parse_errors)
        if parse_errors and audit["functional"] != "FAIL":
            audit["functional"] = "NOT_RUN"
        functional = audit["functional"]
        reasons.extend(audit["errors"])
        if any(not isinstance(row, dict) or row.get("execution", {}).get("cleanup") != "PASS" for row in rows):
            cleanup = "UNKNOWN" if cleanup == "PASS" else cleanup
        if functional == "PASS" and (reasons or status != "completed" or exit_code != 0 or cleanup != "PASS"):
            functional = "NOT_RUN"
        functional_validity = "VALID" if functional in ("PASS", "FAIL") and not reasons else "UNKNOWN"
        observations["phases"].append("VALIDATE")
    except (OSError, EvidenceError, ValueError, KeyError, TypeError, ImportError, AttributeError, IndexError, RecursionError) as error:
        reasons.append(f"{type(error).__name__}: {error}")
        if runner is not None:
            status, exit_code = "failed", runner.get("exit_code")
    except (KeyboardInterrupt, InterruptedError) as error:
        reasons.append(f"cancelled: {error}")
        status, exit_code, cleanup = "cancelled", None, "UNKNOWN"
    # Independent candidate failures survive incomplete/invalid companion data.
    if audit and audit["failures"]:
        functional = "FAIL"
    if experiment and not engine().validate_plan(experiment):
        try:
            analysis = engine().evaluate(experiment, rows, calibration)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as error:
            # Malformed records remain archived. Derive no statistics from a
            # replacement subset; retain any independently observed failure.
            analysis = engine().evaluate(experiment, [], None)
            analysis["functional"] = "FAIL" if audit and audit["failures"] else "NOT_RUN"
            analysis["decision"] = "FAIL" if analysis["functional"] == "FAIL" else "BLOCKED"
            reasons.append("performance record structure invalid: " + str(error))
            analysis["errors"].append(reasons[-1])
        if audit is None:
            audit = {"functional": "NOT_RUN", "errors": ["experiment did not reach raw-evidence validation"],
                     "functional_errors": [], "failures": []}
        performance_validity = analysis["validity"] if not reasons else "INVALID"
        write_json(output / "perf-analysis.json", analysis, exclusive=True)
        write_json(output / "perf-audit.json", audit, exclusive=True)
        observations.update(performance_plan_hash=digest(experiment), performance_environment_hash=digest(perf_environment),
                            performance_config_hash=digest(experiment["config"]), qa_config_hash=digest(case["config"]))
        if analysis["functional"] == "FAIL":
            functional = "FAIL"
    write_json(output / "build-manifest.json", manifest, exclusive=True)
    write_json(output / "environment.json", outer_environment, exclusive=True)
    write_json(output / "execution.json", {"runner": runner, "source_before": before, "source_after": after,
                                           "admission_errors": reasons}, exclusive=True)
    if functional == "PASS" and status != "completed":
        functional = "NOT_RUN"
    result = {"schema_version": 2, "run_id": run_id, "block_id": "experiment", "arm": "B",
              "case_id": case["case_id"], "case_version": case["case_version"], "case_instance_id": case["case_instance_id"],
              "tested_sha": plan["tested_sha"], "plan_hash": digest(plan), "build_manifest_hash": digest(manifest),
              "environment_hash": digest(outer_environment), "config_hash": digest(case["config"]),
              "validity": {"functional": functional_validity, "performance": performance_validity,
                           "reasons": [reason[:1024] for reason in reasons]},
              "functional": {"status": functional, "assertions": [{"id": "performance.raw_probe_contracts", "status": functional,
                    "kind": "safety" if functional == "FAIL" else "execution",
                    "detail": "Every frozen slot was checked against raw probe output; failures are retained." if audit else "No admitted experiment evidence."}]},
              "performance": {"status": analysis["performance"] if analysis else "INCONCLUSIVE",
                              "metrics": analysis["metrics"] if analysis else {},
                              "analysis_hash": file_hash(output / "perf-analysis.json") if analysis else None},
              "attribution": "UNATTRIBUTED", "execution": {"status": status, "exit_code": exit_code, "start_ns": start,
                    "end_ns": time.monotonic_ns(), "clock": "CLOCK_MONOTONIC", "cleanup": cleanup},
              "artifacts": _inventory(output), "observations": observations}
    if case["performance"]["mode"] == "enforce" and calibration is not None and analysis is not None:
        try:
            verified = _verify_staged_calibration(output, result, case, policy_dir, node.get("generation"))
            if verified != calibration:
                raise EvidenceError("independently verified calibration differs from input")
        except (OSError, EvidenceError, ValueError, KeyError, TypeError, ImportError) as error:
            result["validity"]["performance"] = "INVALID"
            result["validity"]["reasons"].append(("calibration verification: " + str(error))[:1024])
            result["performance"]["status"] = "INCONCLUSIVE"
    validate_result(result)
    write_json(output / "result.json", result, exclusive=True)
    return result
