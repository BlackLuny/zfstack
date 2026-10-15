"""Raw relay identity and paired queue-control qualification, offline fixtures."""
import copy
import gzip
import tempfile
import unittest
from pathlib import Path

from qa.common import EvidenceError, canonical, digest, file_hash, read_json, ROOT, write_json
from qa.integration import _l2_module, l2_environment, l2_evidence, queue_negative_evidence, validate_forwarder, worker_identity
from qa.tests.test_integration import worker_manifest_fixture


class RelayQueueIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = copy.deepcopy(next(case for case in read_json(ROOT / "qa/policy/cases.json")["cases"] if case["case_id"] == "C16-l2"))
        self.case["platform"] = "linux-x86_64"
        self.helpers = _l2_module("test_path_queue")

    def save(self, path, result, raw):
        write_json(path / "result.json", result)
        (path / "raw.json.gz").write_bytes(gzip.compress(canonical(raw)))

    def test_relay_admission_checks_executed_forwarder_and_frozen_build(self):
        output = self.root / "case"; path = output / "rig"; path.mkdir(parents=True)
        result, raw = self.helpers.relay_fixture(path)
        result["effective_config"].update(mode="load", worker_tuning_fingerprints=["one", "two"])
        self.case["config"] = {k: v for k, v in result["effective_config"].items() if k != "worker_tuning_fingerprints"}
        worker = worker_manifest_fixture(self.case)
        binary = self.root / "forwarder"; binary.write_bytes(b"frozen fake relay")
        graph = "0forwarder-server v1.0|mmsg\n1private_tun v1.0|mmsg"
        worker["forwarder"] = {"binary_path": str(binary), "binary_sha256": file_hash(binary), "size_bytes": binary.stat().st_size,
                               "build_info": copy.deepcopy(worker["build_info"]), "dependency_graph": graph, "dependency_graph_hash": digest(graph)}
        command = copy.deepcopy(worker["commands"][0])
        command["argv"] = ["forwarder-server" if item == "zf-worker" else item for item in command["argv"]]
        worker["commands"].append(command)
        raw["forwarder.sha256"] = file_hash(binary) + " forwarder-server"
        result["forwarder_build_info"] = copy.deepcopy(worker["forwarder"]["build_info"])
        self.save(path, result, raw)
        self.assertEqual(l2_evidence(self.case, output, worker)[0], "PASS")
        self.assertEqual(validate_forwarder(worker, binary=binary), worker["forwarder"])
        bad = copy.deepcopy(worker); bad["forwarder"]["build_info"]["private_tun"]["commit"] = "9" * 40
        self.assertEqual(l2_evidence(self.case, output, bad)[0], "NOT_RUN")
        raw["forwarder.sha256"] = "c" * 64 + " wrong"; self.save(path, result, raw)
        self.assertEqual(l2_evidence(self.case, output, worker)[0], "NOT_RUN")
        binary.write_bytes(b"substituted")
        with self.assertRaises(EvidenceError): validate_forwarder(worker, binary=binary)
        with self.assertRaises(EvidenceError): validate_forwarder({**worker, "forwarder": None})

    def pair(self, fault_latency=600):
        self.case["runner"] = "l2-queue-control"
        self.case["service_contract"] = {"schema_version": 1, "peer_ids": ["client-1", "client-2", "client-3"], "phases": ["loaded"],
                                         "min_samples_per_peer_phase": 3, "max_latency_ms": 250, "deadline_ms": 250,
                                         "max_deadline_miss_rate": 0}
        entries = []
        for arm in ("baseline", "fault"):
            output = self.root / arm; path = output / "rig"; path.mkdir(parents=True)
            result, raw = self.helpers.fixture(path)
            result.update(start_at=1)
            result["effective_config"].update(mode="load", worker_tuning_fingerprints=["one", "two"])
            for peer in range(1, 4):
                samples = []
                for phase, offset in (("baseline", 1), ("loaded", 9), ("recovery", 26.5)):
                    for index in range(3):
                        at = offset + index * .1
                        latency = fault_latency if arm == "fault" and phase == "loaded" else 10
                        samples.append({"seq": len(samples), "phase": phase, "status": "completed", "scheduled_at": at,
                                        "started_at": at, "latency_ms": latency, "ended_at": at + latency / 1000})
                raw[f"client-{peer}.json"] = {"samples": samples, "errors": [], "counters": _l2_module("load_contract").counters(samples)}
            if arm == "fault":
                result["effective_config"]["queue_control"] = copy.deepcopy(self.helpers.QUEUE)
                result["queue_control"] = self.helpers.queue_report()
                raw["queue-link-samples.json"] = copy.deepcopy(result["queue_control"]["link_samples"])
                self.case["config"] = {k: v for k, v in result["effective_config"].items() if k != "worker_tuning_fingerprints"}
            self.save(path, result, raw)
            entries.append((output, result, raw))
        return entries, worker_manifest_fixture(self.case)

    def test_queue_control_requires_healthy_baseline_slo_failure_and_recovery(self):
        (base, fault), worker = self.pair()
        status, _, observations = queue_negative_evidence(self.case, base[0], fault[0], worker)
        self.assertEqual(status, "PASS", observations)
        self.assertEqual(observations["fault"]["raw_functional_status"], "PASS")
        self.assertEqual(observations["fault"]["service_result"]["status"], "FAIL")
        self.assertEqual(observations["recovery"]["status"], "PASS")
        self.assertIn("not historical", observations["scope"])

    def test_queue_control_rejects_missing_drop_or_untriggered_controls(self):
        (base, fault), worker = self.pair(fault_latency=10)
        self.assertEqual(queue_negative_evidence(self.case, base[0], fault[0], worker)[0], "FAIL")
        fault[1]["queue_control"]["samples"][2]["qdiscs"][0]["drops"] = 1
        self.save(fault[0] / "rig", fault[1], fault[2])
        self.assertEqual(queue_negative_evidence(self.case, base[0], fault[0], worker)[0], "NOT_RUN")
        fault[1].pop("queue_control")
        self.save(fault[0] / "rig", fault[1], fault[2])
        self.assertEqual(queue_negative_evidence(self.case, base[0], fault[0], worker)[0], "NOT_RUN")

    def test_payload_failure_cannot_impersonate_a_successful_queue_control(self):
        (base, fault), worker = self.pair()
        fault[2]["client-2.json"]["errors"].append("corrupt payload")
        self.save(fault[0] / "rig", fault[1], fault[2])
        self.assertEqual(queue_negative_evidence(self.case, base[0], fault[0], worker)[0], "FAIL")

    def test_queue_expected_failure_cannot_hide_an_incomplete_peer(self):
        (base, fault), worker = self.pair()
        peer = fault[2]["client-1.json"]
        peer["samples"] = [sample for sample in peer["samples"] if sample["seq"] != 3]
        peer["counters"] = _l2_module("load_contract").counters(peer["samples"])
        self.save(fault[0] / "rig", fault[1], fault[2])
        status, _, observed = queue_negative_evidence(self.case, base[0], fault[0], worker)
        self.assertEqual(observed["fault"]["service_result"]["status"], "FAIL")
        self.assertTrue(any(row["status"] == "NOT_RUN" for row in observed["fault"]["service_result"]["assertions"]))
        self.assertEqual(status, "NOT_RUN")

    def test_nonfunctional_perf_noise_does_not_erase_slo_failure(self):
        (base, fault), worker = self.pair()
        fault[1]["performance_contaminated"] = True
        self.save(fault[0] / "rig", fault[1], fault[2])
        status, _, _, validity, _ = l2_evidence(self.case, fault[0], worker)
        self.assertEqual((status, validity), ("FAIL", "INVALID"))

    def test_queue_pair_preserves_recovery_failures_and_rejects_different_runtime(self):
        (base, fault), worker = self.pair()
        base[1]["effective_config"]["worker_tuning_fingerprints"] = ["changed", "two"]
        self.save(base[0] / "rig", base[1], base[2])
        self.assertEqual(queue_negative_evidence(self.case, base[0], fault[0], worker)[0], "NOT_RUN")
        for sample in fault[2]["client-1.json"]["samples"]:
            if sample["phase"] == "recovery":
                sample.update(latency_ms=600, ended_at=sample["started_at"] + .6)
        self.save(fault[0] / "rig", fault[1], fault[2])
        self.assertEqual(queue_negative_evidence(self.case, base[0], fault[0], worker)[0], "FAIL")

    def test_queue_pair_missing_execution_cannot_pass(self):
        (base, fault), worker = self.pair()
        self.assertEqual(queue_negative_evidence(self.case, self.root / "absent-base", self.root / "absent-fault", worker)[0], "NOT_RUN")
        base[2]["client-1.json"]["errors"].append("corrupt payload")
        self.save(base[0] / "rig", base[1], base[2])
        self.assertEqual(queue_negative_evidence(self.case, base[0], self.root / "absent-fault", worker)[0], "FAIL")

    def test_queue_and_relay_config_reach_the_existing_load_cli(self):
        original = copy.deepcopy(self.case["config"])
        self.case["config"].update(outbound="relay", probe_port=19002, queue_control=copy.deepcopy(self.helpers.QUEUE))
        environment = l2_environment(self.case["config"])
        self.assertEqual(environment["L2_OUTBOUND"], "relay")
        self.assertEqual(environment["L2_QUEUE_DELAY_MS"], "500.0")
        self.assertEqual(l2_environment(original)["L2_QUEUE_DELAY_MS"], "")
        self.assertEqual(l2_environment(original)["L2_QUEUE_LIMIT_PACKETS"], "")


if __name__ == "__main__":
    unittest.main()
