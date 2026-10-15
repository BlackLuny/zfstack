"""Frozen observed-service contracts; synthetic records do not set product SLOs."""
import copy
import unittest

from qa.common import EvidenceError, digest
from qa.service import COUNTERS, STATUSES, evaluate_service, validate_contract


def contract_fixture(**changes):
    return {"schema_version": 1, "peer_ids": ["light-1", "light-2"], "phases": ["baseline", "loaded"],
            "min_samples_per_peer_phase": 2, "max_latency_ms": 100, "deadline_ms": 50,
            "max_deadline_miss_rate": .25, **changes}


def peer_fixture(samples):
    counters = dict.fromkeys(COUNTERS, 0)
    for row in samples:
        counters["scheduled"] += 1
        counters["started"] += row.get("started_at") is not None
        counters[row["status"]] += 1
    return {"samples": samples, "counters": counters}


def peers_fixture():
    samples = [{"phase": phase, "seq": index * 2 + position, "status": "completed",
                "scheduled_at": index * 10 + position, "started_at": index * 10 + position,
                "latency_ms": 10} for index, phase in enumerate(("baseline", "loaded")) for position in range(2)]
    return {peer: peer_fixture(copy.deepcopy(samples)) for peer in ("light-1", "light-2")}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.contract, self.peers = contract_fixture(), peers_fixture()

    def evaluate(self):
        return evaluate_service(self.contract, self.peers)

    def test_complete_observed_population_passes_without_probability_claim(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["observations"]["contract_hash"], digest(self.contract))
        self.assertIn("zero observed misses does not imply zero", result["observations"]["scope"])
        self.assertTrue(all(assertion["kind"] == "service" for assertion in result["assertions"]))

    def test_contract_is_strict_typed_and_has_no_implicit_thresholds(self):
        for key in self.contract:
            value = copy.deepcopy(self.contract); del value[key]
            with self.subTest(missing=key), self.assertRaises(EvidenceError): validate_contract(value)
        for change in ({"extra": 1}, {"schema_version": True}, {"peer_ids": []}, {"peer_ids": ["a", "a"]},
                       {"phases": [True]}, {"phases": ["loaded", "loaded"]}, {"max_latency_ms": float("nan")},
                       {"deadline_ms": float("inf")}, {"deadline_ms": 0}, {"max_deadline_miss_rate": -1},
                       {"max_deadline_miss_rate": 1.1}, {"max_deadline_miss_rate": True},
                       {"min_samples_per_peer_phase": True}, {"min_samples_per_peer_phase": 0}):
            with self.subTest(change=change), self.assertRaises(EvidenceError):
                validate_contract({**self.contract, **change})

    def test_missing_peer_phase_samples_and_counters_never_pass(self):
        mutations = (lambda peers: peers.pop("light-1"), lambda peers: peers["light-1"].update(samples=[]),
                     lambda peers: peers["light-1"].pop("counters"),
                     lambda peers: peers["light-1"]["samples"].pop(),
                     lambda peers: peers["light-1"]["counters"].update(scheduled=999))
        for mutate in mutations:
            self.peers = peers_fixture(); mutate(self.peers)
            with self.subTest(mutate=mutate): self.assertEqual(self.evaluate()["status"], "NOT_RUN")
        self.assertEqual(evaluate_service(self.contract, None)["status"], "NOT_RUN")

    def test_each_peer_phase_requires_its_own_minimum(self):
        samples = self.peers["light-1"]["samples"]
        samples[0]["phase"] = "loaded"
        self.assertEqual(self.evaluate()["status"], "NOT_RUN")
        self.contract["peer_ids"] = ["light-2"]
        self.assertEqual(self.evaluate()["status"], "PASS")

    def test_arrival_to_completion_includes_generator_and_queue_wait(self):
        sample = self.peers["light-1"]["samples"][0]
        sample["started_at"] += .2  # Small successful request latency cannot hide waiting.
        self.assertEqual(sample["latency_ms"], 10)
        result = self.evaluate()
        self.assertEqual(result["status"], "FAIL")
        self.assertGreater(result["observations"]["peers"]["light-1"]["phases"]["baseline"]["max_observed_arrival_latency_ms"], 200)

    def test_failed_timed_out_and_unfinished_requests_stay_in_denominator(self):
        self.contract.update(peer_ids=["light-1"], phases=["loaded"], max_deadline_miss_rate=.75)
        samples = [{"phase": "loaded", "seq": index, "status": status, "scheduled_at": index,
                    "started_at": index if status != "unfinished" else None,
                    **({"latency_ms": 10} if status == "completed" else {})}
                   for index, status in enumerate(("completed", "failed", "timed_out", "unfinished"))]
        self.peers = {"light-1": peer_fixture(samples)}
        result = self.evaluate()
        self.assertEqual(result["status"], "PASS")
        phase = result["observations"]["peers"]["light-1"]["phases"]["loaded"]
        self.assertEqual((phase["deadline_misses"], phase["observed_business_requests"], phase["observed_deadline_miss_rate"]), (3, 4, .75))
        self.assertEqual(phase["max_observed_arrival_latency_ms"], 10)  # No timeout=deadline imputation.
        self.contract["max_deadline_miss_rate"] = .74
        self.assertEqual(self.evaluate()["status"], "FAIL")

    def test_generator_misses_are_not_business_drops_or_silent_deletions(self):
        peer = self.peers["light-1"]
        peer["samples"][0].update(status="missed", started_at=None)
        peer["samples"][0].pop("latency_ms")
        self.peers["light-1"] = peer_fixture(peer["samples"])
        result = self.evaluate()
        self.assertEqual(result["status"], "NOT_RUN")
        phase = result["observations"]["peers"]["light-1"]["phases"]["baseline"]
        self.assertEqual((phase["generator_missed"], phase["observed_business_requests"], phase["deadline_misses"]), (1, 1, 0))

    def test_observed_failure_survives_missing_peer_and_malformed_companion(self):
        self.peers.pop("light-2")
        self.peers["light-1"]["samples"][0]["latency_ms"] = 101
        self.peers["light-1"]["samples"].append(None)
        self.assertEqual(self.evaluate()["status"], "FAIL")

    def test_malformed_duplicate_or_missing_measurements_do_not_pass(self):
        changes = ({"seq": 1}, {"seq": True}, {"phase": []}, {"status": "unknown"},
                   {"latency_ms": None}, {"latency_ms": float("nan")}, {"latency_ms": -1},
                   {"started_at": None}, {"started_at": -1}, {"scheduled_at": True})
        for change in changes:
            self.peers = peers_fixture(); self.peers["light-1"]["samples"][0].update(change)
            with self.subTest(change=change): self.assertEqual(self.evaluate()["status"], "NOT_RUN")
        self.peers = peers_fixture(); self.peers["light-1"]["counters"]["missed"] = False
        self.assertEqual(self.evaluate()["status"], "NOT_RUN")

    def test_no_successful_latency_sample_cannot_establish_a_latency_slo(self):
        self.contract.update(peer_ids=["light-1"], phases=["loaded"], max_deadline_miss_rate=1)
        samples = [{"phase": "loaded", "seq": index, "status": "timed_out", "scheduled_at": index,
                    "started_at": index, "latency_ms": None} for index in range(2)]
        self.peers = {"light-1": peer_fixture(samples)}
        self.assertEqual(self.evaluate()["status"], "NOT_RUN")

    def test_bounds_are_inclusive_and_extra_observations_do_not_change_contract(self):
        self.contract.update(max_latency_ms=50, max_deadline_miss_rate=0)
        for peer in self.peers.values():
            for sample in peer["samples"]:
                sample.update(latency_ms=50, ended_at=sample["started_at"] + .05)
        self.assertEqual(self.evaluate()["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
