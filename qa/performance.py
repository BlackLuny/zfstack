"""Recompute frozen performance experiments from raw probe artifacts."""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

from .build_manifest import verify_manifest
from .common import ROOT, EvidenceError, digest, file_hash, read_json, safe_path

STABLE_CONFIG = ("args", "timeout_sec", "max_output_bytes", "sample_interval_sec")
NODE_CONFIG = ("cpu", "expected_measurement")


def tool(name):
    path = ROOT / "bench/pareto" / (name + ".py")
    spec = importlib.util.spec_from_file_location("zfstack_trusted_perf_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def engine():
    return tool("perf_gate")


def read_experiment_rows(path):
    """Keep complete earlier failures if cancellation truncated a later JSONL row."""
    path, evaluator = Path(path), engine()
    if path.stat().st_size > evaluator.MAX_INPUT_BYTES:
        raise EvidenceError("performance rows exceed bounded analysis input")
    rows, errors = [], []
    with path.open("rb") as source:
        for index, line in enumerate(source):
            if not line.strip():
                continue
            try:
                rows.append(evaluator.decode_json(line.decode("utf-8")))
            except (UnicodeError, ValueError, TypeError, RecursionError):
                rows.append(None)  # Explicit invalid slot, never a fabricated run.
                errors.append(f"malformed/truncated raw record at line {index + 1}")
    return rows, errors


def check_experiment(plan, case, manifest, outer_environment, perf_environment, tested_sha, *, expected_hash=None):
    """Bind static workload policy and node-specific frozen execution settings."""
    evaluator = engine()
    errors = evaluator.validate_plan(plan)
    if errors:
        raise EvidenceError("invalid performance plan: " + "; ".join(errors))
    if expected_hash is not None and digest(plan) != expected_hash:
        raise EvidenceError("performance plan differs from trusted expected hash")
    if any(plan.get(key) != case[key] for key in ("case_id", "case_version", "profile")):
        raise EvidenceError("performance case/version/profile mismatch")
    mode = case["performance"]["mode"]
    if mode not in ("shadow", "enforce") or plan["mode"] != mode:
        raise EvidenceError("performance mode differs from trusted policy")
    trusted_hash = case["performance"].get("experiment_plan_hash")
    if (trusted_hash is not None or mode == "enforce") and digest(plan) != trusted_hash:
        raise EvidenceError("performance experiment differs from trusted policy plan")
    if mode == "enforce":
        if plan.get("calibration_id") != case["performance"].get("calibration_id"):
            raise EvidenceError("calibration identity differs from trusted policy")
    if set(case["performance"]["metrics"]) != {metric["id"] for metric in plan["metrics"]}:
        raise EvidenceError("primary metric family differs from trusted policy")
    absolute_slos = {metric["id"]: metric["absolute_slo"] for metric in plan["metrics"] if "absolute_slo" in metric}
    if absolute_slos != case["performance"].get("absolute_slos", {}):
        raise EvidenceError("absolute service contracts differ from trusted policy")
    config = case["config"]
    if case["runner"] == "external-performance":
        if set(config) != {*STABLE_CONFIG, "comparison", "blocks"}:
            raise EvidenceError("external performance policy needs the complete fixed probe configuration")
        if set(plan["config"]) != {*STABLE_CONFIG, *NODE_CONFIG}:
            raise EvidenceError("unknown or missing frozen performance execution setting")
        if any(plan["config"].get(key) != config[key] for key in STABLE_CONFIG):
            raise EvidenceError("frozen probe workload differs from policy")
        if plan["comparison"] != config["comparison"] or len(plan["blocks"]) != config["blocks"]:
            raise EvidenceError("comparison or fixed block endpoint differs from policy")
    elif plan["config"] != config:
        raise EvidenceError("performance configuration differs from policy")
    execution_config = plan["config"]
    if (not isinstance(execution_config.get("args"), list) or not execution_config["args"]
            or not all(isinstance(value, str) and value for value in execution_config["args"])
            or type(execution_config.get("timeout_sec")) not in (int, float)
            or not math.isfinite(execution_config["timeout_sec"]) or execution_config["timeout_sec"] <= 0
            or type(execution_config.get("sample_interval_sec")) not in (int, float)
            or not math.isfinite(execution_config["sample_interval_sec"]) or not 0 < execution_config["sample_interval_sec"] <= 10
            or type(execution_config.get("max_output_bytes")) is not int
            or not 1 <= execution_config["max_output_bytes"] <= 64 * 1024 * 1024):
        raise EvidenceError("invalid frozen execution settings")
    if (type(plan["config"].get("cpu")) is not int or plan["config"]["cpu"] < 0
            or perf_environment.get("affinity") != [plan["config"]["cpu"]]
            or not isinstance(plan["config"].get("expected_measurement"), dict)
            or not plan["config"]["expected_measurement"]):
        raise EvidenceError("missing frozen CPU affinity or work-counter contract")
    counters = {"case", "iterations", "flows", "peers", "payload_bytes", "operations", "effective_bytes",
                "allocation_requested_bytes", "checksum_u64", "expected_checksum_u64", "real_network_io",
                "virtual_clock_used_for_rate"}
    if not counters.issubset(plan["config"]["expected_measurement"]):
        raise EvidenceError("incomplete frozen probe work counters")
    if digest(manifest) != plan["build_manifest_hash"]:
        raise EvidenceError("original build manifest differs from frozen performance plan")
    if digest(perf_environment) != plan["environment_hash"]:
        raise EvidenceError("performance environment differs from frozen plan")
    verify_manifest(manifest, case, {"tested_sha": tested_sha, "toolchain": case["toolchain"]}, outer_environment)
    if outer_environment["system"] != "Linux" or outer_environment["kernel"] != perf_environment.get("kernel"):
        raise EvidenceError("outer QA and performance environment are not the same native Linux kernel")
    cpuinfo = outer_environment.get("cpuinfo") or ""
    model = next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines() if line.startswith("model name")), None)
    if model != perf_environment.get("cpu_model"):
        raise EvidenceError("outer QA and performance CPU model differ")
    if not any(item["sha256"] == plan["binary_sha256"]["B"] and item["size_bytes"] > 0 for item in manifest["binaries"]):
        raise EvidenceError("candidate binary is absent from original build manifest")
    return plan


