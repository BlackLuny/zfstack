"""The real-host gate must reject receiver faults even in overflow negatives."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

DRIVER = Path(__file__).parent / 'results/2026-10-16-netem-fifo/remote_selftests.py'
sys.path.insert(0, str(DRIVER.parent))
spec = importlib.util.spec_from_file_location('remote_selftests_gate', DRIVER)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class RemoteGateTests(unittest.TestCase):
    def order(self, seq):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receiver.json'
            path.write_text(json.dumps({'udp': [[i, 100, 200] for i in seq]}))
            return gate.receiver_order(path)

    def fixture(self):
        results = {mode: {'selftest_ok': True, 'oracle_matches_selftest': True,
                          'receiver_order': self.order([0, 1, 2])} for mode in gate.MODES}
        records = [{'label': mode, 'returncode': 0} for mode in gate.MODES]
        return results, records

    def test_overflow_gaps_are_allowed(self):
        results, records = self.fixture()
        results['overflow']['receiver_order'] = self.order([0, 3, 9])
        self.assertTrue(gate.gate_passes(0, results, records))

    def test_overflow_only_reordering_fails_despite_passing_summary(self):
        results, records = self.fixture()
        results['overflow']['receiver_order'] = self.order([0, 9, 3])
        self.assertEqual(results['overflow']['receiver_order']['overtaken'], 1)
        self.assertFalse(gate.gate_passes(0, results, records))

    def test_duplicates_fail_in_every_mode_without_overtakes(self):
        for mode in gate.MODES:
            with self.subTest(mode=mode):
                results, records = self.fixture()
                results[mode]['receiver_order'] = self.order([0, 1, 1, 2])
                self.assertEqual(results[mode]['receiver_order']['overtaken'], 0)
                self.assertFalse(gate.gate_passes(0, results, records))

    def test_absent_empty_or_invalid_receiver_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'receiver.json'
            self.assertFalse(gate.receiver_order(path)['ok'])
            for data in ['{', '{}', '{"udp": []}', '{"udp": [[-1]]}', '{"udp": [[true]]}', '{"udp": [null]}']:
                path.write_text(data)
                self.assertFalse(gate.receiver_order(path)['ok'])

    def test_probe_or_missing_duplicate_failed_execution_prevents_pass(self):
        results, records = self.fixture()
        self.assertFalse(gate.gate_passes(1, results, records))
        self.assertFalse(gate.gate_passes(0, results, records[:-1]))
        self.assertFalse(gate.gate_passes(0, results, records + records[-1:]))
        records[-1]['returncode'] = 1
        self.assertFalse(gate.gate_passes(0, results, records))

    def test_missing_results_or_oracle_disagreement_prevents_pass(self):
        results, records = self.fixture()
        for mode in gate.MODES:
            changed = copy.deepcopy(results)
            del changed[mode]
            self.assertFalse(gate.gate_passes(0, changed, records))
            for field in ('selftest_ok', 'oracle_matches_selftest', 'receiver_order'):
                changed = copy.deepcopy(results)
                changed[mode][field] = False if field != 'receiver_order' else {}
                self.assertFalse(gate.gate_passes(0, changed, records))


if __name__ == '__main__':
    unittest.main()
