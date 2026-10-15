"""Synthetic contract fixtures; these never calibrate or qualify a real node."""
from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa.baseline import promote
from qa.common import ROOT, EvidenceError, canonical, digest, file_hash, read_json, write_json
from qa.plan import make_plan
from qa.performance import inspect_rows, tool
from qa.release import (NATIVE_PLATFORMS, WORKFLOWS, _engine, collect_runs, evaluate_release,
                        main, update_locks, validate_provenance, verify_calibration)
from qa.run import commands, finish_fuzz, prepare_fuzz
from qa.tests.test_identity_process import environment_fixture, manifest_fixture
from qa.tests.test_integration import worker_manifest_fixture

SHA = "1" * 40
CAL = "c" * 64
ANCHOR = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
NOW = dt.datetime(2026, 10, 15, tzinfo=dt.timezone.utc)


def provenance_fixture():
    return {name: {"id": i + 1, "repository": {"full_name": "owner/repo"},
                   "head_repository": {"full_name": "owner/repo"}, "path": path,
                   "event": "workflow_dispatch", "head_branch": "main", "head_sha": SHA,
                   "status": "completed", "conclusion": "success"}
            for i, (name, path) in enumerate(WORKFLOWS.items())}


def probe_measurement():
    return {"tool": "zfstack-pareto-probe", "schema_version": 2, "ok": True,
            "case": "budget", "iterations": 1, "flows": 1, "peers": 1, "payload_bytes": 1,
            "work_wall_ns": 200, "work_cpu_ns": 100, "operations": 1, "effective_bytes": 0,
            "allocation_requested_bytes": 1, "checksum_u64": 0, "expected_checksum_u64": 0,
            "final_reserved_bytes": 0, "all_budget_leases_released": True,
            "virtual_clock_used_for_rate": False, "real_network_io": False}


def freeze_probe_rows(directory, plan, rows, environment):
    """Construct every raw slot; production audit parses logs and recomputes metrics."""
    runs = directory / "runs"
    runs.mkdir()
    for index, row in enumerate(rows):
        measurement = probe_measurement()
        raw = canonical(measurement).decode() + "\n"
        log = runs / f"probe-{index // 4}-{index % 4}.log"
        log.write_text(raw)
        execution = row["execution"]
        row["run_id"] = f"{digest(plan)}:{row['block_id']}:{row['position']}"
        parsed, errors = tool("analyze").validate_row({"variant": "baseline" if row["arm"] == "A" else "candidate",
            "pair": 0, "exit_code": 0, "binary_sha256": row["binary_sha256"], "measurements": [measurement]},
            0, {}, {"args": plan["config"]["args"]})
        if errors:
            raise AssertionError(errors)
        row["metrics"] = parsed["metrics"]
        sample = {**environment, "pid": 42, "at_ns": execution["start_ns"], "phase": "running", "compilers": [],
                  "cgroup": {"path": "/", "ancestors": copy.deepcopy(environment["cgroup_limits"])}}
        row["observations"] = {"measurements": [measurement], "stdout": raw, "stderr": "", "environment_samples": [sample],
            "process_lifecycle": {**{key: execution[key] for key in ("status", "exit_code", "start_ns", "end_ns")},
                "cleanup_status": "PASS", "cleanup_scope": "linux-subreaper", "cleanup_limits": []},
            "output_artifact": {"path": log.name, "size_bytes": log.stat().st_size, "sha256": file_hash(log),
                "limit_bytes": plan["config"]["max_output_bytes"], "streams": "stdout+stderr"}}
    with (directory / "perf-rows.jsonl").open("w") as stream:
        for row in rows:
            stream.write(canonical(row).decode() + "\n")
    audited = inspect_rows(plan, rows, directory, environment)
    if audited != {"functional": "PASS", "errors": [], "functional_errors": [], "failures": []}:
        raise AssertionError(audited)
    write_json(directory / "perf-audit.json", audited)


