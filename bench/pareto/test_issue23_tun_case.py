"""Validate measurement admission without root or a TUN device."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import issue23_tun_case as case


class MeasurementAdmission(unittest.TestCase):
    def arguments(self, *extra):
        argv = ["case", "--binary", "/tmp/proxy", "--loader", "/tmp/load.py",
                "--outdir", "/tmp/result", "--label", "test", "--variant", "B",
                "--source-sha", "a" * 40, "--pair", "0", "--pair-order", "AB",
                "--pair-position", "0", "--build-flags", '{"release":true}', *extra]
        with patch.object(sys, "argv", argv), patch.object(case.os, "geteuid", return_value=0):
            return case.arguments()

    def test_sampler_cannot_be_admitted_as_performance(self):
        with self.assertRaisesRegex(case.CaseError, "sampler requires --diagnostic"):
            self.arguments("--env", "ZFBENCH_TX_CACHE_SAMPLE_MS=1000")

    def test_peer_workload_and_stack_mapping_must_match(self):
        with self.assertRaisesRegex(case.CaseError, "requires multi-peer workload"):
            self.arguments("--env", "ZFBENCH_TUN_PEERS=4")
        with self.assertRaisesRegex(case.CaseError, "multi-peer requires diagnostic"):
            self.arguments("--peer-mode", "fanout")
        with self.assertRaisesRegex(case.CaseError, "requires explicit"):
            self.arguments("--peer-mode", "noisy", "--diagnostic", "--mixed-rr-interval-ms", "1")
        args = self.arguments("--peer-mode", "noisy", "--diagnostic", "--mixed-rr-interval-ms", "1",
                              "--env", "ZFBENCH_TUN_PEERS=4", "--env", "ZFBENCH_TX_CACHE_SAMPLE_MS=1000")
        self.assertEqual(args.peer_mode, "noisy")

    def test_sampler_is_allowed_in_explicit_diagnostic(self):
        args = self.arguments("--diagnostic", "--env", "ZFBENCH_TX_CACHE_SAMPLE_MS=1000")
        self.assertTrue(args.diagnostic)
        self.assertEqual(args.experiment["ZFBENCH_TX_CACHE_SAMPLE_MS"], "1000")

    def test_capture_cannot_be_admitted_as_performance(self):
        with self.assertRaisesRegex(case.CaseError, "tcpdump is allowed only"):
            self.arguments("--tcpdump")

    def test_explicit_cpu_sets_and_original_defaults(self):
        args = self.arguments()
        self.assertEqual((args.proxy_cpus, args.loader_cpus), ({0, 1}, {2, 3, 4, 5}))
        args = self.arguments("--proxy-cpus", "0,1", "--loader-cpus", "2,3,4")
        self.assertEqual((args.proxy_cpus, args.loader_cpus), ({0, 1}, {2, 3, 4}))

    def test_cpu_overlap_is_rejected(self):
        with self.assertRaisesRegex(case.CaseError, "disjoint"):
            self.arguments("--loader-cpus", "1,2")

    def test_malformed_cpu_sets_are_rejected(self):
        for text in ["", "0,0", "-1", "0,", "1.5", "0-3"]:
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                case.parse_cpus(text)


class PeerEvidenceAdmission(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runner = case.Runner.__new__(case.Runner)
        self.runner.out = Path(self.directory.name)
        self.runner.result = {}
        self.runner.args = argparse.Namespace(peer_mode="fanout", direction="down", idle_values=[1],
                                               flows=4, bulk_bytes_per_flow=7)
        self.rows = [{"type": "bulk", "per_flow": [
            {"source": f"172.19.1.{peer}", "verified_bytes": 7} for peer in range(1, 5)],
            "mixed_peers": [{"source": f"172.19.1.{peer}", "fully_overlapping_samples": 1}
                            for peer in range(2, 5)]} for _ in range(2)]
        self.sample = {"peer_limit_bytes": 100, "peers": [
            {"peer_id": peer, "physical_bytes": 10, "egress_packets": int(peer != 0)}
            for peer in range(5)]}

    def validate(self, sample=None):
        (self.runner.out / "proxy.stderr").write_text("ZFBENCH_PEERS " + json.dumps(sample or self.sample) + "\n")
        self.runner.validate_peers(self.rows)

    def test_accepts_distinct_leases_egress_and_overlapping_progress(self):
        self.validate()
        self.assertEqual(self.runner.result["peer_validation"]["traffic_peers"], [1, 2, 3, 4])

    def test_source_labels_without_real_peer_egress_are_rejected(self):
        sample = copy.deepcopy(self.sample)
        sample["peers"][4]["egress_packets"] = 0
        with self.assertRaisesRegex(case.CaseError, "four real traffic peers"):
            self.validate(sample)

    def test_over_budget_or_reader_peer_fallback_is_rejected(self):
        for index, field, value in [(1, "physical_bytes", 101), (0, "egress_packets", 1)]:
            sample = copy.deepcopy(self.sample)
            sample["peers"][index][field] = value
            with self.subTest(field=field), self.assertRaises(case.CaseError):
                self.validate(sample)

    def test_late_only_light_requests_are_not_progress_under_pressure(self):
        self.rows[0]["mixed_peers"][0]["fully_overlapping_samples"] = 0
        with self.assertRaisesRegex(case.CaseError, "no completed progress"):
            self.validate()


if __name__ == "__main__":
    unittest.main()
