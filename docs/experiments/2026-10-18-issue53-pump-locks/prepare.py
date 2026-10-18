#!/usr/bin/env python3
"""Prepare immutable A/A/B release-example overlays; never build or run them."""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

BASELINE = "ed4655e0798efc6b02883967cf933291dba059c8"
CANDIDATE = "e0cd89ab745f025cbc5ccb89c69aba0681a2a13e"

def sha256(data):
    return hashlib.sha256(data).hexdigest()

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--baseline", default=BASELINE)
    p.add_argument("--candidate", default=CANDIDATE)
    args = p.parse_args()
    source = Path(__file__).resolve().parent
    probe = (source / "probe.rs.txt").read_text()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output / "MANIFEST.json"
    if manifest_path.exists():
        raise SystemExit("Refusing to overwrite an existing frozen MANIFEST.json")
    manifest = {"schema": 1, "baseline": args.baseline, "candidate": args.candidate,
                "probe_sha256": sha256(probe.encode()), "overlays": []}
    for label, commit, counted in [("A1", args.baseline, False), ("A2", args.baseline, False),
                                   ("B", args.candidate, False), ("A-counts", args.baseline, True),
                                   ("B-counts", args.candidate, True)]:
        commit = subprocess.check_output(["git", "-C", str(args.repo), "rev-parse", "--verify", commit + "^{commit}"], text=True).strip()
        target = args.output / label
        if target.exists():
            raise SystemExit(f"Refusing to overwrite existing source overlay: {target}")
        archive = subprocess.check_output(["git", "-C", str(args.repo), "archive", commit])
        target.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tf:
            # Python 3.11 compatibility: validate all members before extraction.
            # Git archive needs regular files/directories only for this overlay;
            # reject links and special files rather than following them.
            for member in tf.getmembers():
                relative = PurePosixPath(member.name)
                if (relative.is_absolute() or ".." in relative.parts
                        or not (member.isfile() or member.isdir())):
                    raise SystemExit(f"Unsafe archive member: {member.name!r}")
                resolved = (target / member.name).resolve()
                if not resolved.is_relative_to(target.resolve()):
                    raise SystemExit(f"Archive member escapes output: {member.name!r}")
            tf.extractall(target)
        path = target / "src/tokio_adapter.rs"
        original = path.read_text()
        start = original.index("    fn pump(&mut self, id: ConnId) {")
        # Follow the Rust braces to isolate exactly the production pump body.
        opening = original.index("{", start)
        depth, end = 1, opening + 1
        while depth:
            depth += (original[end] == "{") - (original[end] == "}")
            end += 1
        body = original[start:end]
        lock_sites = body.count("sh.q.lock().unwrap()")
        if lock_sites != 3:
            raise SystemExit(f"Unexpected production pump lock sites at {commit}: {lock_sites}")
        modified = original
        if counted:
            body = body.replace("sh.q.lock().unwrap()", "issue53_pump_probe::lock_queue(&sh)")
            modified = original[:start] + body + original[end:]
        modified += '\n#[cfg(feature = "test-peer")]\nmod issue53_pump_probe;\n#[cfg(feature = "test-peer")]\npub use issue53_pump_probe::main as issue53_pump_probe_main;\n'
        path.write_text(modified)
        (target / "src/tokio_adapter/issue53_pump_probe.rs").write_text(probe)
        (target / "examples/issue53_pump_probe.rs").write_text("fn main() { zfstack::tokio_adapter::issue53_pump_probe_main(); }\n")
        cargo = target / "Cargo.toml"
        cargo.write_text(cargo.read_text().replace("[features]\n", '[features]\nissue53-pump-counts = []\n', 1))
        features = "tokio,test-peer" + (",issue53-pump-counts" if counted else "")
        manifest["overlays"].append({"label": label, "commit": commit,
            "git_tree": subprocess.check_output(["git", "-C", str(args.repo), "rev-parse", commit + "^{tree}"], text=True).strip(),
            "path": str(target.resolve()), "counted": counted, "production_lock_sites": lock_sites,
            "adapter_original_sha256": sha256(original.encode()), "adapter_overlay_sha256": sha256(modified.encode()),
            "build": ["cargo", "+1.94.1", "build", "--locked", "--release", "-p", "zfstack", "--example", "issue53_pump_probe", "--features", features]})
    manifest["build_env"] = {"CARGO_INCREMENTAL": "0", "CARGO_PROFILE_RELEASE_LTO": "fat",
        "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "1", "CARGO_PROFILE_RELEASE_PANIC": "abort"}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"manifest": str(manifest_path), "overlays": len(manifest["overlays"])}))

if __name__ == "__main__":
    main()
