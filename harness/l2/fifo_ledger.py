"""Raw per-frame ledger of the persistent FIFO delay line, and its oracle.

The injector only appends records. This module independently recomputes
every phase, release time, order, occupancy and kernel counter closure from
those records and raw counter snapshots; report summaries are never trusted.
Qualification constants below are pre-registered and frozen with schema 4.
"""
import gzip
import hashlib
import io
import struct

KIND = "fifo-delay-line"
SCHEMA_VERSION = 4
DRAIN_GUARD_SEC = .25
DRAIN_RATE_BPS = 2_000_000_000
LATENESS_LIMIT_NS = 20_000_000
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
MAX_FRAMES = 4_000_000
INSTALL_LEAD_NS = 500_000_000
COVERAGE_NS = 1_000_000_000
TAP = "qa44fifo"
ACTION_INDEX = 44
FILTER_PREF = 44
STRATEGY = {"kind": "tc-mirred-tap-userspace-fifo", "path": "persistent from before baseline until after recovery",
            "active": "release = arrival + delay_ms", "drain": "FIFO behind backlog at drain_rate_bps",
            "drain_rate_bps": DRAIN_RATE_BPS, "transparent": "release = arrival once the FIFO is empty",
            "lateness_limit_ns": LATENESS_LIMIT_NS, "memory_limit_bytes": MEMORY_LIMIT_BYTES,
            "max_frames": MAX_FRAMES}

PRE, ACTIVE, DRAIN, TRANSPARENT = range(4)
PHASES = ("pre", "active", "drain", "transparent")
DELIVERED, OVERFLOW, SEND_FAILED, UNSENT = 1, 2, 3, 4
NOT_SENT = 0xFFFFFFFF
# seq, phase, outcome, gso_type, gso_size, frame bytes (without vnet header),
# arrival ns, release offset, transmit offset, blake2b-64 of vnet header+frame,
# parsed L2-L4 header bytes (0 when not TCP/UDP).
RECORD = struct.Struct("<IBBBxHIQIIQH")
RECORD_FORMAT = "<IBBBxHIQIIQH gzip"
LEDGER_VERSION = 2
HEADER = struct.Struct("<8sHHQ")
MAGIC = b"QA44FIFO"
GSO_TYPE_MASK = 0x7F


def tx_ns(length):
    return (length * 8 * 1_000_000_000 + DRAIN_RATE_BPS - 1) // DRAIN_RATE_BPS