def perf_plan(manifest, environment):
    return {"schema_version": 1, "runner_id": "fixture-only", "profile": "release", "case_id": "PERF",
            "case_version": 1, "mode": "enforce", "comparison": "AB", "config": {"args": ["budget", "--iterations", "1", "--flows", "1", "--payload", "1"],
                "timeout_sec": 2, "max_output_bytes": 1048576, "sample_interval_sec": .1,
                "cpu": 0, "expected_measurement": {key: value for key, value in probe_measurement().items()
                    if key in (*tool("analyze").WORKLOAD_FIELDS, *tool("analyze").EXACT_FIELDS)}},
            "binary_sha256": {"A": "a" * 64, "B": "b" * 64}, "build_manifest_hash": digest(manifest),
            "environment_hash": digest(environment), "blocks": [{"block_id": f"block-{i}", "order": "ABBA" if i % 2 == 0 else "BAAB"} for i in range(20)],
            "metrics": [{"id": "cpu_primary", "direction": "lower", "unit": "CPU-ns/operation", "budget_fraction": .03}],
            "alpha": .05, "method": "block_t", "seed": 17, "min_blocks": 2,
            "max_abs_lag1": .95, "max_abs_skew": 2.0, "calibration_id": CAL}


def perf_rows(plan, prefix, factor=1.0):
    rows = []
    for block_index, block in enumerate(plan["blocks"]):
        for position, arm in enumerate(block["order"]):
            index = block_index * 4 + position
            rows.append({"run_id": f"{prefix}:{index}", "case_id": plan["case_id"], "case_version": plan["case_version"],
                         "block_id": block["block_id"], "position": position, "arm": arm,
                         "plan_hash": digest(plan), "config_hash": digest(plan["config"]),
                         "binary_sha256": plan["binary_sha256"][arm], "build_manifest_hash": plan["build_manifest_hash"],
                         "environment_hash": plan["environment_hash"],
                         "validity": {"functional": "VALID", "performance": "VALID"}, "functional": {"status": "PASS"},
                         "execution": {"status": "completed", "exit_code": 0, "cleanup": "PASS", "clock": "CLOCK_MONOTONIC",
                                       "start_ns": index * 100 + 1, "end_ns": index * 100 + 99},
                         "metrics": {"cpu_primary": 100 * (factor if arm == "B" else 1)}})
    return rows


