from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import time
from unittest.mock import patch
from pathlib import Path

from qa.lease import Lease
from qa.common import EvidenceError, digest, read_json, write_json
from qa.crossrepo import BUILD_FLAGS, COMBINATIONS, _run, archive_matrix, build_matrix, export_commit, resolve_ref, validate_matrix


@unittest.skipUnless(os.name == "posix", "cross-repository build helpers require the designated POSIX build node")
class CrossRepoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.node = {"physical_domain": "fixture-build-domain", "generation": "fixture-generation"}
        self.leases = self.root / "leases"
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.name", "QA")
        self.git("config", "user.email", "qa@example.invalid")
        (self.repo / "Cargo.toml").write_text('[package]\nname="fixture"\nversion="0.1.0"\nedition="2021"\n')
        (self.repo / "Cargo.lock").write_text('version=4\n')
        (self.repo / "src").mkdir()
        (self.repo / "src/lib.rs").write_text('pub const VALUE:u8=1;\n')
        (self.repo / "production.key").write_text('SECRET_SENTINEL_MUST_NOT_COPY\n')
        (self.repo / "rig.env").write_text('TOKEN=SECRET_SENTINEL\n')
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixed source")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.repos = {name: self.repo for name in ("zfc", "zfstack", "private_tun", "shadow_tls", "rig")}
        self.refs = {name: (kind, self.sha) for name, kind in (
            ("zfc_base", "zfc"), ("zfc_head", "zfc"), ("zfstack_base", "zfstack"), ("zfstack_head", "zfstack"),
            ("private_tun", "private_tun"), ("shadow_tls", "shadow_tls"), ("rig", "rig"))}

    def build(self, *args, **kwargs):
        return build_matrix(*args, node=self.node, lease_dir=self.leases, **kwargs)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True).stdout

    def test_dirty_source_and_tracked_secrets_are_not_exported(self):
        (self.repo / "src/lib.rs").write_text('DIRTY_EDIT\n')
        (self.repo / "src/untracked.rs").write_text('UNTRACKED_EDIT\n')
        out = self.root / "export"
        identity = export_commit(self.repo, self.sha, out, "zfc")
        self.assertEqual((out / "src/lib.rs").read_text(), 'pub const VALUE:u8=1;\n')
        self.assertFalse((out / "src/untracked.rs").exists())
        self.assertFalse((out / "production.key").exists())
        self.assertFalse((out / "rig.env").exists())
        self.assertEqual(identity["sha"], self.sha)
        # Excluded secret blob is absent even from the exported Git object DB.
        blob = self.git("rev-parse", "HEAD:production.key").strip()
        found = subprocess.run(["git", "-C", str(out), "cat-file", "-e", blob], capture_output=True)
        self.assertNotEqual(found.returncode, 0)
        self.assertEqual(subprocess.check_output(["git", "-C", str(out), "status", "--porcelain"]), b"")
        with self.assertRaises(FileExistsError):
            export_commit(self.repo, self.sha, out, "zfc")

    def test_missing_ref_is_recorded_for_affected_combinations(self):
        self.refs["zfstack_head"] = ("zfstack", "does-not-exist")
        result = archive_matrix(self.repos, self.refs, self.root / "archive")
        statuses = {row["combination"]: row["status"] for row in result["combinations"]}
        self.assertEqual(statuses, {"base-base": "PREPARED", "base-head": "UNBUILDABLE", "head-base": "PREPARED", "head-head": "UNBUILDABLE"})
        self.assertIn("zfstack_head", result["ref_errors"])
        validate_matrix(result)
        with self.assertRaises(EvidenceError):
            resolve_ref(self.repo, "does-not-exist")

    def test_all_combinations_and_same_private_tun_are_required(self):
        result = archive_matrix(self.repos, self.refs, self.root / "archive")
        self.assertEqual(tuple(row["combination"] for row in result["combinations"]), COMBINATIONS)
        bad = copy.deepcopy(result)
        bad["combinations"].pop()
        with self.assertRaises(EvidenceError):
            validate_matrix(bad)
        bad = copy.deepcopy(result)
        bad["combinations"][0]["source_refs"]["private_tun"] = "f" * 40
        with self.assertRaises(EvidenceError):
            validate_matrix(bad)
        self.assertEqual((self.repo / "Cargo.toml").read_text().count("patch"), 0)
        self.assertIn('../zfstack', (self.root / "archive/base-base/repos/zfc/Cargo.toml").read_text())

    def test_failed_runtime_records_all_arms_without_modifying_archive(self):
        archive = self.root / "archive"
        plan = archive_matrix(self.repos, self.refs, archive)
        original = (archive / "base-base/repos/zfc/Cargo.lock").read_bytes()
        results = self.build(archive, self.root / "build", rustc="/usr/bin/false", minimum_free=0, fakectl=False)
        self.assertEqual(len(results["combinations"]), 4)
        self.assertTrue(all(row["status"] == "UNBUILDABLE" for row in results["combinations"]))
        for name in COMBINATIONS:
            evidence = read_json(self.root / "build" / name / "worker-manifest.json")
            self.assertEqual(evidence["commands"][0]["exit_code"], 1)
            self.assertTrue(evidence["errors"])
        self.assertEqual((archive / "base-base/repos/zfc/Cargo.lock").read_bytes(), original)
        single = self.build(archive, self.root / "single", rustc="/usr/bin/false", minimum_free=0,
                              fakectl=False, cells=["head-head"])
        self.assertEqual([row["status"] for row in single["combinations"]], ["UNEXECUTED"] * 3 + ["UNBUILDABLE"])
        self.assertEqual(single["factorial_status"], "FAIL")
        plan["combinations"].pop()
        plan.pop("archive_hash")
        plan["archive_hash"] = digest(plan)
        write_json(archive / "archive-manifest.json", plan)
        with self.assertRaises(EvidenceError):
            self.build(archive, self.root / "other", minimum_free=0)

    def test_symlink_build_input_is_refused(self):
        (self.repo / "src/alias.rs").symlink_to("/etc/passwd")
        self.git("add", "src/alias.rs")
        self.git("commit", "--quiet", "-m", "unsafe link")
        sha = self.git("rev-parse", "HEAD").strip()
        with self.assertRaises(EvidenceError):
            export_commit(self.repo, sha, self.root / "unsafe", "zfc")

    def test_successful_build_manifest_and_added_source_rejection(self):
        archive = self.root / "archive"
        archive_matrix(self.repos, self.refs, archive)
        # Exercise the build orchestrator with deterministic fake compiler tools.
        # The production CLI still invokes real cargo and reads real build-info.
        rustc = self.root / "rustc"
        rustc.write_text("#!/bin/sh\nprintf 'rustc fixture\\nhost: x86_64-unknown-linux-gnu\\n'\n")
        rustc.chmod(0o755)
        cargo = self.root / "cargo"
        build_info = {"source": {"commit": self.sha}, "private_tun": {"commit": self.sha}}
        worker_script = f"#!{sys.executable}\nprint({json.dumps(build_info)!r})\n"
        cargo.write_text(f'''#!{sys.executable}
import json,os,pathlib,sys
args=sys.argv[2:]
if args == ['-V']:
    print('cargo fixture')
elif args[0] == 'metadata':
    print(json.dumps({{'packages':[{{'name':name,'manifest_path':str(pathlib.Path.cwd().parent/name/'Cargo.toml'),'source':None}} for name in ('zfstack','private_tun')]}}))
elif args[0] == 'tree':
    print('0'+args[args.index('-p')+1]+' v1.0.0|default,mmsg,jemalloc')
elif args[0] == 'build':
    binary=pathlib.Path(os.environ['CARGO_TARGET_DIR'])/'fast-release'/args[args.index('--bin')+1]
    binary.parent.mkdir(parents=True,exist_ok=True)
    binary.write_text({worker_script!r})
    binary.chmod(0o755)
''')
        cargo.chmod(0o755)
        (archive / "base-base/repos/zfc/src/injected.rs").write_text("unrecorded source")
        result = self.build(archive, self.root / "build", cargo=str(cargo), rustc=str(rustc), minimum_free=0, fakectl=False)
        rows = result["combinations"]
        self.assertEqual(rows[0]["status"], "UNBUILDABLE")
        self.assertIn("inventory", rows[0]["errors"][0])
        for row in rows[1:]:
            self.assertEqual(row["status"], "BUILT", row["errors"])
            self.assertEqual(row["zfstack_sha"], self.sha)
            self.assertEqual(len(row["binary_sha256"]), 64)
            self.assertEqual(len(row["lock_sha256"]), 64)
            self.assertTrue(row["runtime_id"])
            self.assertEqual(row["dependency_graph_hash"], digest(row["dependency_graph"]))
            self.assertEqual(set(row["build_flags"]), set(BUILD_FLAGS))
            self.assertEqual(row["host"], row["target"])
            self.assertTrue(all(command["cleanup"] == "PASS" for command in row["commands"]))
            self.assertEqual(row["build_info"]["source"]["commit"], self.sha)
            self.assertFalse(row["performance_eligible"])
        single = self.build(archive, self.root / "single", cargo=str(cargo), rustc=str(rustc),
                              minimum_free=0, fakectl=False, cells=["head-head"], cache_dir=self.root / "cargo-cache")
        self.assertEqual(single["selected_status"], "PASS")
        self.assertEqual(single["build_lease"]["domain"], self.node["physical_domain"])
        self.assertEqual(single["build_lease"]["generation"], self.node["generation"])
        self.assertEqual(single["build_lease"]["release_status"], "PASS")
        self.assertTrue(all(row["build_lease"] == single["build_lease"] for row in single["combinations"]))
        self.assertFalse((self.leases / (digest(self.node["physical_domain"]) + ".json")).exists())
        self.assertEqual(single["factorial_status"], "INCOMPLETE")
        self.assertEqual([row["status"] for row in single["combinations"]], ["UNEXECUTED"] * 3 + ["BUILT"])
        self.assertEqual(single["combinations"][-1]["target_directory"], str((self.root / "cargo-cache/worker").resolve()))
        self.assertTrue((self.root / "single/head-head/zf-worker").is_file())
        relay = self.build(archive, self.root / "relay", cargo=str(cargo), rustc=str(rustc),
                             minimum_free=0, fakectl=False, forwarder=True, cells=["head-head"])
        row = relay["combinations"][-1]
        self.assertEqual(row["status"], "BUILT", row["errors"])
        self.assertTrue((self.root / "relay/head-head/forwarder-server").is_file())
        self.assertEqual(row["forwarder"]["build_info"], build_info)
        self.assertEqual(row["forwarder"]["dependency_graph_hash"], digest(row["forwarder"]["dependency_graph"]))
        self.assertNotIn("forwarder", single["combinations"][-1])
        with self.assertRaisesRegex(EvidenceError, "separate"):
            self.build(archive, self.root / "bad-cache", cache_dir=archive / "cache", minimum_free=0)


    def test_cold_cache_stderr_progress_never_reaches_parsed_stdout(self):
        # A fresh Cargo cache prints download progress and warnings on stderr
        # before the JSON on stdout; the Canada build merged both and failed.
        archive = self.root / "archive"
        archive_matrix(self.repos, self.refs, archive)
        rustc = self.root / "rustc"
        rustc.write_text("#!/bin/sh\necho 'warning: toolchain notice' >&2\nprintf 'rustc fixture\\nhost: x86_64-unknown-linux-gnu\\n'\n")
        rustc.chmod(0o755)
        build_info = {"source": {"commit": self.sha}, "private_tun": {"commit": self.sha}}
        worker_script = f"#!{sys.executable}\nimport sys\nsys.stderr.write('warning: build-info noise\\n')\nprint({json.dumps(build_info)!r})\n"
        cargo = self.root / "cargo"
        cargo.write_text(f'''#!{sys.executable}
import json,os,pathlib,sys
args=sys.argv[2:]
sys.stderr.write('warning: unused manifest key\\n')
if args == ['-V']:
    print('cargo fixture')
elif args[0] == 'metadata':
    for index in range(1352):
        sys.stderr.write(f'  Downloaded crate-{{index}} v1.0.0\\n')
    sys.stderr.flush()
    print(json.dumps({{'packages':[{{'name':name,'manifest_path':str(pathlib.Path.cwd().parent/name/'Cargo.toml'),'source':None}} for name in ('zfstack','private_tun')]}}))
elif args[0] == 'tree':
    print('0'+args[args.index('-p')+1]+' v1.0.0|default,mmsg,jemalloc')
elif args[0] == 'build':
    binary=pathlib.Path(os.environ['CARGO_TARGET_DIR'])/'fast-release'/args[args.index('--bin')+1]
    binary.parent.mkdir(parents=True,exist_ok=True)
    binary.write_text({worker_script!r})
    binary.chmod(0o755)
''')
        cargo.chmod(0o755)
        result = self.build(archive, self.root / "cold", cargo=str(cargo), rustc=str(rustc), minimum_free=0,
                            fakectl=False, forwarder=True, cells=["head-head"])
        row = result["combinations"][-1]
        self.assertEqual(row["status"], "BUILT", row["errors"])
        self.assertNotIn("warning", row["rustc_vv"] + row["cargo_version"] + row["dependency_graph"])
        self.assertEqual(row["build_info"], build_info)
        self.assertEqual(row["forwarder"]["build_info"], build_info)
        out = self.root / "cold/head-head"
        meta = next(c for c in row["commands"] if "metadata" in c["argv"])
        self.assertEqual(read_json(out / meta["log"])["packages"][0]["name"], "zfstack")
        progress = (out / meta["stderr_log"]).read_text().splitlines()
        self.assertEqual(sum(line.strip().startswith("Downloaded") for line in progress), 1352)
        parsed = [c for c in row["commands"] if "stderr_log" in c]
        self.assertEqual(len(parsed), 7)
        self.assertTrue(all((out / c["stderr_log"]).read_text().startswith("warning") for c in parsed))
        unparsed = [c for c in row["commands"] if "build" in c["argv"] or "update" in c["argv"]]
        self.assertTrue(unparsed and all("stderr_log" not in c for c in unparsed))
        self.assertTrue(all("warning" in (out / c["log"]).read_text() for c in unparsed))

    def test_public_build_requires_shared_physical_domain_and_generation(self):
        for node, directory in ((None, self.leases), ({"physical_domain": "only-domain"}, self.leases),
                                ({"physical_domain": "", "generation": "g"}, self.leases), (self.node, None), (self.node, Path("relative"))):
            with self.subTest(node=node, directory=directory), self.assertRaises(EvidenceError):
                build_matrix(self.root / "absent-archive", self.root / "no-build", node=node, lease_dir=directory)
        self.assertFalse((self.root / "no-build").exists())

    def test_measurement_lease_blocks_every_compiler_before_execution(self):
        archive = self.root / "archive"
        archive_matrix(self.repos, self.refs, archive)
        marker = self.root / "compiler-was-started"
        compiler = self.root / "compiler"
        compiler.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('wrong')\n")
        compiler.chmod(0o755)
        with Lease(self.leases, self.node["physical_domain"], "measurement", self.node["generation"]) as measurement:
            before = measurement.path.read_bytes()
            with self.assertRaisesRegex(EvidenceError, "already leased"):
                self.build(archive, self.root / "blocked", rustc=str(compiler), minimum_free=0, fakectl=False)
            measurement.check()
            self.assertEqual(measurement.path.read_bytes(), before)
            self.assertFalse(marker.exists())
            self.assertFalse((self.root / "blocked").exists())

    def test_lost_lease_cancels_compiler_and_seals_failed_cleanup_evidence(self):
        archive = self.root / "archive"
        archive_matrix(self.repos, self.refs, archive)
        lease_path = self.leases / (digest(self.node["physical_domain"]) + ".json")
        marker, pid_file = self.root / "compiler-finished", self.root / "compiler.pid"
        compiler = self.root / "losing-compiler"
        compiler.write_text(f"#!{sys.executable}\nimport json,os,time\nfrom pathlib import Path\n"
                           f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                           # Replace atomically: the lease watcher must observe the new owner, never a torn write.
                           f"p=Path({str(lease_path)!r}); value=json.loads(p.read_text()); value['generation']='replacement'\n"
                           f"t=p.with_name(p.name+'.tmp'); t.write_text(json.dumps(value)); os.replace(t,p)\n"
                           f"time.sleep(20)\nPath({str(marker)!r}).write_text('must not finish')\n")
        compiler.chmod(0o755)
        start = time.monotonic()
        result = self.build(archive, self.root / "lost", rustc=str(compiler), minimum_free=0, fakectl=False)
        self.assertLess(time.monotonic() - start, 12)
        self.assertEqual((result["selected_status"], result["factorial_status"], result["build_lease"]["release_status"]), ("FAIL", "FAIL", "LOST"))
        self.assertTrue(all(entry["status"] == "UNBUILDABLE" for entry in result["combinations"]))
        first = read_json(self.root / "lost/base-base/worker-manifest.json")
        self.assertEqual(first["commands"][0]["status"], "FAILED")
        self.assertEqual(first["commands"][0]["execution"]["status"], "cancelled")
        self.assertEqual(first["commands"][0]["cleanup"], "PASS")
        self.assertIn("generation changed", str(first["errors"]))
        self.assertTrue(all(not entry["commands"] for entry in result["combinations"][1:]))
        self.assertFalse(marker.exists())
        with self.assertRaises(ProcessLookupError): os.kill(int(pid_file.read_text()), 0)
        self.assertEqual(read_json(lease_path)["generation"], "replacement")  # Never remove a foreign lock.
        self.assertTrue(lease_path.with_suffix(".quarantine").is_file())

    def test_disk_reserve_remains_enforced_under_shared_lease(self):
        marker = self.root / "disk-compiler-was-started"
        with Lease(self.leases, self.node["physical_domain"], "disk-test", self.node["generation"]) as lease:
            with patch("qa.crossrepo.shutil.disk_usage", return_value=type("Usage", (), {"free": 0})()):
                with self.assertRaisesRegex(EvidenceError, "disk reserve"):
                    _run([sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"], self.root,
                         self.root / "disk.log", os.environ.copy(), 3, 1, lease=lease)
            lease.check()
        self.assertFalse(marker.exists())

    def test_later_arm_interrupt_retains_pending_prior_command_cleanup(self):
        archive = self.root / "archive"
        archive_matrix(self.repos, self.refs, archive)
        original, calls = _run, []
        def interrupt_later_arm(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise KeyboardInterrupt("cancel second arm")
            return original(*args, **kwargs)
        with patch("qa.crossrepo._run", side_effect=interrupt_later_arm):
            with self.assertRaises(KeyboardInterrupt):
                self.build(archive, self.root / "interrupted", rustc="/usr/bin/false", minimum_free=0, fakectl=False)
        progress = read_json(self.root / "interrupted/base-base/build-progress.json")
        self.assertEqual((progress["status"], progress["build_lease"]["release_status"]), ("PENDING", "PENDING"))
        self.assertEqual(progress["commands"][0]["status"], "FAILED")
        self.assertEqual(progress["commands"][0]["cleanup"], "PASS")
        self.assertEqual(progress["commands"][0]["execution"]["exit_code"], 1)
        self.assertTrue(progress["errors"])
        self.assertFalse((self.root / "interrupted/base-base/worker-manifest.json").exists())
        self.assertFalse((self.root / "interrupted/build-matrix.json").exists())
        self.assertFalse((self.leases / (digest(self.node["physical_domain"]) + ".json")).exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux build descendant containment")
    def test_successful_parent_with_detached_compiler_is_not_a_successful_build(self):
        code = ('import subprocess,sys; child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],'
                'start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'
                'print(child.pid,flush=True)')
        log = self.root / "detached-build.log"
        with Lease(self.leases, self.node["physical_domain"], "test-detached", self.node["generation"]) as lease:
            record = _run([sys.executable, "-c", code], self.root, log, os.environ.copy(), 3, 0, lease=lease)
        self.assertEqual((record["status"], record["exit_code"], record["cleanup"]), ("FAILED", 0, "PASS"))
        self.assertEqual(record["execution"]["cleanup_scope"], "linux-subreaper")
        with self.assertRaises(ProcessLookupError):
            os.kill(int(log.read_text().strip()), 0)


if __name__ == "__main__":
    unittest.main()
