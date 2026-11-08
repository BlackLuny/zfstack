#!/usr/bin/env python3
"""Prepare a fail-closed #66 frozen A/B/C/D/F + independent F2 comparison matrix.

This is an input-identity/coverage planner, NOT a performance runner or a
qualification result. Formal execution must use the leased QA runner.
"""
import argparse
import hashlib
import json
from pathlib import Path
import random
import subprocess

from perf_gate import canonical, digest, read_json, sha


REVISIONS = {
    "A": "56aed4ffd92f628d58a8ad773c52baf685fab79e",
    "B": "8b3f418f15f473baf5dce043c981ab753f0a5a11",
    "C": "37c016d3e136426994e4b40198e236d5ffbc0fb6",
    "D": "c2ae50439e50c081940b951bd075a6ce4bb41335",
    "F": "3459562746e689fc5ec78c5b1764b0bba3465363",
}
COMPARISONS = (
    ("F2/F (independent A/A)", "F", "F2"),
    ("B/A", "A", "B"),
    ("C/B", "B", "C"),
    ("C/A", "A", "C"),
    ("D/C", "C", "D"),
    ("F/D", "D", "F"),
    ("F/B", "B", "F"),
)
COMMON_FIELDS = (
    "rustc", "cargo", "target", "features", "rustflags", "profile",
    "manifest_sha256", "lock_sha256", "probe_sha256",
    "harness_sha256", "case_generator_sha256",
)


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def git_identity(path):
    """Avoid accepting claimed source SHAs detached from real Git checkouts."""
    root = Path(path).resolve(strict=True)
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    changes = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--", "src", "tests"],
        capture_output=True, text=True, check=True,
    ).stdout
    if changes:
        raise ValueError(f"{root}: modified production sources/tests")
    return result


def check_cases(cases):
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a nonempty array")
    names = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("case must be an object")
        name, args = case.get("name"), case.get("args")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"empty/duplicate case: {name!r}")
        if not isinstance(args, list) or not args or not all(isinstance(a, str) and a for a in args):
            raise ValueError(f"{name}: invalid argument vector")
        names.add(name)


def build_matrix(manifest, cases, pairs=12, seed=66, expected_revisions=REVISIONS):
    if type(pairs) is not int or pairs < 12:
        raise ValueError("#66 formal target requires at least 12 independent paired blocks")
    if type(seed) is not int:
        raise ValueError("seed must be integer")
    if not isinstance(manifest, dict):
        raise ValueError("build manifest must be an object")
    common = manifest.get("common")
    variants = manifest.get("variants")
    if not isinstance(common, dict) or set(common) != set(COMMON_FIELDS):
        raise ValueError("incomplete/extra common input fields")
    if not all(isinstance(common[k], str) and common[k] for k in COMMON_FIELDS[:6]):
        raise ValueError("invalid toolchain or build configuration")
    if not all(sha(common[k]) for k in COMMON_FIELDS[6:]):
        raise ValueError("invalid frozen input SHA-256")
    paths = manifest.get("common_input_paths")
    hashed_fields = COMMON_FIELDS[6:]
    if not isinstance(paths, dict) or set(paths) != set(hashed_fields):
        raise ValueError("must locate every frozen common input file")
    for field in hashed_fields:
        if not isinstance(paths[field], str) or file_sha(paths[field]) != common[field]:
            raise ValueError(f"{field}: frozen source file changed")
    if not isinstance(variants, dict) or set(variants) != {*expected_revisions, "F2"}:
        raise ValueError("must provide exactly A/B/C/D/F and independent F2")
    check_cases(cases)
    verified = {}
    seen_build_dirs = set()
    for label in sorted(variants):
        value = variants[label]
        if not isinstance(value, dict) or set(value) != {
            "source_dir", "source_sha", "build_dir", "binary_path", "binary_sha256",
            "common_inputs_sha256",
        }:
            raise ValueError(f"{label}: incomplete variant build provenance")
        expected = expected_revisions["F"] if label == "F2" else expected_revisions[label]
        if value["source_sha"] != expected:
            raise ValueError(f"{label}: wrong historical source revision")
        if value["common_inputs_sha256"] != digest(common):
            raise ValueError(f"{label}: common input drift")
        if not sha(value["binary_sha256"]):
            raise ValueError(f"{label}: invalid binary SHA-256")
        if git_identity(value["source_dir"]) != expected:
            raise ValueError(f"{label}: Git HEAD differs from expected source")
        build_dir = str(Path(value["build_dir"]).resolve(strict=True))
        if build_dir in seen_build_dirs:
            raise ValueError(f"{label}: build directories must be independent")
        seen_build_dirs.add(build_dir)
        binary = Path(value["binary_path"]).resolve(strict=True)
        if not binary.is_file() or file_sha(binary) != value["binary_sha256"]:
            raise ValueError(f"{label}: binary content differs from receipt")
        if not binary.is_relative_to(Path(build_dir)):
            raise ValueError(f"{label}: binary must live under attested build directory")
        verified[label] = {
            "source_sha": expected,
            "build_dir": build_dir,
            "binary_path": str(binary),
            "binary_sha256": value["binary_sha256"],
        }
    if verified["F"]["build_dir"] == verified["F2"]["build_dir"]:
        raise ValueError("A/A must use independent F and F2 builds")
    rng = random.Random(seed)
    comparisons = []
    for name, baseline, candidate in COMPARISONS:
        orders = ["ABBA"] * (pairs // 2) + ["BAAB"] * (pairs - pairs // 2)
        rng.shuffle(orders)  # predeclared, balanced order; no outcome-based sampling
        comparisons.append({
            "name": name, "baseline": baseline, "candidate": candidate,
            "comparison": "AA" if baseline == "F" and candidate == "F2" else "AB",
            "orders": orders, "case_names": [case["name"] for case in cases],
            "status": "NOT_RUN",
        })
    return {
        "schema_version": 1, "issue": 66, "kind": "input_preflight_not_performance_evidence",
        "source_revisions": verified, "common": common,
        "common_input_paths": {field: str(Path(paths[field]).resolve(strict=True)) for field in hashed_fields},
        "common_sha256": digest(common), "cases": cases, "cases_sha256": digest(cases),
        "pairs_per_case": pairs, "seed": seed, "comparisons": comparisons,
        "measurement_gates": {
            "frozen_probe": "NOT_RUN", "socket_tun": "NOT_RUN",
            "mtu_1420_1500": "NOT_RUN", "long_soak": "NOT_RUN",
            "independent_AA": "NOT_RUN", "statistical_noninferiority": "NOT_RUN",
        },
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--pairs", type=int, default=12)
    p.add_argument("--seed", type=int, default=66)
    args = p.parse_args()
    try:
        manifest, cases = read_json(args.manifest), read_json(args.cases)
        result = build_matrix(manifest, cases, pairs=args.pairs, seed=args.seed)
        with args.out.open("x", encoding="utf-8") as stream:
            stream.write(canonical(result).decode() + "\n")
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        p.error(str(error))
    print(f"ISSUE66_PLAN_SHA256={file_sha(args.out)} (NOT_RUN; no performance claim)")


if __name__ == "__main__":
    main()
