import collections
import math
import unittest

import common_cases
from common_collect import bootstrap
from common_env import wanted_mask


class RepresentativeContract(unittest.TestCase):
    def test_balanced_complete_independent_rounds(self):
        rows = common_cases.sequence('ordinary')
        self.assertEqual(len(rows), 576)
        cases = collections.defaultdict(list)
        for row in rows:
            cases[row['case']].append(row)
        self.assertEqual(len(cases), 24)
        for group in cases.values():
            self.assertEqual({r['round'] for r in group}, set(range(12)))
            orders = []
            for rep in range(12):
                pair = sorted([r for r in group if r['round'] == rep], key=lambda r:r['position'])
                self.assertEqual({r['arm'] for r in pair}, {'off', 'p99'})
                orders.append(pair[0]['arm'])
            self.assertEqual(collections.Counter(orders), {'off':6, 'p99':6})
        self.assertEqual([len(common_cases.sequence(p)) for p in ['preflight', 'aa', 'peak']], [8, 16, 48])

    def test_native_roles_override_rr_mask_and_rr_self_assignment_survives(self):
        self.assertEqual(wanted_mask('zfb-stack', [4], 'fixed', True), [0])
        self.assertEqual(wanted_mask('zfb-link-down', [0, 1, 2, 3], 'fixed', True), [2])
        self.assertEqual(wanted_mask('zfbench', [4], 'fixed', True), [4])
        self.assertEqual(wanted_mask('zfbench', [0, 1, 2, 3], 'fixed', True), [3])
        self.assertEqual(wanted_mask('zfbench', [4], 'fixed', False), [3])

    def test_whole_round_bootstrap_preserves_correlated_metrics_and_zero_effect(self):
        x = [math.log(1+v/100) for v in [-2, 1, -1, 3, 0, 2, -3, 1, 0, 2, -1, 0]]
        result = bootstrap({'one':x, 'two':[2*v for v in x], 'zero':[0.]*12},
            simultaneous_keys=['one', 'two', 'zero'], draws=2000)
        a, b, z = [result['series'][k] for k in ['one', 'two', 'zero']]
        self.assertEqual(z['bootstrap_95_pct'], [0., 0.])
        self.assertEqual(z['simultaneous_95_pct'], [0., 0.])
        for field in ['bootstrap_95_pct', 'simultaneous_95_pct']:
            for v, w in zip(a[field], b[field]):
                self.assertAlmostEqual(1+w/100, (1+v/100)**2)
        self.assertLess(a['bootstrap_95_pct'][0], 0)
        self.assertGreater(a['bootstrap_95_pct'][1], 0)


if __name__ == '__main__':
    unittest.main()