def calibration_fixture(plan):
    spec = {"schema_version": 1, "calibration_id": CAL, "target_plan": copy.deepcopy(plan),
            "min_training_blocks": 20, "min_null_trials": 1, "min_delta_trials": 1,
            # Deliberately loose synthetic criteria exercise the contract; they
            # are neither repository policy nor evidence of real statistical power.
            "max_false_positive_rate": .99, "min_detection_rate": .001, "trials": []}
    datasets = {}
    for name in ("run-AA", "build-AA", "held-out-null", "held-out-delta"):
        trial_plan = copy.deepcopy(plan)
        trial_plan["comparison"] = "AB" if name == "held-out-delta" else "AA"
        if name in ("run-AA", "held-out-null"):
            trial_plan["binary_sha256"]["B"] = trial_plan["binary_sha256"]["A"]
        provenance = ({"same_source_verified": True, "independent_build_manifest_hashes": ["d" * 64, "e" * 64], "independent_build_invocation_ids": ["build1", "build2"]}
                      if name == "build-AA" else
                      {"kind": "controlled-delay", "control_artifact_sha256": "f" * 64, "synthetic": False}
                      if name == "held-out-delta" else {})
        trial = {"trial_id": name, "kind": name, "plan_file": f"calibration/{name}/perf-plan.json",
                 "rows_file": f"calibration/{name}/perf-rows.jsonl", "provenance_file": f"calibration/{name}/calibration-provenance.json"}
        if name == "held-out-delta":
            trial["known_delta"] = {"cpu_primary": .10}
        spec["trials"].append(trial)
        datasets[name] = {"plan": trial_plan, "rows": perf_rows(trial_plan, name, 1.10 if name == "held-out-delta" else 1.0),
                          "provenance": provenance}
    calibration = _engine("calibrate").calibrate(spec, datasets)
    return spec, datasets, calibration


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = self.root / "policy"
        self.policy.mkdir()
        self.evidence = self.root / "evidence"
        self.provenance = provenance_fixture()
        self.kwargs = {"provenance": self.provenance, "repository": "owner/repo", "default_branch": "main",
                       "native_status": "success", "generation": "fixture-generation", "now": NOW}
        self.perf_manifest = manifest_fixture()
        self.perf_manifest["binaries"] = [{"name": "fixture-only", "sha256": "b" * 64, "size_bytes": 1}]
        self.perf_env = environment_fixture()
        self.perf_env.update(kernel="fixture-kernel", cpuinfo="model name\t: fixture CPU\n")
        self.probe_env = {"kernel": "fixture-kernel", "cpu_model": "fixture CPU", "affinity": [0], "governors": {},
                          "cgroup_limits": [{"path": "/sys/fs/cgroup", "cpu.max": "max 100000",
                                             "cpuset.cpus.effective": "0", "memory.max": "max"}]}
        self.experiment = perf_plan(self.perf_manifest, self.probe_env)
        self.spec, self.datasets, self.calibration = calibration_fixture(self.experiment)
        self.experiment["calibration_hash"] = digest(self.calibration)
        self.rows = perf_rows(self.experiment, "candidate")
        self.analysis = _engine("perf_gate").evaluate(self.experiment, self.rows, self.calibration)
        definitions = []
        for ident, runner, layer in (("CORE", "core-test", "CORE"), ("L1", "l1", "L1"), ("L2", "l2", "L2"),
                                     ("MIRI", "miri", "CORE"), ("ASAN", "sanitizer", "CORE"),
                                     ("FUZZ", "fuzz", "CORE"), ("LOOM", "loom", "A1"), ("PERF", "external-performance", "P0")):
            config = {"lights": 1, "heavy_conns": 1} if runner == "l1" else {}
            if runner == "external-performance":
                config = {key: self.experiment["config"][key] for key in ("args", "timeout_sec", "max_output_bytes", "sample_interval_sec")}
                config.update(comparison=self.experiment["comparison"], blocks=len(self.experiment["blocks"]))
            definitions.append({"case_id": ident, "case_version": 1, "runner": runner, "layer": layer,
                                "profile": "release", "toolchain": "1.94.1", "features": [], "config": config,
                                "timeout_seconds": 10, "performance": {"mode": "not_applicable", "metrics": []}})
            if runner == "fuzz":
                definitions[-1].update(profile="dev", toolchain="nightly-2026-07-11",
                                       config={"target": "wire_parser", "seconds": 30})
        definitions[-1]["performance"] = {"mode": "enforce", "metrics": ["cpu_primary"], "calibration_id": CAL,
                                           "experiment_plan_hash": digest(self.experiment)}
        self.definitions = {case["case_id"]: case for case in definitions}
        self.definitions["L2"]["integration_build"] = {key: self.definitions["L2"][key] for key in ("toolchain", "profile", "features")}
        required = {"schema_version": 1, "toolchain": "1.94.1", "release_native_platforms": list(NATIVE_PLATFORMS),
                    "release_calibrations": [{"calibration_id": CAL, "sha256": digest(self.calibration),
                                              "generation": "fixture-generation", "expires": "2027-01-01T00:00:00Z"}],
                    "suites": {"portable": ["CORE"], "nightly": ["CORE", "L1", "L2"],
                               "safety": ["MIRI", "ASAN", "FUZZ", "LOOM"], "perf": ["PERF"],
                               "release": ["CORE", "L1", "L2", "PERF"]}}
        write_json(self.policy / "cases.json", {"schema_version": 1, "cases": definitions})
        write_json(self.policy / "required.json", required)
        write_json(self.policy / "known-failures.json", {"schema_version": 1, "exemptions": []})
        shutil.copytree(ROOT / "fuzz/corpus", self.root / "fuzz/corpus")
        # Raw L2 fixture is supplied by the inspector's own tests; no mocked
        # integration validator or summary PASS enters this release fixture.
        self.l2_source = self.root / "raw-l2"
        self.l2_source.mkdir()
        directory = ROOT / "harness/l2"
        sys.path.insert(0, str(directory))
        try:
            module_spec = importlib.util.spec_from_file_location("qa_release_l2_fixture", directory / "test_evidence.py")
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            result, _ = module.fixture(self.l2_source)
        finally:
            sys.path.pop(0)
        result["effective_config"]["worker_tuning_fingerprints"] = ["port1", "port2"]
        self.definitions["L2"]["config"] = {key: value for key, value in result["effective_config"].items()
                                               if key != "worker_tuning_fingerprints"}
        write_json(self.l2_source / "l2-result.json", result)
        (self.l2_source / "result.json").unlink()
        write_json(self.policy / "cases.json", {"schema_version": 1, "cases": list(self.definitions.values())})
        for platform in NATIVE_PLATFORMS:
            self.write_scope("portable", platform, self.evidence / "native" / f"qa-native-{platform}")
        for scope, suite in (("nightly", "nightly"), ("safety", "safety"), ("performance", "perf")):
            self.write_scope(suite, "linux-x86_64", self.evidence / scope)
        self.perf_dir = next((self.evidence / "performance").glob("*"))
        self.perf_case = make_plan(self.policy, "perf", SHA)["cases"][0]

    def write_scope(self, suite, platform, directory):
        plan = make_plan(self.policy, suite, SHA, platform=platform)
        system, machine = platform.split("-", 1)
        system = {"linux": "Linux", "darwin": "Darwin", "windows": "Windows"}[system]
        for case in plan["cases"]:
            target = directory / case["case_instance_id"]
            target.mkdir(parents=True)
            package = {"l1": "zfbench", "loom": "zfstack-qa-models", "fuzz": "zfstack-fuzz"}.get(case["runner"], "zfstack")
            manifest_path = {"loom": "qa-models/Cargo.toml", "fuzz": "fuzz/Cargo.toml"}.get(case["runner"], "Cargo.toml")
            manifest = manifest_fixture(system=system, machine=machine, package=package, manifest_path=manifest_path,
                                        profile=case["profile"], toolchain=case["toolchain"])
            if case["toolchain"].startswith("nightly"):
                manifest["rustc"] = manifest["rustc"].replace("1.94.1", "1.99.0-nightly")
            environment = environment_fixture(system, machine)
            if case["runner"] == "external-performance":
                manifest, environment = self.perf_manifest, self.perf_env
                for name, value in (("perf-plan.json", self.experiment), ("perf-environment.json", self.probe_env),
                                    ("perf-analysis.json", self.analysis), ("calibration.json", self.calibration),
                                    ("calibration-spec.json", self.spec)):
                    write_json(target / name, value)
                freeze_probe_rows(target, self.experiment, self.rows, self.probe_env)
                for trial in self.spec["trials"]:
                    data = self.datasets[trial["trial_id"]]
                    for key, field in (("plan_file", "plan"), ("rows_file", "rows"), ("provenance_file", "provenance")):
                        write_json(target / trial[key], data[field])
            if case["runner"] == "l1":
                write_json(target / "l1.json", {"tool": "zfbench", "ok": True, "errors": [], "config": case["config"],
                           "results": {"test": "isolate", "layer": "L1", "wireguard": False,
                                       "heavy": {"window_bytes": 1000, "window_wall_sec": 1},
                                       **{phase: [{"user": 0, "samples": {"n": 1}}] for phase in ("baseline_users", "loaded_users")}}})
            if case["runner"] == "l2":
                shutil.copytree(self.l2_source, target / "rig")
                worker = worker_manifest_fixture(case, tested_sha=SHA)
                write_json(target / "worker-manifest.json", worker)
                manifest.update(selected_package="zf-worker", manifest_path="worker-Cargo.toml", lock_path="worker-Cargo.lock",
                                lock_hash=worker["lock_sha256"], toolchain=worker["toolchain"], profile=worker["profile"],
                                requested_features=sorted(worker["features"]), rustc=worker["rustc_vv"], cargo=worker["cargo_version"],
                                host=worker["host"], target=worker["target"], build_flags=worker["build_flags"],
                                dependency_graph=worker["dependency_graph"], dependency_graph_hash=worker["dependency_graph_hash"],
                                binaries=[{"name": "zf-worker", "sha256": worker["binary_sha256"], "size_bytes": 1}])
            write_json(target / "build-manifest.json", manifest)
            write_json(target / "environment.json", environment)
            (target / "command-0.log").write_text("test result: ok. 1 passed; 0 failed\nDone 100 runs in 1 second(s)\n")
            argv = ["fixture"]
            if case["runner"] in ("fuzz", "sanitizer"):
                write_json(target / "safety-capability.json", {"host": manifest["host"], "target": manifest["target"],
                    "target_spec": {"supported-sanitizers": ["address", "leak"]}, "errors": []})
            if case["runner"] == "fuzz":
                # Match a staged run: writable inputs remain outside the archive,
                # while the copied original indices retain their actual paths.
                scratch = self.root / "fuzz-inputs" / case["case_instance_id"]
                scratch.mkdir(parents=True)
                seeds = prepare_fuzz(case, self.root, scratch)
                finish_fuzz(self.root, scratch, seeds)
                for name in ("fuzz-seeds.json", "fuzz-artifacts.json"):
                    shutil.copyfile(scratch / name, target / name)
                argv = commands(case, case["toolchain"], self.root, target)[0]
            write_json(target / "execution.json", {"commands": [{"argv": argv, "status": "completed", "exit_code": 0, "cleanup_status": "PASS"}]})
            result = {"schema_version": 2, "run_id": f"fixture:{suite}:{platform}:{case['case_id']}", "block_id": "functional", "arm": "B",
                      "case_id": case["case_id"], "case_version": 1, "case_instance_id": case["case_instance_id"],
                      "tested_sha": SHA, "plan_hash": digest(plan), "build_manifest_hash": digest(manifest),
                      "environment_hash": digest(environment), "config_hash": digest(case["config"]),
                      "validity": {"functional": "VALID", "performance": "UNKNOWN", "reasons": []},
                      "functional": {"status": "PASS", "assertions": [{"id": "fixture", "status": "PASS", "kind": "execution", "detail": "synthetic contract fixture"}]},
                      "performance": {"status": "NOT_APPLICABLE", "metrics": {}, "analysis_hash": None},
                      "attribution": "UNATTRIBUTED", "execution": {"status": "completed", "exit_code": 0,
                      "start_ns": 100, "end_ns": 200, "clock": "monotonic", "cleanup": "PASS"}, "artifacts": [], "observations": {}}
            if case["runner"] == "external-performance":
                result["observations"] = {"performance_plan_hash": digest(self.experiment),
                    "performance_environment_hash": digest(self.probe_env), "performance_config_hash": digest(self.experiment["config"]),
                    "qa_config_hash": digest(case["config"]), "node_generation": "fixture-generation"}
                write_json(target / "execution.json", {"source_before": manifest["source"], "source_after": manifest["source"],
                    "admission_errors": [], "runner": {"status": "completed", "exit_code": 0, "start_ns": 0, "end_ns": 100000,
                        "cleanup_status": "PASS", "cleanup_scope": "linux-subreaper", "cleanup_limits": []}})
                result["validity"]["performance"] = "VALID"
                result["performance"] = {"status": "PASS", "metrics": self.analysis["metrics"],
                                         "analysis_hash": file_hash(target / "perf-analysis.json")}
            write_json(target / "result.json", result)
            self.reinventory(target)

    @staticmethod
    def reinventory(directory):
        result = read_json(directory / "result.json")
        result["artifacts"] = [{"path": path.relative_to(directory).as_posix(), "sha256": file_hash(path), "size_bytes": path.stat().st_size}
                               for path in sorted(directory.rglob("*")) if path.is_file() and path != directory / "result.json"]
        write_json(directory / "result.json", result)

    def report(self, **overrides):
        return evaluate_release(SHA, self.evidence, self.policy, **{**self.kwargs, **overrides})

    def test_complete_raw_release_and_review_only_promotion(self):
        report = self.report()
        self.assertEqual(report["status"], "PASS", report)
        self.assertEqual(len(report["scopes"]), 7)
        plan = make_plan(self.policy, "release", SHA)
        candidate = promote(plan, report, generation="fixture-generation", release_anchor=ANCHOR,
                            calibration_hashes=[digest(self.calibration)], expires="2026-12-01T00:00:00Z",
                            evidence_root=self.evidence, policy_dir=self.policy, **{key: value for key, value in self.kwargs.items() if key != "generation"})
        self.assertEqual(candidate["status"], "REVIEW_REQUIRED")
        self.assertFalse((self.policy / "baseline.json").exists())

    def test_missing_probe_raw_log_cannot_certify_release(self):
        (self.perf_dir / "runs/probe-0-0.log").unlink()
        self.reinventory(self.perf_dir)
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_allowlisted_upload_preserves_complete_raw_release_evidence(self):
        from qa.archive import stage
        staged = self.root / "staged-evidence"
        stage(self.evidence, staged)
        report = evaluate_release(SHA, staged, self.policy, **self.kwargs)
        self.assertEqual(report["status"], "PASS", report["blocked"])

    def test_rehashed_probe_output_tamper_is_independently_recomputed(self):
        path = self.perf_dir / "runs/probe-0-1.log"
        measurement = read_json(path)
        measurement["work_cpu_ns"] = 1
        write_json(path, measurement)
        self.reinventory(self.perf_dir)
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_missing_platform_is_not_green(self):
        shutil.rmtree(self.evidence / "native" / "qa-native-windows-x86_64")
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_uploaded_pass_summary_does_not_replace_raw_results(self):
        shutil.rmtree(self.evidence / "safety")
        write_json(self.evidence / "safety/summary.json", {"status": "PASS", "tested_sha": SHA})
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_wrong_native_environment_is_rejected(self):
        target = next((self.evidence / "native" / "qa-native-darwin-aarch64").glob("*"))
        environment = environment_fixture("Linux", "aarch64")
        write_json(target / "environment.json", environment)
        result = read_json(target / "result.json"); result["environment_hash"] = digest(environment)
        write_json(target / "result.json", result); self.reinventory(target)
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_job_skipped_and_wrong_tested_sha_are_rejected(self):
        self.assertEqual(self.report(native_status="skipped")["status"], "BLOCKED")
        self.provenance["nightly"]["head_sha"] = "3" * 40
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_calibration_report_cannot_replace_missing_raw_trial(self):
        missing = self.perf_dir / self.spec["trials"][0]["rows_file"]
        missing.unlink(); self.reinventory(self.perf_dir)
        with self.assertRaises(EvidenceError):
            verify_calibration(self.perf_dir, self.perf_case, self.policy, now=NOW)
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_rehashed_calibration_trial_tamper_is_recomputed(self):
        path = self.perf_dir / self.spec["trials"][-1]["rows_file"]
        values = read_json(path)
        for row in values:
            if row["arm"] == "B": row["metrics"]["cpu_primary"] = 100
        write_json(path, values); self.reinventory(self.perf_dir)
        with self.assertRaisesRegex(EvidenceError, "recomputed"):
            verify_calibration(self.perf_dir, self.perf_case, self.policy, now=NOW)

    def test_registry_generation_and_expiry_are_required(self):
        self.assertEqual(self.report(generation="another-generation")["status"], "BLOCKED")
        required = read_json(self.policy / "required.json")
        required["release_calibrations"] = []
        write_json(self.policy / "required.json", required)
        with self.assertRaisesRegex(EvidenceError, "registered"):
            verify_calibration(self.perf_dir, self.perf_case, self.policy, now=NOW)

    def test_expired_calibration_and_longer_baseline_expiry_are_rejected(self):
        with self.assertRaises(EvidenceError):
            verify_calibration(self.perf_dir, self.perf_case, self.policy,
                               now=dt.datetime(2028, 1, 1, tzinfo=dt.timezone.utc))
        report = self.report()
        self.assertEqual(report["status"], "PASS", report["blocked"])
        with self.assertRaisesRegex(EvidenceError, "outlive"):
            promote(make_plan(self.policy, "release", SHA), report, generation="fixture-generation",
                    release_anchor=ANCHOR, calibration_hashes=[digest(self.calibration)], expires="2028-01-01T00:00:00Z",
                    evidence_root=self.evidence, policy_dir=self.policy,
                    **{key: value for key, value in self.kwargs.items() if key != "generation"})

    def test_modified_summary_is_rejected_even_with_raw_evidence(self):
        report = self.report()
        self.assertEqual(report["status"], "PASS", report["blocked"])
        report["coverage"]["completed"] += 1
        with self.assertRaisesRegex(EvidenceError, "independently"):
            promote(make_plan(self.policy, "release", SHA), report, generation="fixture-generation",
                    release_anchor=ANCHOR, calibration_hashes=[digest(self.calibration)], expires="2026-12-01T00:00:00Z",
                    evidence_root=self.evidence, policy_dir=self.policy,
                    **{key: value for key, value in self.kwargs.items() if key != "generation"})

    def test_shadow_and_inconclusive_cannot_certify_release(self):
        result = read_json(self.perf_dir / "result.json")
        result["performance"]["status"] = "INCONCLUSIVE"
        result["validity"]["performance"] = "UNKNOWN"
        write_json(self.perf_dir / "result.json", result)
        self.assertEqual(self.report()["status"], "BLOCKED")

    def test_functional_failure_survives_missing_other_scope(self):
        target = next((self.evidence / "nightly").glob("*"))
        result = read_json(target / "result.json")
        result["functional"]["status"] = "FAIL"; result["functional"]["assertions"][0]["status"] = "FAIL"
        write_json(target / "result.json", result)
        shutil.rmtree(self.evidence / "safety")
        self.assertEqual(self.report()["status"], "FAIL")

    def test_forged_promotion_summary_is_not_accepted(self):
        plan = make_plan(self.policy, "release", SHA)
        with self.assertRaisesRegex(EvidenceError, "raw evidence"):
            promote(plan, {"status": "PASS", "tested_sha": SHA, "plan_hash": digest(plan)},
                    generation="fixture-generation", release_anchor=ANCHOR,
                    calibration_hashes=[digest(self.calibration)], expires="2026-12-01T00:00:00Z", now=NOW)

    def test_moving_release_anchor_is_rejected(self):
        with self.assertRaises(EvidenceError):
            promote(make_plan(self.policy, "release", SHA), {}, generation="fixture-generation",
                    release_anchor="main", calibration_hashes=[], expires="2026-12-01T00:00:00Z", now=NOW)


