"""Offline independent verification of the single retained formal pair."""
import collections
import gzip
import json
import sys
from pathlib import Path

P = Path(__file__).resolve().parent
ROOT = P.parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(P.parents[1]))
import fifo_ledger as L
from qa.common import read_json, write_json
from qa.evaluate import evaluate, verify_artifacts, verify_execution_evidence
from qa.build_manifest import verify_manifest
from qa.integration import queue_negative_evidence, verify_integration

plan = read_json(P / "collected/evidence/plan.json")
paths = [p for p in (P / "formal-artifacts").rglob("result.json")
         if read_json(p).get("case_id") == "C16-l2-queue-control"]
assert len(paths) == 1
directory = paths[0].parent
result = read_json(paths[0])
case = next(c for c in plan["cases"] if c["case_id"] == "C16-l2-queue-control")
worker = read_json(directory / "worker-manifest.json")
status, details, observations = queue_negative_evidence(case, directory / "baseline", directory / "fault", worker)
verify_integration(result, case, directory)
manifest = verify_artifacts(result, directory)
verify_manifest(manifest, case, plan, read_json(directory / "environment.json"))
verify_execution_evidence(result, case, directory, ROOT / "qa/policy")
evaluation = evaluate(plan, paths, ROOT / "qa/policy", expected_sha=plan["tested_sha"])
assert evaluation["status"] == "FAIL" and status == result["functional"]["status"] == "FAIL"
write_json(P / "local-evaluation.json", evaluation)

ctrl = read_json(directory / "fault/rig/queue-control.json")
rows = L.decode((directory / "fault/rig/queue-ledger.bin.gz").read_bytes())
zero = ctrl["planned"]["measurement_start_ns"]
failed = [r for r in rows if r[2] == L.SEND_FAILED]
result_data = {"tested_sha": plan["tested_sha"], "case": case["case_id"],
    "case_version": case["case_version"], "functional": status, "functional_validity": result["validity"]["functional"],
    "execution": result["execution"], "worker_sha256": worker["binary_sha256"],
    "artifact_build_and_command_identity": "PASS",
    "evaluation_case_blocked": [x for x in evaluation["blocked"] if case["case_id"] in x],
    "baseline_service": observations["baseline"]["service_result"]["status"],
    "baseline_raw_functional": observations["baseline"]["raw_functional_status"],
    "fault_service": observations["fault"]["service_result"]["status"],
    "fault_raw_functional": observations["fault"]["raw_functional_status"],
    "recovery_service": observations["recovery"]["status"],
    "queue_errors": observations["queue_evidence_errors"], "ledger_frames": len(rows),
    "outcomes": {str(k): v for k, v in collections.Counter(r[2] for r in rows).items()},
    "late_frames": sum(r[8]-r[7] > L.LATENESS_LIMIT_NS for r in rows if r[2] == L.DELIVERED),
    "max_lateness_by_phase_ns": {str(p): max((r[8]-r[7] for r in rows if r[1] == p and r[2] == L.DELIVERED), default=None)
                                 for p in range(4)},
    "send_failure_phases": {str(k): v for k, v in collections.Counter(r[1] for r in failed).items()},
    "send_failure_window_after_measurement_start_ns": [min(r[6]+r[8]-zero for r in failed),
                                                      max(r[6]+r[8]-zero for r in failed)],
    "transparent_late_by_ns": ctrl["actual"]["transparent_ns"]-ctrl["planned"]["recovery_start_ns"],
    "planned": ctrl["planned"], "actual": ctrl["actual"], "resources": ctrl["resources"],
    "kernel": ctrl["kernel"], "arms": {}}
for arm in ("baseline", "fault"):
    raw = json.loads(gzip.decompress((directory / arm / "rig/raw.json.gz").read_bytes()))
    udp = {}
    for index in range(4):
        value = raw[f"udp-{index}.json"]
        bad = [r for r in value["samples"] if r["status"] != "completed"]
        udp[str(index)] = {"counters": value["counters"], "non_completed_samples": bad}
    result_data["arms"][arm] = {"udp": udp,
        "udp_timeouts": sum(v["counters"]["timed_out"] for v in udp.values())}
fault = read_json(directory / "fault/rig/l2-result.json")
start, phases = fault["start_at"], case["config"]["phases_sec"]
expected = {"measurement_start_ns": round(start*1e9), "start_ns": round((start+phases["baseline"])*1e9),
    "end_ns": round((start+phases["baseline"]+phases["loaded"]-.5-.25)*1e9),
    "recovery_start_ns": round((start+phases["baseline"]+phases["loaded"])*1e9),
    "measurement_end_ns": round((start+sum(phases.values()))*1e9)}
assert expected == ctrl["planned"]
result_data["actual_load_binding_matches"] = True
result_data["interval_message_caveat"] = "evidence.py emits the generic missing-interval message when earlier control_errors are nonempty; the retained start_at and planned times do match."
write_json(P / "formal-recomputed.json", result_data)
print(json.dumps({"functional": status, "validity": result["validity"],
    "max_lateness_ms": {p: v/1e6 for p, v in result_data["max_lateness_by_phase_ns"].items()},
    "failed_sends": len(failed), "UDP_timeouts": result_data["arms"]["fault"]["udp_timeouts"],
    "transparent_late_ms": result_data["transparent_late_by_ns"]/1e6}, indent=2))
