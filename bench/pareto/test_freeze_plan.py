"""Plan preparation tests only: no synthetic observations become qualification."""
import copy
from pathlib import Path
import tempfile
import unittest

from freeze_plan import freeze
from perf_gate import digest, validate_plan
from test_analysis import measurement


class FrozenPlan(unittest.TestCase):
    def test_actual_binary_identity_reference_work_and_reproducible_order(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fixture-not-executable"
            binary.write_bytes(b"synthetic immutable identity fixture")
            args = dict(baseline=binary, candidate=binary, reference=measurement(2),
                        environment={"affinity": [0]}, build_manifest={"profile": "release"},
                        runner_id="synthetic-only", profile="release", case_id="P0-tx", case_version=1,
                        comparison="AA", trial_id="unique", cpu=0, blocks=20, seed=17, timeout=2,
                        budget=.03, probe_args=["tx-alloc", "--iterations", "1", "--payload", "1"])
            first = freeze(**args)
            self.assertEqual(first, freeze(**args))
            self.assertEqual(validate_plan(first), [])
            self.assertEqual(first["mode"], "shadow")
            self.assertEqual(first["binary_sha256"]["A"], first["binary_sha256"]["B"])
            self.assertEqual(first["reference_measurement_hash"], digest(args["reference"]))
            self.assertNotIn("work_cpu_ns", first["config"]["expected_measurement"])
            self.assertEqual(first["config"]["expected_measurement"]["checksum_u64"], 0)
            self.assertEqual(len(first["blocks"]), 20)
            for key, bad in (("case_version", "1"), ("comparison", "AB"),
                             ("profile", "unknown"), ("environment", {"affinity": [1]})):
                changed = {**args, key: bad}
                with self.subTest(key=key), self.assertRaises(ValueError): freeze(**changed)
            changed = copy.deepcopy(args["reference"]); changed["ok"] = False
            with self.assertRaises(ValueError): freeze(**{**args, "reference": changed})


if __name__ == "__main__":
    unittest.main()
