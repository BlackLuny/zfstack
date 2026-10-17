"""The persistent netem FIFO oracle pairs raw captures itself and fails closed."""
import copy
import hashlib
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import netem_fifo
import netem_fifo_ledger as N
from queue_control import FifoQueueControl, LEDGER_NAME, configuration, validate_evidence
from test_queue_links import link_fixture

S, MS, US = 1_000_000_000, 1_000_000, 1_000
OFFSET = 1_700_000_000 * S
CONFIG = {"kind": N.KIND, "node": "worker", "interface": "eth0", "phase": "loaded",
          "delay_ms": 500.0, "limit_packets": 100000}
PLANNED = {"measurement_start_ns": 1 * S, "start_ns": 9 * S, "end_ns": 24 * S + 250 * MS,
           "recovery_start_ns": 25 * S, "measurement_end_ns": 33 * S}
PERIOD = 4 * MS
NOQUEUE = {"kind": "noqueue", "handle": "0:", "root": True, "refcnt": 2, "options": {}}
CLSACT = {"kind": "clsact", "handle": "ffff:", "parent": "ffff:fff1", "options": {}}


def netem_root(phase, config=CONFIG, **stats):
    return dict({"kind": "netem", "handle": N.HANDLE, "root": True, "refcnt": 2,
                 "options": N.expected_options(phase, config)}, **stats)


def simulate(config=CONFIG, *, shift=None, flow_of=None, transparent_at=PLANNED["end_ns"] + 600 * MS, burst=()):
    """Replay netem rate chaining in capture (realtime) clock."""
    shift, flow_of = shift or {}, flow_of or {}
    delay_ns = round(config["delay_ms"] * MS)
    changes = [{"phase": "active", "t0_ns": PLANNED["start_ns"] + 100 * US, "t1_ns": PLANNED["start_ns"] + 300 * US},
               {"phase": "drain", "t0_ns": PLANNED["end_ns"] + 100 * US, "t1_ns": PLANNED["end_ns"] + 300 * US},
               {"phase": "transparent", "t0_ns": transparent_at, "t1_ns": transparent_at + 200 * US, "reason": "empty"}]
    for row, phase in zip(changes, (N.ACTIVE, N.DRAIN, N.TRANSPARENT)):
        row["shown"] = N.expected_options(phase, config)
    arrivals = [(t, 1000, 42, 0, 0) if seq % 5 else (t, 66 + 2 * 1448, 66, 1, 1448)
                for seq, t in enumerate(range(300 * MS, 33_500 * MS, PERIOD))]
    for at, count, length, gso_size in burst:
        arrivals += [(at + index * US, length, 66, 1, gso_size) for index in range(count)]
    arrivals.sort()
    in_rows, out_rows, last = [], [], 0
    for seq, (mono, length, header, gso_type, gso_size) in enumerate(arrivals):
        mono += shift.get(seq, 0)
        phase = N.PRE
        for row, after in zip(changes, (N.ACTIVE, N.DRAIN, N.TRANSPARENT)):
            if mono >= row["t0_ns"]:
                phase = after
        delay, rate = N.policy(phase, delay_ns)
        size = N.segments(length, gso_type, gso_size, header)[1]
        last = max(mono + delay, last) + size * S // rate
        flow = flow_of.get(seq, 1 + seq % 3)
        in_rows.append((mono + OFFSET, length, header, gso_type, 0, gso_size, 0, flow, seq * 7919 + 1))
        out_rows.append((last + 10 * US + OFFSET, length, 0, seq * 7919 + 1))
    pre = [(200 * MS - 50 * MS + index + OFFSET, 60, 0, 99 + index) for index in range(2)]
    return in_rows, pre + out_rows, changes


def counters(report, in_rows, out_rows):
    accounted = [N.segments(row[1], row[3], row[5], row[2]) or (1, row[1]) for row in in_rows]
    kernel = report["kernel"]
    kernel["action"] = {"packets": sum(c for c, _ in accounted), "bytes": sum(b for _, b in accounted),
                        "drops": 0, "overlimits": 0}
    zero = {"packets": 0, "bytes": 0, "dropped": 0, "errors": 0}
    kernel["dummy_before"] = {"rx": dict(zero), "tx": dict(zero)}
    kernel["dummy_after"] = {"rx": dict(zero), "tx": dict(zero, packets=len(in_rows), bytes=sum(r[1] for r in in_rows))}
    kernel["capture"] = {"in": {"packets": len(in_rows), "drops": 0, "freezes": 0},
                         "out": {"packets": len(out_rows), "drops": 0, "freezes": 0}}


