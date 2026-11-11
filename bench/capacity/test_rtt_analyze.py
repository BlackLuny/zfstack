"""Observer audits: zero-progress connections must not disappear in aggregates."""
import unittest
from rtt_analyze import progress_stats


class ProgressTests(unittest.TestCase):
    def test_idle_connection_remains_in_fairness_denominator(self):
        rows = [{"elapsed_ms": 0, "bytes": [100, 100]},
                {"elapsed_ms": 250, "bytes": [150, 100]},
                {"elapsed_ms": 500, "bytes": [200, 100]}]
        r = progress_stats(rows, 2)
        self.assertEqual(r["heavy_zero_window_flows"], 1)
        self.assertEqual(r["heavy_jain"], .5)
        self.assertEqual(r["max_observed_no_read_sec"], .5)

    def test_progress_breaks_a_gap_and_wall_time_is_used(self):
        rows = [{"elapsed_ms": 0, "bytes": [0]},
                {"elapsed_ms": 260, "bytes": [0]},
                {"elapsed_ms": 510, "bytes": [1]},
                {"elapsed_ms": 880, "bytes": [1]}]
        self.assertAlmostEqual(progress_stats(rows, 1)["max_observed_no_read_sec"], .37)

    def test_missing_or_nonmonotonic_observation_is_invalid(self):
        for rows, n in [([], 1), ([{"elapsed_ms": 0, "bytes": [0]}], 1),
                        ([{"elapsed_ms": 0, "bytes": [3]}, {"elapsed_ms": 100, "bytes": [2]}], 1),
                        ([{"elapsed_ms": 100, "bytes": [0]}, {"elapsed_ms": 0, "bytes": [1]}], 1),
                        ([{"elapsed_ms": 0, "bytes": [0]}, {"elapsed_ms": 100, "bytes": [1]}], 2)]:
            with self.assertRaises(ValueError):
                progress_stats(rows, n)


if __name__ == "__main__":
    unittest.main()
