import math
import unittest
from collections import defaultdict

import adaptive_cases as rig
from adaptive_analyze import bootstrap, comparison


class FrozenAdaptiveStudy(unittest.TestCase):
    def test_sequences_balance_pairs_and_never_supply_a_capacity(self):
        expected={'pilot':12,'aa':8,'primary':192,'bandwidth':32,'heavy':16,'disabled':16,'peak':24,'dynamic':16,'demand':8}
        for phase,count in expected.items():
            rows=rig.sequence(phase)
            self.assertEqual(len(rows),count)
            self.assertEqual([r['index'] for r in rows],list(range(count)))
            grouped=defaultdict(list)
            for row in rows:
                grouped[row['case']].append(row)
                self.assertEqual(row['egress_rate'],0)
                self.assertEqual(row.get('cc','bbr'),'bbr')
                if row['binary']=='v5':self.assertEqual(row['mode'],'off')
            for case,values in grouped.items():
                first=[v for v in values if v['position']==0]
                if len(first)>1:self.assertEqual(sum(v['arm'] in ['off','old'] for v in first)*2,len(first),(phase,case))

    def test_whole_round_bootstrap_preserves_identical_columns(self):
        values=[math.log(1+x) for x in [-.04,.01,.02,-.01,.03,-.02,.01,.00]]
        result=bootstrap({'a':values,'b':values},['a','b'],draws=1000)
        self.assertEqual(result['series']['a'],result['series']['b'])

    def test_a_missing_arrival_never_becomes_a_complete_latency_comparison(self):
        rows=[]
        for rep in range(4):
            for position,arm in enumerate(['off','auto']):
                rows.append(dict(case='one',round=rep,position=position,arm=arm,observation_valid=not(rep==2 and arm=='auto'),strict_valid=True,
                    heavy=8,rtt=50,rate_mbps=100,mbps=95.,stack_cpu_s_per_gb=5.,user_equal_mean_ms=None if rep==2 and arm=='auto' else 50.))
        result=comparison(dict(phase='bandwidth',rows=rows))
        self.assertFalse(result['cells']['one']['observed_valid'])
        self.assertNotIn('user_equal_mean_ms',result['cells']['one']['changes'])
        self.assertNotIn('user_equal_mean_ms',result['aggregates']['all'])


if __name__=='__main__':unittest.main()
