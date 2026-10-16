"""Source inventory negatives; this scanner never substitutes for actual tests."""
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from qa.common import EvidenceError, read_json, write_json
from qa.coverage import audit, python_tests, rust_tests, test_inventory, verify_audit


class CoverageInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "src").mkdir(); (self.root / "qa/policy").mkdir(parents=True)
        self.policy = self.root / "qa/policy"
        self.rust = self.root / "src/adapter.rs"
        self.rust.write_text('#[test]\nfn release_wakes_writer() {}\n#[tokio::test(flavor="multi_thread",worker_threads=2)]\nasync fn another() {}\n')
        write_json(self.policy / "ignored.json", {"schema_version": 1, "tests": []})
        self.freeze()

    def freeze(self):
        write_json(self.policy / "test-inventory.json", test_inventory(self.root))

    def test_explicit_review_inventory_and_hash_bound_audit_shape(self):
        result = audit(self.root, self.policy)
        self.assertEqual(result["counts"], {"rust": 2, "python": 0, "rust_configuration_files": 0})
        self.assertEqual(verify_audit(result, self.policy), result)
        expected = read_json(self.policy / "test-inventory.json")
        self.assertEqual([entry["name"] for entry in expected["rust_tests"]], ["another", "release_wakes_writer"])
        self.assertIn("no Rust macro", expected["scope"]["limitations"])

    def test_missing_wrong_inventory_hash_counts_and_old_bare_ok_are_rejected(self):
        result = audit(self.root, self.policy)
        missing = {key: value for key, value in result.items() if key != "test_inventory_hash"}
        for changed in (missing, {**result, "test_inventory_hash": "0" * 64},
                        {**result, "counts": {**result["counts"], "rust": 3}},
                        {**result, "counts": {**result["counts"], "python": False}},
                        {"ok": True, "registered": 0, "tests": []}):
            with self.subTest(changed=changed), self.assertRaisesRegex(EvidenceError, "frozen test inventory"):
                verify_audit(changed, self.policy)

    def test_deleting_renaming_or_swapping_names_with_equal_count_is_drift(self):
        original = self.rust.read_text()
        for changed in ('#[test] fn another() {}', original.replace("release_wakes_writer", "removed_regression"),
                        original.replace("another", "temporary").replace("release_wakes_writer", "another").replace("temporary", "release_wakes_writer")):
            self.rust.write_text(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(EvidenceError, "inventory drift"):
                audit(self.root, self.policy)
        self.assertEqual(read_json(self.policy / "test-inventory.json")["rust_tests"][1]["name"], "release_wakes_writer")

    def test_new_ignore_or_runtime_capability_on_existing_test_requires_policy_review(self):
        original = self.rust.read_text()
        for changed in (original.replace("#[test]", "#[test] #[ignore]"),
                        original.replace('worker_threads=2', 'worker_threads=4'), original.replace("#[test]", '#[cfg(target_os="linux")] #[test]'),
                        original.replace("release_wakes_writer", "renamed") + "\n#[test] fn added() {}"):
            self.rust.write_text(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(EvidenceError, "inventory drift"):
                audit(self.root, self.policy)

    def test_pure_additions_pass_bound_to_reviewed_inventory(self):
        reviewed = audit(self.root, self.policy)
        python = self.root / "test_added.py"
        python.write_text("def test_new(): pass\n")
        self.rust.write_text(self.rust.read_text() + '\n#[test] fn added() {}\n#[cfg(target_os="linux")] #[test] fn added_linux() {}'
                             '\n#[cfg(test)] mod more { #[test] fn nested() {} }')
        result = audit(self.root, self.policy)
        self.assertEqual({key: value for key, value in result.items() if key != "additions"}, reviewed)
        self.assertEqual([entry["name"] for entry in result["additions"]["rust_tests"]], ["added", "added_linux", "nested"])
        self.assertEqual([entry["name"] for entry in result["additions"]["python_tests"]], ["test_new"])
        self.assertEqual(result["additions"]["rust_configuration"],
                         [{"source": "src/adapter.rs", "attributes": ["cfg ( target_os = \"linux\" )", "cfg ( test )"]}])
        self.assertEqual(verify_audit(result, self.policy), result)
        self.assertEqual(read_json(self.policy / "test-inventory.json")["rust_tests"][1]["name"], "release_wakes_writer")

    def test_additions_cannot_hide_gate_or_ignore_reviewed_tests(self):
        original = self.rust.read_text()
        for changed in (original + '\n#[test] #[ignore] fn added_ignored() {}',
                        original + '\n#[test] #[cfg_attr(not(miri), ignore)] fn added_cfg_ignored() {}',
                        original + '\n#[test] #[cfg_attr(target_os="linux", ignore = "slow")] fn added_cfg_ignored() {}',
                        '#![cfg(feature="never")]\n' + original + '\n#[test] fn added() {}',
                        original + '\n#[cfg(target_os="linux")] mod gated { #[test] fn added() {} }'):
            self.rust.write_text(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(EvidenceError, "drift"):
                audit(self.root, self.policy)

    def test_added_python_skips_or_conditional_definitions_require_review(self):
        for source in ('import unittest\nclass T(unittest.TestCase):\n    @unittest.skip("x")\n    def test_new(self): pass\n',
                       'import unittest, sys\nclass T(unittest.TestCase):\n    @unittest.skipIf(sys.platform == "win32", "x")\n    def test_new(self): pass\n',
                       'import unittest\n@unittest.skipUnless(False, "x")\nclass T(unittest.TestCase):\n    def test_new(self): pass\n',
                       'import unittest\nclass T(unittest.TestCase):\n    @unittest.expectedFailure\n    def test_new(self): pass\n',
                       'import pytest\n@pytest.mark.skipif(True, reason="x")\ndef test_new(): pass\n',
                       'import pytest\n@pytest.mark.xfail\ndef test_new(): pass\n',
                       'import sys\nif sys.platform == "linux":\n    def test_new(): pass\n',
                       'try:\n    import missing\n    def test_new(): pass\nexcept ImportError:\n    pass\n'):
            path = self.root / "test_added.py"
            path.write_text(source)
            with self.subTest(source=source), self.assertRaisesRegex(EvidenceError, "inventory drift"):
                audit(self.root, self.policy)
        path.write_text('import unittest\nfrom unittest import mock\nclass T(unittest.TestCase):\n'
                        '    @mock.patch("os.skip_marker")\n    def test_new(self, patched): pass\n')
        result = audit(self.root, self.policy)
        self.assertEqual([entry["name"] for entry in result["additions"]["python_tests"]], ["test_new"])
        self.assertEqual(verify_audit(result, self.policy), result)

    def test_reporter_rejects_forged_additions(self):
        self.rust.write_text(self.rust.read_text() + '\n#[test] fn added() {}')
        (self.root / "test_added.py").write_text("def test_new(): pass\n")
        result = audit(self.root, self.policy)
        reviewed = read_json(self.policy / "test-inventory.json")["rust_tests"][0]
        added = result["additions"]["rust_tests"][0]
        python = result["additions"]["python_tests"][0]
        for additions in ({}, [], {"rust_tests": []}, {"unknown": [added]}, {"rust_tests": [reviewed]},
                          {"rust_tests": [{**added, "ignored": True}]},
                          {"rust_tests": [{**added, "attributes": ["test", "ignore"]}]},
                          {"rust_tests": [{**added, "attributes": ["test", "cfg_attr ( not ( miri ) , ignore )"]}]},
                          {"rust_tests": [{key: value for key, value in added.items() if key != "attributes"}]},
                          {"python_tests": [{**python, "decorators": ["unittest.skip('x')"]}]},
                          {"python_tests": [{**python, "guards": ["if False"]}]},
                          {"python_tests": [{key: value for key, value in python.items() if key != "decorators"}]},
                          {"rust_tests": [added], "rust_configuration": [{"source": "src/adapter.rs", "attributes": ['cfg ( feature = "never" )']}]}):
            with self.subTest(additions=additions), self.assertRaises(EvidenceError):
                verify_audit({**result, "additions": additions}, self.policy)

    def test_comments_strings_and_nested_block_comments_never_create_tests(self):
        text = r'''
// #[test] fn in_line_comment() {}
/* outer /* #[test] fn in_nested_comment() {} */ still comment */
const A: &str = "#[test] fn in_string() {}";
const B: &str = r###"#[test] fn in_raw_string() {}"###;
const C: &[u8] = br#"#[test] fn in_byte_raw_string() {}"#;
#[test_case(1, "fake #[test] fn not_a_test() {}")]
fn actual_case(value:u8) {}
#[tokio::test]
async fn actual_async() { let lifetime: &'static str = "x"; let c = 'x'; }
'''
        entries = rust_tests(text, "src/file.rs")
        self.assertEqual([entry["name"] for entry in entries], ["actual_case", "actual_async"])
        self.assertEqual(entries[0]["attributes"][0].split()[0], "test_case")
        # Harmless whitespace/comments have no inventory effect.
        self.rust.write_text(self.rust.read_text().replace("fn release_wakes_writer", "fn /* comment */ release_wakes_writer"))
        audit(self.root, self.policy)

    def test_same_named_tests_have_distinct_occurrences_and_removal_is_detected(self):
        self.rust.write_text('#[test] fn repeated() {}\nmod inner { #[test] fn repeated() {} }')
        self.freeze()
        self.assertEqual([entry["occurrence"] for entry in test_inventory(self.root)["rust_tests"]], [1, 2])
        self.rust.write_text('#[test] fn repeated() {}')
        with self.assertRaises(EvidenceError): audit(self.root, self.policy)

    def test_file_module_gates_and_restricted_visibility_are_not_invisible(self):
        text = '#![cfg(feature="tokio")]\n#[cfg(not(target_os="windows"))]\nmod unit { #[test] pub(crate) fn visible_test() {} }'
        self.rust.write_text(text)
        self.freeze()
        self.assertEqual(test_inventory(self.root)["rust_tests"][0]["name"], "visible_test")
        self.rust.write_text(text.replace('feature="tokio"', 'feature="disabled"'))
        with self.assertRaisesRegex(EvidenceError, "rust_configuration"): audit(self.root, self.policy)

    def test_ignored_names_sources_and_duplicate_occurrences_are_checked(self):
        self.rust.write_text('#[test] #[ignore="heavy"] fn registered() {}')
        self.freeze()
        with self.assertRaisesRegex(EvidenceError, "ignored coverage drift"): audit(self.root, self.policy)
        write_json(self.policy / "ignored.json", {"schema_version": 1, "tests": [
            {"name": "adapter::registered", "source": "src/adapter.rs", "category": "soak", "schedule": "nightly", "reason": "bounded expensive test"}]})
        self.assertEqual(audit(self.root, self.policy)["registered"], 1)
        self.rust.write_text(self.rust.read_text() + '\nmod other { #[test] #[ignore] fn registered() {} }')
        self.freeze()
        with self.assertRaisesRegex(EvidenceError, "ignored coverage drift"): audit(self.root, self.policy)

    def test_python_ast_inventory_tracks_class_method_and_skip_capability(self):
        path = self.root / "test_python.py"
        original = '''import unittest
@unittest.skipUnless(CAPABILITY, "requires designated node")
class AdapterTests(unittest.TestCase):
    @unittest.skipIf(PLATFORM == "Windows", "socket capability")
    def test_release(self):
        """def test_fake(): pass"""
        pass
    async def test_async(self): pass
# def test_comment(): pass
'''
        path.write_text(original); self.freeze()
        entries = test_inventory(self.root)["python_tests"]
        self.assertEqual([(row["class"], row["name"]) for row in entries], [("AdapterTests", "test_async"), ("AdapterTests", "test_release")])
        self.assertTrue(all(row["decorators"] for row in entries))
        for changed in (original.replace("test_release", "test_renamed"), original.replace("CAPABILITY", "False")):
            path.write_text(changed)
            with self.subTest(changed=changed), self.assertRaisesRegex(EvidenceError, "inventory drift"):
                audit(self.root, self.policy)

    def test_generated_artifacts_are_excluded_and_source_symlinks_are_rejected(self):
        generated = self.root / "target/source.rs"; generated.parent.mkdir(); generated.write_text("#[test] fn not_handwritten() {}")
        self.assertEqual(len(test_inventory(self.root)["rust_tests"]), 2)
        virtualenv = self.root / ".qa-venv/lib/test_vendor.py"
        virtualenv.parent.mkdir(parents=True); virtualenv.write_text("def test_not_repository_code(): pass")
        self.assertEqual(test_inventory(self.root)["python_tests"], [])
        path = self.root / "src/link.rs"
        try: path.symlink_to(self.rust)
        except (OSError, NotImplementedError): self.skipTest("host does not grant source symlink capability")
        with self.assertRaisesRegex(EvidenceError, "symlink source"): test_inventory(self.root)

    def test_python_conditional_class_capability_is_frozen_even_when_not_executed(self):
        path = self.root / "test_conditional.py"
        source = 'if PLATFORM == "Linux":\n    class Tests:\n        def test_socket(self): pass\n'
        path.write_text(source); self.freeze()
        self.assertEqual(test_inventory(self.root)["python_tests"][0]["guards"], ["if PLATFORM == 'Linux'"])
        path.write_text(source.replace("Linux", "never-supported"))
        with self.assertRaisesRegex(EvidenceError, "python_tests"): audit(self.root, self.policy)

    def test_git_sources_include_new_tests_and_exclude_ignored_caches(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text("vendor-cache/\n.qa-venv/\n")
        ignored = self.root / "vendor-cache/test_dependency.py"
        ignored.parent.mkdir(); ignored.write_text("def test_dependency(): pass")
        subprocess.run(["git", "add", "src/adapter.rs", ".gitignore"], cwd=self.root, check=True)
        (self.root / "test_new.py").write_text("def test_untracked_new_source(): pass")
        self.assertEqual([entry["name"] for entry in test_inventory(self.root)["python_tests"]], ["test_untracked_new_source"])

    def test_utf8_sources_ignore_host_default_encoding(self):
        # U+0410 contains byte 0x90 in UTF-8, which is undefined in cp1252.
        original = self.rust.read_text(encoding="utf-8")
        self.rust.write_text("// \u0410\n" + original, encoding="utf-8")
        (self.root / "test_unicode.py").write_text("# \u0410\ndef test_unicode(): pass\n", encoding="utf-8")
        read_text = Path.read_text

        def windows_read_text(path, encoding=None, errors=None):
            return read_text(path, encoding=encoding or "cp1252", errors=errors)

        with mock.patch.object(Path, "read_text", windows_read_text):
            inventory = test_inventory(self.root)
        self.assertEqual([entry["name"] for entry in inventory["rust_tests"]], ["another", "release_wakes_writer"])
        self.assertEqual([entry["name"] for entry in inventory["python_tests"]], ["test_unicode"])

    def test_malformed_sources_and_missing_inventory_fail_closed(self):
        for text in ('/* missing end', 'const A:&str = r#"missing end', '#[test fn broken() {}'):
            with self.subTest(text=text), self.assertRaises(EvidenceError): rust_tests(text, "broken.rs")
        with self.assertRaises(EvidenceError): python_tests("def test_broken(:", "test_broken.py")
        (self.policy / "test-inventory.json").unlink()
        with self.assertRaises((EvidenceError, OSError)): audit(self.root, self.policy)


if __name__ == "__main__":
    unittest.main()
