#!/usr/bin/env python3
"""Run one isolated lock-boundary mutation; caller owns the host QA lease."""

import argparse
import datetime
import difflib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import time


TEST = "a0_issue53_pump_consumes_refills_drains_and_wakes_outside_queue"
EXPECTED_FAILURE = "callback ran while the stream queue was locked"
ADAPTER = Path("src/tokio_adapter.rs")
QA = Path("src/tokio_adapter/tests/qa.rs")
ORIGINAL_BOUNDARY = """        // Application callbacks and stream removal may re-enter the queue.
        drop(q);
        if shutdown {
            self.shard.shutdown_write(id);
        }
        if let Some(w) = wake_reader {
            w.wake();
        }
        if let Some(w) = wake_writer {
            w.wake();
        }
        if let Some(w) = wake_flush {
            w.wake();
        }
        if app_closed {
            self.release_if_done(id);
        }
"""


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def extract_archive(archive, destination):
    """Validate every member before extraction, including on Python 3.11.2."""
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        seen = set()
        for member in source.getmembers():
            relative = PurePosixPath(member.name)
            if (relative.is_absolute() or ".." in relative.parts
                    or not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsafe archive member: {member.name!r}")
            resolved = (destination / member.name).resolve()
            if not resolved.is_relative_to(destination) or resolved in seen:
                raise ValueError(f"Escaping or duplicate archive member: {member.name!r}")
            seen.add(resolved)
            member.mode &= 0o777
        source.extractall(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve(strict=True)
    output = args.output.resolve()
    if output.is_relative_to(repo):
        parser.error("--output must be outside the source repository")
    if output.exists():
        parser.error("Refusing to replace an existing output directory")

    commit = git(repo, "rev-parse", "--verify", "--end-of-options", args.commit + "^{commit}").decode().strip()
    tree = git(repo, "rev-parse", commit + "^{tree}").decode().strip()
    qa_source = (repo / QA).read_bytes()
    if qa_source.count(("async fn " + TEST + "(").encode()) != 1:
        raise ValueError("Working-tree qa.rs must contain exactly the requested regression test")
    archive = git(repo, "archive", "--format=tar", commit)
    output.mkdir(parents=True, exist_ok=False)
    (output / "candidate.tar").write_bytes(archive)
    overlay = output / "source"
    overlay.mkdir()
    extract_archive(archive, overlay)

    original = (overlay / ADAPTER).read_text()
    if original.count(ORIGINAL_BOUNDARY) != 1:
        raise ValueError("Candidate must contain exactly one expected final pump lock boundary")
    archived_qa_sha256 = sha256((overlay / QA).read_bytes())
    (overlay / QA).write_bytes(qa_source)
    # Keep release_if_done outside the guard: this mutation exercises only
    # explicit application wakes, and its try_lock oracle cannot deadlock.
    mutated_boundary = ORIGINAL_BOUNDARY.replace("        drop(q);\n", "", 1)
    mutated_boundary = mutated_boundary.replace("        if app_closed {", "        drop(q);\n        if app_closed {", 1)
    mutated = original.replace(ORIGINAL_BOUNDARY, mutated_boundary, 1)
    (overlay / ADAPTER).write_text(mutated)
    patch = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), mutated.splitlines(keepends=True),
        fromfile="candidate/src/tokio_adapter.rs", tofile="negative-control/src/tokio_adapter.rs"))
    (output / "mutation.diff").write_text(patch)

    command = ["cargo", "+1.94.1", "test", "--lib", "--release", "--locked", "--features", "tokio,test-peer", TEST, "--", "--nocapture"]
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(output / "target")
    environment["CARGO_INCREMENTAL"] = "0"
    manifest = {
        "schema": 1,
        "kind": "expected-failure-negative-control",
        "repo": str(repo),
        "candidate_commit": commit,
        "candidate_tree": tree,
        "rustc_version": subprocess.check_output(["rustc", "+1.94.1", "-Vv"], cwd=overlay, env=environment, text=True),
        "archive_sha256": sha256(archive),
        "script_sha256": sha256(Path(__file__).read_bytes()),
        "adapter_original_sha256": sha256(original.encode()),
        "adapter_mutated_sha256": sha256(mutated.encode()),
        "qa_source": str(repo / QA),
        "qa_archived_sha256": archived_qa_sha256,
        "qa_copied_sha256": sha256(qa_source),
        "mutation_diff_sha256": sha256(patch.encode()),
        "command": command,
        "cwd": str(overlay),
        "environment": {name: environment[name] for name in (
            "CARGO_TARGET_DIR", "CARGO_INCREMENTAL", "CARGO_BUILD_JOBS",
            "CARGO_PROFILE_RELEASE_LTO", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS") if name in environment},
        "expected_failure": EXPECTED_FAILURE,
        "criterion": "Nonzero cargo exit, exactly one selected test, named test failed, and exact lock-boundary assertion text present.",
    }
    (output / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"status": "RUNNING", "manifest": str(output / "MANIFEST.json"), "test": TEST}), flush=True)
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    started = time.monotonic_ns()
    log = output / "cargo-test.log"
    # The outer lease runner owns timeout and process-tree cancellation.
    # Do not start a detached process group that could escape its cleanup.
    with log.open("xb") as sink:
        completed = subprocess.run(command, cwd=overlay, env=environment, stdout=sink, stderr=subprocess.STDOUT)
    elapsed = time.monotonic_ns() - started
    log_bytes = log.read_bytes()
    log_text = log_bytes.decode(errors="replace")
    checks = {
        "cargo_failed": completed.returncode > 0,
        "exactly_one_test": "running 1 test\n" in log_text,
        "named_test_failed": "tokio_adapter::tests::qa::" + TEST + " ... FAILED" in log_text,
        "expected_assertion": EXPECTED_FAILURE in log_text,
        "unchanged_mutated_adapter": sha256((overlay / ADAPTER).read_bytes()) == manifest["adapter_mutated_sha256"],
        "unchanged_copied_qa": sha256((overlay / QA).read_bytes()) == manifest["qa_copied_sha256"],
    }
    detected = all(checks.values())
    result = {
        "status": "EXPECTED_FAILURE_DETECTED" if detected else "NEGATIVE_CONTROL_NOT_CONFIRMED",
        "cargo_exit_code": completed.returncode,
        "wrapper_exit_code": 0 if detected else 1,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "wall_ns": elapsed,
        "log": str(log),
        "log_sha256": sha256(log_bytes),
        "manifest_sha256": sha256((output / "MANIFEST.json").read_bytes()),
        "checks": checks,
    }
    (output / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result), flush=True)
    return result["wrapper_exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
