import unittest
from analyze import quantile, request_stats
from run import plan


class EvidenceTests(unittest.TestCase):
    def test_missing_response_cannot_improve_unconditional_mean_or_tail(self):
        rows = [dict(outcome="completed", planned_ns=0, completed_ns=10_000_000),
                dict(outcome="timed_out", planned_ns=40_000_000, completed_ns=None)]
        s = request_stats(rows)
        self.assertEqual((s["planned"], s["completed"], s["missing"]), (2, 1, 1))
        self.assertIsNone(s["mean_ms"])
        self.assertIsNone(s["p99_ms"])
        self.assertEqual(s["observed_mean_ms"], 10)

    def test_nearest_rank_and_small_samples(self):
        self.assertEqual(quantile(list(range(1, 101)), .99), 99)
        self.assertIsNone(request_stats([])["mean_ms"])
        self.assertIsNone(request_stats([dict(outcome="completed", planned_ns=0, completed_ns=1)])["p99_ms"])

    def test_frozen_balanced_plan_has_every_attempt(self):
        rows = plan()
        self.assertEqual(rows, plan())
        self.assertEqual(len(rows), 56)
        for case in {r["case"] for r in rows}:
            for block in ("B", "C", "D"):
                group = [r["arm"] for r in rows if r["case"] == case and r["block"] == block]
                self.assertEqual(group, ["A", block, block, "A"])


if __name__ == "__main__":
    unittest.main()
