import collections
import unittest

from common_sign import sign_tail


class ExactSignCalibration(unittest.TestCase):
    def test_tail_matches_exhaustive_null_sign_assignments(self):
        for trials in range(1, 13):
            # Independent enumeration rather than another binomial formula.
            counts = collections.Counter(mask.bit_count() for mask in range(1 << trials))
            for threshold in range(trials+1):
                expected = sum(count for good, count in counts.items() if good >= threshold)/(1 << trials)
                self.assertEqual(sign_tail(threshold, trials), expected)


if __name__ == '__main__':
    unittest.main()
