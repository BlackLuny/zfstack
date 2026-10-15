import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa.common import EvidenceError, read_json, ROOT, write_json, digest, file_hash
from qa.integration import l1_evidence, l2_evidence, verify_integration, validate_worker_manifest, worker_identity
from qa.build_manifest import FLAGS
from qa.run import commands


def worker_manifest_fixture(case, tested_sha="1" * 40, binary_hash="a" * 64):
    """Synthetic complete worker identity; never real build qualification."""
    build = case["integration_build"]
    host = "x86_64-unknown-linux-gnu"
    toolchain = build["toolchain"]
    features = sorted(build["features"])
    refs = {"zfc": "2" * 40, "zfstack": tested_sha, "private_tun": "3" * 40, "shadow_tls": "4" * 40, "rig": "5" * 40}
    rustc = (f"rustc {toolchain}\nbinary: rustc\ncommit-hash: {'6' * 40}\ncommit-date: 2026-03-25\n"
             f"host: {host}\nrelease: {toolchain}\nLLVM version: 21.1.8")
    graph = "0zf-worker v0.0.1|" + ",".join(features) + "\n1zfstack v0.0.1|tokio"
    argv = ["cargo", "+" + toolchain, "build", "--locked", "-p", "zf-worker", "--bin", "zf-worker", "--profile", build["profile"]]
    if features:
        argv += ["--features", ",".join(features)]
    value = {"schema_version": 1, "status": "BUILT", "errors": [], "source_refs": refs, "zfstack_sha": tested_sha,
             "binary_sha256": binary_hash, "profile": build["profile"], "toolchain": toolchain, "features": features,
             "default_features": True, "lock_sha256": "7" * 64, "metadata_hash": "8" * 64,
             "build_info": {"source": {"commit": refs["zfc"]}, "private_tun": {"commit": refs["private_tun"]}},
             "commands": [{"argv": argv, "status": "PASS", "exit_code": 0, "cleanup": "PASS",
                           "execution": {"status": "completed", "exit_code": 0, "cleanup_status": "PASS"}}],
             "rustc_vv": rustc, "cargo_version": f"cargo {toolchain} (fixture)", "host": host, "target": host,
             "build_flags": {key: "" for key in FLAGS}, "dependency_graph": graph, "dependency_graph_hash": digest(graph)}
    value["runtime_id"] = digest({key: value[key] for key in ("rustc_vv", "cargo_version", "toolchain", "host", "target", "build_flags")})
    return value


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.cases = {c["runner"]: c for c in read_json(ROOT / "qa/policy/cases.json")["cases"]
                      if c["case_id"] in ("C11-l1", "C16-l2")}
        self.case = self.cases["l1"]
        self.raw = {"tool": "zfbench", "ok": True, "errors": [], "config": self.case["config"],
                    "results": {"test": "isolate", "layer": "L1", "wireguard": False,
                                "heavy": {"window_bytes": 1024, "window_wall_sec": 1},
                                **{phase: [{"user": i, "samples": {"n": 2}} for i in range(16)]
                                   for phase in ("baseline_users", "loaded_users")}}}

    def check(self):
        write_json(self.path / "l1.json", self.raw)
        return l1_evidence(self.case, self.path)[0]

    def test_real_l1_path_and_every_user_are_required(self):
        self.assertEqual(self.check(), "PASS")
        self.raw["results"]["loaded_users"][3]["samples"]["n"] = 0
        self.assertEqual(self.check(), "NOT_RUN")

    def test_bare_ok_is_not_l1_evidence(self):
        self.raw = {"ok": True}
        self.assertEqual(self.check(), "NOT_RUN")

    def test_wrong_path_and_configuration_are_rejected(self):
        self.raw["results"]["wireguard"] = True
        self.assertEqual(self.check(), "NOT_RUN")
        self.raw["results"]["wireguard"] = False
        self.raw["config"] = {**self.raw["config"], "ports": 1}
        self.assertEqual(self.check(), "NOT_RUN")

    def test_observed_failure_survives_missing_evidence(self):
        self.raw = {"ok": False, "errors": ["payload mismatch"]}
        self.assertEqual(self.check(), "FAIL")

    def test_self_reported_l1_pass_is_recomputed(self):
        self.raw["results"]["loaded_users"] = []
        self.check()
        with self.assertRaises(EvidenceError):
            verify_integration({"functional": {"status": "PASS"}}, self.case, self.path)

    def test_l1_command_is_isolated_and_serial_build(self):
        rows = commands(self.case, "1.94.1", ROOT, self.path)
        self.assertIn("--locked", rows[0])
        self.assertEqual(rows[1][:4], ["bash", "qa/isolate-linux.sh", "sh", "bench/run-isolate.sh"])
        self.assertIn(str(self.path / "l1.json"), rows[1])

    def test_worker_build_identity_is_frozen_and_shared_by_live_admission(self):
        case = {**self.cases["l2"], "platform": "linux-x86_64"}
        binary = self.path / "worker"; binary.write_bytes(b"fixture")
        worker = worker_manifest_fixture(case, binary_hash=file_hash(binary))
        self.assertEqual(validate_worker_manifest(worker, case, "1" * 40, binary=binary), worker)
        write_json(self.path / "worker-manifest.json", worker)
        self.assertEqual(worker_identity({"worker_manifest": str(self.path / "worker-manifest.json"), "worker_binary": str(binary)}, "1" * 40, case), worker)
        binary.write_bytes(b"changed")
        with self.assertRaises(EvidenceError):
            validate_worker_manifest(worker, case, "1" * 40, binary=binary)

    def test_unbuildable_wrong_profile_and_missing_cleanup_workers_are_rejected(self):
        case = {**self.cases["l2"], "platform": "linux-x86_64"}
        original = worker_manifest_fixture(case)
        changes = (("status", "UNBUILDABLE"), ("errors", ["fakectl build failed"]), ("profile", "dev"),
                   ("toolchain", "wrong-toolchain"), ("features", []), ("runtime_id", "not-a-hash"),
                   ("metadata_hash", "not-a-hash"), ("commands", []), ("default_features", False))
        for key, value in changes:
            worker = copy.deepcopy(original); worker[key] = value
            with self.subTest(key=key), self.assertRaises(EvidenceError):
                validate_worker_manifest(worker, case, "1" * 40)
        for field, value in (("status", "FAILED"), ("exit_code", 1), ("cleanup", "UNKNOWN")):
            worker = copy.deepcopy(original); worker["commands"][0][field] = value
            with self.subTest(field=field), self.assertRaises(EvidenceError):
                validate_worker_manifest(worker, case, "1" * 40)

    def test_worker_source_graph_and_compiler_fingerprints_are_cross_checked(self):
        case = {**self.cases["l2"], "platform": "linux-x86_64"}
        original = worker_manifest_fixture(case)
        for change in (lambda w: w["build_info"]["source"].update(commit="9" * 40),
                       lambda w: w["source_refs"].update(private_tun="9" * 40),
                       lambda w: w.update(dependency_graph="0different v0.0.1|"),
                       lambda w: w["build_flags"].update(RUSTFLAGS="unrecorded"),
                       lambda w: w["commands"][0].update(argv=["true"])):
            worker = copy.deepcopy(original); change(worker)
            with self.subTest(change=change), self.assertRaises(EvidenceError):
                validate_worker_manifest(worker, case, "1" * 40)

    def test_l2_all_workload_fields_and_executed_binary_are_checked(self):
        case = self.cases["l2"]
        (self.path / "rig").mkdir()
        write_json(self.path / "rig/l2-result.json", {})
        inspected = {"config": {**case["config"], "worker_tuning_fingerprints": ["port1", "port2"]},
                     "functional_evidence_errors": [], "functional_errors": [], "performance_errors": [],
                     "binary_sha256": "abc", "result": {}, "metrics": {}}
        with patch("qa.integration._l2_module") as module:
            module.return_value.inspect_case.return_value = inspected
            self.assertEqual(l2_evidence(case, self.path, {"binary_sha256": "abc"})[0], "PASS")
            inspected["config"]["light_conns"] += 1
            self.assertEqual(l2_evidence(case, self.path, {"binary_sha256": "abc"})[0], "NOT_RUN")
            inspected["functional_errors"] = ["verified UDP request lost"]
            self.assertEqual(l2_evidence(case, self.path, {"binary_sha256": "wrong"})[0], "FAIL")


if __name__ == "__main__":
    unittest.main()
