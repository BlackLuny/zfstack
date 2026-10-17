"""Admission for real TUN/WireGuard observations, independent of shell exit."""
from __future__ import annotations

import importlib.util
import gzip
import re
import sys
from pathlib import Path

from .common import ROOT, EvidenceError, canonical, digest, file_hash, read_json
from .build_manifest import FLAGS, native_platform


def _l2_module(name):
    directory = ROOT / "harness/l2"
    spec = importlib.util.spec_from_file_location("qa_l2_" + name, directory / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(directory))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def l1_evidence(case, output):
    path = Path(output) / "l1.json"
    if not path.is_file():
        return "NOT_RUN", ["missing structured L1 evidence"], {}
    raw = read_json(path)
    observed = raw.get("results", {})
    errors = [str(x) for x in raw.get("errors", [])]
    missing = []
    if raw.get("tool") != "zfbench" or raw.get("ok") not in (True, False):
        missing.append("unknown L1 evidence format")
    if observed.get("layer") != "L1" or observed.get("wireguard") is not False or observed.get("test") != "isolate":
        missing.append("wrong L1 path")
    for key, expected in case["config"].items():
        if raw.get("config", {}).get(key) != expected:
            missing.append(f"L1 configuration differs: {key}")
    if raw.get("ok") is False and not errors:
        errors.append("L1 explicitly reported failure")
    for phase in ("baseline_users", "loaded_users"):
        users = observed.get(phase, [])
        if not isinstance(users, list) or len(users) != case["config"]["lights"]:
            missing.append(f"{phase}: incomplete user set")
            continue
        if len({row.get("user") for row in users}) != len(users):
            missing.append(f"{phase}: duplicate user")
        if any(not isinstance(row.get("samples"), dict) or row["samples"].get("n", 0) <= 0 for row in users):
            missing.append(f"{phase}: user did not complete a verified request")
    heavy = observed.get("heavy", {})
    if case["config"]["heavy_conns"] and (heavy.get("window_bytes", 0) <= 0 or heavy.get("window_wall_sec", 0) <= 0):
        missing.append("heavy flow has no measured delivered bytes")
    observations = {"raw_config_hash": digest(raw.get("config", {})), "path": "kernel-tcp/tun/zfstack",
                    "heavy": heavy, "baseline_users": observed.get("baseline_users"),
                    "loaded_users": observed.get("loaded_users"), "identity_errors": missing}
    return ("FAIL" if errors else "NOT_RUN" if missing else "PASS"), errors + missing, observations


