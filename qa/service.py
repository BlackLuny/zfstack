"""Optional frozen, observed-request service contracts; no product defaults."""
from __future__ import annotations

import math

from .common import EvidenceError, digest

FIELDS = {"schema_version", "peer_ids", "phases", "min_samples_per_peer_phase", "max_latency_ms",
          "deadline_ms", "max_deadline_miss_rate"}
STATUSES = ("completed", "failed", "timed_out", "unfinished", "missed")
COUNTERS = {"scheduled", "started", *STATUSES}
SCOPE = ("Observed arrival-to-completion latency and deadline-miss proportions for each frozen peer/phase. "
         "No population/tail probability guarantee; zero observed misses does not imply zero failure probability.")


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_contract(contract):
    """Reject malformed or ambiguous policy; callers freeze this object in case identity."""
    if not isinstance(contract, dict) or set(contract) != FIELDS or type(contract.get("schema_version")) is not int or contract["schema_version"] != 1:
        raise EvidenceError("service contract requires exactly the versioned frozen fields")
    for key in ("peer_ids", "phases"):
        values = contract[key]
        if (not isinstance(values, list) or not 1 <= len(values) <= 256
                or any(not isinstance(value, str) or not value or len(value) > 128 for value in values)
                or len(set(values)) != len(values)):
            raise EvidenceError("service contract requires nonempty unique " + key)
    if type(contract["min_samples_per_peer_phase"]) is not int or not 1 <= contract["min_samples_per_peer_phase"] <= 1_000_000:
        raise EvidenceError("service minimum sample count must be a bounded positive integer")
    for key in ("max_latency_ms", "deadline_ms"):
        if not _number(contract[key]) or contract[key] <= 0:
            raise EvidenceError("service time bounds must be finite and positive")
    if not _number(contract["max_deadline_miss_rate"]) or contract["max_deadline_miss_rate"] > 1:
        raise EvidenceError("service deadline miss rate must be between zero and one")
    return contract


def evaluate_service(contract, peers):
    """Evaluate raw samples without filling censored latencies or deleting failures.

    Malformed policy raises EvidenceError. Missing/malformed evidence is NOT_RUN;
    an independently observed bound violation remains FAIL alongside missing proof.
    Counters are recomputed over the peer's full sample list before phase selection.
    """
    validate_contract(contract)
    assertions, observations = [], {"contract_hash": digest(contract), "scope": SCOPE, "peers": {}}
    def assertion(ident, status, detail):
        assertions.append({"id": ident, "status": status, "kind": "service", "detail": detail})
    peers = peers if isinstance(peers, dict) else {}
    for peer_id in contract["peer_ids"]:
        peer = peers.get(peer_id)
        prefix = "service." + peer_id
        if not isinstance(peer, dict) or not isinstance(peer.get("samples"), list) or not peer["samples"]:
            assertion(prefix + ".evidence", "NOT_RUN", "missing complete raw request samples")
            continue
        samples = peer["samples"]
        if len(samples) > 1_000_000:
            assertion(prefix + ".evidence", "NOT_RUN", "raw sample count exceeds analysis budget")
            continue
        counts = dict.fromkeys(COUNTERS, 0)
        malformed, seen, selected = [], set(), {phase: [] for phase in contract["phases"]}
        for index, row in enumerate(samples):
            if not isinstance(row, dict):
                malformed.append(f"sample {index} is not an object")
                continue
            phase, seq, state = row.get("phase"), row.get("seq"), row.get("status")
            counts["scheduled"] += 1
            if row.get("started_at") is not None:
                counts["started"] += 1
            if state in STATUSES:
                counts[state] += 1
            else:
                malformed.append(f"sample {index} has unknown status")
            identity_ok = ({"phase", "seq", "status", "scheduled_at", "started_at"}.issubset(row)
                           and isinstance(phase, str) and bool(phase) and type(seq) is int and seq >= 0 and seq not in seen)
            if type(seq) is int:
                seen.add(seq)
            scheduled, started, latency = row.get("scheduled_at"), row.get("started_at"), row.get("latency_ms")
            timing_ok = _number(scheduled) and (started is None or (_number(started) and started >= scheduled))
            if state == "completed":
                timing_ok = timing_ok and started is not None and _number(latency)
            elif latency is not None:
                timing_ok = timing_ok and _number(latency)
            if state == "missed" and started is not None:
                timing_ok = False
            if not identity_ok or not timing_ok:
                malformed.append(f"sample {index} has malformed/duplicate request identity or timing")
            if isinstance(phase, str) and phase in selected and state in STATUSES:
                # Retain independently readable failures even alongside a
                # malformed companion record. Never turn a duplicate into more evidence.
                selected[phase].append((row, identity_ok and timing_ok))
        declared = peer.get("counters")
        if (not isinstance(declared, dict) or set(declared) != COUNTERS
                or any(type(value) is not int or value < 0 for value in declared.values()) or declared != counts):
            malformed.append("declared counters do not match the full raw sample population")
        assertion(prefix + ".completeness", "NOT_RUN" if malformed else "PASS", "; ".join(malformed) or "full sample counters match")
        observed_phases = {}
        observations["peers"][peer_id] = {"counters": counts, "phases": observed_phases}
        for phase, records in selected.items():
            label = prefix + "." + phase
            admitted = [row for row, valid in records if valid]
            missing = sum(row["status"] == "missed" for row in admitted)
            business = [row for row in admitted if row["status"] != "missed"]
            completed = [row for row in business if row["status"] == "completed"]
            latencies = [(row["started_at"] - row["scheduled_at"]) * 1000 + row["latency_ms"] for row in completed]
            misses = sum(row["status"] != "completed" for row in business) + sum(value > contract["deadline_ms"] for value in latencies)
            rate = misses / len(business) if business else None
            observed_phases[phase] = {"observed_business_requests": len(business), "completed": len(completed),
                "generator_missed": missing, "deadline_misses": misses, "observed_deadline_miss_rate": rate,
                "max_observed_arrival_latency_ms": max(latencies) if latencies and all(math.isfinite(x) for x in latencies) else None}
            complete = not malformed and len(admitted) == len(records) and not missing and len(business) >= contract["min_samples_per_peer_phase"]
            assertion(label + ".samples", "PASS" if complete else "NOT_RUN", f"{len(business)} observed business requests; {missing} generator misses; minimum {contract['min_samples_per_peer_phase']}")
            exceeded = [value for value in latencies if not math.isfinite(value) or value > contract["max_latency_ms"]]
            assertion(label + ".latency", "FAIL" if exceeded else "PASS" if complete and completed else "NOT_RUN",
                      f"{len(exceeded)} observed completed requests exceed {contract['max_latency_ms']} ms; censored requests have no imputed latency")
            assertion(label + ".deadline", "FAIL" if rate is not None and rate > contract["max_deadline_miss_rate"] else "PASS" if complete else "NOT_RUN",
                      f"observed misses/population {misses}/{len(business)}; maximum proportion {contract['max_deadline_miss_rate']}")
    statuses = [entry["status"] for entry in assertions]
    status = "FAIL" if "FAIL" in statuses else "NOT_RUN" if "NOT_RUN" in statuses or not statuses else "PASS"
    return {"status": status, "assertions": assertions, "observations": observations}
