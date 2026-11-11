"""Raw capture ledger of the persistent kernel netem FIFO, and its oracle.

The injector only records kernel-timestamped frames seen before the queue
(a clsact mirror to a private dummy device) and after it (eth0 outgoing), plus
raw kernel counters. This module pairs the two captures itself and recomputes
order, per-frame release bounds from the netem rate-chaining rule, phases,
lifetime and counter closure; report summaries are never trusted. The
constants below are pre-registered and frozen with schema 5.
"""
import collections
import gzip
import hashlib
import io
import struct

KIND = "netem-fifo"
SCHEMA_VERSION = 5
HANDLE = "44:"
HANDLE_ID = 0x00440000
DUMMY = "qa44in"
ACTION_INDEX = 44
FILTER_PREF = 44
DRAIN_GUARD_SEC = .25
DRAIN_RATE_BPS = 2_000_000_000
# netem chains every enqueue behind the last pending deadline only while a
# rate is set; a rate this high adds at most a few ns per frame.
TRANSPARENT_RATE_BPS = 10_000_000_000_000
LATENESS_LIMIT_NS = 20_000_000
EARLY_TOLERANCE_NS = 1_000_000
# Frames from different CPUs pass the mirror before they serialize on the
# qdisc lock, so their capture order may differ by this much from FIFO order.
ORDER_SKEW_NS = 2_000_000
CHANGE_WINDOW_LIMIT_NS = 5_000_000
PHASE_TOLERANCE_NS = 5_000_000
TRANSPARENT_DEADLINE_LEAD_NS = 50_000_000
CLOCK_DRIFT_LIMIT_NS = 1_000_000
CLOCK_SAMPLE_GAP_NS = 1_000_000_000
INSTALL_LEAD_NS = 500_000_000
COVERAGE_NS = 1_000_000_000
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
MAX_FRAMES = 2_000_000
SNAPLEN = 96
STRATEGY = {"kind": "persistent-root-netem-rate-chained-fifo",
            "path": "one root netem on worker eth0 from before baseline until after recovery; never replaced or reset during measurement",
            "pre": "delay 0, rate transparent_rate_bps", "active": "delay delay_ms, rate transparent_rate_bps",
            "drain": "delay 0, rate drain_rate_bps", "transparent": "delay 0, rate transparent_rate_bps once qlen is observed empty or at the deadline",
            "release": "max(arrival + delay, previous release) + qdisc_pkt_len / rate",
            "evidence": "clsact matchall mirred mirror to a dummy device and eth0 outgoing, both TPACKET_V3 with kernel timestamps",
            "drain_rate_bps": DRAIN_RATE_BPS, "transparent_rate_bps": TRANSPARENT_RATE_BPS,
            "lateness_limit_ns": LATENESS_LIMIT_NS, "early_tolerance_ns": EARLY_TOLERANCE_NS,
            "order_skew_ns": ORDER_SKEW_NS, "change_window_limit_ns": CHANGE_WINDOW_LIMIT_NS,
            "phase_tolerance_ns": PHASE_TOLERANCE_NS, "transparent_deadline_lead_ns": TRANSPARENT_DEADLINE_LEAD_NS,
            "clock_drift_limit_ns": CLOCK_DRIFT_LIMIT_NS, "memory_limit_bytes": MEMORY_LIMIT_BYTES,
            "max_frames": MAX_FRAMES, "snaplen": SNAPLEN}

PRE, ACTIVE, DRAIN, TRANSPARENT = range(4)
PHASES = ("pre", "active", "drain", "transparent")
CHANGES = ("active", "drain", "transparent")
# in: arrival real ns, frame bytes, parsed L2-L4 header bytes, virtio gso
# type, flags, gso size, reserved, flow hash, blake2b-64 of length+snap.
IN_RECORD = struct.Struct("<QIHBBHHIQ")
# out: release real ns, frame bytes, reserved, blake2b-64 of length+snap.
OUT_RECORD = struct.Struct("<QIIQ")
RECORD_FORMAT = "<QIHBBHHIQ/<QIIQ gzip"
HEADER = struct.Struct("<8sHHHHQQ")
MAGIC = b"QA44NTEM"
LEDGER_VERSION = 1
GSO_TYPE_MASK = 0x7F


def rate_bytes(bps):
    return bps // 8


