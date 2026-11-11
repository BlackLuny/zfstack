"""Frozen paired allocation, raw validity and conservative missing-result inference."""
import contextlib
import importlib.util
import io
import itertools
import json
import math
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

DRIVER = Path(__file__).parent / "results/2026-10-17-order-rate/order_rate.py"
spec = importlib.util.spec_from_file_location("order_rate", DRIVER)
rate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rate)
ARCHIVE = DRIVER.parent.parent / "2026-10-17-netem-fifo-canada192-r2/collected-artifacts.tar.gz"


def row(arm, overtaken, number=1, usable=True):
    return {"arm": arm, "round": number, "validity": {"valid": usable},
            "receiver": {"received": rate.SENT, "unique": rate.SENT, "duplicates": 0,
                         "overtaken": overtaken, "exact_sequences": True, "payload_intact": True}}


def experiment(f_hits=(), p_hits=(), unknown=()):
    return [row(arm, int(n in (f_hits if arm == "F" else p_hits)), n, (n, arm) not in unknown)
            for n, arm in rate.schedule()]


def child(code=0, **kw):
    return {"status": "failed" if code else "completed", "exit_code": code,
            "cleanup_status": "PASS", "cleanup_scope": "linux-subreaper", "lifecycle_detail": "", **kw}


def unpack_fixture(directory, name, label):
    """Read retained raw evidence for offline regression only; no measurement/regrade."""
    target = Path(directory) / label
    with tarfile.open(ARCHIVE) as archive:
        for member in archive.getmembers():
            if member.name == f"gate/{name}.json":
                target.with_suffix(".json").write_bytes(archive.extractfile(member).read())
            elif member.isfile() and member.name.startswith(f"gate/{name}/"):
                filename = Path(member.name).name
                target.mkdir(exist_ok=True)
                (target / filename).write_bytes(archive.extractfile(member).read())
    return target.with_suffix(".json")


