"""Containment validation is testable without changing host namespaces."""
from pathlib import Path
import os
import subprocess
import unittest

from isolation import isolation_errors


class IsolatedRig(unittest.TestCase):
    def setUp(self):
        self.current = {"net": "net:[20]", "mnt": "mnt:[21]", "pid": "pid:[22]"}
        self.host = {"net": "net:[10]", "mnt": "mnt:[11]", "pid": "pid:[12]"}
        self.env = {"QA_NETNS_ISOLATED": "1", **{f"QA_HOST_{name.upper()}NS_ID": value for name, value in self.host.items()}}

    def test_host_namespace_or_control_socket_is_rejected(self):
        self.assertFalse(isolation_errors(self.env, self.current, self.current, {"systemd": False}, "bash"))
        for name in self.current:
            altered = {**self.current, name: self.host[name]}
            self.assertTrue(isolation_errors(self.env, altered, altered, {}, "bash"))
        self.assertTrue(isolation_errors(self.env, self.current, self.current, {"systemd": True}, "bash"))

    def test_marker_alone_and_host_init_are_insufficient(self):
        self.assertTrue(isolation_errors({"QA_NETNS_ISOLATED": "1"}, self.host, self.host, {}, "systemd"))
        self.assertTrue(isolation_errors({}, self.current, self.host, {}, "systemd"))
        self.assertFalse(isolation_errors({"QA_NETNS_ISOLATED": "1"}, self.current, self.host, {}, "systemd"))

    def test_script_rejects_isolated_mode_before_rig_services_without_wrapper(self):
        script = Path(__file__).with_name("run-rig.sh")
        environment = {k: v for k, v in os.environ.items() if not k.startswith("QA_")}
        completed = subprocess.run(["bash", str(script)], env={**environment, "L2_ISOLATED": "1"}, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Isolation rejected", completed.stderr)


if __name__ == "__main__":
    unittest.main()
