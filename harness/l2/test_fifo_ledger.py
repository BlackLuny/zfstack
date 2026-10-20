"""The persistent FIFO oracle recomputes every frame and fails closed."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

import fifo_ledger as L
from evidence import inspect_case
from fifo_injector import VNET_HDR, header_length
from queue_control import FifoQueueControl, LEDGER_NAME, configuration, validate_evidence
from test_evidence import fixture
from test_queue_links import link_fixture

S, MS = 1_000_000_000, 1_000_000
CONFIG = {"kind": L.KIND, "node": "worker", "interface": "eth0", "phase": "loaded",
          "delay_ms": 500.0, "limit_packets": 100000}
PLANNED = {"measurement_start_ns": 1 * S, "start_ns": 9 * S, "end_ns": 24 * S + 250 * MS,
           "recovery_start_ns": 25 * S, "measurement_end_ns": 33 * S}
PERIOD = 4 * MS


def simulate(planned=PLANNED, delay_ns=500 * MS):
    """Replay the frozen admission policy against an ideal single FIFO."""
    rows, previous_due, previous_out, transparent = [], 0, 0, None
    for seq, t_in in enumerate(range(300 * MS, 33_500 * MS, PERIOD)):
        gso = seq % 5 == 0
        length, header = (66 + 2 * 1448, 66) if gso else (1000, 42)
        if transparent is None and t_in >= planned["end_ns"] and previous_out <= t_in:
            transparent = t_in
        if transparent is not None:
            phase, due = L.TRANSPARENT, t_in
        elif t_in < planned["start_ns"]:
            phase, due = L.PRE, t_in
        elif t_in < planned["end_ns"]:
            phase, due = L.ACTIVE, max(t_in + delay_ns, previous_due)
        else:
            phase, due = L.DRAIN, max(t_in, previous_due) + L.tx_ns(length)
        t_out = max(due, previous_out) + 1000
        rows.append((seq, phase, L.DELIVERED, 1 if gso else 0, 1448 if gso else 0, length, t_in,
                     due - t_in, t_out - t_in, seq * 7919, header))
        previous_due, previous_out = due, t_out
    return rows, transparent


def counters(report, rows):
    accounted = [L.segments(row) or (1, row[5]) for row in rows]
    report["kernel"]["action"] = {"packets": sum(c for c, _ in accounted), "bytes": sum(b for _, b in accounted),
                                  "drops": 0, "overlimits": 0}
    zero = {"packets": 0, "bytes": 0, "dropped": 0, "errors": 0}
    report["kernel"]["tap_before"] = {"rx": dict(zero), "tx": dict(zero)}
    report["kernel"]["tap_after"] = {"rx": dict(zero), "tx": dict(zero, packets=len(rows), bytes=sum(r[5] for r in rows))}


def seal(report, rows, *, recount=True):
    blob = L.encode(rows)
    report["ledger"] = {"path": LEDGER_NAME, "sha256": hashlib.sha256(blob).hexdigest(), "frames": len(rows),
                        "record_format": L.RECORD_FORMAT}
    if recount:
        counters(report, rows)
    return blob


def fifo_fixture(config=CONFIG):
    rows, transparent = simulate()
    noqueue = {"kind": "noqueue", "handle": "0:", "root": True, "refcnt": 2, "options": {}}
    report = {"schema_version": L.SCHEMA_VERSION, "strategy": dict(L.STRATEGY), "config": copy.deepcopy(config),
              "clock": "CLOCK_MONOTONIC", "planned": dict(PLANNED),
              "actual": {"installed_ns": 200 * MS, "transparent_ns": transparent,
                         "removed_ns": 33_600 * MS, "closed_ns": 33_700 * MS},
              "kernel": {"qdisc_before": [dict(noqueue)], "tap_qdisc": [dict(noqueue)],
                         "qdisc_installed": [dict(noqueue), {"kind": "clsact", "handle": "ffff:", "parent": "ffff:fff1"}],
                         "qdisc_restored": [dict(noqueue)]},
              "resources": {"max_frames": 130, "max_bytes": 200_000}, "errors": [], "cancelled": False,
              "cleanup": "PASS"}
    return report, rows, seal(report, rows)


def first(rows, phase):
    return next(index for index, row in enumerate(rows) if row[1] == phase)


def last(rows, phase):
    return max(index for index, row in enumerate(rows) if row[1] == phase)


def replace(rows, index, **fields):
    names = ("seq", "phase", "outcome", "gso_type", "gso_size", "length", "t_in", "due", "out", "digest", "header")
    row = list(rows[index])
    for name, value in fields.items():
        row[names.index(name)] = value
    rows[index] = tuple(row)


class FifoLedgerTests(unittest.TestCase):
    def check(self, mutate, expected, *, recount=True):
        report, rows, _ = fifo_fixture()
        rows = list(rows)
        blob = mutate(report, rows)
        if blob is None:
            blob = seal(report, rows, recount=recount)
        errors = L.validate(report, CONFIG, blob)
        self.assertTrue(any(expected in error for error in errors), errors)

    def test_policy_replay_passes_and_covers_every_phase(self):
        report, rows, blob = fifo_fixture()
        self.assertEqual(L.validate(report, CONFIG, blob), [])
        self.assertEqual({row[1] for row in rows}, {L.PRE, L.ACTIVE, L.DRAIN, L.TRANSPARENT})
        self.assertLess(report["actual"]["transparent_ns"], PLANNED["recovery_start_ns"])
        self.assertEqual(L.decode(blob), rows)
        self.assertEqual(L.encode(rows), blob)
        summary = L.summary(rows)
        self.assertGreaterEqual(summary["active"]["max_residence_ns"], 500 * MS)
        self.assertLess(summary["transparent"]["max_residence_ns"], MS)

    def test_missing_duplicate_reordered_and_dropped_frames_fail(self):
        sequence = "missing, duplicate or reordered"
        self.check(lambda r, rows: rows.pop(1000) and None, sequence)
        self.check(lambda r, rows: rows.insert(1000, rows[1000]), sequence)
        self.check(lambda r, rows: rows.__setitem__(slice(1000, 1002), [rows[1001], rows[1000]]), sequence)
        for outcome in (L.OVERFLOW, L.SEND_FAILED, L.UNSENT):
            self.check(lambda r, rows: replace(rows, 3000, outcome=outcome), "dropped, failed or left unsent")
        self.check(lambda r, rows: replace(rows, 3000, out=L.NOT_SENT), "dropped, failed or left unsent")

    def test_release_times_are_recomputed_from_the_frozen_policy(self):
        active = first(simulate()[0], L.ACTIVE) + 200
        self.check(lambda r, rows: replace(rows, active, due=rows[active][7] - MS), "release time")
        self.check(lambda r, rows: replace(rows, active, due=0, out=1000), "release time")
        self.check(lambda r, rows: replace(rows, active, out=rows[active][7] - 1), "released early")
        self.check(lambda r, rows: replace(rows, active, out=rows[active][7] + L.LATENESS_LIMIT_NS + 1), "lateness bound")
        self.check(lambda r, rows: replace(rows, active, phase=L.PRE), "differs from recomputed")
        drain = first(simulate()[0], L.DRAIN)
        self.check(lambda r, rows: replace(rows, drain, due=rows[drain][7] - L.tx_ns(rows[drain][5])), "release time")
        transparent = first(simulate()[0], L.TRANSPARENT) + 5
        self.check(lambda r, rows: replace(rows, transparent, due=1), "release time")
        self.check(lambda r, rows: replace(rows, transparent - 1, out=PERIOD + 5000), "out of order")
        self.check(lambda r, rows: replace(rows, 0, t_in=0, due=rows[0][7] + rows[0][6], out=rows[0][8] + rows[0][6]),
                   "outside the owned path lifetime")
        self.check(lambda r, rows: replace(rows, 10, t_in=rows[9][6] - 1), "not monotonic")

    def test_transparent_switch_must_follow_an_empty_fifo_and_precede_recovery(self):
        rows, transparent = simulate()
        switch, drained = first(rows, L.TRANSPARENT), last(rows, L.DRAIN)

        def shaping_after_empty(report, rows):
            replace(rows, switch, phase=L.DRAIN, due=L.tx_ns(rows[switch][5]),
                    out=max(L.tx_ns(rows[switch][5]), rows[switch][8]))
            report["actual"]["transparent_ns"] = rows[switch + 1][6]
        self.check(shaping_after_empty, "kept shaping after its queue was empty")

        def behind_backlog(report, rows):
            replace(rows, drained, phase=L.TRANSPARENT, due=0)
            report["actual"]["transparent_ns"] = rows[drained][6]
        self.check(behind_backlog, "behind an unfinished backlog")

        out = rows[drained][6] + rows[drained][8]
        self.assertGreater(out - 1, rows[drained][6])
        self.check(lambda r, rows: r["actual"].update(transparent_ns=out - 1), "before its backlog was empty")
        self.check(lambda r, rows: r["actual"].update(transparent_ns=PLANNED["recovery_start_ns"]), "before the recovery boundary")
        self.check(lambda r, rows: r["actual"].update(transparent_ns=PLANNED["end_ns"] - 1), "before the recovery boundary")

    def test_kernel_counters_close_per_frame_and_per_gso_segment(self):
        def action(**values):
            return lambda r, rows: r["kernel"]["action"].update(values) or seal(r, rows, recount=False)
        report, rows, _ = fifo_fixture()
        packets, size = report["kernel"]["action"]["packets"], report["kernel"]["action"]["bytes"]
        self.assertGreater(packets, len(rows))
        self.check(action(packets=packets + 1), "redirect segment counters")
        self.check(action(bytes=size - 1), "redirect segment counters")
        self.check(action(packets=len(rows)), "redirect segment counters")
        self.check(action(drops=1), "dropped frames before the FIFO")
        self.check(action(overlimits=1), "dropped frames before the FIFO")

        def tap(direction, **values):
            return lambda r, rows: r["kernel"]["tap_after"][direction].update(values) or seal(r, rows, recount=False)
        self.check(tap("tx", packets=len(rows) + 1), "tap frame counters")
        self.check(tap("tx", bytes=1), "bytes differ from tap counters")
        self.check(tap("tx", dropped=1), "dropped frames before the FIFO")
        self.check(tap("tx", errors=1), "dropped frames before the FIFO")
        self.check(tap("rx", packets=1), "unexpected frames were written")
        self.check(lambda r, rows: r["kernel"]["tap_before"]["tx"].update(packets=10**9) or seal(r, rows, recount=False), "reset")
        self.check(lambda r, rows: r["kernel"]["tap_after"]["tx"].pop("dropped") or seal(r, rows, recount=False),
                   "invalid raw kernel counters")
        self.check(lambda r, rows: replace(rows, 0, header=0), "cannot be bound")
        self.check(lambda r, rows: replace(rows, 0, gso_size=0), "cannot be bound")
        self.check(lambda r, rows: replace(rows, 0, header=rows[0][5]), "cannot be bound")
        self.check(lambda r, rows: replace(rows, 0, length=rows[0][5] - 1) or seal(r, rows, recount=False), "redirect segment counters")

    def test_segment_model_matches_the_observed_kernel_accounting(self):
        self.assertEqual(L.segments((0, 0, 0, 0, 0, 1000, 0, 0, 0, 0, 42)), (1, 1000))
        self.assertEqual(L.segments((0, 0, 0, 1, 1448, 66 + 3 * 1448, 0, 0, 0, 0, 66)), (3, 3 * (66 + 1448)))
        self.assertEqual(L.segments((0, 0, 0, 0x81, 1448, 66 + 1449, 0, 0, 0, 0, 66)), (2, 2 * 66 + 1449))
        self.assertIsNone(L.segments((0, 0, 0, 5, 1000, 5000, 0, 0, 0, 0, 0)))

    def test_header_length_parses_tcp_udp_vlan_and_ipv6_frames(self):
        vnet = VNET_HDR.pack(0, 0, 0, 0, 0, 0)
        ether = b"\x00" * 12
        tcp = struct.pack("!HHIIBBHHH", 1, 2, 0, 0, 8 << 4, 0, 0, 0, 0) + b"\x00" * 12
        ipv4 = bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 64, 6]) + b"\x00" * 10
        self.assertEqual(header_length(vnet + ether + b"\x08\x00" + ipv4 + tcp + b"x" * 100), 14 + 20 + 32)
        udp4 = ipv4[:9] + b"\x11" + ipv4[10:]
        self.assertEqual(header_length(vnet + ether + b"\x08\x00" + udp4 + b"\x00" * 8 + b"x"), 42)
        ipv6 = b"\x60" + b"\x00" * 5 + b"\x06" + b"\x40" + b"\x00" * 32
        self.assertEqual(header_length(vnet + ether + b"\x86\xdd" + ipv6 + tcp), 14 + 40 + 32)
        self.assertEqual(header_length(vnet + ether + b"\x81\x00\x00\x01\x08\x00" + ipv4 + tcp), 18 + 20 + 32)
        self.assertEqual(header_length(vnet + ether + b"\x08\x06" + b"\x00" * 28), 0)
        self.assertEqual(header_length(vnet + ether + b"\x08\x00" + ipv4[:9] + b"\x01" + ipv4[10:]), 0)
        self.assertEqual(header_length(vnet + ether + b"\x08\x00" + ipv4 + tcp[:10]), 0)
        self.assertEqual(header_length(vnet[:4]), 0)

    def test_path_lifetime_and_queue_resources_are_bound(self):
        self.check(lambda r, rows: r["actual"].update(installed_ns=PLANNED["measurement_start_ns"] - L.INSTALL_LEAD_NS + 1),
                   "not installed before the measured baseline")
        self.check(lambda r, rows: r["actual"].update(removed_ns=PLANNED["measurement_end_ns"] - 1), "removed before")
        self.check(lambda r, rows: r["actual"].update(closed_ns=r["actual"]["removed_ns"] - 1), "removed before")
        self.check(lambda r, rows: r["actual"].pop("removed_ns") and None, "timestamps")
        self.check(lambda r, rows: r["resources"].update(max_frames=100001) and None, "resource guard")
        self.check(lambda r, rows: r["resources"].update(max_bytes=L.MEMORY_LIMIT_BYTES + 1) and None, "resource guard")
        self.check(lambda r, rows: r.pop("resources") and None, "resource guard")
        self.check(lambda r, rows: r["planned"].update(end_ns=r["planned"]["end_ns"] - MS) and None, "drain guard")
        self.check(lambda r, rows: r["planned"].update(start_ns=r["planned"]["end_ns"]) and None, "unordered")
        small = dict(CONFIG, limit_packets=100)
        report, rows, blob = fifo_fixture(small)
        report["resources"]["max_frames"] = 100
        self.assertIn("FIFO occupancy exceeded its frozen packet or memory bound", L.validate(report, small, blob))

    def test_every_phase_must_be_covered_by_real_traffic(self):
        coverage = "does not cover baseline, recovery and measurement end"
        self.check(lambda r, rows: rows.__delitem__(slice(first(rows, L.TRANSPARENT) + 10, None)), coverage)
        self.check(lambda r, rows: rows.__delitem__(slice(len(rows) - 400, None)), coverage)

        def no_baseline(report, rows):
            kept = [row for row in rows if row[1] != L.PRE]
            rows[:] = [(index, *row[1:]) for index, row in enumerate(kept)]
        self.check(no_baseline, "every measured phase")

    def test_flush_after_the_switch_stamp_is_rejected(self):
        report, rows, blob = fifo_fixture()
        drained = last(rows, L.DRAIN)
        stale = rows[drained][6] + rows[drained][7] - 1
        self.assertGreater(stale, rows[drained][6])
        report["actual"]["transparent_ns"] = stale
        self.assertEqual(L.validate(report, CONFIG, blob), ["FIFO declared transparent before its backlog was empty"])

    def test_report_ledger_and_cleanup_claims_cannot_substitute_raw_evidence(self):
        report, rows, blob = fifo_fixture()
        for mutate, expected in (
                (lambda r: r.update(cancelled=True), "cancelled or incomplete"),
                (lambda r: r.update(errors=["child failed"]), "cancelled or incomplete"),
                (lambda r: r.update(cleanup="FAIL"), "cleanup did not complete"),
                (lambda r: r.update(schema_version=3), "frozen strategy"),
                (lambda r: r["strategy"].update(drain_rate_bps=10**11), "frozen strategy"),
                (lambda r: r.update(config=dict(CONFIG, delay_ms=50.0)), "frozen strategy"),
                (lambda r: r.update(clock="CLOCK_REALTIME"), "frozen strategy"),
                (lambda r: r["ledger"].update(sha256="0" * 64), "differs from the report digest"),
                (lambda r: r["ledger"].update(frames=len(rows) - 1), "frame count"),
                (lambda r: r.pop("ledger"), "differs from the report digest"),
                (lambda r: r["kernel"].update(tap_qdisc=[{"kind": "pfifo_fast", "root": True}]), "own queue"),
                (lambda r: r["kernel"]["qdisc_restored"].append({"kind": "clsact"}), "auxiliary"),
                (lambda r: r["kernel"].update(qdisc_restored=[{"kind": "netem", "root": True}]), "original noqueue"),
                (lambda r: r["kernel"].update(qdisc_before=[]), "original noqueue"),
                (lambda r: r["kernel"].update(qdisc_installed=[{"kind": "noqueue", "root": True}]), "auxiliary"),
                (lambda r: r["kernel"].pop("qdisc_installed"), "qdisc listing"),
                (lambda r: r.pop("kernel"), "kernel counters"),
                (lambda r: r.update(ok=True, cleanup="UNKNOWN"), "cleanup did not complete")):
            with self.subTest(expected=expected):
                bad = copy.deepcopy(report); mutate(bad)
                errors = L.validate(bad, CONFIG, blob)
                self.assertTrue(any(expected in error for error in errors), errors)
        self.assertIn("raw FIFO ledger is missing or differs from the report digest", L.validate(report, CONFIG, None))
        self.assertTrue(L.validate(report, CONFIG, b"not gzip"))
        self.assertTrue(L.validate(report, dict(CONFIG, limit_packets=1000), blob))

    def test_ledger_container_is_bounded_and_self_describing(self):
        _, rows, blob = fifo_fixture()
        with self.assertRaises(ValueError):
            L.decode(None)
        with self.assertRaises(ValueError):
            L.decode(b"\x00" * (64 * 1024 * 1024 + 1))
        data = gzip.decompress(blob)
        for bad in (b"QA44XXXX" + data[8:], data[:8] + struct.pack("<H", 1) + data[10:],
                    data[:-1], data + b"\x00" * L.RECORD.size, data[:L.HEADER.size - 1]):
            with self.assertRaises(ValueError):
                L.decode(gzip.compress(bad))
        huge = L.HEADER.pack(L.MAGIC, L.LEDGER_VERSION, L.RECORD.size, L.MAX_FRAMES + 1)
        with self.assertRaises(ValueError):
            L.decode(gzip.compress(huge + b"\x00" * ((L.MAX_FRAMES + 1) * L.RECORD.size)))
        self.assertEqual(L.decode(L.encode([])), [])

    def test_queue_control_dispatch_requires_links_and_ledger(self):
        report, _, blob = fifo_fixture()
        report["link_samples"] = link_fixture()
        self.assertEqual(validate_evidence(report, CONFIG, blob), [])
        self.assertTrue(validate_evidence(report, CONFIG))
        self.assertTrue(validate_evidence(dict(report, link_samples=None), CONFIG, blob))
        self.assertTrue(validate_evidence(None, CONFIG, blob))
        self.assertTrue(validate_evidence(report, dict(CONFIG, kind="netem-delay"), blob))
        self.assertEqual(configuration({"L2_QUEUE_DELAY_MS": "500", "L2_QUEUE_KIND": L.KIND}), CONFIG)
        self.assertEqual(configuration({"L2_QUEUE_DELAY_MS": "500", "L2_QUEUE_KIND": ""})["kind"], "netem-delay")
        with self.assertRaises(ValueError):
            configuration({"L2_QUEUE_DELAY_MS": "500", "L2_QUEUE_KIND": "nfqueue"})

    def test_case_evidence_binds_fifo_schedule_and_archived_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result, raw = fixture(root)
            report, _, blob = fifo_fixture()
            report["link_samples"] = link_fixture()
            result.update(start_at=1, queue_control=report)
            result["effective_config"]["queue_control"] = copy.deepcopy(CONFIG)
            raw["queue-link-samples.json"] = copy.deepcopy(report["link_samples"])

            def observed(ledger=blob):
                (root / "result.json").write_text(json.dumps(result))
                (root / "raw.json.gz").write_bytes(gzip.compress(json.dumps(raw).encode()))
                if ledger is None:
                    (root / LEDGER_NAME).unlink(missing_ok=True)
                else:
                    (root / LEDGER_NAME).write_bytes(ledger)
                return inspect_case(root)
            checked = observed()
            self.assertEqual(checked["functional_evidence_errors"], [])
            self.assertIn("not clean performance", str(checked["performance_errors"]))
            self.assertIn("ledger", str(observed(None)["functional_evidence_errors"]))
            self.assertTrue(observed(L.encode([]))["functional_evidence_errors"])
            result["start_at"] = 2
            self.assertIn("interval", str(observed()["functional_evidence_errors"]))


class FakeProcess:
    def __init__(self, ready=b"ready 1\n", exits=0, hang=False):
        read, self.write_end = os.pipe()
        self.stdout = os.fdopen(read, "rb", buffering=0)
        if ready is not None:
            os.write(self.write_end, ready)
        if ready is not None and not ready:
            os.close(self.write_end)
            self.write_end = None
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        self.returncode, self.hang, self.signals, self.pid = None, hang, [], 1
        self.exits = exits

    def wait(self, timeout=None):
        if self.hang and not self.signals:
            import subprocess
            raise subprocess.TimeoutExpired("fifo", timeout)
        if self.write_end is not None:
            os.close(self.write_end)
            self.write_end = None
        self.returncode = self.exits
        return self.returncode

    def terminate(self):
        self.signals.append("term")

    def kill(self):
        self.signals.append("kill")


class FifoParentHandleTests(unittest.TestCase):
    def control(self, root, lead=2.0):
        now = time.monotonic()
        with patch("isolation.verify", return_value=[]):
            return FifoQueueControl(CONFIG, "rig-abcdef-worker", now + lead, now + lead + 8, now + lead + 24,
                                    now + lead + 32, root)

    def run_child(self, process, report=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control = self.control(root, lead=.7)
            with patch("queue_control.subprocess.Popen", return_value=process) as popen:
                control.begin()
                if report is not None:
                    (root / "queue-control.json").write_text(json.dumps(report))
                result = control.finish()
            spec = json.loads((root / "queue-fifo-spec.json").read_text())
            return result, process.stdin.getvalue(), popen.call_args, spec

    def test_spec_carries_the_frozen_schedule_and_finish_waits_for_the_child(self):
        result, sent, call, spec = self.run_child(FakeProcess(), {"errors": [], "cleanup": "PASS"})
        self.assertEqual(result["errors"], [])
        self.assertEqual(sent, b"finish\n")
        self.assertEqual(call.args[0][:4], ["ip", "netns", "exec", "rig-abcdef-worker"])
        self.assertTrue(call.args[0][5].endswith("fifo_injector.py"))
        planned = spec["planned"]
        self.assertEqual(planned["start_ns"] - planned["measurement_start_ns"], 8 * S)
        self.assertEqual(planned["recovery_start_ns"] - planned["end_ns"], 750 * MS)
        self.assertEqual(planned["measurement_end_ns"] - planned["recovery_start_ns"], 8 * S)

    def test_late_missing_failed_or_hung_children_are_explicit_errors(self):
        result, sent, _, _ = self.run_child(FakeProcess(ready=None), {"errors": [], "cleanup": "PASS"})
        self.assertIn("FIFO injector was not installed before the measured baseline", result["errors"])
        self.assertEqual(sent, b"cancel\n")
        result, _, _, _ = self.run_child(FakeProcess(ready=b""), {"errors": [], "cleanup": "PASS"})
        self.assertIn("FIFO injector exited before installation", result["errors"])
        result, _, _, _ = self.run_child(FakeProcess(ready=b"installed\n"), {"errors": [], "cleanup": "PASS"})
        self.assertIn("FIFO injector reported an invalid readiness line", result["errors"])
        result, _, _, _ = self.run_child(FakeProcess(), None)
        self.assertIn("FIFO injector report is missing", result["errors"])
        self.assertEqual(result["cleanup"], "UNKNOWN")
        result, _, _, _ = self.run_child(FakeProcess(exits=1), {"errors": [], "cleanup": "PASS"})
        self.assertIn("FIFO injector exited 1", result["errors"])
        result, _, _, _ = self.run_child(FakeProcess(), {"errors": "none"})
        self.assertIn("malformed FIFO injector report", result["errors"])
        hung = FakeProcess(hang=True)
        result, _, _, _ = self.run_child(hung, {"errors": [], "cleanup": "PASS"})
        self.assertIn("FIFO injector did not exit after its teardown request", result["errors"])
        self.assertEqual(hung.signals, ["term"])
        with tempfile.TemporaryDirectory() as temp:
            control = self.control(Path(temp))
            with patch("queue_control.subprocess.Popen", side_effect=OSError("no ip")):
                control.begin()
            result = control.finish(cancelled=True)
            self.assertTrue(any("could not start" in error for error in result["errors"]))
            self.assertIs(result["cancelled"], True)

    def test_parent_refuses_unowned_namespaces_wrong_kind_and_short_phases(self):
        now = time.monotonic()
        args = (now + 1, now + 9, now + 25, now + 33, "/nonexistent")
        with patch("isolation.verify", return_value=[]):
            for config, namespace in ((CONFIG, "host"), (CONFIG, "rig-abcdef-target"),
                                      (dict(CONFIG, kind="netem-delay"), "rig-abcdef-worker")):
                with self.assertRaises(ValueError):
                    FifoQueueControl(config, namespace, *args)
            with self.assertRaises(ValueError):
                FifoQueueControl(CONFIG, "rig-abcdef-worker", now + 1, now + 9, now + 9.7, now + 12, "/nonexistent")
        with patch("isolation.verify", return_value=["not isolated"]):
            with self.assertRaises(ValueError):
                FifoQueueControl(CONFIG, "rig-abcdef-worker", *args)


if __name__ == "__main__":
    unittest.main()
