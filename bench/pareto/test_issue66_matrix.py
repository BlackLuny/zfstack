"""#66 preflight must reject stale and incomplete evidence, not invent PASS."""
import copy
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from issue66_matrix import build_matrix, file_sha, REVISIONS, COMPARISONS
from perf_gate import digest


class Issue66Matrix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repo = root / "source"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "lib.rs").write_text("pub fn hello() {}")
        subprocess.run(["git", "-C", str(self.repo), "add", "src"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
        self.sha = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.revs = {k: self.sha for k in REVISIONS}
        self.common = {
            "rustc": "rustc fixture", "cargo": "cargo fixture", "target": "x86_64-unknown-linux-gnu",
            "features": "test-peer", "rustflags": "-C target-cpu=x86-64", "profile": "release-fat-lto",
            "manifest_sha256": "a" * 64, "lock_sha256": "b" * 64,
            "probe_sha256": "c" * 64, "harness_sha256": "d" * 64,
            "case_generator_sha256": "e" * 64,
        }
        input_paths = {}
        for field in ("manifest_sha256", "lock_sha256", "probe_sha256",
                      "harness_sha256", "case_generator_sha256"):
            path = root / field
            path.write_text(field)
            self.common[field] = file_sha(path)
            input_paths[field] = str(path)
        self.manifest = {"common": self.common, "common_input_paths": input_paths, "variants": {}}
        for name in (*REVISIONS, "F2"):
            build = root / f"build-{name}"
            build.mkdir()
            binary = build / "probe"
            binary.write_bytes(f"fixture-{name}".encode())
            self.manifest["variants"][name] = {
                "source_dir": str(self.repo), "source_sha": self.sha,
                "build_dir": str(build), "binary_path": str(binary),
                "binary_sha256": file_sha(binary),
                "common_inputs_sha256": digest(self.common),
            }
        self.cases = [{"name": "tx_64", "args": ["tx-alloc", "--iterations", "1000",
                                                "--payload", "64"]}]

    def make(self, **kwargs):
        return build_matrix(self.manifest, self.cases, expected_revisions=self.revs, **kwargs)

    def test_all_seven_comparisons_are_present_and_explicitly_not_run(self):
        p = self.make()
        self.assertEqual(len(p["comparisons"]), len(COMPARISONS))
        self.assertEqual([x["name"] for x in p["comparisons"]], [x[0] for x in COMPARISONS])
        self.assertEqual(p["comparisons"][0]["comparison"], "AA")
        self.assertEqual(p["comparisons"][0]["baseline"], "F")
        self.assertEqual(p["comparisons"][0]["candidate"], "F2")
        self.assertEqual({v for v in p["measurement_gates"].values()}, {"NOT_RUN"})
        self.assertEqual(len(p["comparisons"][0]["orders"]), 12)
        self.assertTrue(all(set(o) == {"A", "B"} and len(o) == 4
                            for x in p["comparisons"] for o in x["orders"]))
        self.assertEqual(p, self.make())
        self.assertNotEqual(p["cases_sha256"], p["common_sha256"])

    def test_less_than_twelve_pairs_never_qualifies(self):
        for value in (0, 11, 12.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.make(pairs=value)

    def test_stale_binary_rejected(self):
        Path(self.manifest["variants"]["C"]["binary_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "binary content differs"):
            self.make()

    def test_source_revision_mismatch_rejected(self):
        self.manifest["variants"]["B"]["source_sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "wrong historical source"):
            self.make()

    def test_source_checkout_unchanged_but_receipt_lies_rejected(self):
        self.revs["A"] = "1" * 40
        self.manifest["variants"]["A"]["source_sha"] = self.revs["A"]
        with self.assertRaisesRegex(ValueError, "Git HEAD differs"):
            self.make()

    def test_dirty_production_sources_rejected(self):
        (self.repo / "src" / "lib.rs").write_text("modified")
        with self.assertRaisesRegex(ValueError, "modified production"):
            self.make()

    def test_common_inputs_mismatch_rejected(self):
        self.manifest["variants"]["D"]["common_inputs_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "common input drift"):
            self.make()

    def test_frozen_input_bytes_changed_rejected(self):
        Path(self.manifest["common_input_paths"]["probe_sha256"]).write_text("modified")
        with self.assertRaisesRegex(ValueError, "probe_sha256: frozen source file changed"):
            self.make()

    def test_same_build_dir_rejected_for_independent_aa(self):
        self.manifest["variants"]["F2"]["build_dir"] = self.manifest["variants"]["F"]["build_dir"]
        self.manifest["variants"]["F2"]["binary_path"] = self.manifest["variants"]["F"]["binary_path"]
        self.manifest["variants"]["F2"]["binary_sha256"] = self.manifest["variants"]["F"]["binary_sha256"]
        with self.assertRaisesRegex(ValueError, "build directories must be independent"):
            self.make()

    def test_binary_outside_build_dir_rejected(self):
        self.manifest["variants"]["B"]["binary_path"] = self.manifest["variants"]["A"]["binary_path"]
        self.manifest["variants"]["B"]["binary_sha256"] = self.manifest["variants"]["A"]["binary_sha256"]
        with self.assertRaisesRegex(ValueError, "binary must live"):
            self.make()

    def test_no_missing_case_or_duplicate_case_qualifies(self):
        for cases in ([], [{"name": "one", "args": []}],
                      self.cases + copy.deepcopy(self.cases)):
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                build_matrix(self.manifest, cases, expected_revisions=self.revs)

    def test_missing_variant_or_frozen_field_rejected(self):
        del self.manifest["variants"]["F2"]
        with self.assertRaisesRegex(ValueError, "exactly"):
            self.make()
        self.manifest["variants"]["F2"] = copy.deepcopy(self.manifest["variants"]["F"])
        del self.common["lock_sha256"]
        with self.assertRaisesRegex(ValueError, "common input fields"):
            self.make()


if __name__ == "__main__":
    unittest.main()
