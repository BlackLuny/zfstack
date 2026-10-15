"""Re-evaluate release evidence and prepare review-only baseline candidates.

Artifact summaries are deliberately ignored. The reporter rebuilds every scope
from reviewed policy, validates raw results/manifests, and recomputes both the
performance experiment and its measured calibration controls.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from .build_manifest import verify_manifest
from .common import ROOT, EvidenceError, digest, file_hash, read_json, safe_path, write_json
from .evaluate import evaluate, verify_artifacts
from .plan import make_plan
from .validate import validate_result

NATIVE_PLATFORMS = ("linux-x86_64", "linux-aarch64", "darwin-aarch64", "windows-x86_64")
WORKFLOWS = {"nightly": ".github/workflows/qa-nightly.yml", "safety": ".github/workflows/qa-safety.yml",
             "performance": ".github/workflows/qa-perf.yml"}
SHA = re.compile(r"[a-f0-9]{40}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
MAX_EVIDENCE_BYTES = 1024 * 1024 * 1024


def expiry(value, now=None):
    if not isinstance(value, str):
        raise EvidenceError("expiry must be an ISO timestamp")
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError("invalid expiry timestamp") from error
    if result.tzinfo is None or result <= (now or dt.datetime.now(dt.timezone.utc)):
        raise EvidenceError("expired or timezone-free evidence")
    return result


def validate_provenance(provenance, *, repository, default_branch, tested_sha):
    """Input is fetched by the trusted job, never imported from an artifact."""
    if not isinstance(provenance, dict) or set(provenance) != set(WORKFLOWS):
        raise EvidenceError("all nightly, safety and performance run provenance is required")
    ids = []
    for scope, path in WORKFLOWS.items():
        run = provenance[scope]
        if not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0:
            raise EvidenceError(f"{scope}: invalid immutable run ID")
        ids.append(run["id"])
        if (not isinstance(run.get("repository"), dict) or not isinstance(run.get("head_repository"), dict)
                or run["repository"].get("full_name") != repository
                or run["head_repository"].get("full_name") != repository):
            raise EvidenceError(f"{scope}: evidence run belongs to a different repository")
        if not isinstance(run.get("path"), str) or run["path"].split("@", 1)[0] != path:
            raise EvidenceError(f"{scope}: wrong evidence workflow")
        if run.get("event") not in ("schedule", "workflow_dispatch") or run.get("head_branch") != default_branch:
            raise EvidenceError(f"{scope}: evidence must come from the protected default branch")
        if run.get("status") != "completed" or run.get("conclusion") != "success":
            raise EvidenceError(f"{scope}: workflow is missing, failed or incomplete")
        if not isinstance(run.get("head_sha"), str) or not SHA.fullmatch(run["head_sha"]):
            raise EvidenceError(f"{scope}: missing immutable workflow SHA")
        # These workflows execute their checkout SHA; performance explicitly
        # accepts a reviewed tested_sha input, checked again in every raw result.
        if scope != "performance" and run["head_sha"] != tested_sha:
            raise EvidenceError(f"{scope}: workflow executed a different tested SHA")
    if len(set(ids)) != len(ids):
        raise EvidenceError("one workflow run cannot impersonate multiple evidence sources")
    return provenance


def collect_runs(repository, run_ids, token, api_url="https://api.github.com"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise EvidenceError("invalid repository identity")
    if not token:
        raise EvidenceError("read-only GitHub token is required to verify run provenance")
    result = {}
    for scope in WORKFLOWS:
        run_id = str(run_ids[scope])
        if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
            raise EvidenceError("run IDs must be positive integers")
        request = urllib.request.Request(f"{api_url.rstrip('/')}/repos/{repository}/actions/runs/{run_id}",
                                         headers={"Authorization": "Bearer " + token,
                                                  "Accept": "application/vnd.github+json",
                                                  "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise EvidenceError("oversized GitHub run response")
        import json
        run = json.loads(data)
        if run.get("id") != int(run_id):
            raise EvidenceError("GitHub returned a different run identity")
        result[scope] = run
    return result


def _engine(name):
    path = ROOT / "bench/pareto" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"qa_release_trusted_{name}", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("perf_gate")
    if name == "calibrate":
        sys.modules["perf_gate"] = _engine("perf_gate")
    try:
        spec.loader.exec_module(module)
    finally:
        if name == "calibrate":
            if previous is None:
                sys.modules.pop("perf_gate", None)
            else:
                sys.modules["perf_gate"] = previous
    return module


def _result_paths(directory):
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise EvidenceError("missing or unsafe evidence scope directory")
    paths, size = [], 0
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory).as_posix()
        safe_path(directory, relative)
        if path.is_file():
            size += path.stat().st_size
            if size > MAX_EVIDENCE_BYTES:
                raise EvidenceError("release evidence scope exceeds size budget")
            if path.name == "result.json":
                paths.append(path)
                if len(paths) > 4096:
                    raise EvidenceError("too many case results")
    if not paths:
        raise EvidenceError("scope contains no raw case results")
    return paths


def _integration(result, case, directory):
    try:
        from .integration import verify_integration
    except ImportError as error:
        raise EvidenceError("trusted reporter lacks independent integration verification") from error
    verify_integration(result, case, directory)


def verify_calibration(directory, case, policy_dir, *, generation=None, now=None):
    """Verify reviewed calibration identity and recompute its raw measured controls.

    Single-case gates may omit generation; release qualification supplies it.
    The return value is the independently recomputed calibration report.
    """
    directory = Path(directory)
    result = validate_result(read_json(safe_path(directory, "result.json")))
    verify_artifacts(result, directory)
    if result["case_instance_id"] != case["case_instance_id"]:
        raise EvidenceError("calibration result differs from requested case instance")
    names = {artifact["path"] for artifact in result["artifacts"]}
    required = {"perf-plan.json", "calibration.json", "calibration-spec.json"}
    if not required.issubset(names):
        raise EvidenceError("missing frozen calibration specification and measured controls")
    plan = read_json(safe_path(directory, "perf-plan.json"))
    calibration = read_json(safe_path(directory, "calibration.json"))
    spec = read_json(safe_path(directory, "calibration-spec.json"))
    policy = case["performance"]
    if not all(isinstance(value, dict) for value in (plan, calibration, spec)):
        raise EvidenceError("calibration plan/report/specification must be objects")
    if plan.get("calibration_id") != policy.get("calibration_id") or calibration.get("calibration_id") != policy.get("calibration_id"):
        raise EvidenceError("calibration identity differs from reviewed case policy")
    calibration_hash = digest(calibration)
    registry = read_json(Path(policy_dir) / "required.json").get("release_calibrations", [])
    if not isinstance(registry, list):
        raise EvidenceError("trusted calibration registry must be a list")
    entries = [entry for entry in registry if isinstance(entry, dict)
               and entry.get("calibration_id") == policy["calibration_id"] and entry.get("sha256") == calibration_hash]
    if len(entries) != 1:
        raise EvidenceError("calibration is not uniquely registered in trusted release policy")
    entry = entries[0]
    if (set(entry) != {"calibration_id", "sha256", "generation", "expires"}
            or not isinstance(entry["generation"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", entry["generation"])
            or (generation is not None and entry["generation"] != generation)):
        raise EvidenceError("calibration generation differs from release generation")
    expiry(entry["expires"], now)
    if plan.get("calibration_hash") != calibration_hash:
        raise EvidenceError("experiment does not bind the registered calibration report")
    trials = spec.get("trials")
    if not isinstance(trials, list) or not 1 <= len(trials) <= 512:
        raise EvidenceError("calibration control trial count is missing or exceeds budget")
    engine = _engine("perf_gate")
    datasets, total = {}, 0
    for trial in trials:
        if not isinstance(trial, dict) or not isinstance(trial.get("trial_id"), str) or trial["trial_id"] in datasets:
            raise EvidenceError("duplicate or invalid calibration trial")
        values = {}
        for key, value_key in (("plan_file", "plan"), ("rows_file", "rows"), ("provenance_file", "provenance")):
            name = trial.get(key)
            if key == "provenance_file" and name is None:
                values[value_key] = {}
                continue
            if not isinstance(name, str) or name not in names:
                raise EvidenceError("calibration trial input is missing from hashed artifacts")
            path = safe_path(directory, name)
            total += path.stat().st_size
            if total > 256 * 1024 * 1024:
                raise EvidenceError("calibration controls exceed analysis size budget")
            values[value_key] = engine.read_rows(path) if key == "rows_file" else read_json(path)
        datasets[trial["trial_id"]] = values
    recomputed = _engine("calibrate").calibrate(spec, datasets)
    if recomputed != calibration or recomputed.get("validated") is not True or recomputed.get("errors"):
        raise EvidenceError("calibration does not match independently recomputed measured controls")
    if recomputed.get("scope_hash") != engine.calibration_scope(plan):
        raise EvidenceError("calibration controls do not cover this experiment scope")
    return recomputed


def _calibration(case, result, directory, policy_dir, generation, now):
    names = {artifact["path"] for artifact in result["artifacts"]}
    required = {"perf-plan.json", "perf-rows.jsonl", "perf-analysis.json", "calibration.json", "calibration-spec.json"}
    if not required.issubset(names):
        raise EvidenceError("release performance requires raw experiment and calibration control artifacts")
    plan = read_json(safe_path(directory, "perf-plan.json"))
    analysis = read_json(safe_path(directory, "perf-analysis.json"))
    policy = case["performance"]
    if policy["mode"] != "enforce" or plan.get("mode") != "enforce":
        raise EvidenceError("shadow performance cannot certify a release")
    if digest(plan) != policy.get("experiment_plan_hash"):
        raise EvidenceError("performance experiment differs from reviewed policy")
    calibration = verify_calibration(directory, case, policy_dir, generation=generation, now=now)
    if plan.get("case_id") != case["case_id"] or plan.get("case_version") != case["case_version"]:
        raise EvidenceError("performance case identity mismatch")
    # The QA policy and node-specific experiment have distinct configuration
    # and environment schemas. Bind both instead of conflating their hashes.
    observations = result.get("observations", {})
    bindings = {"performance_plan_hash": digest(plan),
                "performance_environment_hash": plan.get("environment_hash"),
                "performance_config_hash": digest(plan.get("config")),
                "qa_config_hash": digest(case["config"])}
    if (any(observations.get(key) != value for key, value in bindings.items())
            or plan.get("build_manifest_hash") != result["build_manifest_hash"]
            or result["config_hash"] != bindings["qa_config_hash"]
            or digest(read_json(safe_path(directory, "perf-environment.json"))) != bindings["performance_environment_hash"]):
        raise EvidenceError("performance/raw result identity mismatch")
    engine = _engine("perf_gate")
    rows = engine.read_rows(safe_path(directory, "perf-rows.jsonl"))
    actual = engine.evaluate(plan, rows, calibration)
    if actual != analysis or actual.get("calibrated") is not True or actual.get("decision") != "PASS":
        raise EvidenceError("release performance is uncalibrated, failed or inconclusive")
    if (actual.get("performance") != result["performance"]["status"]
            or actual.get("metrics") != result["performance"]["metrics"]
            or set(policy["metrics"]) != {metric["id"] for metric in plan["metrics"]}
            or file_hash(directory / "perf-analysis.json") != result["performance"]["analysis_hash"]):
        raise EvidenceError("reported performance differs from independently recomputed analysis")
    manifest = read_json(directory / "build-manifest.json")
    if plan.get("binary_sha256", {}).get("B") not in {binary["sha256"] for binary in manifest["binaries"]}:
        raise EvidenceError("candidate measurement binary differs from its build manifest")
    registry = read_json(Path(policy_dir) / "required.json")["release_calibrations"]
    return dict(next(entry for entry in registry if entry["calibration_id"] == policy["calibration_id"]
                     and entry["sha256"] == digest(calibration)))


def evaluate_release(tested_sha, evidence_root, policy_dir, *, provenance, repository, default_branch,
                     native_status, generation, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    report = {"schema_version": 1, "status": "BLOCKED", "tested_sha": tested_sha,
              "plan_hash": None, "policy_hash": None, "generation": generation,
              "failures": [], "blocked": [], "scopes": {}, "calibrations": [],
              "coverage": {"required": 0, "completed": 0},
              "scope": "Four native platform suites, full nightly including L1/L2, safety and calibrated performance."}
    try:
        if not isinstance(tested_sha, str) or not SHA.fullmatch(tested_sha):
            raise EvidenceError("release requires a full immutable tested SHA")
        if not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", generation):
            raise EvidenceError("release requires an explicit baseline generation")
        validate_provenance(provenance, repository=repository, default_branch=default_branch, tested_sha=tested_sha)
        if native_status != "success":
            raise EvidenceError("native platform workflow is missing, skipped, cancelled or failed")
        policy_dir, evidence_root = Path(policy_dir), Path(evidence_root)
        required = read_json(policy_dir / "required.json")
        declared_platforms = required.get("release_native_platforms", [])
        if not isinstance(declared_platforms, list) or len(declared_platforms) != 4 or set(declared_platforms) != set(NATIVE_PLATFORMS):
            raise EvidenceError("release must declare all four native platforms")
        release_plan = make_plan(policy_dir, "release", tested_sha)
        report.update(plan_hash=digest(release_plan), policy_hash=release_plan["policy_hash"])
        planned = {}
        scopes = [(f"native/{platform}", "portable", platform, evidence_root / "native" / f"qa-native-{platform}")
                  for platform in NATIVE_PLATFORMS]
        scopes += [("nightly", "nightly", "linux-x86_64", evidence_root / "nightly"),
                   ("safety", "safety", "linux-x86_64", evidence_root / "safety"),
                   ("performance", "perf", "linux-x86_64", evidence_root / "performance")]
        for scope, suite, platform, directory in scopes:
            try:
                plan = make_plan(policy_dir, suite, tested_sha, platform=platform)
                planned.update({case["case_instance_id"]: case for case in plan["cases"]})
                runners = {case["runner"] for case in plan["cases"]}
                mandatory = {"nightly": {"l1", "l2"}, "safety": {"miri", "sanitizer", "fuzz", "loom"}}.get(suite, set())
                if not mandatory.issubset(runners):
                    raise EvidenceError("trusted suite omits mandatory release integration/safety runners")
                paths = _result_paths(directory)
                scope_report = evaluate(plan, paths, policy_dir, expected_sha=tested_sha, now=now)
                report["scopes"][scope] = scope_report
                report["coverage"]["required"] += scope_report["coverage"]["required"]
                report["coverage"]["completed"] += scope_report["coverage"]["completed"]
                if scope_report["status"] == "FAIL":
                    report["failures"].append(f"{scope}: required raw contracts failed")
                elif scope_report["status"] != "PASS":
                    report["blocked"].append(f"{scope}: required raw evidence incomplete or invalid")
                expected = {case["case_instance_id"]: case for case in plan["cases"]}
                for path in paths:
                    result = validate_result(read_json(path))
                    case = expected.get(result["case_instance_id"])
                    if case is None:
                        raise EvidenceError("unexpected case result")
                    manifest = verify_artifacts(result, path.parent)
                    verify_manifest(manifest, case, plan, read_json(path.parent / "environment.json"))
                    if case["runner"] in ("l1", "l2"):
                        _integration(result, case, path.parent)
                    if case["performance"]["mode"] != "not_applicable":
                        calibration = _calibration(case, result, path.parent, policy_dir, generation, now)
                        if calibration not in report["calibrations"]:
                            report["calibrations"].append(calibration)
            except (OSError, EvidenceError, ValueError, KeyError, TypeError, OverflowError) as error:
                report["blocked"].append(f"{scope}: {error}")
        missing = [case["case_id"] for case in release_plan["cases"] if case["case_instance_id"] not in planned]
        if missing:
            report["blocked"].append("release-only cases missing from source suites: " + ", ".join(missing))
        if not report["calibrations"]:
            report["blocked"].append("release requires registered, measured and independently recomputed calibration")
        if any(case["performance"]["mode"] == "shadow" for case in release_plan["cases"]):
            report["blocked"].append("release performance is still SHADOW")
        report["provenance_hash"] = digest(provenance)
    except (OSError, EvidenceError, ValueError, KeyError, TypeError) as error:
        report["blocked"].append(str(error))
    report["status"] = "FAIL" if report["failures"] else "BLOCKED" if report["blocked"] else "PASS"
    return report


def update_locks(root, output, *, scope="all", package=""):
    """Produce an explicit patch; this never commits, opens a PR or merges."""
    scopes = {"root": "Cargo.toml", "models": "qa-models/Cargo.toml", "fuzz": "fuzz/Cargo.toml"}
    if scope not in (*scopes, "all"):
        raise EvidenceError("invalid lock update scope")
    if package and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+/@-]{0,199}", package):
        raise EvidenceError("invalid Cargo package selector")
    root, output = Path(root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = subprocess.run(["git", "diff", "--exit-code", "HEAD"], cwd=root, capture_output=True, check=False)
    if before.returncode:
        raise EvidenceError("dependency update requires a clean tracked checkout")
    selected = scopes.values() if scope == "all" else [scopes[scope]]
    error = None
    try:
        for manifest in selected:
            argv = ["cargo", "+1.94.1", "update", "--manifest-path", manifest]
            if package:
                argv += ["--package", package]
            subprocess.run(argv, cwd=root, check=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as caught:
        error = caught
    # Preserve partial changes for review even if a later workspace update fails.
    names = ["Cargo.lock", "qa-models/Cargo.lock", "fuzz/Cargo.lock"]
    patch = subprocess.run(["git", "diff", "--binary", "HEAD", "--", *names], cwd=root,
                           capture_output=True, check=True).stdout
    (output / "locks.patch").write_bytes(patch)
    write_json(output / "review.json", {"schema_version": 1,
               "status": "UPDATE_FAILED" if error else "REVIEW_REQUIRED" if patch else "NO_CHANGE",
               "base_sha": subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                                           text=True, check=True).stdout.strip(),
               "scope": scope, "package": package or None, "toolchain": "1.94.1",
               "lock_hashes": {name: file_hash(root / name) for name in names},
               "patch_sha256": file_hash(output / "locks.patch"),
               "note": "Validation belongs to the accompanying workflow; this patch is never an approved baseline."})
    if error:
        raise EvidenceError("dependency update failed; partial patch retained for review") from error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect-runs")
    collect.add_argument("--repository", required=True)
    for scope in WORKFLOWS:
        collect.add_argument(f"--{scope}-run", required=True)
    collect.add_argument("--output", type=Path, required=True)
    update = sub.add_parser("update-locks")
    update.add_argument("--scope", choices=("all", "root", "models", "fuzz"), default="all")
    update.add_argument("--package", default="")
    update.add_argument("--output", type=Path, required=True)
    for kind in ("evaluate", "prepare-baseline"):
        item = sub.add_parser(kind)
        item.add_argument("--tested-sha", required=True)
        item.add_argument("--evidence", type=Path, required=True)
        item.add_argument("--policy", type=Path, default=ROOT / "qa/policy")
        item.add_argument("--provenance", type=Path, required=True)
        item.add_argument("--repository", required=True)
        item.add_argument("--default-branch", required=True)
        item.add_argument("--native-status", required=True)
        item.add_argument("--generation", required=True)
        item.add_argument("--output", type=Path, required=True)
        if kind == "prepare-baseline":
            item.add_argument("--release-anchor", required=True)
            item.add_argument("--expires", required=True)
            item.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect-runs":
            result = collect_runs(args.repository, {scope: getattr(args, scope + "_run") for scope in WORKFLOWS},
                                  os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_API_URL", "https://api.github.com"))
            write_json(args.output, result, exclusive=True)
            return 0
        if args.command == "update-locks":
            update_locks(ROOT, args.output, scope=args.scope, package=args.package)
            return 0
        provenance = read_json(args.provenance)
        kwargs = dict(provenance=provenance, repository=args.repository, default_branch=args.default_branch,
                      native_status=args.native_status, generation=args.generation)
        report = evaluate_release(args.tested_sha, args.evidence, args.policy, **kwargs)
        write_json(args.output, report, exclusive=True)
        if args.command == "prepare-baseline" and report["status"] == "PASS":
            from .baseline import promote
            plan = make_plan(args.policy, "release", args.tested_sha)
            candidate = promote(plan, report, generation=args.generation, release_anchor=args.release_anchor,
                                calibration_hashes=[entry["sha256"] for entry in report["calibrations"]], expires=args.expires,
                                evidence_root=args.evidence, policy_dir=args.policy, provenance=provenance,
                                repository=args.repository, default_branch=args.default_branch, native_status=args.native_status)
            write_json(args.candidate, candidate, exclusive=True)
        print(f"Release {report['status']}: {len(report['blocked'])} blocked; {len(report['failures'])} failed scopes")
        return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[report["status"]]
    except (OSError, EvidenceError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Release BLOCKED: {error}", file=sys.stderr)
        if hasattr(args, "output") and not args.output.exists():
            write_json(args.output, {"schema_version": 1, "status": "BLOCKED", "blocked": [str(error)]})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
