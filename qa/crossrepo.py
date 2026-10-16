"""Build frozen ZFC/ZFStack combinations from trusted local Git objects.

Only allowlisted committed build inputs enter sparse checkouts. Git credentials,
configuration, history, keys, working-tree edits and runtime configs are excluded.
Run ``python -m qa.crossrepo archive --help`` then ``build --help``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
import uuid
from pathlib import Path

from .common import EvidenceError, digest, file_hash, read_json, write_json
from .process import run_child
from .lease import Lease

COMBINATIONS = ("base-base", "base-head", "head-base", "head-head")
SOURCE_REPOS = ("zfc", "zfstack", "private_tun", "shadow_tls", "rig")
SKIP = {".agents", ".claude", ".grok", ".github", ".commandcode", "tasks", "docs", "target", "node_modules", ".gitnexus"}
RIG_FILES = {"Cargo.toml", "Cargo.lock", "rig.env.example", "lib/common.sh", "lib/topo.sh", "lib/nftpath.sh", "lib/fwd.sh", "tools/wg-portmap.py", "tools/portmap.py"}
GEO_FILES = {"shared-crate/crates/sc-geo/data/Country.mmdb", "shared-crate/crates/sc-geo/data/dbip-asn-lite.mmdb.gz"}
BUILD_FLAGS = ("RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS", "RUSTDOCFLAGS", "CC", "CXX", "CARGO_BUILD_TARGET")


def _git(root, *args, data=None):
    process = subprocess.run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
                             input=data, capture_output=True, timeout=120)
    if process.returncode:
        raise EvidenceError(f"Git operation failed: {args[0]}")
    return process.stdout


def resolve_ref(repo, ref):
    repo = Path(repo).resolve()
    if not repo.is_dir() or not isinstance(ref, str) or not ref or "\x00" in ref:
        raise EvidenceError("missing repository or fixed ref")
    sha = _git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40,64}", sha):
        raise EvidenceError("ref did not resolve to a full commit")
    return sha


def _allowed(path, kind):
    p = Path(path)
    if p.is_absolute() or any(x in SKIP or x in ("..", ".git") for x in p.parts):
        return False
    if kind == "rig":
        return path in RIG_FILES or (path.startswith("crates/") and (p.suffix == ".rs" or p.name == "Cargo.toml"))
    return p.suffix in (".rs", ".c", ".h", ".S", ".proto", ".cmake") or p.name in (
        "Cargo.toml", "Cargo.lock", "rust-toolchain.toml", "CMakeLists.txt") or path in GEO_FILES


def export_commit(repo, sha, destination, kind):
    """Preserve original SHA in a sparse checkout without copying excluded blobs.

    Only this commit object and tree objects are retained; no parent history,
    local Git configuration or excluded blob (including tracked keys) is copied.
    """
    repo, destination = Path(repo), Path(destination)
    if resolve_ref(repo, sha) != sha or kind not in SOURCE_REPOS:
        raise EvidenceError("invalid fixed source identity")
    destination.mkdir(parents=True, exist_ok=False)
    _git(destination, "init", "--quiet", "--template=")
    rows = _git(repo, "ls-tree", "-rtz", sha).split(b"\0")
    files, excluded = [], []
    # Persistent cat-file avoids launching a process for each Rust source file.
    with subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as reader:
        def copy_object(obj, expected_kind):
            reader.stdin.write(obj.encode() + b"\n")
            reader.stdin.flush()
            header = reader.stdout.readline().decode().split()
            if len(header) != 3 or header[1] != expected_kind:
                raise EvidenceError("invalid Git object response")
            payload = reader.stdout.read(int(header[2]))
            if reader.stdout.read(1) != b"\n":
                raise EvidenceError("truncated Git object")
            actual = _git(destination, "hash-object", "-w", "-t", expected_kind, "--stdin", data=payload).decode().strip()
            if actual != obj:
                raise EvidenceError("exported object hash mismatch")
            return payload
        for row in rows:
            if not row:
                continue
            details, raw_path = row.split(b"\t", 1)
            mode, objkind, obj = details.decode().split()
            path = raw_path.decode("utf-8")
            if objkind == "tree":
                copy_object(obj, "tree")
            elif objkind == "blob" and _allowed(path, kind):
                if mode not in ("100644", "100755"):
                    raise EvidenceError(f"symlink build input refused: {path}")
                payload = copy_object(obj, "blob")
                files.append({"path": path, "sha256": hashlib.sha256(payload).hexdigest()})
            elif objkind == "blob":
                excluded.append(path)
            else:
                raise EvidenceError(f"unsupported source entry: {path}")
        tree = _git(repo, "rev-parse", sha + "^{tree}").decode().strip()
        copy_object(tree, "tree")
        copy_object(sha, "commit")
        reader.stdin.close()
        reader.stdin = None
        reader.wait(timeout=10)
    (destination / ".git/shallow").write_text(sha + "\n")
    _git(destination, "update-ref", "refs/heads/qa-fixed", sha)
    _git(destination, "symbolic-ref", "HEAD", "refs/heads/qa-fixed")
    _git(destination, "read-tree", sha)
    if excluded:
        _git(destination, "update-index", "--skip-worktree", "-z", "--stdin", data=b"\0".join(p.encode() for p in excluded) + b"\0")
    # Older Git versions do not consistently honor skip-worktree with --all.
    # Name only approved blobs so excluded keys are never read or materialized.
    _git(destination, "checkout-index", "-z", "--stdin",
         data=b"\0".join(item["path"].encode() for item in files) + (b"\0" if files else b""))
    if _git(destination, "status", "--porcelain"):
        raise EvidenceError("exported sparse source is dirty")
    return {"sha": sha, "tree": tree, "source_hash": digest(files), "files": files,
            "excluded_files": len(excluded), "export": "committed_allowlist_sparse_checkout"}


def _patch_stack(zfc):
    path = zfc / "Cargo.toml"
    if not path.is_file():
        raise EvidenceError("ZFC workspace manifest is missing")
    text = path.read_text()
    definition = tomllib.loads(text)
    url = "https://github.com/BlackLuny/zfstack.git"
    patches = definition.get("patch", {})
    if any("zfstack" in row for key, row in patches.items() if "BlackLuny/zfstack" in key):
        raise EvidenceError("existing zfstack source patch requires explicit reconciliation")
    before = file_hash(path)
    text += '\n# QA frozen cross-repository source override.\n[patch."' + url + '"]\nzfstack = { path = "../zfstack" }\n'
    path.write_text(text)
    return {"path": "Cargo.toml", "before_sha256": before, "after_sha256": file_hash(path),
            "kind": "git_source_to_fixed_sibling_path", "target": "../zfstack"}


def archive_matrix(repositories, refs, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    resolved, errors = {}, {}
    for key, (repo_kind, ref) in refs.items():
        try:
            resolved[key] = resolve_ref(repositories[repo_kind], ref)
        except (EvidenceError, OSError) as error:
            resolved[key] = None
            errors[key] = str(error)
    manifest = {"schema_version": 1, "type": "frozen_crossrepo_matrix", "resolved_refs": resolved,
                "ref_errors": errors, "combinations": [], "performance_eligible": False}
    for combination in COMBINATIONS:
        zfc_arm, stack_arm = combination.split("-")
        selected = {"zfc": resolved.get("zfc_" + zfc_arm), "zfstack": resolved.get("zfstack_" + stack_arm),
                    **{name: resolved.get(name) for name in ("private_tun", "shadow_tls", "rig")}}
        entry = {"combination": combination, "source_refs": selected, "status": "PREPARED", "errors": [], "sources": {}}
        manifest["combinations"].append(entry)
        case = output / combination
        case.mkdir()
        try:
            if not all(selected.values()):
                raise EvidenceError("missing fixed ref: " + ", ".join(name for name, value in selected.items() if not value))
            # Each combination contains identical frozen private_tun/shadow blobs,
            # avoiding shared mutable targets or dependency checkouts across arms.
            for name, sha in selected.items():
                directory = "shadow-tls-tokio" if name == "shadow_tls" else ("zfc-rig" if name == "rig" else name)
                entry["sources"][name] = export_commit(repositories[name], sha, case / "repos" / directory, name)
            entry["patches"] = [_patch_stack(case / "repos/zfc")]
            links = case / "repos/zfc-rig/.repos"
            links.mkdir()
            for name in ("zfc", "private_tun", "shadow-tls-tokio"):
                (links / name).symlink_to(Path("../..") / name)
        except (EvidenceError, OSError, subprocess.SubprocessError) as error:
            entry.update(status="UNBUILDABLE", errors=[str(error)])
        write_json(case / "source-manifest.json", entry, exclusive=True)
    manifest["archive_hash"] = digest(manifest)
    write_json(output / "archive-manifest.json", manifest, exclusive=True)
    return manifest


def validate_matrix(manifest):
    rows = manifest.get("combinations", [])
    if len(rows) != 4 or {row.get("combination") for row in rows} != set(COMBINATIONS):
        raise EvidenceError("all four distinct cross-repository combinations are required")
    for row in rows:
        refs = row.get("source_refs", {})
        if row.get("status") == "PREPARED" and (set(refs) != set(SOURCE_REPOS) or not all(refs.values())):
            raise EvidenceError("prepared combination lacks fixed source refs")
    for name in ("private_tun", "shadow_tls", "rig"):
        if len({row.get("source_refs", {}).get(name) for row in rows}) != 1:
            raise EvidenceError(f"{name} differs across the frozen four-factor matrix")
    for name, position in (("zfc", 0), ("zfstack", 1)):
        for arm in ("base", "head"):
            values = {row.get("source_refs", {}).get(name) for row in rows
                      if row["combination"].split("-")[position] == arm}
            if len(values) != 1:
                raise EvidenceError(f"inconsistent {name} {arm} factor")


def _run(argv, cwd, log, env, timeout, minimum_free, *, lease, stderr=None):
    class Reserve:
        def check(self):
            lease.check()
            if shutil.disk_usage(log.parent).free < minimum_free:
                raise EvidenceError("disk reserve below required minimum")
    Reserve().check()
    execution = run_child(argv, cwd, log, timeout, env, Reserve(), stderr=stderr)
    passed = (execution["status"] == "completed" and execution["exit_code"] == 0
              and execution["cleanup_status"] == "PASS")
    record = {"argv": argv, "log": log.name, "status": "PASS" if passed else "FAILED",
              "exit_code": execution["exit_code"], "cleanup": execution["cleanup_status"],
              "execution": execution,
              "elapsed_seconds": (execution["end_ns"] - execution["start_ns"]) / 1_000_000_000}
    if stderr is not None:
        record["stderr_log"] = stderr.name
    return record


def _progress(entry, out, lease):
    """Incremental raw evidence is explicitly pending, never a released build."""
    pending = {**entry, "status": "PENDING",
               "build_lease": {**lease.value, "release_status": "PENDING"}}
    write_json(out / "build-progress.json", pending)


def _build_matrix_locked(archive, output, *, lease, cargo="cargo", rustc="rustc", toolchain="1.98.1", profile="fast-release",
                 features=("mmsg", "jemalloc"), jobs=2, timeout=3600, minimum_free=3 * 1024**3, fakectl=True,
                 cells=(), cache_dir=None, forwarder=False):
    archive, output = Path(archive).resolve(), Path(output).resolve()
    plan = read_json(archive / "archive-manifest.json")
    validate_matrix(plan)
    claimed = plan.pop("archive_hash", None)
    if claimed != digest(plan):
        raise EvidenceError("archive manifest hash mismatch")
    if output.is_relative_to(archive):
        raise EvidenceError("build outputs must be outside the frozen archive")
    cache = Path(cache_dir).resolve() if cache_dir is not None else None
    if cache is not None:
        if not Path(cache_dir).is_absolute() or any(cache.is_relative_to(p) or p.is_relative_to(cache) for p in (archive, output)):
            raise EvidenceError("build cache must be an absolute directory separate from archive and outputs")
        cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=False)
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", toolchain) or not re.fullmatch(r"[a-zA-Z0-9_-]+", profile) or not 1 <= jobs <= 2:
        raise EvidenceError("invalid bounded build settings")
    if any(not re.fullmatch(r"[a-zA-Z0-9_/-]+", item) for item in features):
        raise EvidenceError("invalid feature name")
    selected = set(cells or COMBINATIONS)
    if not selected.issubset(COMBINATIONS):
        raise EvidenceError("unknown cross-repository combination")
    results = []
    for source in plan["combinations"]:
        label = source["combination"]
        out = output / label
        out.mkdir()
        entry = {"schema_version": 1, "combination": label, "status": "UNBUILDABLE", "source_refs": source["source_refs"],
                 "zfstack_sha": source["source_refs"]["zfstack"], "toolchain": toolchain, "profile": profile,
                 "features": list(features), "default_features": True, "commands": [],
                 "errors": list(source["errors"]), "performance_eligible": False}
        results.append(entry)
        if label not in selected:
            entry.update(status="UNEXECUTED", errors=["combination was not selected for execution"])
            _progress(entry, out, lease)
            continue
        root = out / "repos"
        env = os.environ.copy()
        worker_target = cache / "worker" if cache else out / "target"
        env.update(CARGO_TARGET_DIR=str(worker_target), CARGO_BUILD_JOBS=str(jobs))
        entry["target_directory"] = str(worker_target)
        def run(argv, cwd=root / "zfc", *, parsed=False):
            # Cargo writes download progress and warnings to stderr; parsed
            # stdout must not share a file with them. Both logs are retained.
            index = len(entry["commands"])
            rec = _run(argv, cwd, out / f"command-{index}.log", env, timeout, minimum_free, lease=lease,
                       stderr=out / f"command-{index}.stderr.log" if parsed else None)
            entry["commands"].append(rec)
            _progress(entry, out, lease)
            if rec["status"] != "PASS":
                raise EvidenceError("command failed: " + " ".join(argv[:5]))
            return out / rec["log"]
        try:
            lease.check()
            if source["status"] != "PREPARED":
                raise EvidenceError("fixed source combination is unavailable")
            shutil.copytree(archive / label / "repos", root, symlinks=True)
            for name, snapshot in source["sources"].items():
                directory = "shadow-tls-tokio" if name == "shadow_tls" else ("zfc-rig" if name == "rig" else name)
                checkout = root / directory
                if resolve_ref(checkout, "HEAD") != source["source_refs"][name]:
                    raise EvidenceError("source checkout commit differs from frozen ref")
                actual_files = {path.relative_to(checkout).as_posix() for path in checkout.rglob("*")
                                if path.is_file() and ".git" not in path.relative_to(checkout).parts
                                and ".repos" not in path.relative_to(checkout).parts}
                if actual_files != {item["path"] for item in snapshot["files"]}:
                    raise EvidenceError("source input inventory differs from frozen archive")
                for item in snapshot["files"]:
                    path = root / directory / item["path"]
                    patch = next((p for p in source.get("patches", []) if name == "zfc" and p["path"] == item["path"]), None)
                    if not path.is_file() or path.is_symlink() or file_hash(path) != (patch["after_sha256"] if patch else item["sha256"]):
                        raise EvidenceError("source changed since archive: " + str(path.relative_to(root)))
            rust_log = run([rustc, "+" + toolchain, "-vV"], parsed=True)
            cargo_log = run([cargo, "+" + toolchain, "-V"], parsed=True)
            entry["rustc_vv"] = rust_log.read_text().strip()
            entry["cargo_version"] = cargo_log.read_text().strip()
            entry["host"] = next((line[6:] for line in entry["rustc_vv"].splitlines() if line.startswith("host: ")), None)
            if not entry["host"]:
                raise EvidenceError("rustc omitted actual host identity")
            entry["target"] = env.get("CARGO_BUILD_TARGET") or entry["host"]
            entry["build_flags"] = {key: env.get(key, "") for key in BUILD_FLAGS}
            entry["runtime_id"] = digest({key: entry[key] for key in
                                          ("rustc_vv", "cargo_version", "toolchain", "host", "target", "build_flags")})
            lock = root / "zfc/Cargo.lock"
            entry["original_lock_sha256"] = file_hash(lock)
            run([cargo, "+" + toolchain, "update", "-p", "zfstack"])
            entry["lock_sha256"] = file_hash(lock)
            shutil.copyfile(lock, out / "worker-Cargo.lock")
            meta_log = run([cargo, "+" + toolchain, "metadata", "--locked", "--format-version", "1"], parsed=True)
            metadata = read_json(meta_log)
            packages = [p for p in metadata["packages"] if p["name"] == "zfstack"]
            if len(packages) != 1 or Path(packages[0]["manifest_path"]).resolve() != (root / "zfstack/Cargo.toml").resolve() or packages[0]["source"] is not None:
                raise EvidenceError("worker did not resolve the frozen local ZFStack")
            entry["metadata_hash"] = digest(metadata)
            tree = [cargo, "+" + toolchain, "tree", "--locked", "-p", "zf-worker", "--edges", "normal,build",
                    "--prefix", "depth", "--charset", "ascii", "--format", "{p}|{f}"]
            if features:
                tree += ["--features", ",".join(features)]
            if env.get("CARGO_BUILD_TARGET"):
                tree += ["--target", env["CARGO_BUILD_TARGET"]]
            entry["dependency_graph"] = run(tree, parsed=True).read_text().strip()
            if not entry["dependency_graph"]:
                raise EvidenceError("worker dependency graph is empty")
            entry["dependency_graph_hash"] = digest(entry["dependency_graph"])
            argv = ["nice", "-n", "10", cargo, "+" + toolchain, "build", "--locked", "-p", "zf-worker", "--bin", "zf-worker", "--profile", profile]
            if features:
                argv += ["--features", ",".join(features)]
            run(argv)
            profile_dir = "release" if profile == "release" else ("debug" if profile == "dev" else profile)
            target = env.get("CARGO_BUILD_TARGET")
            binary = worker_target / (target or "") / profile_dir / "zf-worker"
            if not binary.is_file():
                raise EvidenceError("worker binary is missing")
            installed = out / "zf-worker"
            shutil.copyfile(binary, installed)
            installed.chmod(0o755)
            entry["binary_path"] = str(installed)
            entry["binary_sha256"] = file_hash(installed)
            build_log = run([str(installed), "--build-info"], parsed=True)
            entry["build_info"] = read_json(build_log)
            if (not isinstance(entry["build_info"], dict)
                    or entry["build_info"].get("source", {}).get("commit") != source["source_refs"]["zfc"]
                    or entry["build_info"].get("private_tun", {}).get("commit") != source["source_refs"]["private_tun"]):
                raise EvidenceError("worker build-info does not identify the frozen product sources")
            if forwarder:
                packages = [p for p in metadata["packages"] if p["name"] == "private_tun"]
                if (len(packages) != 1 or packages[0]["source"] is not None
                        or Path(packages[0]["manifest_path"]).resolve() != (root / "private_tun/Cargo.toml").resolve()):
                    raise EvidenceError("forwarder did not resolve the frozen local private_tun")
                fwd_tree = list(tree)
                fwd_tree[fwd_tree.index("-p") + 1] = "forwarder-server"
                graph = run(fwd_tree, parsed=True).read_text().strip()
                if not graph.startswith("0forwarder-server v"):
                    raise EvidenceError("forwarder selected dependency graph is missing")
                fwd_argv = list(argv)
                fwd_argv[fwd_argv.index("-p") + 1] = "forwarder-server"
                fwd_argv[fwd_argv.index("--bin") + 1] = "forwarder-server"
                run(fwd_argv)
                built = worker_target / (target or "") / profile_dir / "forwarder-server"
                if not built.is_file():
                    raise EvidenceError("forwarder binary is missing")
                installed_fwd = out / "forwarder-server"
                shutil.copyfile(built, installed_fwd)
                installed_fwd.chmod(0o755)
                fwd_info = read_json(run([str(installed_fwd), "--build-info"], parsed=True))
                if (not isinstance(fwd_info, dict) or fwd_info.get("source", {}).get("commit") != source["source_refs"]["zfc"]
                        or fwd_info.get("private_tun", {}).get("commit") != source["source_refs"]["private_tun"]):
                    raise EvidenceError("forwarder build-info does not identify the frozen product sources")
                entry["forwarder"] = {"binary_path": str(installed_fwd), "binary_sha256": file_hash(installed_fwd),
                                      "size_bytes": installed_fwd.stat().st_size, "build_info": fwd_info,
                                      "dependency_graph": graph, "dependency_graph_hash": digest(graph)}
            if fakectl:
                rig_target = cache / "rig" if cache else out / "rig-target"
                env["CARGO_TARGET_DIR"] = str(rig_target)
                rig_lock = root / "zfc-rig/Cargo.lock"
                entry["rig_original_lock_sha256"] = file_hash(rig_lock)
                # A different fixed ZFC arm can change path package versions.
                # Resolve that source combination explicitly before --locked.
                run([cargo, "+" + toolchain, "update", "-p", "sc-types"], root / "zfc-rig")
                entry["rig_lock_sha256"] = file_hash(rig_lock)
                shutil.copyfile(rig_lock, out / "fakectl-Cargo.lock")
                run(["nice", "-n", "10", cargo, "+" + toolchain, "build", "--locked", "--release", "-p", "fakectl"], root / "zfc-rig")
                ctl = rig_target / (target or "") / "release/fakectl"
                shutil.copyfile(ctl, out / "fakectl")
                (out / "fakectl").chmod(0o755)
                entry["fakectl_sha256"] = file_hash(out / "fakectl")
            entry["source_hashes"] = {key: value["source_hash"] for key, value in source["sources"].items()}
            entry["patches"] = source.get("patches", [])
            lease.check()
            entry["status"] = "BUILT"
        except (EvidenceError, OSError, subprocess.SubprocessError, KeyError, ValueError) as error:
            entry["errors"].append(str(error))
        _progress(entry, out, lease)
    reasons = []
    if selected != set(COMBINATIONS):
        reasons.append("all four combinations were not executed")
    for name in ("zfc", "zfstack"):
        if len({row["source_refs"][name] for row in results}) != 2:
            reasons.append(f"{name} base/head factors do not identify two distinct commits")
    failed = any(row["status"] == "UNBUILDABLE" for row in results)
    result = {"schema_version": 1, "archive_hash": claimed, "combinations": results, "performance_eligible": False,
              "selected_status": "FAIL" if failed else "PASS",
              "factorial_status": "FAIL" if failed else ("INCOMPLETE" if reasons else "PASS"),
              "factorial_reasons": reasons}
    return result


def build_matrix(archive, output, *, node=None, lease_dir=None, **settings):
    """Hold the measurement domain's public lease throughout every build cell.

    No borrowed lease/implicit unlocked mode exists. Final manifests are sealed
    only after ownership-checked release, so a lost lease cannot publish BUILT.
    """
    if (not isinstance(node, dict) or any(not isinstance(node.get(key), str) or not node[key].strip()
            or len(node[key]) > 256 for key in ("physical_domain", "generation"))):
        raise EvidenceError("crossrepo build requires fixed node physical_domain and generation")
    if lease_dir is None or not Path(lease_dir).is_absolute():
        raise EvidenceError("crossrepo build requires an absolute shared lease_dir")
    output = Path(output).resolve()
    lease = Lease(lease_dir, node["physical_domain"], "crossrepo:" + str(uuid.uuid4()), node["generation"])
    result = None
    try:
        with lease:
            result = _build_matrix_locked(archive, output, lease=lease, **settings)
            lease.check()
    except EvidenceError as error:
        if result is None:
            raise
        # Preserve every original command and cleanup record. Even previously
        # built arms cannot claim an uncontested matrix after ownership loss.
        for entry in result["combinations"]:
            if entry["status"] != "UNEXECUTED":
                entry["status"] = "UNBUILDABLE"
                entry["errors"].append("matrix lease ownership lost: " + str(error))
        result.update(selected_status="FAIL", factorial_status="FAIL")
        result["factorial_reasons"].append("physical-domain lease lost; node quarantined")
    lease_record = {**lease.value, "release_status": "PASS" if not lease.owned else "LOST"}
    result["build_lease"] = lease_record
    for entry in result["combinations"]:
        entry["build_lease"] = lease_record
        write_json(output / entry["combination"] / "worker-manifest.json", entry, exclusive=True)
    write_json(output / "build-matrix.json", result, exclusive=True)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("archive")
    for name in SOURCE_REPOS:
        prepare.add_argument("--" + name.replace("_", "-") + "-repo", type=Path, required=True)
        for suffix in (("base-ref", "head-ref") if name in ("zfc", "zfstack") else ("ref",)):
            prepare.add_argument("--" + name.replace("_", "-") + "-" + suffix, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    build = sub.add_parser("build")
    build.add_argument("--archive", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--node", type=Path, required=True, help="node JSON with frozen physical_domain and generation")
    build.add_argument("--lease-dir", type=Path, required=True, help="absolute public lease directory shared with measurements")
    build.add_argument("--cargo", default="cargo")
    build.add_argument("--rustc", default="rustc")
    build.add_argument("--toolchain", default="1.98.1")
    build.add_argument("--profile", default="fast-release")
    build.add_argument("--jobs", type=int, default=2)
    build.add_argument("--cache-dir", type=Path,
                       help="absolute mutable Cargo cache outside archive/output; binaries are still copied and verified per build")
    build.add_argument("--timeout", type=int, default=3600)
    build.add_argument("--no-fakectl", action="store_true")
    build.add_argument("--forwarder", action="store_true", help="also freeze/build the actual private_tun relay executable")
    build.add_argument("--cell", choices=COMBINATIONS, action="append", default=[],
                       help="execute only selected cells; the remaining cells stay UNEXECUTED")
    args = parser.parse_args(argv)
    try:
        if args.command == "archive":
            repos = {name: getattr(args, name + "_repo") for name in SOURCE_REPOS}
            refs = {name + "_" + arm: (name, getattr(args, name + "_" + arm + "_ref")) for name in ("zfc", "zfstack") for arm in ("base", "head")}
            refs.update({name: (name, getattr(args, name + "_ref")) for name in ("private_tun", "shadow_tls", "rig")})
            result = archive_matrix(repos, refs, args.output)
            return 0 if all(x["status"] == "PREPARED" for x in result["combinations"]) else 1
        result = build_matrix(args.archive, args.output, node=read_json(args.node), lease_dir=args.lease_dir, cargo=args.cargo, rustc=args.rustc, toolchain=args.toolchain,
                              profile=args.profile, jobs=args.jobs, timeout=args.timeout, fakectl=not args.no_fakectl,
                              cells=args.cell, cache_dir=args.cache_dir, forwarder=args.forwarder)
        return 0 if result["selected_status"] == "PASS" else 1
    except (EvidenceError, OSError, subprocess.SubprocessError) as error:
        print(f"crossrepo BLOCKED: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
