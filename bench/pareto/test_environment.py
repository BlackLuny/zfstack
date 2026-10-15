"""Unknown Linux capabilities can never qualify performance evidence."""
import copy
import unittest

from environment import validity


class EnvironmentEvidence(unittest.TestCase):
    def fixture(self):
        limits = [{"path": "/sys/fs/cgroup", "cpu.max": "max 100000", "cpuset.cpus.effective": "0", "memory.max": "max"}]
        expected = {"kernel": "fixture", "cpu_model": "fixture", "affinity": [0], "governors": {}, "cgroup_limits": limits}
        observed = {**expected, "compilers": [], "cgroup": {"path": "/", "ancestors": copy.deepcopy(limits)}}
        return observed, expected

    def test_missing_cgroup_v2_or_ancestor_limit_is_invalid_even_if_frozen_unknown(self):
        observed, expected = self.fixture()
        self.assertEqual(validity([observed], expected), [])
        observed["cgroup"] = {"path": None, "ancestors": []}
        expected["cgroup_limits"] = []
        self.assertIn("DUT cgroup v2 ancestor limits unavailable", validity([observed], expected))
        observed, expected = self.fixture()
        observed["cgroup"]["ancestors"][0]["cpu.max"] = None
        expected["cgroup_limits"][0]["cpu.max"] = None
        self.assertIn("DUT cgroup v2 ancestor limits unavailable", validity([observed], expected))

    def test_compile_and_changed_affinity_remain_invalid(self):
        observed, expected = self.fixture()
        observed["compilers"] = [{"pid": 2, "name": "rustc"}]
        observed["affinity"] = [1]
        errors = validity([observed], expected)
        self.assertIn("concurrent compiler detected", errors)
        self.assertIn("environment fingerprint missing/changed: affinity", errors)


if __name__ == "__main__":
    unittest.main()
