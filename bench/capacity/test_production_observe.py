import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from production_observe import analyze,DEADLINE_ERROR


class ObservationAuditTests(unittest.TestCase):
    def audited(self,arrival,rr_cpu=None):
        strict=dict(rows=[dict(name='attempt',cc='cubic',rtt=20,heavy=200,
            valid=False,errors=[DEADLINE_ERROR],user_equal_mean_ms=3100)],
            comparisons=[dict(cc='cubic',rtt=20,heavy=200,pairs=12,valid=False)])
        if rr_cpu is not None:strict['rows'][0]['rr_cpu']=rr_cpu
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);(root/'attempt').mkdir()
            (root/'attempt/arrivals.json').write_text(json.dumps([dict(arrivals=[arrival])]))
            with patch('production_observe.production_pacing.analyze',return_value=copy.deepcopy(strict)):
                return analyze(root)

    def response(self):
        return dict(outcome='completed',planned_ns=5_000_000,started_ns=6_000_000,completed_ns=3_105_000_000)

    def test_late_delivery_remains_in_actual_mean_and_keeps_its_slo_failure(self):
        result=self.audited(self.response());row=result['rows'][0]
        self.assertTrue(row['observation_valid'])
        self.assertFalse(row['valid'])
        self.assertFalse(row['service_slo_passed'])
        self.assertEqual(row['errors'],[DEADLINE_ERROR])
        self.assertEqual(row['completed_after_deadline'],1)
        self.assertEqual(row['user_equal_mean_ms'],3100)
        self.assertFalse(result['comparisons'][0]['valid'])

    def test_generator_lag_is_still_a_measurement_failure(self):
        arrival=self.response();arrival['started_ns']=55_000_000
        self.assertFalse(self.audited(arrival)['rows'][0]['observation_valid'])

    def test_unobserved_request_is_not_a_measured_latency(self):
        arrival=self.response();arrival.update(outcome='missed',started_ns=None,completed_ns=None)
        row=self.audited(arrival)['rows'][0]
        self.assertFalse(row['observation_valid'])
        self.assertEqual(row['completed_after_deadline'],0)

    def test_completion_before_send_is_not_accepted_as_a_late_response(self):
        arrival=self.response();arrival['completed_ns']=4_000_000
        self.assertFalse(self.audited(arrival)['rows'][0]['observation_valid'])

    def test_missing_actual_affinity_does_not_qualify_an_observation(self):
        result=self.audited(self.response(),rr_cpu=4)
        self.assertFalse(result['rows'][0]['observation_valid'])
        self.assertEqual(result['rows'][0]['observation_errors'],['RR affinity differs from the frozen plan'])


if __name__=='__main__':unittest.main()
