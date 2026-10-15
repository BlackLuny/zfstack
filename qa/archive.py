"""Small allowlisted archives; never upload rig keys, configs, or arbitrary logs."""
from __future__ import annotations

import re
import shutil
import tarfile
from pathlib import Path

from .common import EvidenceError, file_hash, read_json, safe_path, write_json

ALLOWED = {"plan.json", "result.json", "build-manifest.json", "environment.json", "execution.json",
           "harness.json", "l1.json", "l2-result.json", "raw.json.gz", "analysis.json", "worker.sha256", "build-info.json", "worker-manifest.json", "calibration-spec.json", "calibration-provenance.json", "coverage.json", "summary.json", "report.md", "perf-plan.json",
           "perf-analysis.json", "perf-rows.jsonl", "perf-environment.json", "perf-audit.json", "calibration.json", "qdisc.json", "path-pre.json", "path-post.json",
           "queue-control.json", "forwarder.sha256", "forwarder-build-info.json",
           "fuzz-seeds.json", "fuzz-artifacts.json", "safety-capability.json"}

CALIBRATION_FILES = {"plan_file": "perf-plan.json", "rows_file": "perf-rows.jsonl",
                     "provenance_file": "calibration-provenance.json"}


def calibration_paths(spec):
    """One explicit import/upload layout; arbitrary spec paths are not copied."""
    trials = spec.get("trials") if isinstance(spec, dict) else None
    if not isinstance(trials, list) or len(trials) > 512:
        raise EvidenceError("invalid calibration trial list/size")
    paths, seen = {"calibration.json", "calibration-spec.json"}, set()
    for trial in trials:
        ident = trial.get("trial_id") if isinstance(trial, dict) else None
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", ident) or ident in seen:
            raise EvidenceError("invalid or duplicate calibration trial path identity")
        seen.add(ident)
        for key, filename in CALIBRATION_FILES.items():
            relative = trial.get(key)
            if key == "provenance_file" and relative is None:
                continue
            expected = f"calibration/{ident}/{filename}"
            if relative != expected:
                raise EvidenceError(f"unsupported calibration {key}: expected {expected}")
            paths.add(expected)
    return paths


def _selected(root, limit_bytes):
    """Resolve and budget the complete allowlist before writing an upload."""
    files = sorted(root.rglob("*"))
    control_files, control_roots, performance = set(), set(), {}
    for file in files:
        relative = file.relative_to(root).as_posix()
        if file.name == "calibration-spec.json":
            path = safe_path(root, relative)
            if not path.is_file():
                continue
            base = path.parent
            paths = calibration_paths(read_json(path))
            control_roots.add(base / "calibration")
            for name in paths:
                control = safe_path(base, name)
                if not control.is_file():
                    raise EvidenceError("calibration artifact required by spec is missing: " + name)
                control_files.add(control)
        if file.name == "perf-plan.json" and (file.parent / "result.json").is_file():
            path = safe_path(root, relative)
            plan = read_json(path)
            blocks = plan.get("blocks") if isinstance(plan, dict) else None
            if not isinstance(blocks, list) or len(blocks) > 1024:
                raise EvidenceError("invalid frozen performance block list for archive")
            performance[path.parent] = {"runner.log", "runs/runs.jsonl", *(
                f"runs/probe-{index}-{position}.log" for index in range(len(blocks)) for position in range(4))}

    def allowed(path):
        # A spec's calibration subtree includes only its exact referenced files.
        if any(path.is_relative_to(base) for base in control_roots):
            return path in control_files
        if "calibration" in path.relative_to(root).parts and path not in control_files:
            return False
        if path.name in ALLOWED or re.fullmatch(r"command-\d+\.log", path.name):
            return True
        return any(path.is_relative_to(base) and path.relative_to(base).as_posix() in names
                   for base, names in performance.items())

    # The result inventory is a closure requirement, not an extension to the
    # allowlist. Refuse a perf upload that would silently omit referenced proof.
    for base in performance:
        result = read_json(safe_path(base, "result.json"))
        inventory = result.get("artifacts") if isinstance(result, dict) else None
        if not isinstance(inventory, list):
            raise EvidenceError("performance result lacks artifact inventory")
        for artifact in inventory:
            name = artifact.get("path") if isinstance(artifact, dict) else None
            path = safe_path(base, name)
            if not allowed(path):
                raise EvidenceError("performance artifact is outside upload allowlist: " + name)
            if not path.is_file():
                raise EvidenceError("performance artifact required by result is missing: " + name)

    entries, total = [], 0
    for file in files:
        if not allowed(file):
            continue
        relative = file.relative_to(root).as_posix()
        path = safe_path(root, relative)
        if not path.is_file():
            continue
        total += path.stat().st_size
        if total > limit_bytes:
            raise EvidenceError("upload/archive budget exceeded; preserve evidence locally, do not silently truncate")
        entries.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": file_hash(path)})
    if not entries:
        raise EvidenceError("empty upload/archive")
    return entries


def stage(root, destination, *, limit_bytes=256 * 1024 * 1024):
    """Create an upload directory without ever copying key/config trees."""
    root, destination = Path(root).resolve(), Path(destination)
    entries = _selected(root, limit_bytes)
    destination.mkdir(parents=True, exist_ok=False)
    for entry in entries:
        target = destination / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / entry["path"], target)


def archive(root, destination, *, limit_bytes=256 * 1024 * 1024):
    root, destination = Path(root).resolve(), Path(destination)
    entries = _selected(root, limit_bytes)
    # x mode preserves an earlier upload even when a run is mistakenly repeated.
    with destination.open("xb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as output:
        for entry in entries:
            output.add(root / entry["path"], arcname=entry["path"], recursive=False)
    write_json(destination.with_suffix(destination.suffix + ".index.json"), {"schema_version": 1, "files": entries}, exclusive=True)
    return entries