def segments(row):
    """Kernel accounting of one redirected skb: (gso_segs, qdisc_pkt_len).

    tc action stats count a GSO skb as gso_segs packets of
    len + (gso_segs - 1) * header bytes (qdisc_pkt_len_init); the tap counts
    it once at len. Returns None when a GSO frame's header cannot be bound.
    """
    gso_type, gso_size, length, header = row[3] & GSO_TYPE_MASK, row[4], row[5], row[10]
    if not gso_type:
        return 1, length
    if not gso_size or not header or header >= length:
        return None
    count = -(-(length - header) // gso_size)
    return count, length + (count - 1) * header


def encode(records):
    """Gzip a complete seq-ordered ledger; mtime 0 keeps the hash stable."""
    body = b"".join(RECORD.pack(*row) for row in records)
    return gzip.compress(HEADER.pack(MAGIC, LEDGER_VERSION, RECORD.size, len(records)) + body, mtime=0)


def decode(blob):
    if not isinstance(blob, (bytes, bytearray)) or len(blob) > 64 * 1024 * 1024:
        raise ValueError("missing or oversized FIFO ledger")
    limit = HEADER.size + MAX_FRAMES * RECORD.size
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or len(data) < HEADER.size:
        raise ValueError("FIFO ledger exceeds its bounded size or lacks a header")
    magic, version, size, count = HEADER.unpack_from(data)
    if magic != MAGIC or version != LEDGER_VERSION or size != RECORD.size or len(data) != HEADER.size + count * RECORD.size:
        raise ValueError("FIFO ledger header or record count differs from its content")
    return [RECORD.unpack_from(data, HEADER.size + index * RECORD.size) for index in range(count)]


def _int(value):
    return type(value) is int and 0 <= value < 2**64


def _stats(row, keys):
    if not isinstance(row, dict) or any(not _int(row.get(key)) for key in keys):
        raise ValueError("missing or invalid raw kernel counters")
    return row


def _qdiscs(rows, *, installed):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("missing raw eth0 qdisc listing")
    roots = [row for row in rows if row.get("root") is True]
    others = [row for row in rows if row.get("root") is not True]
    if len(roots) != 1 or roots[0].get("kind") != "noqueue":
        raise ValueError("eth0 root qdisc is not the original noqueue")
    expected = [{"kind": "clsact"}] if installed else []
    if [{"kind": row.get("kind")} for row in others] != expected:
        raise ValueError("eth0 auxiliary qdiscs differ from the owned clsact hook")


def _expected_phase(t_in, planned, transparent_ns):
    if t_in >= transparent_ns:
        return TRANSPARENT
    if t_in < planned["start_ns"]:
        return PRE
    return ACTIVE if t_in < planned["end_ns"] else DRAIN


def validate(report, config, blob):
    """Recompute FIFO qualification from raw records and counters, never `ok`."""
    errors = []
    try:
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
                or report["schema_version"] != SCHEMA_VERSION or report.get("config") != config
                or report.get("strategy") != STRATEGY or report.get("clock") != "CLOCK_MONOTONIC"):
            raise ValueError("FIFO report configuration or frozen strategy differs from the case")
        if report.get("errors") != [] or report.get("cancelled") is not False:
            errors.append("FIFO injector execution was cancelled or incomplete")
        if report.get("cleanup") != "PASS":
            errors.append("FIFO injector cleanup did not complete")
        ledger = report.get("ledger")
        if (not isinstance(ledger, dict) or not isinstance(blob, (bytes, bytearray))
                or ledger.get("sha256") != hashlib.sha256(blob).hexdigest()):
            raise ValueError("raw FIFO ledger is missing or differs from the report digest")
        rows = decode(blob)
        if ledger.get("frames") != len(rows):
            raise ValueError("FIFO ledger frame count differs from the report")
        planned, actual = report.get("planned"), report.get("actual")
        keys = ("measurement_start_ns", "start_ns", "end_ns", "recovery_start_ns", "measurement_end_ns")
        if (not isinstance(planned, dict) or any(not _int(planned.get(key)) for key in keys)
                or not all(planned[a] < planned[b] for a, b in zip(keys, keys[1:]))):
            raise ValueError("missing or unordered frozen FIFO schedule")
        delay_ns = round(config["delay_ms"] * 1_000_000)
        if abs(planned["recovery_start_ns"] - planned["end_ns"] - round((config["delay_ms"] / 1000 + DRAIN_GUARD_SEC) * 1e9)) > 1:
            errors.append("FIFO drain guard differs from the frozen interval")
        akeys = ("installed_ns", "transparent_ns", "removed_ns", "closed_ns")
        if not isinstance(actual, dict) or any(not _int(actual.get(key)) for key in akeys):
            raise ValueError("missing FIFO install/transparent/removal timestamps")
        if actual["installed_ns"] > planned["measurement_start_ns"] - INSTALL_LEAD_NS:
            errors.append("FIFO path was not installed before the measured baseline")
        if not planned["end_ns"] <= actual["transparent_ns"] < planned["recovery_start_ns"]:
            errors.append("FIFO did not switch to the transparent path before the recovery boundary")
        if not planned["measurement_end_ns"] <= actual["removed_ns"] <= actual["closed_ns"]:
            errors.append("FIFO path was removed before the full measurement ended")
        kernel = report.get("kernel")
        if not isinstance(kernel, dict):
            raise ValueError("missing raw FIFO kernel counters")
        _qdiscs(kernel.get("qdisc_before"), installed=False)
        _qdiscs(kernel.get("qdisc_installed"), installed=True)
        _qdiscs(kernel.get("qdisc_restored"), installed=False)
        try:
            _qdiscs(kernel.get("tap_qdisc"), installed=False)
        except ValueError:
            errors.append("FIFO tap has its own queue in front of the single FIFO")
        tap_keys = ("packets", "bytes", "dropped", "errors")
        before = {d: _stats((kernel.get("tap_before") or {}).get(d), tap_keys) for d in ("rx", "tx")}
        after = {d: _stats((kernel.get("tap_after") or {}).get(d), tap_keys) for d in ("rx", "tx")}
        action = _stats(kernel.get("action"), ("packets", "bytes", "drops", "overlimits"))
        deltas = {d: {k: after[d][k] - before[d][k] for k in tap_keys} for d in ("rx", "tx")}
        if any(value < 0 for row in deltas.values() for value in row.values()):
            errors.append("FIFO tap counters reset")
        if deltas["tx"]["dropped"] or deltas["tx"]["errors"] or action["drops"] or action["overlimits"]:
            errors.append("redirect or tap ring dropped frames before the FIFO")
        if any(deltas["rx"].values()):
            errors.append("unexpected frames were written into the FIFO tap")
        accounted = [segments(row) for row in rows]
        if None in accounted:
            errors.append("FIFO ledger has a GSO frame whose segment header cannot be bound")
        elif (action["packets"] != sum(count for count, _ in accounted)
                or action["bytes"] != sum(size for _, size in accounted)):
            errors.append("FIFO ledger does not close against redirect segment counters")
        if deltas["tx"]["packets"] != len(rows):
            errors.append("FIFO ledger does not close against tap frame counters")
        if deltas["tx"]["bytes"] != sum(row[5] for row in rows):
            errors.append("FIFO ledger bytes differ from tap counters")
        transparent_ns = actual["transparent_ns"]
        previous_due = previous_out = previous_in = None
        released, occupancy, held_bytes, max_held, max_bytes = 0, 0, 0, 0, 0
        seen = [0] * 4
        queued_active = 0
        for index, row in enumerate(rows):
            seq, phase, outcome, _, _, length, t_in, due_off, out_off, _, _ = row
            due, t_out = t_in + due_off, t_in + out_off
            if seq != index:
                errors.append("FIFO ledger has a missing, duplicate or reordered sequence")
                break
            if outcome != DELIVERED or out_off == NOT_SENT:
                errors.append("FIFO frame was dropped, failed or left unsent: seq " + str(seq))
                break
            if previous_in is not None and t_in < previous_in:
                errors.append("FIFO arrivals are not monotonic")
                break
            if t_in < actual["installed_ns"] or t_out > actual["closed_ns"]:
                errors.append("FIFO frame lies outside the owned path lifetime")
                break
            expected = _expected_phase(t_in, planned, transparent_ns)
            if phase != expected:
                errors.append(f"FIFO frame {seq} phase {phase} differs from recomputed {expected}")
                break
            floor = previous_due or 0
            if expected == ACTIVE:
                wanted = max(t_in + delay_ns, floor)
            elif expected == DRAIN:
                wanted = max(t_in, floor) + tx_ns(length)
                if previous_out is not None and previous_out < t_in:
                    errors.append("FIFO kept shaping after its queue was empty")
                    break
            else:
                wanted = t_in
                if floor > t_in:
                    errors.append("FIFO transparent frame queued behind an unfinished backlog")
                    break
            if due != wanted:
                errors.append(f"FIFO release time of frame {seq} differs from the frozen policy")
                break
            if t_out < due or t_out - due > LATENESS_LIMIT_NS:
                errors.append(f"FIFO frame {seq} was released early or exceeded the lateness bound")
                break
            if previous_out is not None and t_out < previous_out:
                errors.append("FIFO transmissions are out of order")
                break
            while released < index and rows[released][6] + rows[released][8] <= t_in:
                occupancy -= 1
                held_bytes -= rows[released][5]
                released += 1
            occupancy += 1
            held_bytes += length
            max_held, max_bytes = max(max_held, occupancy), max(max_bytes, held_bytes)
            if expected == ACTIVE and occupancy > 1:
                queued_active += 1
            seen[expected] += 1
            previous_due, previous_out, previous_in = due, t_out, t_in
        else:
            if max_held > config["limit_packets"] or max_bytes > MEMORY_LIMIT_BYTES:
                errors.append("FIFO occupancy exceeded its frozen packet or memory bound")
            if not queued_active or not seen[ACTIVE]:
                errors.append("FIFO did not observe real queued and transmitted loaded traffic")
            if not seen[PRE] or not seen[TRANSPARENT]:
                errors.append("FIFO did not carry traffic in every measured phase")
            if (not rows or rows[0][6] > planned["start_ns"] - COVERAGE_NS
                    or rows[-1][6] < planned["measurement_end_ns"] - COVERAGE_NS
                    or not any(row[6] >= planned["recovery_start_ns"] for row in rows)):
                errors.append("FIFO ledger does not cover baseline, recovery and measurement end")
            if any(row[6] + row[8] > transparent_ns for row in rows if row[1] != TRANSPARENT):
                errors.append("FIFO declared transparent before its backlog was empty")
            report_resources = report.get("resources")
            if (not isinstance(report_resources, dict)
                    or any(not _int(report_resources.get(key)) for key in ("max_frames", "max_bytes"))
                    or report_resources["max_frames"] > config["limit_packets"]
                    or report_resources["max_bytes"] > MEMORY_LIMIT_BYTES):
                errors.append("FIFO resource guard evidence is missing or exceeded")
    except (ValueError, TypeError, KeyError, AttributeError, OSError, EOFError, struct.error) as error:
        errors.append(str(error))
    return sorted(set(errors))


def summary(rows):
    """Descriptive per-phase residence; qualification uses `validate` only."""
    phases = {}
    for row in rows:
        values = phases.setdefault(PHASES[row[1]], [])
        if row[8] != NOT_SENT:
            values.append(row[8])
    result = {}
    for name, values in phases.items():
        values.sort()
        result[name] = {"frames": len(values), "max_residence_ns": values[-1] if values else None,
                        "p99_residence_ns": values[min(len(values) - 1, int(len(values) * .99))] if values else None}
    return result
