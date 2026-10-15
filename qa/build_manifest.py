"""Capture and verify the build that actually ran, including package feature scope."""
from __future__ import annotations

import os
import platform
import subprocess
import tomllib
from pathlib import Path

from .common import ROOT, EvidenceError, digest, file_hash, read_json

FLAGS = ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTDOCFLAGS", "CC", "CXX", "CARGO_BUILD_TARGET")


def command(args, root, *, timeout=120, env=None):
    try:
        result = subprocess.run(args, cwd=root, capture_output=True, text=True,
                                check=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise EvidenceError(f"identity command failed: {args[0]} {args[1:3]}") from error
    return result.stdout.strip()


def source_identity(root):
    root = Path(root).resolve()
    names = command(["git", "ls-files", "-z"], root).split("\0")
    files = {name: file_hash(root / name) if (root / name).is_file() else None
             for name in names if name and not name.startswith(("bench/results/", "harness/l2/results/"))}
    patch = command(["git", "diff", "HEAD", "--binary"], root)
    return {"sha": command(["git", "rev-parse", "HEAD"], root),
            "tree": command(["git", "rev-parse", "HEAD^{tree}"], root),
            "source_hash": digest(files), "patch_hash": digest(patch),
            "dirty": bool(command(["git", "status", "--porcelain", "--untracked-files=no"], root))}


def native_platform(system, machine):
    machine = {"AMD64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "ARM64": "aarch64"}.get(machine, machine)
    return f"{system.lower()}-{machine}"


def prepare_native_binary(root, toolchain, package, env):
    """Freeze Cargo's output and native target before building a runnable artifact.

    Explicit native target selection also overrides Cargo config build.target;
    an inherited cross target is rejected rather than relabelled as native.
    """
    root = Path(root).resolve()
    rustc = command(["rustc", f"+{toolchain}", "-vV"], root, env=env)
    host = next((line[6:] for line in rustc.splitlines() if line.startswith("host: ")), None)
    system = "darwin" if host and "darwin" in host else "windows" if host and "windows" in host else "linux" if host and "linux" in host else None
    if system is None or native_platform(system, host.split("-")[0]) != native_platform(platform.system(), platform.machine()):
        raise EvidenceError("runnable artifact compiler host differs from the native platform")
    if env.get("CARGO_BUILD_TARGET") not in (None, "", host):
        raise EvidenceError("explicit cross target cannot supply native runnable artifact evidence")
    target_dir = root / "target"
    env["CARGO_TARGET_DIR"] = str(target_dir)
    env["CARGO_BUILD_TARGET"] = host
    binary = target_dir / host / "release" / (package + (".exe" if system == "windows" else ""))
    # run-isolate.sh accepts BIN from its environment; never inherit a stale
    # external executable while recording the newly built one.
    env["BIN"] = str(binary)
    return binary


def integration_manifest(root, worker, binary):
    """Normalize the actual cross-repository worker build; never label it as a library build."""
    root, binary = Path(root), Path(binary)
    return {"schema_version": 1, "source": source_identity(root), "lock_hash": worker["lock_sha256"],
            "lock_path": "worker-Cargo.lock", "manifest_path": "worker-Cargo.toml", "selected_package": "zf-worker",
            "toolchain": worker["toolchain"], "rustc": worker["rustc_vv"], "cargo": worker["cargo_version"],
            "host": worker["host"], "target": worker["target"], "native_platform": native_platform(platform.system(), platform.machine()),
            "profile": worker["profile"], "requested_features": sorted(worker["features"]), "dependency_scope": "normal,build",
            "dependency_graph": worker["dependency_graph"], "dependency_graph_hash": worker["dependency_graph_hash"],
            "build_flags": {key: worker["build_flags"].get(key, "") for key in FLAGS}, "python": platform.python_version(),
            "binaries": [{"name": binary.name, "sha256": file_hash(binary), "size_bytes": binary.stat().st_size}],
            "qa_requirements_hash": file_hash(root / "qa/requirements.lock"),
            "tool_hashes": {p.relative_to(root).as_posix(): file_hash(p) for directory in ("qa", "harness/l2")
                            for p in sorted((root / directory).glob("**/*")) if p.is_file() and p.suffix in (".py", ".sh", ".json")
                            and not any(x in p.parts for x in ("results", "__pycache__"))}}


def build_manifest(root, toolchain="1.94.1", profile="release", features=(), binaries=(), *,
                   manifest_path=None, package=None, build_env=None):
    root = Path(root).resolve()
    env = dict(os.environ if build_env is None else build_env)
    manifest = Path(manifest_path or root / "Cargo.toml")
    if not manifest.is_absolute():
        manifest = root / manifest
    manifest = manifest.resolve()
    if not manifest.is_relative_to(root) or not manifest.is_file():
        raise EvidenceError("build manifest must be an existing repository Cargo.toml")
    definition = tomllib.loads(manifest.read_text())
    package = package or definition.get("package", {}).get("name")
    if not package:
        raise EvidenceError("selected build package is required")
    # Cargo chooses the nearest containing workspace. Independent fuzz/model
    # workspaces must identify their own lock, never the production root lock.
    workspace = manifest.parent
    while workspace != root and "workspace" not in tomllib.loads((workspace / "Cargo.toml").read_text()):
        workspace = workspace.parent
        if not (workspace / "Cargo.toml").is_file():
            raise EvidenceError("cannot identify Cargo workspace")
    lock = workspace / "Cargo.lock"
    if not lock.is_file():
        raise EvidenceError("selected workspace Cargo.lock is required")
    requested = sorted(set(features))
    if any(not isinstance(x, str) or not x for x in requested):
        raise EvidenceError("invalid requested features")
    args = ["cargo", f"+{toolchain}", "tree", "--locked", "--manifest-path", str(manifest),
            "-p", package, "--no-default-features", "--edges", "normal,build", "--prefix", "depth",
            "--charset", "ascii", "--format", "{p}|{f}"]
    if requested:
        args += ["--features", ",".join(requested)]
    target = env.get("CARGO_BUILD_TARGET")
    if target:
        args += ["--target", target]
    graph = command(args, root, env=env)
    if not graph:
        raise EvidenceError("empty selected dependency graph")
    rustc = command(["rustc", f"+{toolchain}", "-vV"], root, env=env)
    host = next((line[6:] for line in rustc.splitlines() if line.startswith("host: ")), None)
    if not host:
        raise EvidenceError("rustc omitted host identity")
    executable = []
    for name in binaries:
        path = Path(name).resolve()
        if not path.is_file():
            raise EvidenceError(f"missing binary: {path.name}")
        executable.append({"name": path.name, "sha256": file_hash(path), "size_bytes": path.stat().st_size})
    return {"schema_version": 1, "source": source_identity(root), "lock_hash": file_hash(lock),
            "lock_path": lock.relative_to(root).as_posix(), "manifest_path": manifest.relative_to(root).as_posix(),
            "selected_package": package, "toolchain": toolchain, "rustc": rustc,
            "cargo": command(["cargo", f"+{toolchain}", "-V"], root, env=env),
            "host": host, "target": target or host, "native_platform": native_platform(platform.system(), platform.machine()),
            "profile": profile, "requested_features": requested, "dependency_scope": "normal,build",
            "dependency_graph": graph, "dependency_graph_hash": digest(graph),
            "build_flags": {key: env.get(key, "") for key in FLAGS}, "python": platform.python_version(),
            "binaries": executable, "qa_requirements_hash": file_hash(root / "qa/requirements.lock"),
            "tool_hashes": {p.relative_to(root).as_posix(): file_hash(p) for directory in
                            ("qa", "harness/src", "bench/pareto", "harness/l2", "fuzz", "qa-models")
                            for p in sorted((root / directory).glob("**/*"))
                            if p.is_file() and p.suffix in (".py", ".rs", ".sh", ".json")
                            and not any(x in p.parts for x in ("results", "__pycache__", "target"))}}


def _schema(value, name):
    from jsonschema import Draft202012Validator
    from .common import finite
    finite(value)
    schema = read_json(ROOT / "qa/schema" / (name + ".json"))
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        raise EvidenceError(f"{name}: " + "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5]))


def verify_manifest(manifest, case, plan, environment):
    """Validate completeness and compare actual native/build identities to policy."""
    _schema(manifest, "build-v1")
    _schema(environment, "environment-v1")
    if manifest["source"]["sha"] != plan["tested_sha"] or manifest["source"]["dirty"]:
        raise EvidenceError("build source differs from clean tested SHA")
    if manifest["toolchain"] != case.get("toolchain", plan["toolchain"]) or manifest["profile"] != case["profile"]:
        raise EvidenceError("actual toolchain/profile differs from case")
    if manifest["requested_features"] != sorted(case.get("features", [])):
        raise EvidenceError("actual requested features differ from case")
    actual = native_platform(environment["system"], environment["machine"])
    if actual != case["platform"] or manifest["native_platform"] != actual:
        raise EvidenceError("native OS/architecture differs from case")
    host = manifest["host"]
    host_system = "darwin" if "darwin" in host else "windows" if "windows" in host else "linux" if "linux" in host else None
    if host_system is None or native_platform(host_system, host.split("-")[0]) != actual:
        raise EvidenceError("compiler host differs from native execution platform")
    if f"host: {host}" not in manifest["rustc"].splitlines():
        raise EvidenceError("compiler verbose identity contradicts host")
    if manifest["source"]["patch_hash"] != digest(""):
        raise EvidenceError("clean source has a nonempty patch identity")
    if manifest["target"] != manifest["host"]:
        raise EvidenceError("cross-compilation cannot supply native execution evidence")
    if manifest["build_flags"]["CARGO_BUILD_TARGET"] not in ("", manifest["target"]):
        raise EvidenceError("explicit Cargo target contradicts the recorded build target")
    if digest(manifest["dependency_graph"]) != manifest["dependency_graph_hash"]:
        raise EvidenceError("selected dependency graph hash mismatch")
    for name in ("manifest_path", "lock_path"):
        path = Path(manifest[name])
        if path.is_absolute() or ".." in path.parts or "\\" in manifest[name]:
            raise EvidenceError("unsafe build identity path")
    expected_manifest = {"loom": "qa-models/Cargo.toml", "fuzz": "fuzz/Cargo.toml", "l2": "worker-Cargo.toml",
                         "l2-queue-control": "worker-Cargo.toml"}.get(case["runner"], "Cargo.toml")
    expected_lock = "worker-Cargo.lock" if case["runner"] in ("l2", "l2-queue-control") else Path(expected_manifest).with_name("Cargo.lock").as_posix()
    if manifest["manifest_path"] != expected_manifest or manifest["lock_path"] != expected_lock:
        raise EvidenceError("manifest/lock does not identify the runner workspace")
    compiler = dict(line.split(": ", 1) for line in manifest["rustc"].splitlines() if ": " in line)
    if not manifest["rustc"].startswith("rustc ") or not manifest["cargo"].startswith("cargo "):
        raise EvidenceError("invalid Rust compiler/Cargo identity")
    if any(not compiler.get(key) for key in ("binary", "commit-hash", "commit-date", "host", "release", "LLVM version")):
        raise EvidenceError("incomplete verbose compiler identity")
    selected = case.get("toolchain", plan["toolchain"])
    if selected[0].isdigit() and compiler["release"] != selected:
        raise EvidenceError("compiler release differs from selected toolchain")
    if selected.startswith("nightly") and "nightly" not in compiler["release"]:
        raise EvidenceError("selected nightly toolchain is not a nightly compiler")
    expected_package = ({"harness-test": "zfharness", "l0": "zfharness", "replay": "zfharness",
                         "loom": "zfstack-qa-models", "fuzz": "zfstack-fuzz", "l1": "zfbench", "l2": "zf-worker",
                         "l2-queue-control": "zf-worker"}
                        .get(case["runner"], "zfbench" if case["runner"].startswith("bench-") else "zfstack"))
    if manifest["selected_package"] != expected_package:
        raise EvidenceError("selected package differs from runner")
    if not manifest["dependency_graph"].startswith(f"0{expected_package} v"):
        raise EvidenceError("dependency graph root differs from selected package")
    if case["runner"] in ("l0", "replay", "l1"):
        expected_binary = expected_package + (".exe" if actual.startswith("windows-") else "")
        if (len(manifest["binaries"]) != 1 or manifest["binaries"][0]["name"] != expected_binary
                or manifest["binaries"][0]["size_bytes"] <= 0):
            raise EvidenceError("runnable case requires its actual nonempty binary inventory")
    if not manifest["tool_hashes"]:
        raise EvidenceError("missing QA/tool source identities")
    return manifest