class ProvenanceAndEntrypointTests(unittest.TestCase):
    def test_wrong_workflow_repo_branch_and_conclusion_are_rejected(self):
        for key, bad in (("path", ".github/workflows/unrelated.yml"), ("head_branch", "feature"),
                         ("conclusion", "failure"), ("status", "in_progress"), ("event", "pull_request")):
            data = provenance_fixture(); data["nightly"][key] = bad
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                validate_provenance(data, repository="owner/repo", default_branch="main", tested_sha=SHA)
        data = provenance_fixture(); data["safety"]["repository"]["full_name"] = "another/repo"
        with self.assertRaises(EvidenceError):
            validate_provenance(data, repository="owner/repo", default_branch="main", tested_sha=SHA)

    def test_collect_requires_positive_run_ids_before_any_request(self):
        with patch("qa.release.urllib.request.urlopen") as request:
            with self.assertRaises(EvidenceError):
                collect_runs("owner/repo", {key: "bad" for key in WORKFLOWS}, "token")
        request.assert_not_called()

    def test_missing_cli_evidence_writes_blocked_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); write_json(root / "provenance.json", provenance_fixture())
            result = main(["evaluate", "--tested-sha", SHA, "--evidence", str(root / "missing"),
                           "--policy", str(ROOT / "qa/policy"), "--provenance", str(root / "provenance.json"),
                           "--repository", "owner/repo", "--default-branch", "main", "--native-status", "success",
                           "--generation", "fixture", "--output", str(root / "report.json")])
            self.assertEqual(result, 2)
            self.assertEqual(read_json(root / "report.json")["status"], "BLOCKED")

    def test_dependency_patch_is_reviewable_without_committing(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("Cargo.lock", "qa-models/Cargo.lock", "fuzz/Cargo.lock"):
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("old-lock\n")
            real_run(["git", "init", "-q"], cwd=root, check=True)
            real_run(["git", "add", "."], cwd=root, check=True)
            real_run(["git", "-c", "user.name=QA", "-c", "user.email=qa@example.invalid", "commit", "-qm", "fixture"], cwd=root, check=True)
            before = real_run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True).stdout
            def cargo_or_git(argv, **kwargs):
                if argv[0] == "cargo":
                    (root / "Cargo.lock").write_text("new-lock\n")
                    return subprocess.CompletedProcess(argv, 0)
                return real_run(argv, **kwargs)
            with patch("qa.release.subprocess.run", side_effect=cargo_or_git):
                update_locks(root, root / "review", scope="root")
            self.assertIn("+new-lock", (root / "review/locks.patch").read_text())
            self.assertEqual(read_json(root / "review/review.json")["status"], "REVIEW_REQUIRED")
            self.assertEqual(real_run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True).stdout, before)

    def test_dependency_update_rejects_option_injection_and_dirty_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(EvidenceError):
                update_locks(ROOT, Path(directory) / "out", package="--manifest-path=/other")
        with tempfile.TemporaryDirectory() as directory, patch("qa.release.subprocess.run") as run:
            run.return_value.returncode = 1
            with self.assertRaisesRegex(EvidenceError, "clean"):
                update_locks(ROOT, Path(directory) / "out")
            self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
