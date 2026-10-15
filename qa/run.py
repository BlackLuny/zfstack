"""Typed runners, bounded child lifetimes and immutable per-case evidence.

Plans select known runners and validated values; they never contain shell code.
Privileged integration is available only on a designated test node.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

from .build_manifest import build_manifest, source_identity, integration_manifest, prepare_native_binary, command
from .common import EvidenceError, digest, file_hash, read_json, safe_path, write_json
from .coverage import audit
from .environment import capability_errors, collect_environment, platform_id
from .lease import Lease
from .plan import check_trusted_plan
from .process import run_child
from .integration import l1_evidence, l2_evidence, l2_environment, worker_identity, freeze_l2_raw
from .validate import validate_result


def _integer(config, key, default, minimum=0, maximum=1_000_000):
    value = config.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise EvidenceError(f"invalid {key}")
    return value


def safety_capability(case, root, env):
    """Probe the selected compiler, never infer native support from a cross target."""
    tc = case["toolchain"]
    rustc = command(["rustc", f"+{tc}", "-vV"], root, env=env)
    host = next((line[6:] for line in rustc.splitlines() if line.startswith("host: ")), "")
    system = next((name for name in ("darwin", "linux", "windows") if name in host), None)
    if not system or f"{system}-{host.split('-')[0]}" != case["platform"] or case["platform"] != platform_id():
        raise EvidenceError("safety compiler host differs from requested native platform")
    if env.get("CARGO_BUILD_TARGET") not in (None, "", host):
        raise EvidenceError("cross target cannot supply native safety evidence")
    spec = json.loads(command(["rustc", f"+{tc}", "-Z", "unstable-options", "--print", "target-spec-json", "--target", host], root, env=env))
    supported = spec.get("supported-sanitizers", [])
    errors = []
    if "address" not in supported:
        errors.append("native compiler target does not support AddressSanitizer")
    if case["runner"] == "sanitizer" and "leak" not in supported:
        errors.append("native target lacks LeakSanitizer; this case requires ASan with detect_leaks=1")
    fuzz_version = None
    if case["runner"] == "fuzz":
        fuzz_version = command(["cargo", f"+{tc}", "fuzz", "--version"], root, env=env)
        if fuzz_version != "cargo-fuzz 0.13.2":
            errors.append("cargo-fuzz 0.13.2 is required")
    env["CARGO_BUILD_TARGET"] = host
    # Encoded flags override RUSTFLAGS; inherited flags must not silently
    # remove the sanitizer that this runner promises to execute.
    env.pop("CARGO_ENCODED_RUSTFLAGS", None)
    env["RUSTFLAGS"] = "-Zsanitizer=address" if case["runner"] == "sanitizer" else ""
    return {"schema_version": 1, "host": host, "target": host, "rustc": rustc,
            "target_spec": spec, "cargo_fuzz": fuzz_version,
            "scope": "native ASan; leak detection required" if case["runner"] == "sanitizer" else
                     "native ASan fuzz; no LeakSanitizer guarantee on targets without leak support",
            "errors": errors}


def _corpus_snapshot(root):
    corpus = root / "fuzz/corpus"
    files = sorted(corpus.rglob("*"))
    if len(files) > 1024 or any(p.is_symlink() for p in files):
        raise EvidenceError("unsupported source corpus size or symlink")
    return [{"path": p.relative_to(corpus).as_posix(), "sha256": file_hash(p), "size_bytes": p.stat().st_size}
            for p in files if p.is_file()]


def prepare_fuzz(case, root, output):
    """Copy only reviewed inputs; libFuzzer never receives a source directory."""
    target = case["config"]["target"]
    before = _corpus_snapshot(root)
    manifest = read_json(root / "fuzz/corpus/manifest.json")
    seeds = [seed for seed in manifest["seeds"] if seed["target"] == target]
    if not seeds:
        raise EvidenceError("no reviewed seeds for fuzz target")
    corpus, artifacts = output / "fuzz-corpus", output / "fuzz-artifacts"
    corpus.mkdir(exist_ok=False)
    artifacts.mkdir(exist_ok=False)
    entries = []
    for seed in seeds:
        path = safe_path(root / "fuzz/corpus", seed["path"])
        if path.is_symlink() or path.parent.name != target or path.stat().st_size > 1024 * 1024 or file_hash(path) != seed["sha256"]:
            raise EvidenceError("reviewed fuzz seed changed or exceeds budget")
        destination = corpus / path.name
        if destination.exists():
            raise EvidenceError("duplicate fuzz seed basename")
        shutil.copyfile(path, destination)
        entries.append({"source_path": seed["path"], "name": path.name,
                        "sha256": file_hash(destination), "size_bytes": destination.stat().st_size})
    index = {"schema_version": 1, "target": target, "corpus_path": str(corpus.resolve()),
             "build_path": str((output / "fuzz-build").resolve()),
             "artifact_path": str(artifacts.resolve()), "manifest_sha256": file_hash(root / "fuzz/corpus/manifest.json"),
             "initial_seeds": entries, "source_before": before}
    write_json(output / "fuzz-seeds.json", index, exclusive=True)
    return index


def finish_fuzz(root, output, seeds):
    artifacts = output / "fuzz-artifacts"
    entries = []
    # Keep raw failures locally. Upload the explicit small index, never arbitrary
    # corpus output or attacker-selected crash bytes.
    for path in sorted(artifacts.iterdir()):
        if len(entries) >= 128 or path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise EvidenceError("fuzz failure artifact index budget exceeded")
        entries.append({"name": path.name, "sha256": file_hash(path), "size_bytes": path.stat().st_size})
    after = _corpus_snapshot(root)
    write_json(output / "fuzz-artifacts.json", {"schema_version": 1, "artifacts": entries,
               "source_after": after, "source_unchanged": after == seeds["source_before"]}, exclusive=True)
    if after != seeds["source_before"]:
        raise EvidenceError("reviewed source fuzz corpus changed during execution")


def commands(case, toolchain, root, output, node=None, policy_dir=None, *, binary=None):
    c = case["config"]
    kind = case["runner"]
    cargo = ["cargo", f"+{toolchain}"]
    test = cargo + ["test", "--locked", "--release"]
    core = test + ["-p", "zfstack", "--no-default-features"]
    adapter = test + ["-p", "zfstack", "--features", "tokio,test-peer"]
    mapping = {
        "lint": [cargo + ["fmt", "--all", "--", "--check"],
                 cargo + ["clippy", "--locked", "--workspace", "--all-targets", "--features", "zfstack/tokio"]],
        "core-build": [cargo + ["build", "--locked", "--release", "-p", "zfstack", "--no-default-features"]],
        "core-test": [core], "adapter-test": [adapter],
        "adapter-qa": [adapter + ["tokio_adapter::tests::qa::"]],
        "budget-qa": [core + ["budget::tests::qa::"]],
        "harness-test": [test + ["-p", "zfharness"]],
        "bench-build": [cargo + ["build", "--locked", "--release", "-p", "zfbench"]],
        "bench-test": [test + ["-p", "zfbench"]],
        "bench-all-test": [test + ["-p", "zfbench", "--all-features"]],
        "loom": [cargo + ["test", "--locked", "--release", "--manifest-path", "qa-models/Cargo.toml"]],
        "feature-audit": [cargo + ["tree", "--locked", "-p", "zfstack", "--no-default-features",
                                     "--edges", "normal", "--prefix", "none"],
                          cargo + ["tree", "--locked", "-p", "zfstack", "--features", "tokio",
                                   "--edges", "normal,features", "--prefix", "none"]],
    }
    if kind in mapping:
        return mapping[kind]
    if kind.startswith("python-"):
        directory = {"python-qa": "qa/tests", "python-l2": "harness/l2", "python-perf": "bench/pareto"}[kind]
        return [[sys.executable, "-m", "unittest", "discover", "-s", directory, "-p", "test_*.py"]]
    if kind in ("l0", "replay"):
        binary = binary or root / "target/release" / ("zfharness.exe" if os.name == "nt" else "zfharness")
        build = cargo + ["build", "--locked", "--release", "-p", "zfharness"]
        if kind == "replay":
            script = safe_path(root / "harness/regressions", c["script"])
            if not script.is_file() or file_hash(script) != c["script_sha256"]:
                raise EvidenceError("missing/changed frozen replay script")
            return [build, [str(binary), "replay", "--script", str(script), "--json", str(output / "harness.json")]]
        scenario = c.get("scenario", "matrix")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,80}", scenario):
            raise EvidenceError("invalid scenario")
        mode = c.get("app_drive", "eager")
        if mode not in ("eager", "event-only") or type(c.get("full", False)) is not bool:
            raise EvidenceError("invalid application drive/full mode")
        run = [str(binary), "l0", "--scenario", scenario, "--seed", str(_integer(c, "seed", 1)),
               "--app-drive", mode, "--json", str(output / "harness.json")]
        if c.get("full"):
            run += ["--full"]
        return [build, run]
    if kind == "ignored":
        registry = read_json((policy_dir or root / "qa/policy") / "ignored.json")["tests"]
        name = c.get("test_name")
        if name not in {t["name"] for t in registry if t["schedule"] == "nightly"}:
            raise EvidenceError("ignored test is not registered for automated execution")
        return [adapter + [name, "--", "--ignored", "--exact", "--test-threads=1"]]
    if kind == "l1":
        for key, values in (("placement", ("cross-iface", "same-iface")), ("topology", ("shared-shard", "per-port-shard"))):
            if c.get(key) not in values:
                raise EvidenceError(f"invalid L1 {key}")
        flags = ("ports", "lights", "heavy_conns", "placement", "topology", "budget_mb", "seed", "secs", "warmup",
                 "idle_rr_secs", "rr_size", "rr_interval_ms", "connect_timeout_ms", "rate_mbps", "rate_up_mbps",
                 "rtt_ms", "loss", "loss_up", "queue_bdp", "sock_buf_kb", "client_cc")
        run = ["bash", "qa/isolate-linux.sh", "sh", "bench/run-isolate.sh"]
        for key in flags:
            if key not in c or isinstance(c[key], (list, dict, bool)):
                raise EvidenceError(f"missing/invalid frozen L1 setting: {key}")
            run.extend(["--" + key.replace("_", "-"), str(c[key])])
        return [cargo + ["build", "--locked", "--release", "-p", "zfbench"],
                run + ["--json", str(output / "l1.json")]]
    if kind in ("l2", "l2-queue-control"):
        rig, worker = Path(node["rig_path"]), Path(node["worker_binary"])
        if not rig.is_absolute() or not worker.is_absolute() or not worker.is_file():
            raise EvidenceError("missing fixed rig/worker artifacts")
        base = ["bash", "qa/isolate-linux.sh", "bash", "harness/l2/run-rig.sh", str(rig), str(worker)]
        if kind == "l2":
            return [base + [str(output / "rig")]]
        if "queue_control" not in c or "service_contract" not in case:
            raise EvidenceError("queue negative control requires frozen injection and service contract")
        baseline = {key: value for key, value in c.items() if key != "queue_control"}
        return [["env", *(key + "=" + value for key, value in sorted(l2_environment(config).items())),
                 *base, str(output / phase / "rig")] for phase, config in (("baseline", baseline), ("fault", c))]
    if kind == "miri":
        return [["cargo", "+nightly-2026-07-11", "miri", "test", "--locked", "-p", "zfstack",
                 "--lib", "--no-default-features", c["test_filter"], "--", "--exact"]]
    if kind == "sanitizer":
        target = read_json(output / "safety-capability.json")["target"]
        return [cargo + ["test", "--locked", "-Zbuild-std", "--target", target,
                 "-p", "zfstack", "--lib", "--no-default-features", c["test_filter"]]]
    if kind == "fuzz":
        target = c.get("target")
        if target not in ("wire_parser", "sequence_sack", "event_script"):
            raise EvidenceError("unregistered fuzz target")
        capability = read_json(output / "safety-capability.json")
        seeds = read_json(output / "fuzz-seeds.json")
        if case["profile"] != "dev":
            raise EvidenceError("unregistered fuzz build profile")
        return [cargo + ["fuzz", "run", "--dev", "--sanitizer", "address", "--target", capability["target"],
                         "--target-dir", seeds["build_path"], target, seeds["corpus_path"],
                         "--", "-max_total_time=" + str(_integer(c, "seconds", 30, 1, 3600)), "-seed=1",
                         "-artifact_prefix=" + seeds["artifact_path"] + os.sep]]
    if kind in ("coverage", "external-performance"):
        return []
    raise EvidenceError(f"unsupported runner: {kind}")


def _test_count(text, python=False):
    if python:
        total = sum(int(n) for n in re.findall(r"Ran (\d+) tests?", text))
        skipped = sum(int(n) for n in re.findall(r"(?m)^(?:OK|FAILED) \([^\n)]*\bskipped=(\d+)", text))
        return max(0, total - skipped)
    return sum(int(n) for n in re.findall(r"test result: (?:ok|FAILED)\. (\d+) passed", text))


def _functional_evidence(case, output, records):
    assertions = []
    kind = case["runner"]
    if not records and kind != "coverage":
        return "NOT_RUN", [{"id": "execution.missing_commands", "status": "NOT_RUN", "kind": "precondition", "detail": "no executed command records"}]
    state = records[-1]["status"] if records else "completed"
    if state in ("timeout", "cancelled"):
        return "NOT_RUN", assertions
    passed = not any(r["exit_code"] != 0 or r["status"] != "completed" for r in records)
    detail = "all child processes completed successfully" if passed else "a child exited nonzero"
    if kind == "feature-audit" and passed:
        expected = commands(case, case["toolchain"], Path(), output)
        if len(records) != 2 or [r.get("argv") for r in records] != expected:
            return "NOT_RUN", [{"id": "features.missing_graph", "status": "NOT_RUN", "kind": "precondition",
                                "detail": "both locked core and Tokio normal dependency graphs are required"}]
        core_tree, tokio_tree = [(output / f"command-{i}.log").read_text() for i in range(2)]
        if not all(tree.startswith("zfstack v") for tree in (core_tree, tokio_tree)) or "tokio v" not in tokio_tree:
            return "NOT_RUN", [{"id": "features.missing_graph", "status": "NOT_RUN", "kind": "precondition",
                                "detail": "missing selected core or Tokio dependency graph"}]
        leaked = (any(word in core_tree for word in ("tokio", "loom", "libfuzzer"))
                  or any(word in tokio_tree for word in ('tokio feature "test-util"', "loom v", "libfuzzer")))
        passed = not leaked
        return ("PASS" if passed else "FAIL"), [{"id": "features.production_dependencies", "status": "PASS" if passed else "FAIL",
                    "kind": "safety", "detail": "test-only dependencies or features leaked into production graphs" if leaked
                    else "core and Tokio production dependency graphs exclude test-only dependencies and test-util"}]
    if kind in ("l0", "replay"):
        if not (output / "harness.json").is_file():
            return "NOT_RUN", [{"id": "evidence.harness_missing", "status": "NOT_RUN", "kind": "precondition", "detail": "no structured harness output"}]
        raw = read_json(output / "harness.json")
        if kind == "l0":
            config = case["config"]
            expected_scenario = config["scenario"]
            rows = raw.get("scenarios", []) if expected_scenario == "matrix" else [raw]
            identity_ok = (raw.get("scenario") == expected_scenario and raw.get("app_drive") == config["app_drive"]
                           and isinstance(rows, list) and bool(rows))
            expected_names = config.get("expected_scenarios", [expected_scenario])
            if identity_ok:
                identity_ok = Counter(r.get("scenario") for r in rows) == Counter(expected_names)
            for row in rows if isinstance(rows, list) else []:
                identity_ok = identity_ok and isinstance(row, dict) and row.get("app_drive") == config["app_drive"] and row.get("seed") == config["seed"]
                identity_ok = identity_ok and isinstance(row.get("assertions"), list) and bool(row["assertions"]) and bool(row.get("contract_ids"))
            if not identity_ok:
                return "NOT_RUN", [{"id": "evidence.l0_identity", "status": "NOT_RUN", "kind": "precondition",
                                     "detail": "missing/wrong scenario set, mode, seed or assertions"}]
        else:
            config = case["config"]
            replay = raw.get("replay", {}).get("input", {})
            if (any(raw.get(k) != config[k] for k in ("scenario", "app_drive", "seed")) or not replay
                    or digest(replay) != config["script_input_hash"] or not raw.get("assertions")):
                return "NOT_RUN", [{"id": "evidence.replay_identity", "status": "NOT_RUN", "kind": "precondition",
                                     "detail": "missing replay input/assertions or changed scenario/mode/seed"}]
        passed = passed and raw.get("ok") is True
        rows = raw.get("scenarios", [raw])
        if any(a.get("status") == "FAIL" for row in rows for a in row.get("assertions", [])):
            passed = False
        elif any(a.get("status") != "PASS" for row in rows for a in row.get("assertions", [])):
            return "NOT_RUN", [{"id": "evidence.assertion_status", "status": "NOT_RUN", "kind": "precondition", "detail": "incomplete harness assertion"}]
        if raw.get("scenario") == "matrix" or "scenarios" in raw:
            rows = raw.get("scenarios", [])
            passed = passed and bool(rows) and all(row.get("ok") is True for row in rows)
        detail = "harness contracts and nonempty scenario set" if passed else str(raw.get("errors", "harness failure"))[:8000]
    if kind in ("core-test", "adapter-test", "adapter-qa", "budget-qa", "harness-test", "bench-test", "bench-all-test", "ignored", "loom", "miri", "sanitizer") or kind.startswith("python-"):
        count = sum(_test_count((output / f"command-{i}.log").read_text(errors="replace"), kind.startswith("python-")) for i in range(len(records)))
        if count == 0 and passed:
            return "NOT_RUN", [{"id": "evidence.no_tests", "status": "NOT_RUN", "kind": "precondition", "detail": "selector executed zero tests"}]
        detail += f"; {count} passing tests observed"
    if kind == "fuzz" and passed:
        seeds, failures = read_json(output / "fuzz-seeds.json"), read_json(output / "fuzz-artifacts.json")
        capability = read_json(output / "safety-capability.json")
        if (not seeds.get("initial_seeds") or seeds.get("target") != case["config"]["target"]
                or not failures.get("source_unchanged") or failures.get("source_after") != seeds.get("source_before")
                or capability.get("errors") or not capability.get("host") or capability.get("host") != capability.get("target")
                or failures.get("artifacts") or len(records) != 1
                or records[0].get("argv") != commands(case, case["toolchain"], Path(), output)[0]):
            return "NOT_RUN", [{"id": "evidence.fuzz_identity", "status": "NOT_RUN", "kind": "precondition",
                                "detail": "missing or inconsistent native capability, copied seeds, source integrity or command evidence"}]
        logs = "\n".join((output / f"command-{i}.log").read_text(errors="replace") for i in range(len(records)))
        counts = re.findall(r"Done (\d+) runs|#(\d+)\s+DONE", logs)
        if not any(int(a or b) > 0 for a, b in counts):
            return "NOT_RUN", [{"id": "evidence.no_fuzz_runs", "status": "NOT_RUN", "kind": "precondition", "detail": "no completed fuzz executions"}]
    assertions.append({"id": "execution.contracts", "status": "PASS" if passed else "FAIL", "kind": "execution", "detail": detail})
    return ("PASS" if passed else "FAIL"), assertions


def run_case(plan, case, root, output, node=None, lease_dir=None, policy_dir=None):
    if case["runner"] == "external-performance":
        from .perf_run import run_performance_case
        return run_performance_case(plan, case, root, output, node, lease_dir, policy_dir)
    output.mkdir(parents=True, exist_ok=False)
    start = time.monotonic_ns()
    run_id = str(uuid.uuid4())
    environment = collect_environment()
    before = source_identity(root)
    records, reasons = [], []
    status, exit_code, functional, assertions = "completed", 0, "NOT_RUN", []
    phases = ["PREPARE"]
    cleanup = "PASS"
    manifests = None
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    observations, perf_metrics, perf_validity = {}, {}, "UNKNOWN"
    worker = None
    runnable_binary = None
    runnable_binary_hash = None
    fuzz_seeds = None
    try:
        if shutil.disk_usage(output).free < 512 * 1024 * 1024:
            raise EvidenceError("disk preflight: less than 512 MiB free")
        errors = capability_errors(case["runner"], node)
        if case["platform"] != platform_id():
            errors.append(f"native platform mismatch: requested {case['platform']}, actual {platform_id()}")
        if not errors and case["runner"] in ("fuzz", "sanitizer"):
            capability = safety_capability(case, root, env)
            write_json(output / "safety-capability.json", capability, exclusive=True)
            errors.extend(capability["errors"])
        if errors:
            status, exit_code = "capability_missing", None
            reasons.extend(errors)
        else:
            if case["runner"] == "fuzz":
                fuzz_seeds = prepare_fuzz(case, root, output)
            if case["runner"] in ("l0", "replay", "l1"):
                package = "zfbench" if case["runner"] == "l1" else "zfharness"
                runnable_binary = prepare_native_binary(root, case["toolchain"], package, env)
            needs_lease = case["runner"] in ("l1", "l2", "l2-queue-control")
            if needs_lease and not lease_dir:
                raise EvidenceError("physical-domain lease directory is required")
            lease = Lease(lease_dir, node["physical_domain"], run_id, node["generation"]) if needs_lease else contextlib.nullcontext()
            with lease as owned:
                phases.extend(["LEASE", "PRECHECK"] if needs_lease else ["PRECHECK"])
                if case["runner"] == "coverage":
                    write_json(output / "coverage.json", audit(root, policy_dir), exclusive=True)
                if case["runner"] == "sanitizer":
                    env["RUSTFLAGS"] = "-Zsanitizer=address"
                    env["ASAN_OPTIONS"] = "detect_leaks=1"
                if case["runner"] in ("l2", "l2-queue-control"):
                    worker = worker_identity(node, plan["tested_sha"], case)
                    write_json(output / "worker-manifest.json", worker, exclusive=True)
                    env.update(l2_environment(case["config"]))
                    env["RIG_BUILD"] = str(Path(node["rig_build"]).resolve())
                    env["PYTHON"] = sys.executable
                    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
                    if case["config"].get("outbound") == "relay":
                        env["L2_FORWARDER_BINARY"] = str(Path(node["forwarder_binary"]).resolve())
                argv_list = commands(case, case["toolchain"], root, output, node, policy_dir, binary=runnable_binary)
                phases.append("MEASURE")
                for i, argv in enumerate(argv_list):
                    if i > 0 and runnable_binary is not None and (not runnable_binary.is_file() or runnable_binary.stat().st_size <= 0):
                        raise EvidenceError("successful Cargo build omitted the frozen runnable artifact")
                    if i > 0 and runnable_binary is not None:
                        runnable_binary_hash = file_hash(runnable_binary)
                    remaining = case["timeout_seconds"] - (time.monotonic_ns() - start) / 1e9
                    if remaining <= 0:
                        status, exit_code = "timeout", None
                        break
                    record = run_child(argv, root, output / f"command-{i}.log", remaining, env, owned)
                    records.append(record)
                    status, exit_code = record["status"], record["exit_code"]
                    if status != "completed":
                        break
                phases.append("VALIDATE")
                if fuzz_seeds is not None:
                    if any(r["exit_code"] != 0 or r["status"] != "completed" for r in records):
                        functional, assertions = _functional_evidence(case, output, records)
                    finish_fuzz(root, output, fuzz_seeds)
                functional, assertions = _functional_evidence(case, output, records)
                if status in ("timeout", "cancelled"):
                    functional = "NOT_RUN"
                if case["runner"] in ("l1", "l2", "l2-queue-control"):
                    if case["runner"] == "l1":
                        functional, details, observations = l1_evidence(case, output)
                    elif case["runner"] == "l2":
                        freeze_l2_raw(output)
                        functional, details, observations, perf_validity, perf_metrics = l2_evidence(case, output, worker)
                        worker_identity(node, plan["tested_sha"], case)
                    else:
                        from .integration import queue_negative_evidence
                        for phase in ("baseline", "fault"):
                            freeze_l2_raw(output / phase)
                        functional, details, observations = queue_negative_evidence(case, output / "baseline", output / "fault", worker)
                        worker_identity(node, plan["tested_sha"], case)
                    assertions = [{"id": case["runner"] + ".path_workload_contract", "status": functional,
                                   "kind": "execution", "detail": "; ".join(details) or "Path, full workload and payload evidence verified"}]
                    if functional == "NOT_RUN":
                        reasons.extend(details)
            phases.extend(["CLEANUP", "RELEASE"] if needs_lease else ["CLEANUP"])
    except (OSError, EvidenceError, KeyError, ValueError, TypeError) as error:
        reasons.append(str(error))
        status, exit_code = "capability_missing", None
        if "lease" in str(error):
            cleanup = "UNKNOWN"
    except KeyboardInterrupt:
        status, exit_code = "cancelled", None
        reasons.append("runner interrupted")
    finally:
        if fuzz_seeds is not None and not (output / "fuzz-artifacts.json").exists():
            try:
                finish_fuzz(root, output, fuzz_seeds)
            except (OSError, EvidenceError) as error:
                reasons.append(str(error))
        cleanup = "PASS" if all(r.get("cleanup_status", "UNKNOWN") == "PASS" for r in records) else "UNKNOWN"
        try:
            kind = case["runner"]
            package = {"harness-test": "zfharness", "l0": "zfharness", "replay": "zfharness", "loom": "zfstack-qa-models",
                       "fuzz": "zfstack-fuzz", "l1": "zfbench"}.get(kind, "zfbench" if kind.startswith("bench-") else "zfstack")
            manifest_path = {"loom": "qa-models/Cargo.toml", "fuzz": "fuzz/Cargo.toml"}.get(kind)
            binaries = []
            if kind in ("l0", "replay", "l1"):
                if runnable_binary is None or not runnable_binary.is_file() or runnable_binary.stat().st_size <= 0:
                    raise EvidenceError("actual runnable artifact is missing; binary identity is incomplete")
                if runnable_binary_hash is not None and file_hash(runnable_binary) != runnable_binary_hash:
                    raise EvidenceError("runnable artifact changed after its execution was admitted")
                binaries = [runnable_binary]
            if worker:
                binaries.append(Path(node["worker_binary"]))
            if worker:
                manifests = integration_manifest(root, worker, node["worker_binary"])
            else:
                manifests = build_manifest(root, case["toolchain"], case["profile"], case["features"], binaries,
                                           manifest_path=manifest_path, package=package, build_env=env)
        except EvidenceError as error:
            manifests = {"schema_version": 1, "source": before, "identity_error": str(error)}
            reasons.append(str(error))
        if source_identity(root) != before:
            reasons.append("source changed during execution")
        write_json(output / "build-manifest.json", manifests, exclusive=True)
        write_json(output / "environment.json", environment, exclusive=True)
        write_json(output / "execution.json", {"phases": phases, "commands": records}, exclusive=True)
    # Keep the legacy L2 payload under a distinct name; only the outer v2
    # envelope is named result.json and participates in aggregation.
    rig_roots = [output / phase / "rig" for phase in ("baseline", "fault")] if case["runner"] == "l2-queue-control" else [output / "rig"]
    for rig_root in rig_roots:
        for legacy in rig_root.rglob("result.json"):
            legacy.rename(legacy.with_name("l2-result.json"))
    artifacts = [{"path": p.name, "sha256": file_hash(p), "size_bytes": p.stat().st_size}
                 for p in sorted(output.iterdir()) if p.is_file() and p.name not in ("build-manifest.json", "environment.json")]
    for rig_root in rig_roots:
        for name in ("l2-result.json", "raw.json.gz", "analysis.json", "worker.sha256", "build-info.json", "qdisc.json",
                     "queue-control.json", "forwarder.sha256", "forwarder-build-info.json"):
            path = rig_root / name
            if path.is_file():
                artifacts.append({"path": path.relative_to(output).as_posix(), "sha256": file_hash(path), "size_bytes": path.stat().st_size})
    functional_validity = "VALID" if functional in ("PASS", "FAIL") and not reasons else "UNKNOWN"
    # Independently observed functional failures are retained even if other
    # evidence is incomplete; validity never overwrites the failure itself.
    result = {"schema_version": 2, "run_id": run_id, "block_id": "functional", "arm": "B",
              "case_id": case["case_id"], "case_version": case["case_version"],
              "case_instance_id": case["case_instance_id"], "tested_sha": plan["tested_sha"],
              "plan_hash": digest(plan), "build_manifest_hash": digest(manifests), "environment_hash": digest(environment),
              "config_hash": digest(case["config"]), "validity": {"functional": functional_validity,
              "performance": perf_validity, "reasons": reasons}, "functional": {"status": functional, "assertions": assertions},
              "performance": {"status": "NOT_APPLICABLE" if case["performance"]["mode"] == "not_applicable" else "INCONCLUSIVE",
                              "metrics": perf_metrics, "analysis_hash": None}, "attribution": "UNATTRIBUTED",
              "execution": {"status": status, "exit_code": exit_code, "start_ns": start, "end_ns": time.monotonic_ns(),
                            "clock": "process-monotonic-ns", "cleanup": cleanup}, "artifacts": artifacts,
              "observations": {"phases": phases, "scope": case["layer"], **observations,
                               "process_containment": [{k: r.get(k) for k in ("cleanup_scope", "cleanup_status", "cleanup_limits", "lifecycle_detail")} for r in records]}}
    if result["functional"]["status"] == "PASS" and status != "completed":
        result["functional"]["status"] = "NOT_RUN"
    validate_result(result)
    write_json(output / "result.json", result, exclusive=True)
    return result


def run_plan(plan, root, output, policy_dir, case_ids=(), node=None, lease_dir=None):
    check_trusted_plan(plan, policy_dir)
    root, output = Path(root).resolve(), Path(output).resolve()
    if source_identity(root)["sha"] != plan["tested_sha"]:
        raise EvidenceError("checked-out SHA differs from tested SHA")
    selected = [case for case in plan["cases"] if not case_ids or case["case_id"] in case_ids]
    if not selected or set(case_ids) - {c["case_id"] for c in selected}:
        raise EvidenceError("empty/unknown case selection")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "plan.json", plan, exclusive=True)
    results = []
    for case in selected:
        print(f"QA {case['case_id']} ({case['layer']})", flush=True)
        result = run_case(plan, case, root, output / case["case_instance_id"], node, lease_dir, policy_dir)
        results.append(result)
        print(f"  functional={result['functional']['status']} validity={result['validity']['functional']}", flush=True)
        if result["execution"]["status"] == "cancelled":
            break
    return results
