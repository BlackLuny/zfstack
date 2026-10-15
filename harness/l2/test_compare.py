"""Protect the comparison from censored latency and mismatched AB workloads."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from compare import compare
from test_netem import clean_report


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for profile in ("small", "normal", "delayed"):
            for source in ("base", "candidate"):
                for repeat in "ab":
                    p = self.root / f"{profile}-{source}-{repeat}"
                    p.mkdir()
                    result = {"ok": True, "errors": [], "request_timeout_sec": 3,
                              "memory_mb": 64 if profile == "normal" else 8,
                              "uplink_delay_ms": 50 if profile == "delayed" else 0,
                              "phases_sec": {"baseline": 8, "loaded": 16, "recovery": 8},
                              "heavy_mbps": 1000,
                              "worker": {"loaded": {"worker_cpu_sec": 10,
                                                    "sample_window_sec": 10,
                                                    "rss_peak_kib": 20 * 1024}}}
                    result["qdisc"] = clean_report(result["uplink_delay_ms"])
                    raw = {"client-0.json": {"flows": [{"bytes": 1000, "errors": []}] * 16},
                           "worker.sha256": ("a" if source == "base" else "b") * 64 + " worker\n",
                           "light-capture.log": "0 packets dropped by kernel\n",
                           "target-capture.log": "0 packets dropped by kernel\n"}
                    for peer in range(1, 4):
                        raw[f"client-{peer}.json"] = {
                            "errors": [], "samples": [{"phase": phase, "latency_ms": 1}
                                                     for phase in ("baseline", "loaded", "recovery")]}
                    analysis = {"loaded_n": 100, "loaded_max_ms": 1, "loaded_p99_ms": 1,
                                "requests_retransmitted": 0}
                    (p / "result.json").write_text(json.dumps(result))
                    (p / "raw.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))
                    (p / "analysis.json").write_text(json.dumps(analysis))

    def edit(self, case, file, edit):
        p = self.root / case / file
        compressed = file.endswith(".gz")
        value = json.loads(gzip.decompress(p.read_bytes()) if compressed else p.read_bytes())
        edit(value)
        data = json.dumps(value).encode()
        p.write_bytes(gzip.compress(data) if compressed else data)

    def test_success_and_censored_baseline(self):
        self.assertTrue(compare(self.root)["ok"])
        self.edit("small-base-a", "result.json",
                  lambda r: r.update(ok=False, errors=["light request timed out"]))
        self.assertTrue(compare(self.root)["ok"], "baseline failure remains visible without rejecting the experiment")
        self.assertFalse(compare(self.root)["profiles"]["small"]["base"][0]["ok"])

    def test_fast_completed_samples_cannot_hide_a_failed_candidate_request(self):
        self.edit("small-candidate-a", "raw.json.gz",
                  lambda r: r["client-1.json"]["errors"].append("request timed out after 3s"))
        self.assertFalse(compare(self.root)["ok"])

    def test_longer_deadline_and_missing_recovery_do_not_pass(self):
        self.edit("small-candidate-a", "result.json", lambda r: r.update(request_timeout_sec=30))
        self.edit("normal-candidate-b", "raw.json.gz",
                  lambda r: r["client-2.json"]["samples"].pop())
        report = compare(self.root)
        self.assertFalse(report["ok"])
        self.assertIn("missing recovery", str(report["errors"]))
        self.assertIn("original 3s", str(report["errors"]))

    def test_capture_loss_and_heavy_corruption_invalidate_baseline_evidence(self):
        self.edit("small-base-a", "raw.json.gz",
                  lambda r: r.update({"light-capture.log": "1 packets dropped by kernel"}))
        self.edit("normal-base-b", "raw.json.gz",
                  lambda r: r["client-0.json"]["flows"][0]["errors"].append("corrupt payload"))
        self.assertFalse(compare(self.root)["ok"])

    def test_changed_workload_binary_and_retransmission_are_rejected(self):
        self.edit("small-candidate-a", "result.json", lambda r: r.update(memory_mb=64))
        self.edit("normal-candidate-b", "raw.json.gz",
                  lambda r: r.update({"worker.sha256": "c" * 64 + " worker"}))
        self.edit("delayed-candidate-a", "analysis.json", lambda r: r.update(requests_retransmitted=1))
        report = compare(self.root)
        self.assertFalse(report["ok"])
        for error in ("workload dimensions", "immutable", "retransmitted"):
            self.assertIn(error, str(report["errors"]))

    def test_performance_limits_use_cpu_per_delivered_gib(self):
        for repeat in "ab":
            self.edit(f"small-candidate-{repeat}", "result.json", lambda r: r.update(heavy_mbps=800))
        self.assertTrue(compare(self.root)["ok"], "no hidden default performance threshold")
        self.assertFalse(compare(self.root, throughput_drop=10)["ok"])
        self.assertFalse(compare(self.root, cpu_rise=20)["ok"], "unchanged CPU with less goodput costs more per GiB")

    def test_qdisc_loss_in_either_arm_invalidates_comparison(self):
        for source in ("base", "candidate"):
            self.edit(f"delayed-{source}-a", "result.json",
                      lambda r: r["qdisc"]["clients"]["0"]["after"].update(drops=570157))
        report = compare(self.root)
        self.assertFalse(report["ok"])
        for source in ("base", "candidate"):
            self.assertIn(f"delayed-{source}-a: invalid comparison evidence", str(report["errors"]))

    def test_missing_delayed_evidence_and_mismatched_limits_are_rejected(self):
        self.edit("delayed-base-a", "result.json", lambda r: r.pop("qdisc"))
        self.edit("delayed-candidate-a", "result.json", lambda r: r.update(qdisc=clean_report(50, 1000)))
        report = compare(self.root)
        self.assertFalse(report["ok"])
        self.assertIn("missing qdisc evidence", str(report["errors"]))
        self.assertIn("workload dimensions", str(report["errors"]))

    def test_contamination_in_either_arm_is_not_performance_evidence(self):
        for source in ("base", "candidate"):
            self.edit(f"normal-{source}-a", "result.json", lambda r: r.update(performance_contaminated=True))
        report = compare(self.root)
        self.assertFalse(report["ok"])
        self.assertIn("contaminated", str(report["errors"]))

    def test_full_configuration_changes_include_light_churn_udp_and_unknown_new_keys(self):
        for field, value in (("light_conns", 2), ("churn_requests", 4), ("udp_enabled", True),
                             ("config", {"future_setting": "changed"})):
            self.edit("small-candidate-a", "result.json", lambda r, k=field, v=value: r.update({k: v}))
            self.assertIn("workload dimensions", str(compare(self.root)["errors"]))
        self.edit("normal-base-a", "result.json", lambda r: r.update(effective_config={"future_setting": 1}))
        self.edit("normal-candidate-a", "result.json", lambda r: r.update(effective_config={"future_setting": 2}))
        self.assertIn("workload dimensions", str(compare(self.root)["errors"]))

    def test_both_arms_empty_zero_cpu_window_and_nan_are_rejected(self):
        with tempfile.TemporaryDirectory() as empty:
            self.assertFalse(compare(Path(empty))["ok"])
        self.edit("small-candidate-a", "result.json", lambda r: r["worker"]["loaded"].update(sample_window_sec=0))
        self.edit("normal-base-a", "result.json", lambda r: r.update(heavy_mbps=float("nan")))
        report = compare(self.root)
        self.assertFalse(report["ok"])
        self.assertIn("nonfinite", str(report["errors"]))

    def test_old_cpu_approximation_cannot_pass_an_explicit_limit(self):
        self.assertFalse(compare(self.root, throughput_drop=100)["ok"])
        self.assertEqual(compare(self.root)["performance"], "INCONCLUSIVE")


if __name__ == "__main__":
    unittest.main()