def seal(report, in_rows, out_rows, *, recount=True):
    blob = N.encode(in_rows, out_rows)
    report["ledger"] = {"path": LEDGER_NAME, "sha256": hashlib.sha256(blob).hexdigest(),
                        "in_frames": len(in_rows), "out_frames": len(out_rows), "record_format": N.RECORD_FORMAT}
    if recount:
        counters(report, in_rows, out_rows)
    return blob


def netem_fixture(config=CONFIG, **options):
    in_rows, out_rows, changes = simulate(config, **options)
    samples = [[mono, mono + 500 + OFFSET, mono + 1000] for mono in range(150 * MS, 33_700 * MS, 100 * MS)]
    report = {"schema_version": N.SCHEMA_VERSION, "strategy": dict(N.STRATEGY), "config": copy.deepcopy(config),
              "clock": "CLOCK_MONOTONIC", "planned": dict(PLANNED),
              "actual": {"installed_ns": 100 * MS, "mirror_installed_ns": 200 * MS, "changes": changes,
                         "mirror_remove_ns": 33_600 * MS, "mirror_removed_ns": 33_610 * MS,
                         "drained_ns": 33_620 * MS, "removed_ns": 33_700 * MS, "closed_ns": 33_800 * MS},
              "clock_samples": samples,
              "kernel": {"qdisc_before": [dict(NOQUEUE)], "qdisc_installed": [netem_root(N.PRE, config), dict(CLSACT)],
                         "qdisc_final": [netem_root(N.TRANSPARENT, config, packets=len(out_rows), bytes=1, drops=0,
                                                    overlimits=0, requeues=0, backlog=0, qlen=0), dict(CLSACT)],
                         "qdisc_restored": [dict(NOQUEUE)]},
              "resources": {"ring_bytes": 1, "copied_bytes": 1}, "errors": [], "cancelled": False, "cleanup": "PASS"}
    return report, in_rows, out_rows, seal(report, in_rows, out_rows)


def index_at(in_rows, mono):
    return next(index for index, row in enumerate(in_rows) if row[0] - OFFSET >= mono)


def replace(rows, index, position, value):
    row = list(rows[index])
    row[position] = value
    rows[index] = tuple(row)


