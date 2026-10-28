import copy
import unittest

from common_reaudit import rr_transition_error, startup_transition_error


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

    def test_initial_taskset_transition_requires_parentage_and_assignment_bound(self):
        row = dict(kind='native', placement='fixed', lights=3)
        wrapper = dict(pid=7, name='python3', first_seen_sec=.001,
            first=dict(affinity=[0, 1, 2, 3]), last=dict(affinity=[3]))
        roles = {name:dict(tid=tid, first_seen_sec=.05) for name, tid in [('zfb-stack', 8), ('zfb-link-up', 9), ('zfb-link-down', 10)]}
        threads = {'7':wrapper, **{str(v['tid']):dict(first=dict(ppid=7)) for v in roles.values()}}
        observer = dict(threads=threads, required_roles=roles,
            samples=[dict(threads=[dict(tid=7, name='python3', affinity=[3])]) for _ in range(2)])
        error = 'actual mask differs 7 taskset'
        self.assertTrue(startup_transition_error(error, row, observer))
        wrong = copy.deepcopy(observer)
        wrong['required_roles']['zfb-stack']['first_seen_sec'] = 2.1
        self.assertFalse(startup_transition_error(error, row, wrong))
        wrong = copy.deepcopy(observer)
        wrong['threads']['8']['first']['ppid'] = 99
        self.assertFalse(startup_transition_error(error, row, wrong))
        wrong = copy.deepcopy(observer)
        wrong['samples'][0]['threads'][0]['affinity'] = [0, 1, 2, 3]
        self.assertFalse(startup_transition_error(error, row, wrong))


if __name__ == '__main__':
    unittest.main()
