"""The receiver-order rate comparison alternates arms, counts strictly and decides by its frozen rule."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

DRIVER = Path(__file__).parent / "results/2026-10-17-order-rate/order_rate.py"
spec = importlib.util.spec_from_file_location("order_rate", DRIVER)
rate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rate)


def row(arm, overtaken, unique=rate.SENT, duplicates=0):
    return {"arm": arm, "receiver": {"received": unique + duplicates, "unique": unique, "duplicates": duplicates,
                                     "overtaken": overtaken}}


class OrderRateTests(unittest.TestCase):
    def test_schedule_strictly_alternates_and_is_balanced(self):
        order = rate.schedule()
        self.assertEqual(len(order), 2 * rate.RUNS)
        self.assertEqual([arm for _, arm in order[:4]], ["F", "P", "F", "P"])
        self.assertEqual(order[-1], (rate.RUNS, "P"))

    def test_fisher_and_intervals(self):
        self.assertAlmostEqual(rate.fisher_greater(4, 20, 0, 20), 4845 / 91390)
        self.assertAlmostEqual(rate.fisher_greater(6, 30, 0, 30), 593775 / 50063860)
        self.assertEqual(rate.fisher_greater(0, 30, 0, 30), 1.0)
        low, high = rate.clopper_pearson(0, 30)
        self.assertEqual(low, 0.0)
        self.assertAlmostEqual(high, 0.1157, places=3)
        low, high = rate.clopper_pearson(6, 30)
        self.assertAlmostEqual(low, 0.0771, places=3)
        self.assertAlmostEqual(high, 0.3857, places=3)

    def test_receiver_counts_overtakes_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receiver.json"
            path.write_text(json.dumps({"udp": [[0, 1, 2, True], [2, 1, 2, True], [1, 1, 2, True], [2, 1, 2, True]]}))
            self.assertEqual(rate.receiver_stats(path), {"received": 4, "unique": 3, "duplicates": 1, "overtaken": 1})
            self.assertIsNone(rate.receiver_stats(Path(directory) / "missing.json"))

    def test_decision_rules(self):
        rows = [row("F", 1 if i < 6 else 0) for i in range(30)] + [row("P", 0) for _ in range(30)]
        result = rate.decide(rows)
        self.assertEqual(result["verdict"], "F_HIGHER")
        self.assertEqual(result["arms"]["F"]["overtaking_runs"], 6)
        shared = [row("F", 1 if i < 2 else 0) for i in range(30)] + [row("P", 1 if i < 1 else 0) for i in range(30)]
        self.assertEqual(rate.decide(shared)["verdict"], "NOT_SHOWN_SHARED")
        quiet = [row("F", 1 if i < 1 else 0) for i in range(30)] + [row("P", 0) for _ in range(30)]
        self.assertEqual(rate.decide(quiet)["verdict"], "NOT_SHOWN")
        lossy = [row("F", 0, unique=rate.SENT - 1) for _ in range(6)] + [row("F", 0) for _ in range(24)] + [row("P", 0)] * 30
        result = rate.decide(lossy)
        self.assertEqual((result["verdict"], result["arms"]["F"]["valid"]), ("INCONCLUSIVE", 24))
        broken = [{"arm": "F", "receiver": None}] + [row("F", 1)] * 29 + [row("P", 0)] * 30
        self.assertEqual(rate.decide(broken)["arms"]["F"]["valid"], 29)

    def test_injector_and_control_are_unchanged_since_the_gate(self):
        root = Path(__file__).resolve().parents[2]
        if subprocess.run(["git", "-C", str(root), "cat-file", "-e", rate.GATE_SHA + "^{commit}"], capture_output=True).returncode:
            self.skipTest("gate commit not in this clone")
        changed = subprocess.run(["git", "-C", str(root), "diff", "--name-only", rate.GATE_SHA, "HEAD", "--", *rate.UNCHANGED],
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(changed, "")


if __name__ == "__main__":
    unittest.main()