class NetemLedgerTests(unittest.TestCase):
    def check(self, mutate, expected, *, config=CONFIG, recount=True, **options):
        report, in_rows, out_rows, _ = netem_fixture(config, **options)
        in_rows, out_rows = list(in_rows), list(out_rows)
        blob = mutate(report, in_rows, out_rows)
        if not isinstance(blob, bytes):
            blob = seal(report, in_rows, out_rows, recount=recount)
        errors = N.validate(report, config, blob)
        self.assertTrue(any(expected in error for error in errors), errors)
        return errors

    def test_chained_replay_passes_with_pre_mirror_frames_and_ambiguous_changes(self):
        report, in_rows, out_rows, blob = netem_fixture()
        self.assertEqual(N.validate(report, CONFIG, blob), [])
        self.assertEqual(N.decode(blob), (in_rows, out_rows))
        self.assertEqual(N.encode(in_rows, out_rows), blob)
        windows = [(phase, (row["t0_ns"] + OFFSET - 3 * MS, row["t1_ns"] + OFFSET + MS))
                   for row, phase in zip(report["actual"]["changes"], (N.ACTIVE, N.DRAIN, N.TRANSPARENT))]
        self.assertTrue(any(len(N._phase_at(row[0], windows)) > 1 for row in in_rows))
        summary = N.summary(in_rows, out_rows)
        self.assertEqual((summary["lost"], summary["unpaired_out"]), (0, 2))
        self.assertGreaterEqual(summary["residence_ns"]["max"], 500 * MS)

    def test_cross_cpu_capture_skew_is_bounded_not_waived(self):
        k = index_at(simulate()[0], 12 * S)
        report, in_rows, out_rows, blob = netem_fixture(shift={k + 1: -5 * MS})
        self.assertEqual(N.validate(report, CONFIG, blob), [])
        self.check(lambda r, i, o: None, "ahead of an earlier arrival", shift={k + 1: -7 * MS})

    def test_lost_duplicate_and_bypassing_frames_fail(self):
        k = index_at(simulate()[0], 12 * S)
        self.check(lambda r, i, o: o.pop(k + 2) and None, "lost or never released")
        self.check(lambda r, i, o: i.insert(k, i[k]), "lost or never released")
        self.check(lambda r, i, o: o.insert(k + 2, (o[k + 2][0], 60, 0, 12345)), "bypassed the mirrored FIFO")

    def test_reordering_fails_globally_and_per_flow(self):
        k = index_at(simulate()[0], 12 * S)

        def swap(a, b):
            def mutate(r, i, o):
                o[a + 2], o[b + 2] = (o[a + 2][0], *o[b + 2][1:]), (o[b + 2][0], *o[a + 2][1:])
            return mutate
        self.check(swap(k, k + 10), "before its frozen release time")
        # Drain frames wait in one chained backlog, so swapping two arrivals
        # 40 ms apart keeps every release bound and only breaks FIFO order.
        drain = index_at(simulate()[0], PLANNED["end_ns"] + 10 * MS)
        self.check(swap(drain, drain + 10), "ahead of an earlier arrival")
        flows = {k: 7, k + 1: 7}
        self.check(swap(k, k + 1), "reordered flow frames", shift={k + 1: -5 * MS}, flow_of=flows)

    def test_release_bounds_follow_the_netem_chain(self):
        k = index_at(simulate()[0], 12 * S)
        self.check(lambda r, i, o: replace(i, k, 0, i[k][0] + 2 * MS), "before its frozen release time")
        self.check(lambda r, i, o: replace(o, k + 2, 0, o[k + 2][0] + N.LATENESS_LIMIT_NS + MS), "lateness bound")
        at, count = PLANNED["end_ns"] + 11 * MS, 100
        burst = [(at, count, 66 + 44 * 1448, 1448)]
        report, _, _, blob = netem_fixture(burst=burst)
        self.assertEqual(N.validate(report, CONFIG, blob), [])

        def faster_than_drain_rate(r, i, o):
            first = index_at(i, at) + 2
            for j in range(count):
                replace(o, first + j, 0, o[first][0] + j * US)
        self.check(faster_than_drain_rate, "before its frozen release time", burst=burst)

    def test_phase_changes_are_bounded_and_read_back(self):
        def change(index, **fields):
            return lambda r, i, o: r["actual"]["changes"][index].update(fields)
        self.check(change(0, t0_ns=PLANNED["start_ns"] - 1), "missed its planned boundary")
        self.check(change(1, t1_ns=PLANNED["end_ns"] + N.PHASE_TOLERANCE_NS + 1), "missed its planned boundary")
        self.check(change(0, t1_ns=PLANNED["start_ns"] + 100 * US + N.CHANGE_WINDOW_LIMIT_NS + 1), "bounded window")
        shown = N.expected_options(N.DRAIN, CONFIG)
        shown["rate"] = dict(shown["rate"], rate=125_000_000)
        self.check(change(1, shown=shown), "options after a phase change")
        self.check(change(0, shown=dict(N.expected_options(N.ACTIVE, CONFIG), loss=1)), "options after a phase change")
        self.check(lambda r, i, o: r["actual"]["changes"].reverse(), "missing, reordered or malformed")
        self.check(lambda r, i, o: r["actual"]["changes"].pop(), "missing, reordered or malformed")
        self.check(change(2, reason="later"), "transparent path before the recovery boundary")
        self.check(change(2, reason="deadline"), "transparent path before the recovery boundary")
        late = PLANNED["recovery_start_ns"] + MS
        self.check(lambda r, i, o: None, "transparent path before the recovery boundary", transparent_at=late)
        self.check(lambda r, i, o: None, "recovery frame was not on the transparent path", transparent_at=late)

    def test_recovery_frames_cannot_wait_behind_a_drain_backlog(self):
        options = {"transparent_at": PLANNED["recovery_start_ns"] - 5 * MS,
                   "burst": [(PLANNED["recovery_start_ns"] - 30 * MS, 300, 66 + 44 * 1448, 1448)]}
        self.check(lambda r, i, o: None, "queued behind an unfinished backlog", **options)

    def test_capture_and_kernel_counters_must_close(self):
        def kernel(path, value):
            def mutate(r, i, o):
                node = r["kernel"]
                for key in path[:-1]:
                    node = node[key]
                node[path[-1]] = value
            return mutate
        self.check(kernel(("capture", "in", "drops"), 1), "capture ring dropped", recount=False)
        self.check(kernel(("capture", "out", "drops"), 1), "capture ring dropped", recount=False)
        report = netem_fixture()[0]
        self.check(kernel(("capture", "out", "packets"), report["kernel"]["capture"]["out"]["packets"] + 1),
                   "capture ring counters", recount=False)
        self.check(kernel(("action", "packets"), report["kernel"]["action"]["packets"] - 1), "mirror segment counters", recount=False)
        self.check(kernel(("action", "drops"), 1), "arrival mirror dropped", recount=False)
        self.check(kernel(("dummy_after", "tx", "packets"), 1), "capture device counters", recount=False)
        self.check(kernel(("dummy_after", "tx", "dropped"), 1), "reset or dropped", recount=False)
        self.check(lambda r, i, o: r["kernel"]["qdisc_final"][0].update(drops=1), "dropped or overlimited")
        self.check(lambda r, i, o: r["kernel"]["qdisc_final"][0].update(backlog=10, qlen=1), "not empty before removal")
        self.check(kernel(("qdisc_final",), [netem_root(N.DRAIN, drops=0, overlimits=0, backlog=0, qlen=0, packets=1, bytes=1),
                                             dict(CLSACT)]), "root changed before its drained removal")
        self.check(kernel(("qdisc_installed",), [netem_root(N.ACTIVE), dict(CLSACT)]), "pre-phase policy")
        self.check(kernel(("qdisc_before",), [netem_root(N.PRE)]), "bare noqueue")
        self.check(kernel(("qdisc_restored",), [dict(NOQUEUE), dict(CLSACT)]), "not restored")

    def test_clock_mapping_must_be_stable_and_cover_the_capture(self):
        def drift(r, i, o):
            r["clock_samples"][100][1] += 2 * MS
        self.check(drift, "realtime clock drifted")
        self.check(lambda r, i, o: r["clock_samples"].__delitem__(slice(100, 120)), "do not cover the capture lifetime")
        self.check(lambda r, i, o: r["clock_samples"].pop(0), "do not cover the capture lifetime")
        self.check(lambda r, i, o: r.update(clock_samples=[]), "clock samples")

    def test_lifetime_execution_and_report_identity(self):
        def actual(**fields):
            return lambda r, i, o: r["actual"].update(fields)
        self.check(actual(mirror_installed_ns=PLANNED["measurement_start_ns"] - N.INSTALL_LEAD_NS + 1), "before the measured baseline")
        self.check(actual(mirror_remove_ns=PLANNED["measurement_end_ns"] - 1), "removed before the full measurement")
        self.check(actual(removed_ns=33_605 * MS), "removed before the full measurement")
        self.check(lambda r, i, o: r.update(cancelled=True), "cancelled or incomplete")
        self.check(lambda r, i, o: r["errors"].append("x"), "cancelled or incomplete")
        self.check(lambda r, i, o: r.update(cleanup="FAIL"), "cleanup did not complete")
        self.check(lambda r, i, o: r.update(strategy=dict(N.STRATEGY, order_skew_ns=10 * MS)), "frozen strategy")
        report, in_rows, out_rows, blob = netem_fixture()
        self.assertTrue(N.validate(report, dict(CONFIG, delay_ms=400.0), blob))
        self.assertIn("differs from the report digest", " ".join(N.validate(report, CONFIG, blob + b"\0")))
        report["ledger"]["out_frames"] += 1
        self.assertIn("frame counts differ", " ".join(N.validate(report, CONFIG, blob)))

    def test_gso_coverage_and_occupancy_bounds(self):
        k = index_at(simulate()[0], 12 * S)
        gso = next(index for index in range(k, k + 10) if simulate()[0][index][3])
        self.check(lambda r, i, o: replace(i, gso, 2, 0), "segment header cannot be bound")
        late = [index for index, row in enumerate(simulate()[0]) if row[0] - OFFSET >= PLANNED["recovery_start_ns"]]

        def drop_recovery(r, i, o):
            keep = {row[8] for row in i[:late[0]]}
            i[:] = i[:late[0]]
            o[:] = [row for row in o if row[3] in keep or row[1] == 60]
        self.check(drop_recovery, "do not cover baseline, recovery")
        small = dict(CONFIG, limit_packets=100)
        self.check(lambda r, i, o: None, "occupancy exceeded", config=small)

    def test_decode_rejects_malformed_ledgers(self):
        report, in_rows, out_rows, blob = netem_fixture()
        for bad in (b"", b"x" * 10, N.encode(in_rows, out_rows)[:-8]):
            with self.assertRaises(Exception):
                N.decode(bad)
        import gzip
        header = N.HEADER.pack(N.MAGIC, N.LEDGER_VERSION, N.IN_RECORD.size, N.OUT_RECORD.size, 0, 1, 0)
        with self.assertRaises(ValueError):
            N.decode(gzip.compress(header))

    def test_header_flow_and_options_parsers(self):
        ether = b"\0" * 12 + b"\x08\x00"
        ip = bytes([0x45, 0, 0, 0, 0, 0, 0x40, 0, 64, 6, 0, 0]) + bytes([10, 0, 0, 1, 10, 0, 0, 2])
        tcp = b"\x01\x02\x03\x04" + b"\0" * 8 + bytes([0x80]) + b"\0" * 19
        frame = ether + ip + tcp
        self.assertEqual(N.header_length(frame), 14 + 20 + 32)
        other = ether + ip + b"\x01\x02\x03\x05" + tcp[4:]
        self.assertNotEqual(N.flow_hash(frame), N.flow_hash(other))
        self.assertEqual(N.flow_hash(b"\0" * 12 + b"\x08\x06" + b"\0" * 28), 0)
        self.assertTrue(N.options_match(N.expected_options(N.PRE, CONFIG), N.PRE, CONFIG))
        self.assertTrue(N.options_match(dict(N.expected_options(N.PRE, CONFIG), seed=7), N.PRE, CONFIG))
        zero = dict(N.expected_options(N.DRAIN, CONFIG), delay={"delay": 0, "jitter": 0, "correlation": 0})
        self.assertTrue(N.options_match(zero, N.DRAIN, CONFIG))
        self.assertFalse(N.options_match(N.expected_options(N.PRE, CONFIG), N.ACTIVE, CONFIG))
        self.assertEqual(N.segments(66 + 2 * 1448, 1, 1448, 66), (2, 66 * 2 + 2 * 1448))
        self.assertIsNone(N.segments(1000, 4, 0, 66))


