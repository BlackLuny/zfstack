"""Explicit baseline promotion and release-anchor validation (no auto-relaxation)."""
from __future__ import annotations

from .common import EvidenceError, digest


def promote(plan, report, *, generation, release_anchor, calibration_hashes, expires,
            evidence_root=None, policy_dir=None, provenance=None, repository=None,
            default_branch=None, native_status=None, now=None):
    """Prepare a review candidate only after re-evaluating all original evidence.

    A loaded summary, mutable tag, PR plan or caller-supplied calibration PASS
    can never promote a baseline. No repository or external state is changed.
    """
    from .release import SHA, evaluate_release, expiry
    from .plan import check_trusted_plan
    import re

    if (plan.get("suite") != "release" or not isinstance(release_anchor, str)
            or not SHA.fullmatch(release_anchor)):
        raise EvidenceError("promotion requires a release plan and immutable full release anchor SHA")
    if not isinstance(generation, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", generation):
        raise EvidenceError("invalid baseline generation")
    expiry_date = expiry(expires, now)
    if evidence_root is None or policy_dir is None or provenance is None or not repository or not default_branch:
        raise EvidenceError("promotion requires original raw evidence and trusted run provenance; summary PASS is insufficient")
    from .build_manifest import command
    from .common import ROOT
    resolved_anchor = command(["git", "rev-parse", "--verify", release_anchor + "^{commit}"], ROOT)
    if resolved_anchor != release_anchor:
        raise EvidenceError("release anchor does not identify an immutable commit")
    check_trusted_plan(plan, policy_dir)
    actual = evaluate_release(plan["tested_sha"], evidence_root, policy_dir, provenance=provenance,
                              repository=repository, default_branch=default_branch,
                              native_status=native_status, generation=generation, now=now)
    if actual["status"] != "PASS" or actual != report or actual["plan_hash"] != digest(plan):
        raise EvidenceError("baseline promotion requires independently verified complete release evidence")
    if not isinstance(calibration_hashes, list) or any(not isinstance(value, str) for value in calibration_hashes):
        raise EvidenceError("invalid baseline calibration hash set")
    wanted = sorted(entry["sha256"] for entry in actual["calibrations"])
    if not wanted or sorted(calibration_hashes) != wanted or len(set(wanted)) != len(wanted):
        raise EvidenceError("baseline calibration hashes differ from verified release evidence")
    if any(expiry(entry["expires"], now) < expiry_date for entry in actual["calibrations"]):
        raise EvidenceError("baseline cannot outlive its calibration evidence")
    return {"schema_version": 1, "status": "REVIEW_REQUIRED", "generation": generation,
            "tested_sha": plan["tested_sha"], "plan_hash": digest(plan), "report_hash": digest(actual),
            "policy_hash": plan["policy_hash"], "release_anchor": release_anchor,
            "calibration_hashes": wanted, "calibrations": actual["calibrations"],
            "provenance_hash": actual["provenance_hash"], "expires": expires,
            "activation": "Requires independent review; this file does not change any active baseline or threshold."}


def validate_factorial(combinations, required_path):
    required = {"Z0S0", "Z0S1", "Z1S0", "Z1S1"}
    if not isinstance(combinations, dict) or set(combinations) != required:
        raise EvidenceError("cross-repository comparison requires all four recorded cells")
    frozen_tun = set()
    for name, cell in combinations.items():
        if cell.get("build_status") != "PASS":
            raise EvidenceError(f"{name}: unbuildable/unexecuted combination is BLOCKED")
        if cell.get("functional") != "PASS" or cell.get("path") != required_path or cell.get("path_packets", 0) <= 0:
            raise EvidenceError(f"{name}: actual requested data path was not proved")
        for key in ("zfc_sha", "zfstack_sha", "private_tun_sha", "binary_sha256", "build_manifest_hash"):
            if not cell.get(key):
                raise EvidenceError(f"{name}: missing {key}")
        frozen_tun.add(cell["private_tun_sha"])
    if len(frozen_tun) != 1:
        raise EvidenceError("private_tun must be frozen in this 2x2 experiment")
    if combinations['Z0S0']['zfc_sha'] != combinations['Z0S1']['zfc_sha'] or combinations['Z1S0']['zfc_sha'] != combinations['Z1S1']['zfc_sha']:
        raise EvidenceError("zfc factor identity mismatch")
    if combinations['Z0S0']['zfstack_sha'] != combinations['Z1S0']['zfstack_sha'] or combinations['Z0S1']['zfstack_sha'] != combinations['Z1S1']['zfstack_sha']:
        raise EvidenceError("zfstack factor identity mismatch")
    return True
