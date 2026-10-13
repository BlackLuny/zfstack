"""Validate measurement admission without root or a TUN device."""
import argparse
import sys
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


if __name__ == "__main__":
    unittest.main()