class FakeNetlink:
    def __init__(self, replies):
        self.sent, self.replies = [], list(replies)

    def send(self, data):
        self.sent.append(data)

    def recv(self, _):
        return self.replies.pop(0)(self.sent[-1])

    def close(self):
        pass


def ack(error=0):
    def reply(request):
        seq = struct.unpack_from("I", request, 8)[0]
        return struct.pack("IHHII", 36, netem_fifo.NLMSG_ERROR, 0, seq, 0) + struct.pack("i", error) + request[:16]
    return reply


def qdisc_reply(qlen, handle=N.HANDLE_ID):
    def reply(request):
        seq = struct.unpack_from("I", request, 8)[0]
        stats = netem_fifo._attr(netem_fifo.TCA_STATS_QUEUE, struct.pack("5I", qlen, 0, 0, 0, 0))
        body = struct.pack("BBHiIII", 0, 0, 0, 2, handle, netem_fifo.TC_H_ROOT, 0)
        body += netem_fifo._attr(netem_fifo.TCA_KIND, b"netem\0") + netem_fifo._attr(netem_fifo.TCA_STATS2, stats)
        message = struct.pack("IHHII", 16 + len(body), netem_fifo.RTM_NEWQDISC, 0, seq, 0) + body
        return message + ack()(request)
    return reply