def policy(phase, delay_ns):
    """(delay ns, rate bytes/s) of one netem phase."""
    if phase == ACTIVE:
        return delay_ns, rate_bytes(TRANSPARENT_RATE_BPS)
    if phase == DRAIN:
        return 0, rate_bytes(DRAIN_RATE_BPS)
    return 0, rate_bytes(TRANSPARENT_RATE_BPS)


def expected_options(phase, config):
    """tc JSON options a phase must render; zero delay is omitted by tc."""
    delay_ns, rate = policy(phase, round(config["delay_ms"] * 1_000_000))
    options = {"limit": config["limit_packets"], "rate": {"rate": rate, "packetoverhead": 0, "cellsize": 0, "celloverhead": 0},
               "ecn": False, "gap": 0}
    if delay_ns:
        options["delay"] = {"delay": delay_ns / 1e9, "jitter": 0, "correlation": 0}
    return options


def options_match(shown, phase, config):
    if not isinstance(shown, dict):
        return False
    extra = set(shown) - {"limit", "delay", "rate", "ecn", "gap", "seed"}
    expected = expected_options(phase, config)
    observed = {key: value for key, value in shown.items() if key != "seed"}
    if "delay" in observed and "delay" not in expected:
        observed = dict(observed)
        if observed["delay"] != {"delay": 0, "jitter": 0, "correlation": 0}:
            return False
        observed.pop("delay")
    return not extra and observed == expected


def header_length(frame):
    """Ethernet+IP+TCP/UDP header bytes of a captured frame, else 0."""
    offset = 14
    if len(frame) < offset:
        return 0
    ethertype = int.from_bytes(frame[12:14], "big")
    if ethertype == 0x8100 and len(frame) >= 18:
        ethertype, offset = int.from_bytes(frame[16:18], "big"), 18
    if ethertype == 0x0800 and len(frame) >= offset + 20:
        protocol, offset = frame[offset + 9], offset + (frame[offset] & 0x0F) * 4
    elif ethertype == 0x86DD and len(frame) >= offset + 40:
        protocol, offset = frame[offset + 6], offset + 40
    else:
        return 0
    if protocol == 6 and len(frame) >= offset + 20:
        offset += (frame[offset + 12] >> 4) * 4
    elif protocol == 17:
        offset += 8
    else:
        return 0
    return offset if offset <= len(frame) else 0


def flow_hash(frame):
    """32-bit identity of an IP 5-tuple (addresses only if no ports), else 0."""
    offset = 14
    if len(frame) < offset:
        return 0
    ethertype = int.from_bytes(frame[12:14], "big")
    if ethertype == 0x8100 and len(frame) >= 18:
        ethertype, offset = int.from_bytes(frame[16:18], "big"), 18
    if ethertype == 0x0800 and len(frame) >= offset + 20:
        protocol = frame[offset + 9]
        addresses = frame[offset + 12:offset + 20]
        fragment = int.from_bytes(frame[offset + 6:offset + 8], "big") & 0x1FFF
        transport = offset + (frame[offset] & 0x0F) * 4 if not fragment else None
    elif ethertype == 0x86DD and len(frame) >= offset + 40:
        protocol, addresses, transport = frame[offset + 6], frame[offset + 8:offset + 40], offset + 40
    else:
        return 0
    ports = b""
    if protocol in (6, 17) and transport is not None and len(frame) >= transport + 4:
        ports = frame[transport:transport + 4]
    key = ethertype.to_bytes(2, "big") + bytes([protocol]) + addresses + ports
    return int.from_bytes(hashlib.blake2b(key, digest_size=4).digest(), "little") or 1


def digest(length, snap):
    return int.from_bytes(hashlib.blake2b(length.to_bytes(4, "little") + snap, digest_size=8).digest(), "little")


