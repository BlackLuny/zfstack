"""Relay path substitution and the test-side queue oracle must fail closed."""
import copy
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from evidence import inspect_case
from path_contract import client_nodes, inspect_path
from queue_control import DRAIN_RATE_BPS, DRAIN_STRATEGY, QueueControl, configuration, validate_evidence
from test_evidence import fixture
from test_endpoints import endpoint_fixture
from test_queue_links import link_fixture


QUEUE = {"kind": "netem-delay", "node": "worker", "interface": "eth0", "phase": "loaded",
         "delay_ms": 500.0, "limit_packets": 100000}


def queue_report():
    plain = {"kind": "noqueue", "handle": "0:", "root": True, "options": {}}
    def row(phase, at, backlog=0, packets=0):
        q = copy.deepcopy(plain) if phase in ("before", "restored") else {
            "kind": "netem", "handle": "44:", "root": True, "options": {"limit": 100000},
            "bytes": packets * 1000, "packets": packets, "drops": 0, "backlog": backlog}
        text = "qdisc netem 44: root limit 100000"
        if phase == "active":
            q["options"]["delay"] = {"delay": .5, "jitter": 0, "correlation": 0}
            text += " delay 500ms"
        elif phase in ("draining", "cleanup"):
            q["options"]["rate"] = {"rate": DRAIN_RATE_BPS // 8, "cellsize": 0}
            text += f" rate {DRAIN_RATE_BPS // 1_000_000_000}Gbit"
        return {"phase": phase, "at_ns": int(at * 1e9), "qdiscs": [q], "text": text}
    return {"schema_version": 3, "drain_strategy": dict(DRAIN_STRATEGY), "link_samples": link_fixture(),
            "config": copy.deepcopy(QUEUE), "clock": "CLOCK_MONOTONIC", "errors": [], "cleanup": "PASS",
            "actual": {"start_ns": 9_005_000_000, "end_ns": 24_251_000_000, "restored_ns": 24_900_000_000},
            "planned": {"start_ns": 9_000_000_000, "end_ns": 24_250_000_000, "recovery_start_ns": 25_000_000_000}, "samples": [
                row("before", 1), row("active", 9.01, 0, 0), row("active", 16, 128000, 400), row("active", 24.25, 24000, 1000),
                row("draining", 24.8, 0, 1100), row("cleanup", 24.85, 0, 1100), row("restored", 24.9)]}


def relay_fixture(path):
    result, raw = fixture(path)
    result["effective_config"].update(outbound="relay", probe_port=19002)
    result["effective_config"]["topology"]["client_nodes"] = client_nodes("relay")
    evidence = result["path_evidence"]
    for phase in ("before", "after"):
        data = evidence[phase]
        data["target"]["from_fwd4"] = data["target"].pop("from_fwd1")
        data["worker"]["from_fwd4"] = data["worker"].pop("from_fwd1")
        amount = 0 if phase == "before" else 1000
        data["fwd1"] = {"from_target": {"bytes": amount}}
        data["worker"]["from_fwd1"] = {"bytes": amount}
        data["worker"]["from_target"] = {"bytes": 0}
    evidence.update(outbound="relay", forwarder_alive=True,
                    relay_business={"before": {"worker_direct": 0, "forwarder_target": 0},
                                    "after": {"worker_direct": 0, "forwarder_target": 30}})
    raw["forwarder.sha256"] = "b" * 64 + " forwarder-server"
    raw["endpoint-samples.json"] = endpoint_fixture(result["effective_config"])
    return result, raw


class PathAndQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_relay_uses_separate_forwarder_and_real_payload_edges(self):
        result, raw = relay_fixture(self.root)
        self.assertEqual(inspect_path(result)[0], [])
        (self.root / "result.json").write_text(json.dumps(result))
        (self.root / "raw.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))
        self.assertEqual(inspect_case(self.root)["functional_evidence_errors"], [])
        for change in (lambda r: r["effective_config"]["topology"].update(client_nodes=client_nodes("direct")),
                       lambda r: r["effective_config"].update(probe_port=19001),
                       lambda r: r["path_evidence"]["after"]["worker"]["from_fwd1"].update(bytes=1),
                       lambda r: r["path_evidence"]["relay_business"]["after"].update(worker_direct=1),
                       lambda r: r["path_evidence"].update(forwarder_alive=False),
                       lambda r: r["path_evidence"]["before"]["fwd1"]["from_target"].update(bytes=2000)):
            bad = copy.deepcopy(result); change(bad)
            self.assertTrue(inspect_path(bad)[0])
        self.assertNotIn("fwd1", client_nodes("relay"))

    def test_same_linked_binary_without_relay_execution_is_not_coverage(self):
        result, _ = fixture(self.root)
        result["effective_config"].update(outbound="relay", probe_port=19002)
        result["effective_config"]["topology"]["client_nodes"] = client_nodes("relay")
        self.assertTrue(inspect_path(result)[0])

    def test_queue_oracle_requires_backlog_zero_loss_and_clean_restoration(self):
        report = queue_report()
        self.assertEqual(validate_evidence(report, QUEUE), [])
        mutations = (lambda r: r["samples"][2]["qdiscs"][0].update(drops=1),
                     lambda r: [x["qdiscs"][0].update(backlog=0) for x in r["samples"]],
                     lambda r: r["samples"].pop(),
                     lambda r: r["samples"][2].update(text="qdisc netem 44: root limit 100000 delay 50ms"),
                     lambda r: r.update(cleanup="FAIL"),
                     lambda r: r["actual"].update(end_ns=40_000_000_000),
                     lambda r: r["samples"][-2]["qdiscs"][0].update(backlog=1000),
                     lambda r: r["samples"][-2]["qdiscs"][0].update(drops=False),
                     lambda r: r["samples"][-2]["qdiscs"][0].update(packets=1),
                     lambda r: r["samples"][3]["qdiscs"][0].update(bytes=1))
        for mutate in mutations:
            bad = copy.deepcopy(report); mutate(bad)
            self.assertTrue(validate_evidence(bad, QUEUE))

    def test_queue_evidence_is_bound_to_load_window_and_never_clean_performance(self):
        result, raw = fixture(self.root)
        result.update(start_at=1, queue_control=queue_report())
        raw["queue-link-samples.json"] = copy.deepcopy(result["queue_control"]["link_samples"])
        result["effective_config"]["queue_control"] = copy.deepcopy(QUEUE)
        def observed():
            (self.root / "result.json").write_text(json.dumps(result))
            (self.root / "raw.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))
            return inspect_case(self.root)
        checked = observed()
        self.assertEqual(checked["functional_evidence_errors"], [])
        self.assertIn("not clean performance", str(checked["performance_errors"]))
        result["start_at"] = 10
        self.assertIn("interval", str(observed()["functional_evidence_errors"]))
        result["effective_config"]["queue_control"] = {"kind": "netem-delay"}
        self.assertTrue(observed()["functional_evidence_errors"])

    def test_v3_requires_explicit_strategy_and_exact_rate_in_both_raw_views(self):
        mutations = [lambda r: r.update(schema_version=1),
                     lambda r: r.update(schema_version=2),
                     lambda r: r["drain_strategy"].update(rate_bps=100_000_000_000),
                     lambda r: r.pop("drain_strategy"),
                     lambda r: r["drain_strategy"].update(rate_bps=1_000_000_000),
                     lambda r: r["drain_strategy"].update(delay_ms=False),
                     lambda r: r["samples"][4]["qdiscs"][0]["options"].pop("rate"),
                     lambda r: r["samples"][4]["qdiscs"][0]["options"]["rate"].update(rate=2**32-1),
                     lambda r: r["samples"][4]["qdiscs"][0]["options"]["rate"].update(rate=DRAIN_RATE_BPS),
                     lambda r: r["samples"][4].update(text="qdisc netem 44: root limit 100000 rate 34Gbit"),
                     lambda r: r["samples"][4].update(text="qdisc netem 44: root limit 100000"),
                     lambda r: r["samples"][4]["qdiscs"][0]["options"].update(delay={"delay": .5}),
                     lambda r: r["samples"][4].update(text=f"qdisc netem 44: root limit 100000 delay 500ms rate {DRAIN_RATE_BPS}bit"),
                     lambda r: r["samples"][4]["qdiscs"][0].update(kind="pfifo"),
                     lambda r: r["samples"][4]["qdiscs"].append({"kind": "pfifo", "parent": "44:1"}),
                     lambda r: r["samples"][4]["qdiscs"][0]["options"]["rate"].update(packetoverhead=8),
                     lambda r: r["samples"][4]["qdiscs"][0]["options"]["rate"].update(cellsize=64),
                     lambda r: r["samples"][5]["qdiscs"][0]["options"].pop("rate")]
        for index, mutate in enumerate(mutations):
            with self.subTest(mutation=index):
                report = queue_report(); mutate(report)
                self.assertTrue(validate_evidence(report, QUEUE))
        report = queue_report()
        report["samples"][4]["text"] = f"qdisc netem 44: root refcnt 2 limit 100000 delay 0us rate {DRAIN_RATE_BPS // 1_000_000}Mbit"
        report["samples"][4]["qdiscs"][0]["options"]["delay"] = {"delay": 0, "jitter": 0, "correlation": 0}
        self.assertEqual(validate_evidence(report, QUEUE), [])

    def test_active_and_drain_cannot_hide_additional_impairments(self):
        for index in (1, 2, 3, 4, 5):
            for option in ("loss-random", "reorder", "duplicate", "corrupt", "slot"):
                with self.subTest(index=index, option=option):
                    report = queue_report(); report["samples"][index]["qdiscs"][0]["options"][option] = {"probability": 1}
                    self.assertTrue(validate_evidence(report, QUEUE))
            for text in (" loss 1%", " reorder 1%", " duplicate 1%", " corrupt 1%", " gap 1", " 100ms"):
                report = queue_report(); report["samples"][index]["text"] += text
                self.assertTrue(validate_evidence(report, QUEUE))
        for field, value in (("rate", {"rate": DRAIN_RATE_BPS // 8}), ("gap", 1), ("ecn", True)):
            report = queue_report(); report["samples"][1]["qdiscs"][0]["options"][field] = value
            self.assertTrue(validate_evidence(report, QUEUE))
        report = queue_report(); report["samples"][1]["text"] += " rate 100Gbit"
        self.assertTrue(validate_evidence(report, QUEUE))

    def test_runner_keeps_original_active_command_and_changes_existing_queue_for_v3_drain(self):
        now = time.monotonic()
        with patch("isolation.verify", return_value=[]):
            queue = QueueControl(QUEUE, "rig-abcdef-worker", now - 2, now, self.root / "queue.json")
        with patch.object(queue, "_command", return_value="") as command, \
                patch.object(queue, "_sample") as sample:
            # _sample normally records timestamps; retain that narrow side
            # effect so the command test exercises the normal report writer.
            def observed(phase):
                queue.report["samples"].append({"phase": phase, "at_ns": time.monotonic_ns()})
                return {"kind": "noqueue"} if phase == "before" else {"kind": "netem", "handle": "44:", "backlog": 0}
            sample.side_effect = observed
            queue._run()
        commands = [entry.args[0] for entry in command.call_args_list]
        self.assertEqual(commands, [
            ["qdisc", "add", "dev", "eth0", "root", "handle", "44:", "netem", "limit", "100000", "delay", "500.0ms"],
            ["qdisc", "change", "dev", "eth0", "root", "handle", "44:", "netem", "limit", "100000", "delay", "0ms", "rate", f"{DRAIN_RATE_BPS}bit"],
            ["qdisc", "del", "dev", "eth0", "root", "handle", "44:"]])
        self.assertEqual(queue.report["schema_version"], 3)
        self.assertEqual(queue.report["drain_strategy"], DRAIN_STRATEGY)
        self.assertEqual(queue.report["errors"], [])
        self.assertAlmostEqual(queue.end, now - .75)

    def test_queue_is_opt_in_bounded_and_refuses_nonisolated_shell(self):
        self.assertIsNone(configuration({}))
        self.assertEqual(configuration({"L2_QUEUE_DELAY_MS": "500"}), QUEUE)
        for value in ("0", "-1", "nan", "3000"):
            with self.assertRaises(ValueError): configuration({"L2_QUEUE_DELAY_MS": value})
        if os.name == "posix":
            env = dict(os.environ, L2_QUEUE_DELAY_MS="500", L2_ISOLATED="0")
            process = subprocess.run(["bash", str(Path(__file__).with_name("run-rig.sh"))], env=env, capture_output=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"requires verified isolated mode", process.stderr)


if __name__ == "__main__":
    unittest.main()