def failed_observation(objects):
    for value in objects:
        if not isinstance(value, dict):
            continue
        if value.get("ok") is False or value.get("all_budget_leases_released") is False:
            return True
        if type(value.get("final_reserved_bytes")) is int and value["final_reserved_bytes"] > 0:
            return True
        if (type(value.get("checksum_u64")) is int and type(value.get("expected_checksum_u64")) is int
                and value["checksum_u64"] != value["expected_checksum_u64"]):
            return True
    return False


def inspect_rows(plan, rows, directory, perf_environment):
    """Validate every slot against raw output; never silently discard bad rows."""
    evaluator, analyzer, environment = engine(), tool("analyze"), tool("environment")
    directory = Path(directory)
    errors, functional_errors, failures = [], [], []
    expected = [(block["block_id"], position, arm) for block in plan["blocks"] for position, arm in enumerate(block["order"])]
    actual_slots = []
    previous_end = -1
    for index, row in enumerate(rows):
        label = f"run {index}"
        if not isinstance(row, dict):
            errors.append(f"{label}: malformed record")
            continue
        arm, position = row.get("arm"), row.get("position")
        actual_slots.append((row.get("block_id"), position, arm))
        if type(position) is not int or type(row.get("case_version")) is not int or arm not in ("A", "B"):
            errors.append(f"{label}: malformed typed slot/case identity")
        identity = (("plan_hash", digest(plan)), ("config_hash", digest(plan["config"])),
                    ("build_manifest_hash", plan["build_manifest_hash"]), ("environment_hash", plan["environment_hash"]),
                    ("case_id", plan["case_id"]), ("case_version", plan["case_version"]),
                    ("binary_sha256", plan["binary_sha256"].get(arm) if isinstance(arm, str) else None))
        if any(row.get(key) != wanted for key, wanted in identity):
            errors.append(f"{label}: changed run identity")
        if row.get("run_id") != f"{digest(plan)}:{row.get('block_id')}:{position}":
            errors.append(f"{label}: missing or changed frozen run ID")
        observation = row.get("observations", {})
        execution = row.get("execution", {})
        if not isinstance(observation, dict):
            errors.append(f"{label}: observations are missing/malformed")
            observation = {}
        if not isinstance(execution, dict):
            errors.append(f"{label}: execution is missing/malformed")
            execution = {}
        lifecycle = observation.get("process_lifecycle", {})
        if not isinstance(lifecycle, dict):
            lifecycle = {}
        if (lifecycle.get("cleanup_scope") != "linux-subreaper" or lifecycle.get("cleanup_limits") != []
                or lifecycle.get("cleanup_status") != execution.get("cleanup")
                or any(lifecycle.get(key) != execution.get(key) for key in ("status", "exit_code", "start_ns", "end_ns"))):
            errors.append(f"{label}: process containment record missing or contradictory")
        if execution.get("status") != "completed" or execution.get("exit_code") != 0 or execution.get("cleanup") != "PASS":
            functional_errors.append(f"{label}: execution/cleanup did not complete")
        start, end = execution.get("start_ns"), execution.get("end_ns")
        if (type(start) is not int or type(end) is not int or start < previous_end or end <= start
                or execution.get("clock") != "CLOCK_MONOTONIC"):
            errors.append(f"{label}: invalid/overlapping monotonic interval")
        else:
            previous_end = end
        raw = observation.get("output_artifact", {})
        if not isinstance(raw, dict):
            raw = {}
        objects, admission = [], []
        try:
            # run_plan numbers log files by the frozen block index and slot.
            filename = f"probe-{index // 4}-{index % 4}.log"
            if raw.get("path") != filename:
                raise EvidenceError("unexpected raw probe log path")
            path = safe_path(directory, "runs/" + filename)
            limit = plan["config"]["max_output_bytes"]
            if type(limit) is not int or not 1 <= limit <= 64 * 1024 * 1024:
                raise EvidenceError("invalid frozen output limit")
            if (path.stat().st_size != raw.get("size_bytes") or file_hash(path) != raw.get("sha256")
                    or raw.get("limit_bytes") != limit or raw.get("streams") != "stdout+stderr"):
                raise EvidenceError("raw probe output differs from recorded artifact")
            if path.stat().st_size >= limit:
                admission.append("output reached frozen byte limit")
            if path.stat().st_size > limit + 65536:
                raise EvidenceError("raw output exceeds bounded read budget")
            text = path.read_text(encoding="utf-8")
            if text != observation.get("stdout") or observation.get("stderr") != "":
                raise EvidenceError("raw output differs from exported observation")
            for line in text.splitlines():
                try:
                    objects.append(evaluator.decode_json(line))
                except (ValueError, TypeError, RecursionError):
                    admission.append("unparseable probe output")
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{label}: {error}")
        if objects != observation.get("measurements"):
            errors.append(f"{label}: measurement objects differ from raw output")
        raw_fail = failed_observation(objects)
        if arm == "B" and raw_fail:
            failures.append(f"{label}: candidate contract violation in raw output")
        try:
            probe, probe_errors = analyzer.validate_row({"variant": "baseline" if arm == "A" else "candidate", "pair": 0,
                  "position": position, "binary_sha256": row.get("binary_sha256"), "measurements": objects,
                  "exit_code": execution.get("exit_code")}, 0, {}, {"name": plan["case_id"], "args": plan["config"]["args"]})
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            probe, probe_errors = None, ["malformed probe workload/counters"]
        admission.extend(probe_errors)
        if probe:
            if any(probe["measurement"].get(key) != value for key, value in plan["config"]["expected_measurement"].items()):
                admission.append("frozen work counters changed")
        if (probe["metrics"] if probe else {}) != row.get("metrics"):
            errors.append(f"{label}: metrics differ from raw probe recomputation")
        functional = "FAIL" if raw_fail else "PASS" if probe and not admission else "NOT_RUN"
        declared_functional = row.get("functional", {})
        declared_validity = row.get("validity", {})
        if not isinstance(declared_functional, dict): declared_functional = {}
        if not isinstance(declared_validity, dict): declared_validity = {}
        if declared_functional.get("status") != functional:
            errors.append(f"{label}: functional status differs from raw probe recomputation")
        if functional != "PASS":
            functional_errors.extend(f"{label}: {detail}" for detail in admission or [functional])
        samples = observation.get("environment_samples")
        try:
            environmental = environment.validity(samples, perf_environment)
            if not any(sample.get("phase") == "running" for sample in samples):
                environmental.append("missing in-run DUT sample")
        except (TypeError, KeyError, AttributeError):
            environmental = ["malformed environment observations"]
        performance_invalid = bool(environmental or admission or execution.get("status") != "completed"
                                   or execution.get("cleanup") != "PASS")
        if performance_invalid and declared_validity.get("performance") == "VALID":
            errors.append(f"{label}: invalid performance evidence was declared valid")
        if functional in ("PASS", "FAIL") and declared_validity.get("functional") != "VALID":
            functional_errors.append(f"{label}: functional validity is unknown")
    if actual_slots != expected:
        errors.append("missing, duplicated or reordered frozen experiment slot")
    functional = "FAIL" if failures else "NOT_RUN" if errors or functional_errors else "PASS"
    return {"functional": functional, "errors": errors, "functional_errors": functional_errors, "failures": failures}


