import json
from pathlib import Path
import tempfile
import unittest

from adaptive_dynamic import cumulative, latency_phases, recovery, throughput


class NativeTransitionEvidence(unittest.TestCase):
    def test_progress_interpolation_and_recovery_use_full_work_windows(self):
        progress=[dict(elapsed_ms=i*250,bytes=[i*250_000]) for i in range(25)]
        self.assertEqual(cumulative(progress,.125),125_000)
        self.assertEqual(throughput(progress,1,3),8.)
        self.assertEqual(recovery(progress,0,5,8.)['seconds'],2.)
        self.assertFalse(recovery(progress,0,5,20.)['passed'])

    def test_phase_membership_uses_monotonic_planned_time_and_keeps_missing(self):
        streams=[]
        for user in range(3):
            arrivals=[dict(planned_ns=9_950_000_000,started_ns=9_950_000_000,completed_ns=9_951_000_000,outcome='completed'),
                dict(planned_ns=10_100_000_000,started_ns=None,completed_ns=None,outcome='missed')]
            streams.append(dict(count=1250,measurement_origin_ns=100_000_000,arrivals=arrivals))
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp);(p/'arrivals.json').write_text(json.dumps(streams))
            result=latency_phases(p,[('before',0,10),('after',10,25)],50)
        self.assertFalse(result['before']['valid'])
        self.assertFalse(result['after']['valid'])
        self.assertEqual(result['after']['missing'],3)
        self.assertEqual(result['after']['users'][0]['planned'],2)
        self.assertIsNone(result['after']['user_equal_mean_ms'])


if __name__=='__main__':unittest.main()
