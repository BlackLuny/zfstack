#!/usr/bin/env python3
"""Build selected frozen overlays sequentially. Caller owns the host QA Lease."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

# Never dump the full environment: credentials and unrelated configuration are
# unnecessary. Retain only compiler/profile/target fields that affect this build.
ENV_KEYS = (
    "CARGO_INCREMENTAL", "CARGO_BUILD_JOBS", "CARGO_BUILD_TARGET", "CARGO_TARGET_DIR",
    "CARGO_PROFILE_RELEASE_LTO", "CARGO_PROFILE_RELEASE_CODEGEN_UNITS",
    "CARGO_PROFILE_RELEASE_PANIC", "CARGO_PROFILE_RELEASE_OPT_LEVEL",
    "CARGO_PROFILE_RELEASE_DEBUG", "CARGO_PROFILE_RELEASE_DEBUG_ASSERTIONS",
    "CARGO_PROFILE_RELEASE_OVERFLOW_CHECKS", "CARGO_PROFILE_RELEASE_STRIP",
    "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
    "RUSTUP_TOOLCHAIN", "CC", "CXX", "AR",
)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--labels", nargs="+", required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    by_label = {entry["label"]: entry for entry in manifest["overlays"]}
    if len(set(args.labels)) != len(args.labels) or any(label not in by_label for label in args.labels):
        raise SystemExit("Labels must be distinct names from MANIFEST.json")
    if args.output.exists():
        raise SystemExit("Refusing to overwrite existing build evidence")
    args.output.mkdir(parents=True)
    metadata = {
        "manifest_sha256": digest(args.manifest), "hostname": socket.gethostname(),
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "labels": args.labels, "builds": [],
    }
    metadata_path = args.output / "builds.json"
    def save():
        temporary = metadata_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(metadata, indent=2) + "\n")
        temporary.replace(metadata_path)
    save()
    for label in args.labels:
        entry = by_label[label]
        directory = Path(entry["path"]).resolve()
        env = os.environ.copy()
        env.update(manifest["build_env"])
        env["CARGO_TARGET_DIR"] = str(directory / "target")
        # An inherited cross target would put the artifact in a different path;
        # this diagnostic is explicitly the native Linux host build.
        if env.get("CARGO_BUILD_TARGET"):
            raise SystemExit("Unset CARGO_BUILD_TARGET: this frozen probe uses the native Linux host target")
        if digest(directory / "src/tokio_adapter.rs") != entry["adapter_overlay_sha256"]:
            raise SystemExit(f"Overlay source changed before build: {label}")
        if digest(directory / "src/tokio_adapter/issue53_pump_probe.rs") != manifest["probe_sha256"]:
            raise SystemExit(f"Probe source changed before build: {label}")
        record = {"label": label, "commit": entry["commit"], "git_tree": entry["git_tree"],
            "cwd": str(directory), "command": entry["build"],
            "build_environment": {key: env.get(key) for key in ENV_KEYS},
            "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "RUNNING", "log": str((args.output / f"{label}.log").resolve())}
        metadata["builds"].append(record)
        save()
        version = subprocess.run(["rustc", "+1.94.1", "-Vv"], cwd=directory, env=env, text=True, capture_output=True)
        record["rustc_version"] = {"returncode": version.returncode, "stdout": version.stdout, "stderr": version.stderr}
        if version.returncode:
            record["status"] = "TOOLCHAIN_FAILED"
            save()
            raise SystemExit(version.returncode)
        binary = directory / "target/release/examples/issue53_pump_probe"
        record["artifact_preexisting"] = binary.is_file()
        started = time.monotonic_ns()
        print(json.dumps({"label": label, "status": "BUILDING"}), flush=True)
        with Path(record["log"]).open("x") as log:
            result = subprocess.run(entry["build"], cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT)
        record["duration_ns"] = time.monotonic_ns() - started
        record["returncode"] = result.returncode
        record["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        record["log_sha256"] = digest(Path(record["log"]))
        record["status"] = "PASS" if result.returncode == 0 and binary.is_file() else "FAIL"
        if record["status"] == "PASS":
            record["binary"] = {"path": str(binary), "sha256": digest(binary), "bytes": binary.stat().st_size}
        save()
        print(json.dumps({"label": label, "status": record["status"], "returncode": result.returncode}), flush=True)
        if record["status"] != "PASS":
            raise SystemExit(result.returncode or 1)
    metadata["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save()
    print(json.dumps({"completed": len(metadata["builds"]), "metadata": str(metadata_path)}))

if __name__ == "__main__":
    main()
