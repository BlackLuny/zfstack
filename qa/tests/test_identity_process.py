from __future__ import annotations

import copy
import contextlib
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa.build_manifest import FLAGS, build_manifest, native_platform, verify_manifest, prepare_native_binary
from qa.common import EvidenceError, ROOT, digest, file_hash, read_json
from qa.environment import collect_environment
from qa.process import run_child

SHA = "1" * 40
HASH = "2" * 64


def manifest_fixture(*, sha=SHA, toolchain="1.94.1", profile="release", features=(),
                     system="Linux", machine="x86_64", manifest_path="Cargo.toml", package="zfstack"):
    """Complete synthetic identity, for contract tests; never execution evidence."""
    os_part = {"Linux": "unknown-linux-gnu", "Darwin": "apple-darwin", "Windows": "pc-windows-msvc"}[system]
    arch = native_platform(system, machine).split("-", 1)[1]
    host = arch + "-" + os_part
    graph = f"0{package} v0.0.1|" + ",".join(sorted(features))
    return {"schema_version": 1, "source": {"sha": sha, "tree": SHA, "source_hash": HASH,
            "patch_hash": digest(""), "dirty": False}, "lock_hash": HASH,
            "lock_path": Path(manifest_path).with_name("Cargo.lock").as_posix(), "manifest_path": manifest_path,
            "selected_package": package, "toolchain": toolchain, "rustc": f"rustc 1.94.1\nbinary: rustc\ncommit-hash: {SHA}\ncommit-date: 2026-03-25\nhost: {host}\nrelease: 1.94.1\nLLVM version: 21.1.8",
            "cargo": "cargo 1.94.1", "host": host, "target": host,
            "native_platform": native_platform(system, machine), "profile": profile,
            "requested_features": sorted(features), "dependency_scope": "normal,build",
            "dependency_graph": graph, "dependency_graph_hash": digest(graph),
            "build_flags": {name: "" for name in FLAGS}, "python": "3.12.0", "binaries": ([{"name": package + (".exe" if system == "Windows" else ""),
                "sha256": HASH, "size_bytes": 1}] if package in ("zfbench", "zfharness") else []),
            "qa_requirements_hash": HASH, "tool_hashes": {"qa/run.py": HASH}}