class NetemInjectorUnitTests(unittest.TestCase):
    def client(self, *replies):
        client = netem_fifo.Netlink.__new__(netem_fifo.Netlink)
        client.ifindex, client.seq, client.sock = 2, 0, FakeNetlink(replies)
        return client

    def test_change_sends_one_in_place_netem_change(self):
        client = self.client(ack(), ack())
        client.change(500 * MS, N.rate_bytes(N.TRANSPARENT_RATE_BPS), 100000)
        client.change(0, N.rate_bytes(N.DRAIN_RATE_BPS), 100000)
        first, second = client.sock.sent
        size, kind, flags = struct.unpack_from("IHH", first)
        self.assertEqual((size, kind, flags), (len(first), netem_fifo.RTM_NEWQDISC, netem_fifo.NLM_F_REQUEST | netem_fifo.NLM_F_ACK))
        _, _, _, ifindex, handle, parent, _ = struct.unpack_from("BBHiIII", first, 16)
        self.assertEqual((ifindex, handle, parent), (2, N.HANDLE_ID, netem_fifo.TC_H_ROOT))
        attrs = netem_fifo._attrs(first[36:])
        self.assertEqual(attrs[netem_fifo.TCA_KIND], b"netem\0")
        options = attrs[netem_fifo.TCA_OPTIONS]
        self.assertEqual(struct.unpack_from("6I", options), (0, 100000, 0, 0, 0, 0))
        nested = netem_fifo._attrs(options[24:])
        self.assertEqual(struct.unpack("q", nested[netem_fifo.TCA_NETEM_LATENCY64])[0], 500 * MS)
        self.assertEqual(struct.unpack("Q", nested[netem_fifo.TCA_NETEM_RATE64])[0], N.rate_bytes(N.TRANSPARENT_RATE_BPS))
        drain = netem_fifo._attrs(netem_fifo._attrs(second[36:])[netem_fifo.TCA_OPTIONS][24:])
        self.assertEqual(struct.unpack_from("I", drain[netem_fifo.TCA_NETEM_RATE])[0], N.rate_bytes(N.DRAIN_RATE_BPS))
        self.assertNotIn(netem_fifo.TCA_NETEM_RATE64, drain)
        self.assertEqual(struct.unpack("q", drain[netem_fifo.TCA_NETEM_LATENCY64])[0], 0)

    def test_change_errors_and_qlen_parsing(self):
        with self.assertRaises(OSError):
            self.client(ack(-22)).change(0, 1, 100)
        client = self.client(qdisc_reply(7), qdisc_reply(0))
        self.assertEqual((client.qlen(), client.qlen()), (7, 0))
        flags = struct.unpack_from("H", client.sock.sent[0], 6)[0]
        self.assertTrue(flags & netem_fifo.NLM_F_ECHO)
        with self.assertRaises(ValueError):
            self.client(qdisc_reply(0, handle=0x10000)).qlen()

    def test_capture_blocks_parse_kernel_layout(self):
        snap = bytes(range(80))
        vnet = netem_fifo.VNET_HDR.pack(1, 1, 66, 1448, 0, 0)
        mac = 48 + 16 + netem_fifo.VNET_HDR.size
        packet = struct.pack("IIIIIIH", 0, 12, 34, len(snap), 3000, 1, mac).ljust(mac - netem_fifo.VNET_HDR.size, b"\0")
        packet += vnet + snap
        block = struct.pack("IIIIII", 1, 0, 1, 1, 48, 48 + len(packet)).ljust(48, b"\0") + packet
        ring = netem_fifo.Ring.__new__(netem_fifo.Ring)
        ring.blocks = [block]
        self.assertEqual(list(ring.frames()), [(12 * S + 34, 3000, 1, 1448, snap)])


class NetemQueueControlTests(unittest.TestCase):
    def test_configuration_dispatch_and_parent_handle(self):
        self.assertEqual(configuration({"L2_QUEUE_DELAY_MS": "500", "L2_QUEUE_KIND": N.KIND}), CONFIG)
        report, in_rows, out_rows, blob = netem_fixture()
        report["link_samples"] = link_fixture()
        self.assertEqual(validate_evidence(report, CONFIG, blob), [])
        self.assertTrue(validate_evidence(dict(report, link_samples=None), CONFIG, blob))
        with tempfile.TemporaryDirectory() as directory, patch("isolation.verify", return_value=[]):
            now = time.monotonic()
            control = FifoQueueControl(CONFIG, "rig-abcdef-worker", now + 30, now + 38, now + 54, now + 62, directory)
            with patch("queue_control.subprocess.Popen", side_effect=OSError("no ip")) as popen:
                control.begin()
            self.assertEqual(Path(popen.call_args[0][0][5]).name, "netem_fifo.py")
            self.assertEqual(control.ready_by, now + 30 - N.INSTALL_LEAD_NS / 1e9)
            report = control.finish()
            self.assertEqual(report["schema_version"], N.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