def segments(length, gso_type, gso_size, header):
    """Kernel accounting of one skb: (gso_segs, qdisc_pkt_len), None if unbound."""
    if not gso_type & GSO_TYPE_MASK:
        return 1, length
    if not gso_size or not header or header >= length:
        return None
    count = -(-(length - header) // gso_size)
    return count, length + (count - 1) * header


def encode(in_rows, out_rows):
    body = b"".join(IN_RECORD.pack(*row) for row in in_rows) + b"".join(OUT_RECORD.pack(*row) for row in out_rows)
    header = HEADER.pack(MAGIC, LEDGER_VERSION, IN_RECORD.size, OUT_RECORD.size, 0, len(in_rows), len(out_rows))
    return gzip.compress(header + body, mtime=0)


def decode(blob):
    if not isinstance(blob, (bytes, bytearray)) or len(blob) > 64 * 1024 * 1024:
        raise ValueError("missing or oversized netem FIFO ledger")
    limit = HEADER.size + MAX_FRAMES * (IN_RECORD.size + OUT_RECORD.size)
    with gzip.GzipFile(fileobj=io.BytesIO(blob)) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit or len(data) < HEADER.size:
        raise ValueError("netem FIFO ledger exceeds its bounded size or lacks a header")
    magic, version, in_size, out_size, _, n_in, n_out = HEADER.unpack_from(data)
    if (magic != MAGIC or version != LEDGER_VERSION or in_size != IN_RECORD.size or out_size != OUT_RECORD.size
            or n_in > MAX_FRAMES or n_out > MAX_FRAMES
            or len(data) != HEADER.size + n_in * in_size + n_out * out_size):
        raise ValueError("netem FIFO ledger header or record count differs from its content")
    split = HEADER.size + n_in * in_size
    in_rows = [IN_RECORD.unpack_from(data, HEADER.size + index * in_size) for index in range(n_in)]
    out_rows = [OUT_RECORD.unpack_from(data, split + index * out_size) for index in range(n_out)]
    return in_rows, out_rows


def match(in_rows, out_rows):
    """Pair captures by (length, digest), k-th occurrence to k-th occurrence.

    Returns (pairs in release order as (in index, out index), unmatched in
    indices, unmatched out indices).
    """
    waiting = collections.defaultdict(collections.deque)
    for index, row in enumerate(in_rows):
        waiting[(row[1], row[8])].append(index)
    pairs, extra = [], []
    for index, row in enumerate(out_rows):
        queue = waiting.get((row[1], row[3]))
        if queue:
            pairs.append((queue.popleft(), index))
        else:
            extra.append(index)
    lost = sorted(index for queue in waiting.values() for index in queue)
    return pairs, lost, extra


def _int(value):
    return type(value) is int and 0 <= value < 2**64


def _stats(row, keys):
    if not isinstance(row, dict) or any(not _int(row.get(key)) for key in keys):
        raise ValueError("missing or invalid raw kernel counters")
    return row


def _root(rows):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("missing raw eth0 qdisc listing")
    roots = [row for row in rows if row.get("root") is True]
    if len(roots) != 1:
        raise ValueError("eth0 does not have exactly one root qdisc")
    return roots[0], [row for row in rows if row.get("root") is not True]


def _bare(rows):
    root, others = _root(rows)
    return root.get("kind") == "noqueue" and not others


def _owned(rows, phase, config):
    root, others = _root(rows)
    return (root.get("kind") == "netem" and root.get("handle") == HANDLE
            and options_match(root.get("options"), phase, config)
            and [row.get("kind") for row in others] == ["clsact"])


def clock_offset(samples):
    """Median CLOCK_REALTIME - CLOCK_MONOTONIC and its spread over the run."""
    if not isinstance(samples, list) or len(samples) < 2:
        raise ValueError("missing realtime/monotonic clock samples")
    offsets, monos = [], []
    for sample in samples:
        if (not isinstance(sample, list) or len(sample) != 3 or not all(_int(value) for value in sample)
                or sample[0] > sample[2]):
            raise ValueError("invalid realtime/monotonic clock sample")
        mono = (sample[0] + sample[2]) // 2
        monos.append(mono)
        offsets.append(sample[1] - mono)
    if monos != sorted(monos):
        raise ValueError("clock samples are not monotonic")
    ordered = sorted(offsets)
    spread = ordered[-1] - ordered[0] + max(sample[2] - sample[0] for sample in samples)
    return ordered[len(ordered) // 2], spread, monos


def _phase_at(t_in, windows):
    """Candidate phases of a frame mirrored at t_in (real ns)."""
    current, candidates = PRE, []
    for phase, (low, high) in windows:
        if t_in >= high:
            current = phase
        elif t_in >= low:
            candidates.append(phase)
    return [current] + [phase for phase in candidates if phase != current]


def validate(report, config, blob):
    """Recompute netem FIFO qualification from raw captures and counters, never `ok`."""
    errors = []
    try:
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
                or report["schema_version"] != SCHEMA_VERSION or report.get("config") != config
                or report.get("strategy") != STRATEGY or report.get("clock") != "CLOCK_MONOTONIC"):
            raise ValueError("netem FIFO report configuration or frozen strategy differs from the case")
        if report.get("errors") != [] or report.get("cancelled") is not False:
            errors.append("netem FIFO execution was cancelled or incomplete")
        if report.get("cleanup") != "PASS":
            errors.append("netem FIFO cleanup did not complete")
        ledger = report.get("ledger")
        if (not isinstance(ledger, dict) or not isinstance(blob, (bytes, bytearray))
                or ledger.get("sha256") != hashlib.sha256(blob).hexdigest()):
            raise ValueError("raw netem FIFO ledger is missing or differs from the report digest")
        in_rows, out_rows = decode(blob)
        if ledger.get("in_frames") != len(in_rows) or ledger.get("out_frames") != len(out_rows):
            raise ValueError("netem FIFO ledger frame counts differ from the report")
        planned, actual = report.get("planned"), report.get("actual")
        keys = ("measurement_start_ns", "start_ns", "end_ns", "recovery_start_ns", "measurement_end_ns")
        if (not isinstance(planned, dict) or any(not _int(planned.get(key)) for key in keys)
                or not all(planned[a] < planned[b] for a, b in zip(keys, keys[1:]))):
            raise ValueError("missing or unordered frozen netem FIFO schedule")
        delay_ns = round(config["delay_ms"] * 1_000_000)
        if abs(planned["recovery_start_ns"] - planned["end_ns"] - round((config["delay_ms"] / 1000 + DRAIN_GUARD_SEC) * 1e9)) > 1:
            errors.append("netem FIFO drain guard differs from the frozen interval")
        akeys = ("installed_ns", "mirror_installed_ns", "mirror_remove_ns", "mirror_removed_ns", "removed_ns", "closed_ns")
        if not isinstance(actual, dict) or any(not _int(actual.get(key)) for key in akeys):
            raise ValueError("missing netem FIFO install/mirror/removal timestamps")
        if not actual["installed_ns"] <= actual["mirror_installed_ns"] <= planned["measurement_start_ns"] - INSTALL_LEAD_NS:
            errors.append("netem FIFO path was not installed before the measured baseline")
        if not (planned["measurement_end_ns"] <= actual["mirror_remove_ns"] <= actual["mirror_removed_ns"]
                <= actual["removed_ns"] <= actual["closed_ns"]):
            errors.append("netem FIFO path was removed before the full measurement ended")
        changes = actual.get("changes")
        if (not isinstance(changes, list) or [row.get("phase") if isinstance(row, dict) else None for row in changes] != list(CHANGES)
                or any(not _int(row.get("t0_ns")) or not _int(row.get("t1_ns")) or row["t0_ns"] > row["t1_ns"] for row in changes)):
            raise ValueError("netem FIFO phase changes are missing, reordered or malformed")
        if any(row["t1_ns"] - row["t0_ns"] > CHANGE_WINDOW_LIMIT_NS for row in changes):
            errors.append("a netem FIFO phase change exceeded its bounded window")
        if any(not options_match(row.get("shown"), phase, config) for row, phase in zip(changes, (ACTIVE, DRAIN, TRANSPARENT))):
            errors.append("netem FIFO kernel options after a phase change differ from the frozen policy")
        for row, boundary in zip(changes, ("start_ns", "end_ns")):
            if not planned[boundary] <= row["t0_ns"] <= row["t1_ns"] <= planned[boundary] + PHASE_TOLERANCE_NS:
                errors.append(f"netem FIFO {row['phase']} change missed its planned boundary")
        transparent = changes[2]
        if (not changes[1]["t1_ns"] <= transparent["t0_ns"]
                or transparent["t1_ns"] >= planned["recovery_start_ns"]
                or transparent.get("reason") not in ("empty", "deadline")
                or (transparent["reason"] == "deadline"
                    and transparent["t0_ns"] < planned["recovery_start_ns"] - TRANSPARENT_DEADLINE_LEAD_NS)):
            errors.append("netem FIFO did not switch to the transparent path before the recovery boundary")
        offset, spread, monos = clock_offset(report.get("clock_samples"))
        if spread > CLOCK_DRIFT_LIMIT_NS:
            errors.append("realtime clock drifted against the monotonic clock during capture")
        if (monos[0] > actual["mirror_installed_ns"] or monos[-1] < actual["mirror_removed_ns"]
                or any(b - a > CLOCK_SAMPLE_GAP_NS for a, b in zip(monos, monos[1:]))):
            errors.append("clock samples do not cover the capture lifetime")
        real = lambda mono: mono + offset
        margin = ORDER_SKEW_NS + CLOCK_DRIFT_LIMIT_NS

        kernel = report.get("kernel")
        if not isinstance(kernel, dict):
            raise ValueError("missing raw netem FIFO kernel counters")
        if not _bare(kernel.get("qdisc_before")):
            errors.append("netem FIFO did not start from a bare noqueue eth0 root")
        if not _owned(kernel.get("qdisc_installed"), PRE, config):
            errors.append("installed netem FIFO root differs from the frozen pre-phase policy")
        final_root, _ = _root(kernel.get("qdisc_final"))
        if not _owned(kernel.get("qdisc_final"), TRANSPARENT, config):
            errors.append("netem FIFO root changed before its drained removal")
        final = _stats(final_root, ("drops", "overlimits", "backlog", "qlen", "packets", "bytes"))
        if final["drops"] or final["overlimits"]:
            errors.append("netem FIFO dropped or overlimited frames")
        if final["backlog"] or final["qlen"]:
            errors.append("netem FIFO was not empty before removal")
        if not _bare(kernel.get("qdisc_restored")):
            errors.append("worker eth0 qdiscs were not restored")
        action = _stats(kernel.get("action"), ("packets", "bytes", "drops", "overlimits"))
        if action["drops"] or action["overlimits"]:
            errors.append("arrival mirror dropped frames")
        link_keys = ("packets", "bytes", "dropped", "errors")
        before = _stats((kernel.get("dummy_before") or {}).get("tx"), link_keys)
        after = _stats((kernel.get("dummy_after") or {}).get("tx"), link_keys)
        dummy = {key: after[key] - before[key] for key in link_keys}
        if any(value < 0 for value in dummy.values()) or dummy["dropped"] or dummy["errors"]:
            errors.append("arrival capture device counters reset or dropped")
        capture = kernel.get("capture")
        if not isinstance(capture, dict):
            raise ValueError("missing raw capture ring statistics")
        rings = {name: _stats(capture.get(name), ("packets", "drops", "freezes")) for name in ("in", "out")}
        if rings["in"]["drops"] or rings["out"]["drops"]:
            errors.append("a capture ring dropped frames; the FIFO record is incomplete")
        if rings["in"]["packets"] != len(in_rows) or rings["out"]["packets"] != len(out_rows):
            errors.append("capture ring counters do not close against the ledger")
        accounted = [segments(row[1], row[3], row[5], row[2]) for row in in_rows]
        if None in accounted:
            errors.append("netem FIFO ledger has a GSO frame whose segment header cannot be bound")
            accounted = None
        elif (action["packets"] != sum(count for count, _ in accounted)
              or action["bytes"] != sum(size for _, size in accounted)):
            errors.append("arrival capture does not close against mirror segment counters")
        if dummy["packets"] != len(in_rows) or dummy["bytes"] != sum(row[1] for row in in_rows):
            errors.append("arrival capture does not close against the capture device counters")

        pairs, lost, extra = match(in_rows, out_rows)
        if lost:
            errors.append(f"netem FIFO lost or never released {len(lost)} mirrored frames")
        first_in = in_rows[0][0] if in_rows else None
        window_open, window_close = real(actual["mirror_installed_ns"]) + margin, real(actual["mirror_remove_ns"]) - margin
        if any(window_open < out_rows[index][0] < window_close for index in extra):
            errors.append("an egress frame bypassed the mirrored FIFO record")
        if any(b[0] < a[0] - ORDER_SKEW_NS for a, b in zip(in_rows, in_rows[1:])):
            errors.append("arrival capture timestamps are not ordered within the skew bound")
        if any(b[0] < a[0] for a, b in zip(out_rows, out_rows[1:])):
            errors.append("release capture timestamps are not monotonic")
        if first_in is None or accounted is None or lost:
            raise ValueError("netem FIFO captures cannot be paired")

        windows = [(phase, (real(row["t0_ns"]) - margin, real(row["t1_ns"]) + CLOCK_DRIFT_LIMIT_NS))
                   for row, phase in zip(changes, (ACTIVE, DRAIN, TRANSPARENT))]
        recovery = real(planned["recovery_start_ns"])
        last_flow, previous_in = {}, None
        low = high = 0
        seen, phase_of = [0] * 4, {}
        for in_index, out_index in pairs:
            t_in, length, _, _, _, _, _, flow, _ = in_rows[in_index]
            t_out = out_rows[out_index][0]
            if previous_in is not None and t_in < previous_in - ORDER_SKEW_NS:
                errors.append(f"netem FIFO released mirrored frame {in_index} ahead of an earlier arrival")
                break
            if flow and last_flow.get(flow, -1) > in_index:
                errors.append(f"netem FIFO reordered flow frames at mirrored frame {in_index}")
                break
            if flow:
                last_flow[flow] = in_index
            candidates = _phase_at(t_in, windows)
            if len(candidates) == 1:
                seen[candidates[0]] += 1
                phase_of[in_index] = candidates[0]
            size = accounted[in_index][1]
            options = [policy(phase, delay_ns) for phase in candidates]
            floor_low, floor_high = low, high
            low = min(max(t_in + delay, floor_low) + size * 1_000_000_000 // rate for delay, rate in options)
            high = max(max(t_in + delay, floor_high) + size * 1_000_000_000 // rate for delay, rate in options)
            if t_out < low - EARLY_TOLERANCE_NS:
                errors.append(f"netem FIFO released mirrored frame {in_index} before its frozen release time")
                break
            if t_out - high > LATENESS_LIMIT_NS:
                errors.append(f"netem FIFO mirrored frame {in_index} exceeded the lateness bound")
                break
            if t_in >= recovery:
                if candidates != [TRANSPARENT]:
                    errors.append("recovery frame was not on the transparent path")
                    break
                if floor_high > t_in + ORDER_SKEW_NS or t_out - t_in > LATENESS_LIMIT_NS:
                    errors.append("recovery frame queued behind an unfinished backlog")
                    break
            previous_in = max(previous_in or t_in, t_in)
        else:
            releases = sorted(out_rows[out_index][0] for _, out_index in pairs)
            occupancy = held = max_held = max_bytes = released = queued_active = 0
            sizes = sorted((out_rows[out_index][0], in_rows[in_index][1]) for in_index, out_index in pairs)
            for in_index, (t_in, length, *_) in enumerate(in_rows):
                while released < len(sizes) and sizes[released][0] <= t_in:
                    occupancy -= 1
                    held -= sizes[released][1]
                    released += 1
                if occupancy and phase_of.get(in_index) == ACTIVE:
                    queued_active += 1
                occupancy += 1
                held += length
                max_held, max_bytes = max(max_held, occupancy), max(max_bytes, held)
            if max_held > config["limit_packets"] or max_bytes > MEMORY_LIMIT_BYTES:
                errors.append("netem FIFO occupancy exceeded its frozen packet or memory bound")
            if not seen[ACTIVE] or not queued_active:
                errors.append("netem FIFO did not observe real queued and released loaded traffic")
            if not seen[PRE] or not seen[TRANSPARENT]:
                errors.append("netem FIFO did not carry traffic in every measured phase")
            if (first_in > real(planned["start_ns"]) - COVERAGE_NS
                    or in_rows[-1][0] < real(planned["measurement_end_ns"]) - COVERAGE_NS
                    or not any(row[0] >= recovery for row in in_rows)):
                errors.append("netem FIFO captures do not cover baseline, recovery and measurement end")
            if releases and releases[-1] > real(actual["removed_ns"]):
                errors.append("netem FIFO frame left after the path was removed")
    except (ValueError, TypeError, KeyError, AttributeError, OSError, EOFError, struct.error, IndexError) as error:
        errors.append(str(error))
    return sorted(set(errors))


def summary(in_rows, out_rows):
    """Descriptive residence of paired frames; qualification uses `validate` only."""
    pairs, lost, extra = match(in_rows, out_rows)
    residence = sorted(out_rows[out_index][0] - in_rows[in_index][0] for in_index, out_index in pairs)
    pick = lambda q: residence[min(len(residence) - 1, int(q * len(residence)))] if residence else None
    return {"paired": len(pairs), "lost": len(lost), "unpaired_out": len(extra),
            "residence_ns": {"p50": pick(.5), "p99": pick(.99), "max": residence[-1] if residence else None}}