def environment_fixture(system="Linux", machine="x86_64"):
    value = collect_environment()
    value.update(system=system, machine=machine)
    return value


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.case = {"runner": "core-build", "profile": "release", "features": [], "toolchain": "1.94.1",
                     "platform": "linux-x86_64"}
        self.plan = {"tested_sha": SHA, "toolchain": "1.94.1"}
        self.manifest = manifest_fixture()
        self.environment = environment_fixture()

    def verify(self):
        return verify_manifest(self.manifest, self.case, self.plan, self.environment)

    def test_complete_identity(self):
        self.assertEqual(self.verify(), self.manifest)

    def test_every_manifest_field_is_required(self):
        for key in list(self.manifest):
            with self.subTest(key=key):
                manifest = copy.deepcopy(self.manifest)
                del manifest[key]
                with self.assertRaises(EvidenceError):
                    verify_manifest(manifest, self.case, self.plan, self.environment)

    def test_every_environment_field_is_required(self):
        for key in list(self.environment):
            with self.subTest(key=key):
                environment = copy.deepcopy(self.environment)
                del environment[key]
                with self.assertRaises(EvidenceError):
                    verify_manifest(self.manifest, self.case, self.plan, environment)

    def test_identity_mismatch(self):
        for key, wrong in (("toolchain", "nightly-2026-07-11"), ("profile", "dev"),
                           ("requested_features", ["tokio"]), ("native_platform", "windows-x86_64"),
                           ("target", "aarch64-unknown-linux-gnu"), ("dependency_graph_hash", "3" * 64),
                           ("manifest_path", "../Cargo.toml"), ("lock_path", "fuzz/Cargo.lock"),
                           ("tool_hashes", {})):
            with self.subTest(key=key):
                manifest = copy.deepcopy(self.manifest)
                manifest[key] = wrong
                with self.assertRaises(EvidenceError):
                    verify_manifest(manifest, self.case, self.plan, self.environment)

    def test_dirty_wrong_sha_and_patch(self):
        for key, wrong in (("sha", "3" * 40), ("dirty", True), ("patch_hash", HASH)):
            with self.subTest(key=key):
                manifest = copy.deepcopy(self.manifest)
                manifest["source"][key] = wrong
                with self.assertRaises(EvidenceError):
                    verify_manifest(manifest, self.case, self.plan, self.environment)

    def test_native_evidence_cannot_be_cross_platform(self):
        self.environment["system"] = "Windows"
        self.environment["machine"] = "AMD64"
        with self.assertRaises(EvidenceError):
            self.verify()

    def test_compiler_host_must_match_actual_platform(self):
        self.manifest["host"] = self.manifest["target"] = "aarch64-apple-darwin"
        self.manifest["rustc"] = "rustc 1.94.1\nhost: aarch64-apple-darwin"
        with self.assertRaises(EvidenceError):
            self.verify()

    def test_nested_workspaces_use_own_lock(self):
        for runner, directory in (("loom", "qa-models"), ("fuzz", "fuzz")):
            with self.subTest(runner=runner):
                case = {**self.case, "runner": runner}
                manifest = manifest_fixture(manifest_path=f"{directory}/Cargo.toml", package="zfstack-qa-models" if runner == "loom" else "zfstack-fuzz")
                verify_manifest(manifest, case, self.plan, self.environment)
                manifest["lock_path"] = "Cargo.lock"
                with self.assertRaises(EvidenceError):
                    verify_manifest(manifest, case, self.plan, self.environment)

    def test_builder_selects_normal_build_graph_and_nested_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "qa-models").mkdir()
            (root / "qa").mkdir()
            (root / "Cargo.toml").write_text('[workspace]\nmembers=["qa-models"]\n')
            (root / "Cargo.lock").write_text("root-lock")
            (root / "qa-models/Cargo.toml").write_text('[package]\nname="model"\nversion="0.1.0"\n[workspace]\n')
            (root / "qa-models/Cargo.lock").write_text("model-lock")
            (root / "qa/requirements.lock").write_text("requirements")
            def command(argv, _root, **kwargs):
                if argv[0] == "rustc":
                    return "rustc 1.94.1\nhost: x86_64-unknown-linux-gnu"
                if "tree" in argv:
                    return "0model v0.1.0|tokio\n1bytes v1.0.0|std"
                return "cargo 1.94.1"
            with patch("qa.build_manifest.command", side_effect=command) as called, patch(
                    "qa.build_manifest.source_identity", return_value=self.manifest["source"]):
                value = build_manifest(root, features=["tokio"], manifest_path="qa-models/Cargo.toml",
                                       build_env={"RUSTFLAGS": "-Zsanitizer=address"})
            self.assertEqual(value["lock_hash"], file_hash(root / "qa-models/Cargo.lock"))
            self.assertEqual(value["selected_package"], "model")
            self.assertEqual(value["build_flags"]["RUSTFLAGS"], "-Zsanitizer=address")
            argv = called.call_args_list[0].args[0]
            self.assertIn("--no-default-features", argv)
            self.assertEqual(argv[argv.index("--edges") + 1], "normal,build")
            self.assertEqual(argv[argv.index("-p") + 1], "model")
            self.assertEqual(argv[argv.index("--features") + 1], "tokio")


class RunnableIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.system, self.machine = platform.system(), platform.machine()
        self.manifest = manifest_fixture(system=self.system, machine=self.machine, package="zfbench")
        self.host = self.manifest["host"]

    def test_inherited_binary_and_output_directory_cannot_replace_build_product(self):
        env = {"BIN": "/old/zfbench", "CARGO_TARGET_DIR": "/old/target", "CARGO_BUILD_TARGET": self.host}
        with patch("qa.build_manifest.command", return_value=self.manifest["rustc"]):
            binary = prepare_native_binary(self.root, "1.94.1", "zfbench", env)
        self.assertEqual(binary, self.root / "target" / self.host / "release" / ("zfbench.exe" if self.system == "Windows" else "zfbench"))
        self.assertEqual(env["BIN"], str(binary))
        self.assertEqual(env["CARGO_TARGET_DIR"], str(self.root / "target"))
        self.assertEqual(env["CARGO_BUILD_TARGET"], self.host)

    def test_explicit_cross_target_and_foreign_compiler_are_rejected(self):
        with patch("qa.build_manifest.command", return_value=self.manifest["rustc"]):
            with self.assertRaisesRegex(EvidenceError, "cross target"):
                prepare_native_binary(self.root, "1.94.1", "zfbench", {"CARGO_BUILD_TARGET": "wasm32-unknown-unknown"})
        with patch("qa.build_manifest.command", return_value="rustc fixture\nhost: wasm32-unknown-unknown"):
            with self.assertRaisesRegex(EvidenceError, "compiler host"):
                prepare_native_binary(self.root, "1.94.1", "zfbench", {})

    def test_manifest_cannot_hide_a_contradictory_explicit_target(self):
        manifest = manifest_fixture(system=self.system, machine=self.machine)
        manifest["build_flags"]["CARGO_BUILD_TARGET"] = "wasm32-unknown-unknown"
        case = {"runner": "core-build", "profile": "release", "toolchain": "1.94.1", "features": [],
                "platform": native_platform(self.system, self.machine)}
        with self.assertRaisesRegex(EvidenceError, "explicit Cargo target"):
            verify_manifest(manifest, case, {"tested_sha": SHA, "toolchain": "1.94.1"},
                            environment_fixture(self.system, self.machine))

    def test_harness_launch_uses_the_same_frozen_artifact(self):
        from qa.run import commands
        binary = self.root / "target" / self.host / "release" / "zfharness"
        for runner in ("l0", "replay"):
            case = {"runner": runner, "config": {"scenario": "single", "seed": 1, "app_drive": "eager"}}
            if runner == "replay":
                script = self.root / "harness/regressions/fixture.json"
                script.parent.mkdir(parents=True); script.write_text("{}")
                case["config"].update(script="fixture.json", script_sha256=file_hash(script))
            argv = commands(case, "1.94.1", self.root, self.root / "out", binary=binary)
            self.assertEqual(argv[1][0], str(binary))

    def test_empty_or_wrong_binary_inventory_cannot_supply_execution_identity(self):
        plan = {"tested_sha": SHA, "toolchain": "1.94.1"}
        environment = environment_fixture(self.system, self.machine)
        for runner, package in (("l1", "zfbench"), ("l0", "zfharness"), ("replay", "zfharness")):
            case = {"runner": runner, "profile": "release", "toolchain": "1.94.1", "features": [],
                    "platform": native_platform(self.system, self.machine)}
            manifest = manifest_fixture(system=self.system, machine=self.machine, package=package)
            verify_manifest(manifest, case, plan, environment)
            for binaries in ([], [{"name": "wrong", "sha256": HASH, "size_bytes": 1}],
                             [{"name": manifest["binaries"][0]["name"], "sha256": HASH, "size_bytes": 0}]):
                with self.subTest(runner=runner, binaries=binaries), self.assertRaisesRegex(EvidenceError, "binary inventory"):
                    verify_manifest({**manifest, "binaries": binaries}, case, plan, environment)

    def run_l1(self, *, materialize=True, exit_code=0, replace_after_run=False):
        from qa.run import run_case
        case = next(c for c in read_json(ROOT / "qa/policy/cases.json")["cases"] if c["runner"] == "l1")
        case.update(platform=native_platform(self.system, self.machine), case_instance_id=HASH)
        output = self.root / "evidence"
        calls = []
        binary = self.root / "target" / self.host / "release" / ("zfbench.exe" if self.system == "Windows" else "zfbench")
        def child(argv, root, log, timeout, env, lease):
            calls.append((argv, env.copy()))
            Path(log).write_text("fixture")
            if len(calls) == 1 and materialize:
                binary.parent.mkdir(parents=True); binary.write_bytes(b"built by this invocation")
            if len(calls) == 2 and replace_after_run:
                binary.write_bytes(b"different artifact after execution")
            return {"argv": argv, "status": "failed" if exit_code else "completed", "exit_code": exit_code,
                    "cleanup_status": "PASS"}
        def manifest(root, toolchain, profile, features, binaries, **kwargs):
            self.assertEqual(binaries, [binary])
            self.assertEqual(kwargs["build_env"]["BIN"], str(binary))
            return self.manifest
        with patch.dict(os.environ, {"BIN": "/old/zfbench", "CARGO_TARGET_DIR": "/old/target", "CARGO_BUILD_TARGET": self.host}), \
             patch("qa.run.capability_errors", return_value=[]), patch("qa.run.source_identity", return_value=self.manifest["source"]), \
             patch("qa.build_manifest.command", return_value=self.manifest["rustc"]), patch("qa.run.Lease", return_value=contextlib.nullcontext()), \
             patch("qa.run.run_child", side_effect=child), patch("qa.run.build_manifest", side_effect=manifest), \
             patch("qa.run.l1_evidence", return_value=("PASS", [], {})):
            result = run_case({"tested_sha": SHA, "toolchain": "1.94.1"}, case, self.root, output,
                              {"physical_domain": "fixture", "generation": "fixture"}, self.root / "leases")
        return result, calls, binary

    def test_l1_runs_and_records_the_same_controlled_build_artifact(self):
        result, calls, binary = self.run_l1()
        self.assertEqual(result["functional"]["status"], "PASS")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(env["BIN"] == str(binary) and env["CARGO_TARGET_DIR"] == str(self.root / "target") for _, env in calls))

    def test_missing_build_product_does_not_run_inherited_or_stale_binary(self):
        result, calls, _ = self.run_l1(materialize=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["functional"]["status"], "NOT_RUN")
        self.assertIn("runnable artifact", str(result["validity"]["reasons"]))
        self.assertTrue(read_json(self.root / "evidence/build-manifest.json")["identity_error"])

    def test_failed_build_remains_failed_and_never_launches_benchmark(self):
        result, calls, _ = self.run_l1(materialize=False, exit_code=7)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["execution"]["status"], "failed")
        self.assertEqual(result["execution"]["exit_code"], 7)
        self.assertNotEqual(result["functional"]["status"], "PASS")

    def test_changed_binary_after_execution_cannot_receive_valid_identity(self):
        result, calls, _ = self.run_l1(replace_after_run=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["validity"]["functional"], "UNKNOWN")
        self.assertIn("changed after", str(result["validity"]["reasons"]))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "command-0.log"

    def run_python(self, source, timeout=3):
        return run_child([sys.executable, "-c", source], self.root, self.output, timeout, os.environ.copy())

    def test_success_and_explicit_scope(self):
        result = self.run_python("print('complete')")
        self.assertEqual((result["status"], result["exit_code"], result["cleanup_status"]), ("completed", 0, "PASS"))
        expected = "linux-subreaper" if sys.platform.startswith("linux") else "windows-job" if os.name == "nt" else "process-group"
        self.assertEqual(result["cleanup_scope"], expected)
        self.assertEqual(bool(result["cleanup_limits"]), expected == "process-group")

    def test_nonzero(self):
        self.assertEqual(self.run_python("raise SystemExit(7)")["status"], "failed")

    def test_timeout_is_bounded(self):
        result = self.run_python("import time; time.sleep(30)", .1)
        self.assertEqual(result["status"], "timeout")
        self.assertLess(result["end_ns"] - result["start_ns"], 8_000_000_000)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper descendant ownership")
    def test_detached_descendant_causes_failure_and_is_reaped(self):
        source = ('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],'
                  'start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'
                  'print(p.pid,flush=True)')
        result = self.run_python(source)
        pid = int(self.output.read_text())
        try:
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["cleanup_status"], "PASS")
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_portable_process_group_leftover_records_failure_reason(self):
        if os.name == "nt":
            self.skipTest("POSIX process-group fallback")
        from qa.process import _portable_run
        source = ('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],'
                  'stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'
                  'print(p.pid,flush=True)')
        result = _portable_run([sys.executable, "-c", source], self.root, self.output, 3, os.environ.copy(), None)
        pid = int(self.output.read_text())
        try:
            self.assertEqual((result["status"], result["exit_code"]), ("failed", 0))
            self.assertIn("detached/background descendants detected", result["lifecycle_detail"])
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper descendant ownership")
    def test_exited_detached_descendant_is_not_a_leak(self):
        # Mirrors git's detached auto-maintenance: the orphan finishes before
        # the primary exits and is adopted only as a zombie.
        source = ('import os,time\n'
                  'if os.fork() == 0:\n'
                  '    os.setsid()\n'
                  '    if os.fork() == 0: os._exit(0)\n'
                  '    os._exit(0)\n'
                  'os.wait(); time.sleep(.3); print("done",flush=True)')
        result = self.run_python(source)
        self.assertEqual((result["status"], result["exit_code"], result["cleanup_status"]), ("completed", 0, "PASS"))
        self.assertEqual(result["lifecycle_detail"], "")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux subreaper isolation")
    def test_unrelated_child_is_never_reaped_or_killed(self):
        other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            result = self.run_python("print('owned child complete')")
            self.assertEqual(result["status"], "completed")
            self.assertIsNone(other.poll())
        finally:
            other.kill()
            other.wait(timeout=3)

    def test_separate_stderr_keeps_parsed_stdout_clean_for_descendants(self):
        source = ('import json,subprocess,sys\n'
                  'for i in range(1352): sys.stderr.write(f"  Downloaded crate-{i} v1.0.0\\n")\n'
                  'sys.stderr.flush()\n'
                  'subprocess.run([sys.executable,"-c","import sys;sys.stderr.write(\'grandchild warning\\\\n\')"],check=True)\n'
                  'print(json.dumps({"packages":[]}),flush=True)')
        errors = self.root / "command-0.stderr.log"
        result = run_child([sys.executable, "-c", source], self.root, self.output, 5, os.environ.copy(), stderr=errors)
        self.assertEqual((result["status"], result["exit_code"], result["cleanup_status"]), ("completed", 0, "PASS"))
        self.assertEqual(json.loads(self.output.read_text()), {"packages": []})
        text = errors.read_text()
        self.assertEqual(text.count("Downloaded"), 1352)
        self.assertIn("grandchild warning", text)

    def test_merged_output_is_unchanged_without_separate_stderr(self):
        result = self.run_python('import sys; sys.stderr.write("err\\n"); sys.stderr.flush(); print("out")')
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sorted(self.output.read_text().split()), ["err", "out"])

    def test_separate_stderr_must_be_a_distinct_fresh_file(self):
        with self.assertRaises(EvidenceError):
            run_child([sys.executable, "-c", "pass"], self.root, self.output, 3, os.environ.copy(), stderr=self.output)
        existing = self.root / "existing.stderr.log"
        existing.write_text("old")
        with self.assertRaises(FileExistsError):
            run_child([sys.executable, "-c", "pass"], self.root, self.root / "other.log", 3, os.environ.copy(), stderr=existing)
        self.assertEqual(existing.read_text(), "old")

    def test_output_budget_counts_both_streams(self):
        source = "import os; os.write(1,b'x'*(33*1024*1024)); os.write(2,b'y'*(33*1024*1024))"
        result = run_child([sys.executable, "-c", source], self.root, self.output, 5, os.environ.copy(),
                           stderr=self.root / "command-0.stderr.log")
        self.assertEqual(result["status"], "failed")
        self.assertIn("output byte budget exceeded", result["lifecycle_detail"])

    def test_output_budget_checked_even_if_child_already_exited(self):
        with patch("qa.process.MAX_OUTPUT", 16):
            # Linux uses an isolated supervisor with its own constants; its real
            # 64-MiB budget is exercised separately on Linux.
            if sys.platform.startswith("linux"):
                self.skipTest("supervisor budget is not inherited from mocks")
            result = self.run_python("print('x'*64)")
            self.assertEqual(result["status"], "failed")
            self.assertIn("output byte budget exceeded", result["lifecycle_detail"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "independent supervisor output budget")
    def test_linux_output_budget(self):
        result = self.run_python("import os; os.write(1,b'x'*(65*1024*1024))", timeout=5)
        self.assertEqual(result["status"], "failed")
        self.assertIn("output byte budget exceeded", result["lifecycle_detail"])


if __name__ == "__main__":
    unittest.main()
