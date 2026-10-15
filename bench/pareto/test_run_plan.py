"""Lifecycle and admission tests execute tiny disposable children, not probes."""
import sys
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from run_plan import execute, functional_outcome


class FormalRunner(unittest.TestCase):
    def test_missing_output_is_not_a_product_failure(self):
        self.assertEqual(functional_outcome([], None, ["missing measurement"]), "NOT_RUN")
        self.assertEqual(functional_outcome([{}], None, ["malformed"]), "NOT_RUN")
        self.assertEqual(functional_outcome([{"ok": False}], None, ["invalid"]), "FAIL")
        self.assertEqual(functional_outcome([{"checksum_u64": 1, "expected_checksum_u64": 2}], None, ["invalid"]), "FAIL")
        self.assertEqual(functional_outcome([{"ok": True}], {}, ["wrong workload"]), "NOT_RUN")
        self.assertEqual(functional_outcome([{"ok": False}, {"ok": True}], None, ["duplicate measurements"]), "FAIL")

    def test_timeout_retains_stdout_and_kills_owned_child(self):
        with patch("run_plan.snapshot", return_value={}), patch("run_plan.validity", return_value=[]):
            run = execute([sys.executable, "-c", "import time; print('retained', flush=True); time.sleep(10)"], .10, {}, sample_interval=.01)
        self.assertEqual(run["execution"]["status"], "timeout")
        self.assertEqual(run["execution"]["cleanup"], "PASS")
        self.assertIn("retained", run["stdout"])
        self.assertLess(run["execution"]["start_ns"], run["execution"]["end_ns"])

    def test_lost_lease_interrupts_and_retains_failure_reason(self):
        class LostLease:
            def check(self): raise RuntimeError("generation changed")
        with patch("run_plan.snapshot", return_value={}), patch("run_plan.validity", return_value=[]):
            run = execute([sys.executable, "-c", "import time; time.sleep(10)"], 1, {}, lease=LostLease())
        self.assertEqual(run["execution"]["status"], "cancelled")
        self.assertIn("generation changed", run["interruption"])
        self.assertEqual(run["execution"]["cleanup"], "PASS")

    def test_output_limit_is_enforced_without_unbounded_pipe_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "probe.log"
            with patch("run_plan.snapshot", return_value={}), patch("run_plan.validity", return_value=[]):
                run = execute([sys.executable, "-c", "import os; os.write(1, b'x' * 8192)"], 2, {},
                              output=output, output_limit=1024)
            self.assertEqual(run["execution"]["status"], "failed")
            self.assertLessEqual(output.stat().st_size, 1024)
            self.assertLessEqual(len(run["stdout"].encode()), 1024)
            self.assertIn("probe output reached frozen byte limit", run["environment_errors"])
            self.assertEqual(run["execution"]["cleanup"], "PASS")

    def test_observer_tracks_dut_pid_and_preserves_pre_exec_phase(self):
        with patch("run_plan.snapshot", side_effect=lambda pid: {"pid": pid, "affinity": [0]}), patch("run_plan.validity", return_value=[]):
            run = execute([sys.executable, "-c", "import os,time; print(os.getpid(), flush=True); time.sleep(.1)"], 2, {}, sample_interval=.01)
        self.assertEqual(run["execution"]["status"], "completed")
        pid = int(run["stdout"].strip())
        self.assertTrue(run["samples"])
        self.assertEqual({sample["pid"] for sample in run["samples"]}, {pid})
        self.assertIn("pre_exec", {sample["phase"] for sample in run["samples"]})
        self.assertIn("running", {sample["phase"] for sample in run["samples"]})

    @unittest.skipUnless(sys.platform == "linux", "subreaper containment requires Linux")
    def test_detached_descendant_is_failed_and_reaped(self):
        code = ("import subprocess,sys; p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
                "start_new_session=True); print(p.pid,flush=True)")
        with patch("run_plan.snapshot", return_value={"affinity": [0]}), patch("run_plan.validity", return_value=[]):
            run = execute([sys.executable, "-c", code], 2, {})
        self.assertEqual(run["execution"]["status"], "failed")
        self.assertEqual(run["execution"]["cleanup"], "PASS")
        self.assertEqual(run["lifecycle"]["cleanup_scope"], "linux-subreaper")
        with self.assertRaises(ProcessLookupError): os.kill(int(run["stdout"].strip()), 0)


if __name__ == "__main__":
    unittest.main()
