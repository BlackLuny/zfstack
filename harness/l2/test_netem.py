"""Do not mistake ACK queue overflow or absent counters for a stack regression."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from netem import evidence_errors, summarize


def clean_report(delay=50, limit=100000):
    # tc -j -s qdisc show: counters are cumulative, not measurement deltas.
    root = {"kind": "netem" if delay else "noqueue", "handle": "8001:" if delay else "0:",
            "root": True, "options": {"limit": limit} if delay else {},
            "bytes": 6400, "packets": 100, "drops": 2, "backlog": 0, "qlen": 0}
    report = {"uplink_delay_ms": delay, "limit_packets": limit if delay else None,
              "clients": {str(i): {"before": copy.deepcopy(root), "after": copy.deepcopy(root)}
                          for i in range(4)}}
    for row in report["clients"].values():
        row["after"].update(bytes=70400, packets=1100)
    return report


class NetemTests(unittest.TestCase):
    def test_counter_deltas_and_zero_delay(self):
        for delay in (0, 50):
            report = clean_report(delay)
            self.assertEqual(evidence_errors(report, delay), [])
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp)
                for i, row in report["clients"].items():
                    for phase, key in (("pre", "before"), ("post", "after")):
                        (path / f"qdisc-{phase}-{i}.json").write_text(json.dumps([row[key]]))
                result = summarize(path, delay, 100000)
                self.assertTrue(result["ok"])
                self.assertEqual(result["clients"]["0"]["delta"], {"bytes": 64000, "packets": 1000, "drops": 0})

    def test_queue_overflow_invalidates_delay_only_run(self):
        report = clean_report()
        report["clients"]["0"]["after"]["drops"] += 570155
        self.assertIn("qdisc dropped 570155 packets", str(evidence_errors(report, 50)))

    def test_missing_invalid_reset_or_replaced_counters_never_mean_zero_loss(self):
        mutations = [
            lambda r: r["clients"].pop("3"),
            lambda r: r["clients"]["0"]["after"].pop("drops"),
            lambda r: r["clients"]["0"]["after"].update(drops=0),
            lambda r: r["clients"]["0"]["after"].update(drops="0"),
            lambda r: r["clients"]["0"]["after"].update(handle="8002:"),
            lambda r: r["clients"]["0"]["after"]["options"].update(delay=0.1),
            lambda r: r["clients"]["0"].update(error="tc: Operation not permitted"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                report = clean_report()
                mutate(report)
                self.assertTrue(evidence_errors(report, 50))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "qdisc-pre-0.json").write_text("invalid JSON")
            (path / "qdisc-pre-1.json").write_text("{}")
            self.assertEqual(len(summarize(path, 50, 100000)["errors"]), 4)

    def test_actual_limit_and_enabled_qdisc_must_match(self):
        report = clean_report()
        report["clients"]["0"]["before"]["options"]["limit"] = 1000
        self.assertTrue(evidence_errors(report, 50))
        report = clean_report()
        report["clients"]["0"]["after"]["kind"] = "noqueue"
        self.assertTrue(evidence_errors(report, 50))
        self.assertTrue(evidence_errors(clean_report(), 0))
        self.assertTrue(evidence_errors(None, 50))
        self.assertEqual(evidence_errors(None, 0), [])

    def test_invalid_settings_are_rejected_before_rig_setup(self):
        script = Path(__file__).with_name("netem.py")
        for delay, limit in (("nan", "100000"), ("-1", "100000"), ("50", "0"),
                             ("50", "-1"), ("50", "4294967296"), ("50", "1.5")):
            result = subprocess.run([sys.executable, str(script), "--check", "--delay-ms", delay,
                                     "--limit-packets", limit], capture_output=True)
            self.assertEqual(result.returncode, 2)

    def test_run_rig_validates_limit_before_creating_output(self):
        script = Path(__file__).with_name("run-rig.sh")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            result = subprocess.run(["bash", str(script), "/missing-rig", "/missing-worker", str(output)],
                                    env={**os.environ, "L2_MODE": "load", "L2_NETEM_LIMIT_PACKETS": "0"},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("packet limit must be", result.stderr)
            self.assertFalse(output.exists())

    def test_numeric_zero_disables_netem_and_positive_delay_enables_it(self):
        script = Path(__file__).with_name("netem.py")
        for delay, enabled in (("0", "0"), ("0.0", "0"), ("50", "1")):
            result = subprocess.run([sys.executable, str(script), "--check", "--delay-ms", delay,
                                     "--limit-packets", "100000"], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), enabled)


if __name__ == "__main__":
    unittest.main()
