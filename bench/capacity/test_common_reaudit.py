import copy
import unittest

from common_reaudit import rr_transition_error


class PlacementRaceCertificate(unittest.TestCase):
    def test_allows_rr_self_assignment_but_rejects_roles_and_reversals(self):
        row = dict(kind='native', placement='fixed', lights=3)
        thread = dict(name='zfbench-product', first_seen_sec=.2, last_seen_sec=2.,
            first=dict(affinity=[3]), last=dict(affinity=[4]))
        observer = dict(threads={'7':thread}, samples=[dict(elapsed_sec=1., threads=[dict(tid=7, affinity=[4])])])
        error = 'actual mask differs 7 zfbench-product'
        self.assertTrue(rr_transition_error(error, row, observer))
        self.assertFalse(rr_transition_error('late assignment 7 zfbench-product', row, observer))
        self.assertFalse(rr_transition_error(error, dict(row, lights=0), observer))
        self.assertFalse(rr_transition_error(error, dict(row, placement='original'), observer))
        bad = copy.deepcopy(observer)
        bad['threads']['7']['name'] = 'zfb-stack'
        self.assertFalse(rr_transition_error('actual mask differs 7 zfb-stack', row, bad))
        bad = copy.deepcopy(observer)
        bad['samples'].append(dict(elapsed_sec=1.5, threads=[dict(tid=7, affinity=[3])]))
        self.assertFalse(rr_transition_error(error, row, bad))
        bad = copy.deepcopy(observer)
        bad['samples'][0]['threads'][0]['affinity'] = [2, 3]
        self.assertFalse(rr_transition_error(error, row, bad))


if __name__ == '__main__':
    unittest.main()