class OrderRateTests(unittest.TestCase):
    def test_schedule_strictly_alternates_and_is_balanced(self):
        # Retain the inventory's test identity; v2 randomizes the first arm of each pair.
        order = rate.schedule()
        self.assertEqual(len(order), 2 * rate.RUNS)
        self.assertEqual(order[:6], [(1, "F"), (1, "P"), (2, "F"), (2, "P"), (3, "P"), (3, "F")])
        self.assertEqual(sum(arm == "F" for _, arm in order), 30)
        self.assertEqual(sum(arm == "F" for _, arm in order[::2]), 15)
        self.assertEqual(order, rate.schedule())
        allocation = json.loads((DRIVER.parent / "allocation.json").read_text())
        self.assertEqual(allocation, {"plan_version": rate.PLAN_VERSION, "seed": rate.SEED,
                                      "order": [list(pair) for pair in order]})
        for n in range(30):
            self.assertEqual({arm for _, arm in order[2*n:2*n+2]}, {"F", "P"})
            self.assertEqual([i for i, _ in order[2*n:2*n+2]], [n+1, n+1])

    def test_fisher_and_intervals(self):
        self.assertEqual(rate.fisher_greater(2, 10, 0, 6), .375)
        self.assertAlmostEqual(rate.fisher_greater(6, 30, 0, 30), 593775 / 50063860)
        self.assertEqual(rate.fisher_greater(0, 0, 0, 0), 1.0)
        self.assertEqual(rate.clopper_pearson(0, 30), [0.0, .1157])
        self.assertEqual(rate.clopper_pearson(6, 30), [.0771, .3857])
        self.assertEqual(rate.paired_p(0, 0), 1.)
        self.assertEqual(rate.paired_p(6, 0), 1 / 64)
        self.assertEqual(rate.paired_p(4, 0), 1 / 16)
        self.assertEqual(rate.paired_p(5, 0), 1 / 32)
        self.assertEqual(rate.paired_p(5, 5), sum(math.comb(10, k) for k in range(5, 11)) / 1024)

    def test_receiver_counts_overtakes_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receiver.json"
            path.write_text(json.dumps({"udp": [[0, 1, 2, True], [2, 1, 2, True], [1, 1, 2, True], [2, 1, 2, True]]}))
            stats = rate.receiver_stats(path)
            self.assertEqual((stats["received"], stats["unique"], stats["duplicates"], stats["overtaken"]), (4, 3, 1, 1))
            self.assertFalse(rate.valid(stats))
            self.assertIsNone(rate.receiver_stats(Path(directory) / "missing.json"))

    def test_exact_set_and_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receiver.json"
            good = [[n, n + 1, rate.SENT + n, True] for n in range(rate.SENT)]
            path.write_text(json.dumps({"udp": good}))
            self.assertTrue(rate.valid(rate.receiver_stats(path)))
            bad_rows = [[], [True, 1, 2, True], [0, 1, 2, 1], [0, 2, 1, True], [0, 1.0, 2, True],
                        [rate.SENT, 1, 2, True], [-1, 1, 2, True], "broken"]
            for bad in bad_rows:
                with self.subTest(row=bad):
                    path.write_text(json.dumps({"udp": [bad]}))
                    self.assertIsNone(rate.receiver_stats(path))
            good[0][0] = rate.SENT  # all 18475 unique, but wrong set
            path.write_text(json.dumps({"udp": good}))
            self.assertIsNone(rate.receiver_stats(path))
            good[0][0], good[0][3] = 0, False
            path.write_text(json.dumps({"udp": good}))
            self.assertFalse(rate.valid(rate.receiver_stats(path)))
            for obj in ([], {}, {"udp": None}, {"udp": {"0": []}}):
                path.write_text(json.dumps(obj))
                self.assertIsNone(rate.receiver_stats(path))

    def test_decision_rules(self):
        self.assertEqual(rate.decide(experiment(range(1, 7)))["verdict"], "F_HIGHER")
        self.assertEqual(rate.decide(experiment([1, 2], [1]))["verdict"], "NOT_SHOWN_P_OBSERVED")
        self.assertEqual(rate.decide(experiment([1]))["verdict"], "NOT_SHOWN")
        self.assertEqual(rate.decide(experiment(range(1, 7)), aborted="cleanup unknown")["verdict"], "INCONCLUSIVE")
        self.assertEqual(rate.decide(experiment()[:2], runs=1)["verdict"], "INCONCLUSIVE")
        for rows in (experiment()[:-1], experiment() + experiment()[:1], experiment()[::-1]):
            self.assertEqual(rate.decide(rows)["verdict"], "INCONCLUSIVE")

    def test_missing_result_sensitivity_prevents_selection(self):
        rows = experiment(range(1, 6), unknown=[(n, "P") for n in range(26, 31)])
        result = rate.decide(rows)
        self.assertEqual(result["paired_one_sided_p"], 1/32)
        self.assertGreater(result["worst_case_p"], .05)
        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertEqual(result["arms"]["P"]["unknown"], 5)
        # A strong difference may survive missing data, but fewer than 25 valid may not.
        self.assertEqual(rate.decide(experiment(range(1, 21), unknown=[(30, "P")]))["verdict"], "F_HIGHER")
        self.assertEqual(rate.decide(experiment(range(1, 21), unknown=[(n, "P") for n in range(25, 31)]))["verdict"], "INCONCLUSIVE")
        self.assertEqual(rate.decide(experiment(unknown=[(30, "F")]))["verdict"], "INCONCLUSIVE")

    def test_worst_case_bounds_all_missing_assignments(self):
        rows = experiment([1, 2, 3, 4, 5], [2], unknown=[(1, "F"), (6, "F"), (3, "P"), (7, "P")])
        worst = rate.decide(rows)["worst_case_p"]
        missing = [r for r in rows if not r["validity"]["valid"]]
        for values in itertools.product((False, True), repeat=len(missing)):
            resolved = json.loads(json.dumps(rows))
            for source, hit in zip(missing, values):
                r = next(r for r in resolved if (r["round"], r["arm"]) == (source["round"], source["arm"]))
                r["validity"]["valid"], r["receiver"]["overtaken"] = True, int(hit)
            self.assertLessEqual(rate.decide(resolved)["paired_one_sided_p"], worst)

    def test_retained_raw_evidence_order_only_failure_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            f = unpack_fixture(directory, "full", "F-01")
            p = unpack_fixture(directory, "control-1", "P-01")
            result = rate.assess(f, "F", child(1))
            self.assertTrue(result["validity"]["valid"], result)
            self.assertEqual(result["receiver"]["overtaken"], 1)
            self.assertTrue(rate.assess(p, "P", child())["validity"]["valid"])
            summary = json.loads(f.read_text())
            summary["ok"], summary["checks"] = True, {"invented": True}
            f.write_text(json.dumps(summary))
            self.assertTrue(rate.assess(f, "F", child(1))["validity"]["valid"])
            self.assertFalse(rate.assess(f, "F", child())["validity"]["valid"])
            self.assertFalse(rate.assess(f, "F", child(1, lifecycle_detail="detached/background descendants detected"))["validity"]["valid"])

    def test_complete_receiver_cannot_override_failure_or_bad_treatment(self):
        with tempfile.TemporaryDirectory() as directory:
            p = unpack_fixture(directory, "control-1", "P-01")
            for life in (child(124, status="timeout"), child(1), child(cleanup_status="UNKNOWN")):
                self.assertFalse(rate.assess(p, "P", life)["validity"]["valid"])
            summary = json.loads(p.read_text())
            summary["actual_ns"]["drain"] = [1, 2]
            p.write_text(json.dumps(summary))
            result = rate.assess(p, "P", child())
            self.assertFalse(result["validity"]["valid"])
            self.assertIn("treatment_commands_completed", result["validity"]["reasons"])
            # A malformed receiver is recorded as unknown; it must not crash analysis.
            (p.with_suffix("") / "receiver.json").write_text('{"udp":[[]]}')
            self.assertFalse(rate.assess(p, "P", child())["validity"]["valid"])

    def test_cleanup_failure_or_missing_full_evidence_is_unsafe(self):
        with tempfile.TemporaryDirectory() as directory:
            f = unpack_fixture(directory, "full", "F-01")
            path = f.with_suffix("") / "queue-control.json"
            original = path.read_text()
            report = json.loads(original)
            report["cleanup"] = "FAIL"
            path.write_text(json.dumps(report))
            result = rate.assess(f, "F", child(1))
            self.assertFalse(result["cleanup_safe"])
            self.assertFalse(result["validity"]["valid"])
            path.unlink()
            self.assertFalse(rate.assess(f, "F", child(1))["cleanup_safe"])
            path.write_text(original)
            ledger = f.with_suffix("") / "queue-ledger.bin.gz"
            ledger.write_bytes(ledger.read_bytes()[:-8])
            result = rate.assess(f, "F", child(1))
            self.assertFalse(result["cleanup_safe"])
            self.assertFalse(result["validity"]["valid"])

    def test_host_checks_do_not_accept_restarted_services(self):
        with patch.object(rate.subprocess, "check_output", side_effect=["inactive\n", "active\n", ""]):
            self.assertFalse(rate.host_state()["quiet"])
        with patch.object(rate.subprocess, "check_output", side_effect=["inactive\n", "inactive\n", "rig-watch-ab.scope loaded active running"]):
            self.assertFalse(rate.host_state()["quiet"])

    def run_mock_sequence(self, directory, *, cleanup="PASS", host_after=True, runner_error=False):
        # Exercise orchestration without namespaces, traffic, SSH or service changes.
        import qa.process
        output = Path(directory) / "experiment"
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if str(path).startswith("/proc/"):
                return "test-boot\n" if path.name == "boot_id" else "test-counters\n"
            return original_read(path, *args, **kwargs)

        def run(argv, root, log, timeout, env, lease):
            if runner_error:
                raise KeyboardInterrupt("test cancellation")
            target = Path(argv[-1])
            arm = target.stem[0]
            unpack_fixture(output, "full" if arm == "F" else "control-1", target.stem)
            log.write_text("mock process; no traffic\n")
            return child(1 if arm == "F" else 0, cleanup_status=cleanup)

        def git(*args):
            return "test-sha" if args[0] == "rev-parse" else ""

        states = [{"quiet": True}, {"quiet": host_after}] * 2
        argv = ["order_rate.py", "--expected-sha", "test-sha", "--output", str(output), "--domain", "test-domain",
                "--run-id", "test-run", "--runs", "1", "--lease-dir", str(Path(directory) / "lease")]
        with patch.object(sys, "argv", argv), patch.object(sys, "platform", "linux"), \
                patch.object(rate, "git", side_effect=git), patch.object(Path, "read_text", read), \
                patch.object(rate, "host_state", side_effect=states), \
                patch.object(qa.process, "run_child", side_effect=run) as runner, contextlib.redirect_stdout(io.StringIO()):
            code = rate.main()
        return output, code, runner.call_count

    def test_sequence_and_offline_recompute_preserve_raw_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            output, code, calls = self.run_mock_sequence(directory)
            self.assertEqual((code, calls), (0, 2))
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["verdict"], "INCONCLUSIVE")
            self.assertEqual(summary["arms"]["F"]["overtaking_runs"], 1)
            spec = importlib.util.spec_from_file_location("order_recompute", DRIVER.parent / "recompute.py")
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {"order_rate": rate}):
                spec.loader.exec_module(module)
            self.assertTrue(module.recompute(output)["matches_remote"])
            execution = json.loads((output / "execution.json").read_text())
            for row_ in execution:
                row_["validity"]["valid"] = False
                row_["receiver"]["overtaken"] = 123
            (output / "execution.json").write_text(json.dumps(execution))
            self.assertTrue(module.recompute(output)["matches_remote"])
            self.assertEqual(list((Path(directory) / "lease").iterdir()), [])

    def test_sequence_stops_on_cleanup_or_environment_failure(self):
        for cleanup, quiet in (("UNKNOWN", True), ("PASS", False)):
            with self.subTest(cleanup=cleanup), tempfile.TemporaryDirectory() as directory:
                output, code, calls = self.run_mock_sequence(directory, cleanup=cleanup, host_after=quiet)
                self.assertEqual((code, calls), (2, 1))
                self.assertTrue((output / "F-01.child.json").exists())
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["verdict"], "INCONCLUSIVE")
                self.assertTrue(summary["aborted"])
                self.assertTrue((output / "abort.json").exists())
                self.assertEqual(len(list((Path(directory) / "lease").glob("*.quarantine"))), int(cleanup == "UNKNOWN"))

    def test_interruption_without_lifecycle_quarantines_and_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            output, code, calls = self.run_mock_sequence(directory, runner_error=True)
            self.assertEqual((code, calls), (2, 1))
            lifecycle = json.loads((output / "F-01.child.json").read_text())
            self.assertEqual(lifecycle["cleanup_status"], "UNKNOWN")
            self.assertIn("test cancellation", lifecycle["lifecycle_detail"])
            self.assertEqual(len(list((Path(directory) / "lease").glob("*.quarantine"))), 1)
        with self.assertRaises(KeyboardInterrupt):
            rate.interrupt(None, None)

    def test_injector_and_control_are_unchanged_at_the_registering_commit(self):
        root = Path(__file__).resolve().parents[2]
        if subprocess.run(["git", "-C", str(root), "cat-file", "-e", rate.GATE_SHA + "^{commit}"], capture_output=True).returncode:
            self.skipTest("gate commit not in this clone")
        registered = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%H", "--", str(DRIVER.parent / "PLAN.md")],
                                    capture_output=True, text=True, check=True).stdout.strip()
        if not registered:
            self.skipTest("plan not committed in this clone")
        changed = subprocess.run(["git", "-C", str(root), "diff", "--name-only", rate.GATE_SHA, registered, "--", *rate.UNCHANGED],
                                 capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(changed, "")


if __name__ == "__main__":
    unittest.main()