def validate_worker_manifest(manifest, case, tested_sha, *, binary=None):
    """One strict admission contract for live and archived product workers."""
    required = {"schema_version", "status", "errors", "zfstack_sha", "binary_sha256", "profile", "toolchain",
                "features", "default_features", "source_refs", "lock_sha256", "build_info", "commands", "runtime_id",
                "rustc_vv", "cargo_version", "host", "target", "build_flags", "dependency_graph", "dependency_graph_hash", "metadata_hash"}
    if not isinstance(manifest, dict) or not required.issubset(manifest) or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise EvidenceError("incomplete independently built worker identity")
    if manifest["status"] != "BUILT" or manifest["errors"] != []:
        raise EvidenceError("worker build is not complete or contains build errors")
    for key in ("binary_sha256", "lock_sha256", "runtime_id", "dependency_graph_hash", "metadata_hash"):
        if not isinstance(manifest[key], str) or not re.fullmatch(r"[a-f0-9]{64}", manifest[key]):
            raise EvidenceError("invalid worker build hash: " + key)
    refs = manifest["source_refs"]
    if (not isinstance(refs, dict) or set(refs) != {"zfc", "zfstack", "private_tun", "shadow_tls", "rig"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", value) for value in refs.values())
            or manifest["zfstack_sha"] != tested_sha or refs.get("zfstack") != tested_sha):
        raise EvidenceError("worker does not identify all frozen source refs and tested ZFStack SHA")
    build_info = manifest["build_info"]
    if (not isinstance(build_info, dict) or not isinstance(build_info.get("source"), dict)
            or not isinstance(build_info.get("private_tun"), dict)
            or build_info["source"].get("commit") != refs["zfc"]
            or build_info["private_tun"].get("commit") != refs["private_tun"]):
        raise EvidenceError("worker build-info differs from frozen ZFC/private_tun sources")
    expected = case.get("integration_build")
    if (not isinstance(expected, dict) or set(expected) != {"toolchain", "profile", "features"}
            or any(not isinstance(expected.get(key), str) or not expected[key] for key in ("toolchain", "profile"))
            or not isinstance(expected.get("features"), list)
            or any(not isinstance(value, str) or not value for value in expected["features"])):
        raise EvidenceError("trusted L2 case must freeze the product integration build")
    features = manifest["features"]
    if (not isinstance(features, list) or any(not isinstance(value, str) or not value for value in features)
            or len(set(features)) != len(features) or sorted(features) != sorted(expected["features"])
            or manifest["toolchain"] != expected["toolchain"] or manifest["profile"] != expected["profile"]
            or manifest["default_features"] is not True):
        raise EvidenceError("actual worker toolchain/profile/features differ from trusted integration build")
    if (manifest["toolchain"] != case["toolchain"] or manifest["profile"] != case["profile"]
            or sorted(features) != sorted(case["features"])):
        raise EvidenceError("outer QA case mislabels the product worker build")
    flags = manifest["build_flags"]
    if not isinstance(flags, dict) or set(flags) != set(FLAGS) or any(not isinstance(value, str) for value in flags.values()):
        raise EvidenceError("worker build flags are incomplete")
    host, target = manifest["host"], manifest["target"]
    if not isinstance(host, str) or not host or target != host or flags["CARGO_BUILD_TARGET"] not in ("", target):
        raise EvidenceError("worker lacks native compiler host/target identity")
    system = "linux" if "linux" in host else "darwin" if "darwin" in host else "windows" if "windows" in host else None
    if system is None or native_platform(system, host.split("-")[0]) != case["platform"]:
        raise EvidenceError("worker target differs from native case platform")
    rustc, cargo = manifest["rustc_vv"], manifest["cargo_version"]
    if not isinstance(rustc, str) or not rustc.startswith("rustc ") or not isinstance(cargo, str) or not cargo.startswith("cargo "):
        raise EvidenceError("worker compiler/Cargo identity is invalid")
    compiler = dict(line.split(": ", 1) for line in rustc.splitlines() if ": " in line)
    if (any(not compiler.get(key) for key in ("binary", "commit-hash", "commit-date", "host", "release", "LLVM version"))
            or compiler["host"] != host
            or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", compiler["commit-hash"])):
        raise EvidenceError("worker verbose compiler identity is incomplete")
    toolchain = manifest["toolchain"]
    if toolchain[0].isdigit() and compiler["release"] != toolchain:
        raise EvidenceError("worker compiler release differs from frozen toolchain")
    if toolchain.startswith("nightly") and "nightly" not in compiler["release"]:
        raise EvidenceError("worker compiler is not the requested nightly toolchain")
    graph = manifest["dependency_graph"]
    if not isinstance(graph, str) or not graph.startswith("0zf-worker v") or digest(graph) != manifest["dependency_graph_hash"]:
        raise EvidenceError("worker selected dependency graph is missing or tampered")
    runtime = {key: manifest[key] for key in ("rustc_vv", "cargo_version", "toolchain", "host", "target", "build_flags")}
    if digest(runtime) != manifest["runtime_id"]:
        raise EvidenceError("worker compiler/runtime fingerprint mismatch")
    records = manifest["commands"]
    if not isinstance(records, list) or not records:
        raise EvidenceError("worker has no successful build command evidence")
    for record in records:
        if (not isinstance(record, dict) or record.get("status") != "PASS" or type(record.get("exit_code")) is not int
                or record["exit_code"] != 0 or not isinstance(record.get("argv"), list) or not record["argv"]
                or any(not isinstance(arg, str) for arg in record["argv"])):
            raise EvidenceError("worker build command failed or is malformed")
        execution = record.get("execution", {})
        if not isinstance(execution, dict):
            raise EvidenceError("worker command lifecycle evidence is malformed")
        cleanups = [record[key] for key in ("cleanup", "cleanup_status") if key in record]
        if "cleanup_status" in execution:
            cleanups.append(execution["cleanup_status"])
        if not cleanups or any(value != "PASS" for value in cleanups):
            raise EvidenceError("worker command cleanup is incomplete")
        if execution and (execution.get("status") != "completed" or execution.get("exit_code") != 0):
            raise EvidenceError("worker child execution contradicts successful build command")
    build_commands = [record["argv"] for record in records if "build" in record["argv"]]
    def selected_build(argv):
        def option(name):
            try:
                return argv[argv.index(name) + 1]
            except (ValueError, IndexError):
                return None
        return ("+" + toolchain in argv and "--locked" in argv and "--no-default-features" not in argv
                and (option("-p") or option("--package")) == "zf-worker" and option("--bin") == "zf-worker"
                and option("--profile") == manifest["profile"]
                and sorted((option("--features") or "").split(",") if option("--features") else []) == sorted(features))
    if not any(selected_build(argv) for argv in build_commands):
        raise EvidenceError("worker manifest lacks the frozen locked worker build command")
    if binary is not None and (not Path(binary).is_file() or file_hash(binary) != manifest["binary_sha256"]):
        raise EvidenceError("worker binary differs from its immutable build manifest")
    return manifest


def worker_identity(node, tested_sha, case):
    worker = validate_worker_manifest(read_json(node["worker_manifest"]), case, tested_sha, binary=node["worker_binary"])
    if case["config"].get("outbound") == "relay":
        binary = node.get("forwarder_binary")
        if not isinstance(binary, str) or not Path(binary).is_absolute():
            raise EvidenceError("relay requires an immutable absolute forwarder_binary")
        validate_forwarder(worker, binary=binary)
    return worker


def validate_forwarder(worker, *, binary=None):
    """The forwarder must be built from this worker's frozen ZFC/PT inputs."""
    value = worker.get("forwarder")
    required = {"binary_path", "binary_sha256", "size_bytes", "build_info", "dependency_graph", "dependency_graph_hash"}
    if not isinstance(value, dict) or set(value) != required:
        raise EvidenceError("missing complete immutable relay forwarder identity")
    if (not isinstance(value["binary_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["binary_sha256"])
            or type(value["size_bytes"]) is not int or value["size_bytes"] <= 0):
        raise EvidenceError("invalid relay forwarder binary identity")
    info, refs = value["build_info"], worker["source_refs"]
    if (not isinstance(info, dict) or info.get("source", {}).get("commit") != refs["zfc"]
            or info.get("private_tun", {}).get("commit") != refs["private_tun"]):
        raise EvidenceError("relay forwarder differs from frozen ZFC/private_tun sources")
    graph = value["dependency_graph"]
    if not isinstance(graph, str) or not graph.startswith("0forwarder-server v") or digest(graph) != value["dependency_graph_hash"]:
        raise EvidenceError("relay forwarder selected dependency graph differs")
    def matches(argv):
        def option(name):
            try: return argv[argv.index(name) + 1]
            except (ValueError, IndexError): return None
        return ("build" in argv and "--locked" in argv and "+" + worker["toolchain"] in argv
                and (option("-p") or option("--package")) == "forwarder-server" and option("--bin") == "forwarder-server"
                and option("--profile") == worker["profile"] and "--no-default-features" not in argv
                and sorted((option("--features") or "").split(",") if option("--features") else []) == sorted(worker["features"]))
    if not any(matches(record["argv"]) for record in worker["commands"]):
        raise EvidenceError("relay forwarder lacks a frozen locked build command")
    if binary is not None and (not Path(binary).is_file() or Path(binary).stat().st_size != value["size_bytes"] or file_hash(binary) != value["binary_sha256"]):
        raise EvidenceError("actual relay forwarder differs from its frozen build")
    return value


def l2_evidence(case, output, worker):
    case_dir = Path(output) / "rig"
    if not any((case_dir / name).is_file() for name in ("result.json", "l2-result.json")):
        return "NOT_RUN", ["missing L2 workload evidence"], {}, "UNKNOWN", {}
    inspected = _l2_module("evidence").inspect_case(case_dir)
    observed = inspected["config"]
    missing = list(inspected["functional_evidence_errors"])
    expected = case["config"]
    # Workload fields are frozen by policy. Binary-specific tuning is retained
    # in the full observed manifest and compared by the statistical L2 tools.
    actual_workload = {k: v for k, v in observed.items() if k != "worker_tuning_fingerprints"}
    if actual_workload != expected:
        missing.append("L2 full workload manifest differs from the frozen plan")
    fingerprints = observed.get("worker_tuning_fingerprints")
    if not isinstance(fingerprints, list) or len(fingerprints) != 2 or not all(fingerprints):
        missing.append("missing startup tuning fingerprints for both WG ports")
    if inspected["binary_sha256"] != worker["binary_sha256"]:
        missing.append("L2 executed binary differs from the independent worker build")
    if expected.get("outbound") == "relay":
        try:
            forwarder = validate_forwarder(worker)
            words = inspected["raw"].get("forwarder.sha256", "").split()
            if (not words or words[0] != forwarder["binary_sha256"]
                    or inspected["result"].get("forwarder_build_info") != forwarder["build_info"]):
                missing.append("L2 executed forwarder differs from the independent frozen build")
        except EvidenceError as error:
            missing.append(str(error))
    errors = inspected["functional_errors"]
    status = "FAIL" if errors else "NOT_RUN" if missing else "PASS"
    raw_status = status
    service = None
    if case.get("service_contract") is not None:
        from .service import evaluate_service
        peers = {key[:-5]: value for key, value in inspected["raw"].items() if key.startswith("client-") and key.endswith(".json") and key != "client-0.json"}
        service = evaluate_service(case["service_contract"], peers)
        status = "FAIL" if "FAIL" in (status, service["status"]) else "NOT_RUN" if "NOT_RUN" in (status, service["status"]) else "PASS"
        errors = [*errors, *(row["id"] + ": " + row["detail"] for row in service["assertions"] if row["status"] != "PASS")]
    observations = {"effective_config": observed, "effective_config_hash": digest(observed),
                    "worker_manifest_hash": digest(worker), "functional_evidence_errors": missing,
                    "performance_errors": inspected["performance_errors"],
                    "path_evidence": inspected["result"].get("path_evidence"),
                    "startup_anomalies": inspected["result"].get("startup_anomalies"),
                    "performance_contaminated": inspected["result"].get("performance_contaminated"),
                    "raw_functional_status": raw_status, "service_result": service,
                    "queue_control": inspected["result"].get("queue_control"),
                    "outbound_path": "private_tun-relay" if expected.get("outbound") == "relay" else "direct"}
    perf_validity = "INVALID" if inspected["performance_errors"] or missing else "VALID"
    return status, errors + missing, observations, perf_validity, inspected["metrics"]


def queue_negative_evidence(case, baseline_output, fault_output, worker):
    """Positive/negative pair for the QA latency oracle, never product coverage."""
    from .service import evaluate_service
    config = case["config"]
    if not case.get("service_contract") or not config.get("queue_control"):
        return "NOT_RUN", ["queue negative control requires frozen service and injection contracts"], {}
    baseline = {**case, "config": {key: value for key, value in config.items() if key != "queue_control"}}
    base_status, base_details, base_observed, _, _ = l2_evidence(baseline, baseline_output, worker)
    fault_status, fault_details, fault_observed, _, _ = l2_evidence(case, fault_output, worker)
    if not fault_observed:
        return ("FAIL" if base_status == "FAIL" else "NOT_RUN"), [*base_details, *fault_details], {"baseline": base_observed, "fault": {}}
    inspected = _l2_module("evidence").inspect_case(Path(fault_output) / "rig")
    queue = _l2_module("queue_control")
    try:
        ledger = queue.ledger_blob(Path(fault_output) / "rig")
    except (OSError, ValueError):
        ledger = None
    control_errors = queue.validate_evidence(inspected["result"].get("queue_control"), config["queue_control"], ledger)
    fault_config = {key: value for key, value in fault_observed.get("effective_config", {}).items() if key != "queue_control"}
    if base_observed.get("effective_config") != fault_config:
        control_errors.append("baseline/fault full runtime configuration differs beyond the frozen injection")
    peers = {key[:-5]: value for key, value in inspected["raw"].items() if key.startswith("client-") and key.endswith(".json") and key != "client-0.json"}
    recovery = evaluate_service({**case["service_contract"], "phases": ["recovery"]}, peers)
    observations = {"scope": "test-side egress queue negative control; not historical #44 root-cause reproduction",
                    "baseline": base_observed, "fault": fault_observed, "recovery": recovery,
                    "queue_evidence_errors": control_errors, "expected_failure": "service maximum latency"}
    if (base_status == "FAIL" or fault_observed.get("raw_functional_status") == "FAIL" or recovery["status"] == "FAIL"):
        return "FAIL", ["positive baseline, payload integrity or recovery contract failed", *base_details, *fault_details], observations
    service = fault_observed.get("service_result")
    incomplete_service = (not service or not service.get("assertions")
                          or any(row["status"] == "NOT_RUN" for row in service["assertions"]))
    if (base_status != "PASS" or fault_observed.get("raw_functional_status") != "PASS" or recovery["status"] != "PASS"
            or control_errors or incomplete_service or service["status"] == "NOT_RUN"):
        return "NOT_RUN", ["queue negative-control precondition/evidence incomplete", *base_details, *control_errors, *fault_details], observations
    if fault_status != "FAIL" or fault_observed["service_result"]["status"] != "FAIL":
        return "FAIL", ["valid queued traffic did not trigger the frozen service contract"], observations
    return "PASS", ["healthy baseline passed; queued traffic violated service latency; recovery passed"], observations


def freeze_l2_raw(output):
    """Archive only typed clients/counters, never runtime configs or keys."""
    directory = Path(output) / "rig"
    if not (directory / "result.json").is_file() or (directory / "raw.json.gz").exists():
        return
    _, raw, _ = _l2_module("evidence").load_case(directory)
    data = canonical(raw)
    if len(data) > 64 * 1024 * 1024:
        raise EvidenceError("L2 raw evidence exceeds bounded archive size")
    with (directory / "raw.json.gz").open("xb") as stream:
        stream.write(gzip.compress(data, mtime=0))


def verify_integration(result, case, directory):
    directory = Path(directory)
    if case["runner"] == "l1":
        status, details, _ = l1_evidence(case, directory)
    elif case["runner"] in ("l2", "l2-queue-control"):
        worker = read_json(directory / "worker-manifest.json")
        validate_worker_manifest(worker, case, result["tested_sha"])
        outer = read_json(directory / "build-manifest.json")
        pairs = (("toolchain", "toolchain"), ("profile", "profile"), ("rustc", "rustc_vv"), ("cargo", "cargo_version"),
                 ("host", "host"), ("target", "target"), ("build_flags", "build_flags"),
                 ("dependency_graph_hash", "dependency_graph_hash"), ("lock_hash", "lock_sha256"))
        if (any(outer.get(left) != worker[right] for left, right in pairs)
                or outer.get("requested_features") != sorted(worker["features"])
                or worker["binary_sha256"] not in {item.get("sha256") for item in outer.get("binaries", [])}):
            raise EvidenceError("outer build identity differs from the actual archived worker")
        if case["runner"] == "l2-queue-control":
            status, details, _ = queue_negative_evidence(case, directory / "baseline", directory / "fault", worker)
        else:
            status, details, _, _, _ = l2_evidence(case, directory, worker)
    else:
        return
    if status != result["functional"]["status"] or status == "NOT_RUN":
        raise EvidenceError("independent integration validation: " + "; ".join(details or ["status differs"]))


def l2_environment(config):
    fields = {"memory_mb": "MEMORY_MB", "heavy_conns": "HEAVY_CONNS", "request_timeout_sec": "REQUEST_TIMEOUT_SECS",
              "uplink_delay_ms": "UPLINK_DELAY_MS", "netem_limit_packets": "NETEM_LIMIT_PACKETS",
              "light_conns": "LIGHT_CONNS", "churn_requests": "CHURN_REQUESTS", "request_rate_hz": "LIGHT_RATE_HZ",
              "light_mode": "LIGHT_MODE", "max_generator_lag_sec": "MAX_GENERATOR_LAG_SECS", "udp_rate_hz": "UDP_RATE_HZ",
              "mtu": "MTU", "runtime": "RUNTIME_ID", "profile": "PROFILE_ID", "mode": "MODE"}
    result = {"L2_" + target: str(config[key]) for key, target in fields.items()}
    for phase, value in config["phases_sec"].items():
        result[f"L2_{phase.upper()}_SECS"] = str(value)
    result["L2_UDP_PROBES"] = "1" if config["udp_enabled"] else "0"
    result["L2_CAPTURE"] = "1" if config["capture"] else "0"
    result["L2_OUTBOUND"] = config.get("outbound", "direct")
    # A queue-control pair reuses a parent environment. The healthy arm must
    # explicitly cancel inherited injection settings, not merely omit keys.
    result["L2_QUEUE_DELAY_MS"] = ""
    result["L2_QUEUE_LIMIT_PACKETS"] = ""
    result["L2_QUEUE_KIND"] = ""
    if config.get("queue_control") is not None:
        from_queue = _l2_module("queue_control").validate_config(config["queue_control"])
        result["L2_QUEUE_DELAY_MS"] = str(from_queue["delay_ms"])
        result["L2_QUEUE_LIMIT_PACKETS"] = str(from_queue["limit_packets"])
        result["L2_QUEUE_KIND"] = from_queue["kind"]
    return result
