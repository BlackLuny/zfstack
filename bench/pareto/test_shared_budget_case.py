"""Evidence admission must reject incomplete pressure/lifecycle claims."""
import copy
import unittest

from shared_budget_case import validate


class SharedBudgetEvidence(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {'type': 'config', 'global_bytes': 100, 'port_bytes': 100, 'peer_bytes': 100,
             'flows_per_port': 2, 'bytes_per_flow': 10},
            *[{'type': 'sample', 'round': 0, 'port': port, 'phase': phase,
               'global_bytes': 90, 'port_bytes': 40, 'peer_bytes': 40,
               'tx_waiters': 1, 'core_waiting': False, 'record_waiters': 0}
              for port, phase in [(0, 'natural'), (1, 'natural'), (1, 'release'), (1, 'cancel')]],
            {'type': 'natural_done', 'round': 0, 'verified_bytes': 40, 'global_failures': [1, 1]},
            *[{'type': 'held', 'round': 0, 'phase': phase, 'bytes': 0, 'quiet_ms': 300}
              for phase in ['release', 'cancel']],
            {'type': 'release_done', 'round': 0, 'injected_host_bytes': 50,
             'epoch_before': 10, 'epoch_after': 11, 'verified_bytes': 10, 'wake_observed_us': 2000},
            {'type': 'round_done', 'round': 0, 'peer_b_after_cancel': 0, 'global_reserved': 0,
             'global_cached': 0, 'time_wait_bytes': 0, 'connection_counts': [0, 0],
             'cancelled_app_writers': 1, 'peer_a_survived_bytes': 50},
            {'type': 'summary', 'ok': True, 'ledger_zero': True, 'rounds': 1},
        ]

    def test_complete_evidence_passes(self):
        report = validate(self.rows, 1)
        self.assertEqual(report['natural_verified_bytes'], 40)
        self.assertTrue(report['ledger_zero_every_round'])

    def test_idle_transfer_without_a_real_waiter_is_rejected(self):
        for row in self.rows:
            if row['type'] == 'sample':
                row['tx_waiters'] = 0
        with self.assertRaisesRegex(ValueError, 'parked writer'):
            validate(self.rows, 1)

    def test_success_summary_cannot_hide_missing_round_or_pressure(self):
        natural = next(row for row in self.rows if row['type'] == 'natural_done')
        natural['global_failures'] = [0, 0]
        with self.assertRaisesRegex(ValueError, 'global-pressure'):
            validate(self.rows, 1)
        self.rows.remove(natural)
        with self.assertRaisesRegex(ValueError, 'incomplete natural_done'):
            validate(self.rows, 1)

    def test_cancellation_must_not_release_other_ports_or_leave_charges(self):
        for field, value in [('peer_a_survived_bytes', 0), ('peer_b_after_cancel', 1),
                             ('global_cached', 1), ('time_wait_bytes', 1),
                             ('connection_counts', [0, 1]), ('cancelled_app_writers', 0)]:
            rows = copy.deepcopy(self.rows)
            next(row for row in rows if row['type'] == 'round_done')[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(rows, 1)

    def test_unreleased_or_short_payload_is_rejected(self):
        for field, value in [('epoch_after', 10), ('verified_bytes', 9), ('wake_observed_us', 3_000_000)]:
            rows = copy.deepcopy(self.rows)
            next(row for row in rows if row['type'] == 'release_done')[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(rows, 1)

    def test_sampled_limit_violation_is_rejected(self):
        self.rows[1]['global_bytes'] = 101
        with self.assertRaisesRegex(ValueError, 'ceiling exceeded'):
            validate(self.rows, 1)


if __name__ == '__main__':
    unittest.main()