def verify_performance(result, case, directory, *, policy_dir=None):
    """Reporter entry point for shadow and enforce; no self-reported PASS trust."""
    directory = Path(directory)
    names = {artifact["path"] for artifact in result["artifacts"]}
    required = {"perf-plan.json", "perf-rows.jsonl", "perf-analysis.json", "perf-audit.json", "perf-environment.json",
                "build-manifest.json", "environment.json", "execution.json"}
    if not required.issubset(names):
        raise EvidenceError("missing frozen performance/raw execution artifacts")
    # Also verify here for callers other than qa.evaluate.
    for artifact in result["artifacts"]:
        path = safe_path(directory, artifact["path"])
        if path.stat().st_size != artifact["size_bytes"] or file_hash(path) != artifact["sha256"]:
            raise EvidenceError("performance artifact missing/tampered: " + artifact["path"])
    plan = read_json(directory / "perf-plan.json")
    perf_environment = read_json(directory / "perf-environment.json")
    manifest, outer_environment = read_json(directory / "build-manifest.json"), read_json(directory / "environment.json")
    check_experiment(plan, case, manifest, outer_environment, perf_environment, result["tested_sha"])
    bindings = {"performance_plan_hash": digest(plan), "performance_environment_hash": digest(perf_environment),
                "performance_config_hash": digest(plan["config"]), "qa_config_hash": digest(case["config"])}
    if any(result["observations"].get(key) != value for key, value in bindings.items()):
        raise EvidenceError("QA/performance identity binding mismatch")
    for key, value in (("config_hash", bindings["qa_config_hash"]), ("environment_hash", digest(outer_environment)),
                       ("build_manifest_hash", digest(manifest))):
        if result[key] != value:
            raise EvidenceError("outer performance result identity mismatch: " + key)
    calibration = None
    if case["performance"]["mode"] == "enforce":
        if policy_dir is None:
            raise EvidenceError("trusted calibration policy directory is required")
        from .release import verify_calibration
        calibration = verify_calibration(directory, case, policy_dir,
                       generation=result["observations"].get("node_generation"))
    evaluator = engine()
    rows, parse_errors = read_experiment_rows(safe_path(directory, "perf-rows.jsonl"))
    audited = inspect_rows(plan, rows, directory, perf_environment)
    audited["errors"].extend(parse_errors)
    if parse_errors and audited["functional"] != "FAIL":
        audited["functional"] = "NOT_RUN"
    if audited != read_json(directory / "perf-audit.json") or audited["errors"]:
        raise EvidenceError("independent raw performance audit differs/failed: " + "; ".join(audited["errors"]))
    actual = evaluator.evaluate(plan, rows, calibration)
    if actual != read_json(directory / "perf-analysis.json"):
        raise EvidenceError("performance analysis differs from independent recomputation")
    execution = read_json(directory / "execution.json")
    record = execution["runner"]
    if (not isinstance(record, dict) or record.get("cleanup_scope") != "linux-subreaper" or record.get("cleanup_limits") != []
            or execution.get("source_before") != manifest["source"] or execution.get("source_after") != manifest["source"]
            or execution.get("admission_errors")):
        raise EvidenceError("source/runner containment admission is incomplete")
    if any(row["execution"]["start_ns"] < record["start_ns"] or row["execution"]["end_ns"] > record["end_ns"] for row in rows):
        raise EvidenceError("probe interval lies outside the owned runner lifetime")
    clean = record.get("cleanup_status") == "PASS" and all(row.get("execution", {}).get("cleanup") == "PASS" for row in rows)
    functional = "FAIL" if actual["functional"] == "FAIL" else audited["functional"]
    if functional == "PASS" and (record.get("status") != "completed" or record.get("exit_code") != 0 or not clean):
        functional = "NOT_RUN"
    if functional != result["functional"]["status"]:
        raise EvidenceError("outer functional status differs from raw experiment")
    if result["execution"]["cleanup"] == "PASS" and not clean:
        raise EvidenceError("outer cleanup status hides an unclean child")
    if (actual["performance"] != result["performance"]["status"] or actual["metrics"] != result["performance"]["metrics"]
            or actual["validity"] != result["validity"]["performance"]
            or file_hash(directory / "perf-analysis.json") != result["performance"]["analysis_hash"]):
        raise EvidenceError("self-reported performance differs from independently recomputed analysis")
    if case["performance"]["mode"] == "enforce" and actual.get("calibrated") is not True:
        raise EvidenceError("performance experiment has no independently validated calibration")
    return actual
