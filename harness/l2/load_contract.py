"""Test-side arrival, accounting and measurement contracts; no product hooks."""
import math
import os
import mmap
from pathlib import Path
import struct
import time


STATUSES = ("completed", "failed", "timed_out", "unfinished", "missed")
BOUNDARY_TOLERANCE_NS = 5_000_000


def arrivals(start, duration, rate_hz):
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, duration, rate_hz)) or min(duration, rate_hz) <= 0:
        raise ValueError("arrival duration and rate must be finite and positive")
    # Freeze the external process before observing any response. The same index
    # always denotes the same offered arrival, even if a prior request stalls.
    count = math.ceil(duration * rate_hz)
    return [start + index / rate_hz for index in range(count) if index / rate_hz < duration]


def counters(samples, scheduled=None):
    counts = {key: 0 for key in ("scheduled", "started", *STATUSES)}
    for sample in samples:
        counts["scheduled"] += 1
        if sample.get("started_at") is not None: counts["started"] += 1
        state = sample.get("status")
        if state not in STATUSES: raise ValueError(f"unknown request status: {state}")
        counts[state] += 1
    if scheduled is not None:
        if scheduled < counts["scheduled"]: raise ValueError("more observations than scheduled requests")
        counts["unfinished"] += scheduled - counts["scheduled"]
        counts["scheduled"] = scheduled
    return counts


class Progress:
    """One locked cumulative verified-byte counter per heavy flow.

    Locks serialize cross-process snapshots without relying on CPU memory-order
    assumptions. Writers own disjoint records; this is generator overhead only.
    """
    def __init__(self, path, count, create=False):
        # Only the native Linux load generator needs record locks. Keep the
        # offline accounting/configuration validators importable on Windows.
        import fcntl
        self.fcntl = fcntl
        self.count = count
        if create:
            with Path(path).open("xb") as file:
                file.truncate(count * 16)
        self.file = Path(path).open("r+b")
        self.map = mmap.mmap(self.file.fileno(), count * 16)

    def write(self, index, total):
        fcntl = self.fcntl
        offset = index * 16
        fcntl.lockf(self.file.fileno(), fcntl.LOCK_EX, 16, offset)
        try:
            generation = struct.unpack_from("=Q", self.map, offset)[0]
            struct.pack_into("=QQ", self.map, offset, generation + 2, total)
        finally:
            fcntl.lockf(self.file.fileno(), fcntl.LOCK_UN, 16, offset)

    def read(self, deadline_ns=None):
        # CPU and bytes share the caller's original observation budget. Brief
        # writer contention is retried, but never extends that 5 ms window.
        if deadline_ns is None:
            deadline_ns = time.monotonic_ns() + BOUNDARY_TOLERANCE_NS
        fcntl = self.fcntl
        while time.monotonic_ns() < deadline_ns:
            try:
                fcntl.lockf(self.file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                remaining = deadline_ns - time.monotonic_ns()
                if remaining > 0:
                    time.sleep(min(remaining, 50_000) / 1e9)
                continue
            try:
                total = sum(struct.unpack_from("=Q", self.map, index * 16 + 8)[0] for index in range(self.count))
                return total if time.monotonic_ns() <= deadline_ns else None
            finally:
                fcntl.lockf(self.file.fileno(), fcntl.LOCK_UN)
        return None

    def close(self):
        self.map.close()
        self.file.close()


def resources_for_phase(timeline, begin, end, tolerance_ns=BOUNDARY_TOLERANCE_NS):
    planned_start, planned_end = round(begin * 1e9), round(end * 1e9)
    rows = [s for s in timeline if planned_start <= s.get("scheduled_ns", round(s["at"] * 1e9)) <= planned_end]
    result = {"sample_count": len(rows), "worker_cpu_sec": None, "sample_window_sec": None,
              "rss_peak_kib": max((s["rss_kib"] for s in rows), default=None),
              "rss_end_kib": rows[-1]["rss_kib"] if rows else None}
    window = {"clock": "CLOCK_MONOTONIC", "start_ns": None, "end_ns": None,
              "cpu_sec": None, "verified_bytes": None, "sample_count": len(rows),
              "max_boundary_skew_ns": None, "boundary_tolerance_ns": tolerance_ns,
              "planned_start_ns": planned_start, "planned_end_ns": planned_end, "errors": []}
    result["measurement_window"] = window
    if len(rows) < 2:
        window["errors"].append("fewer than two same-window boundary samples")
        return result
    first, last = rows[0], rows[-1]
    if first.get("scheduled_ns") != planned_start or last.get("scheduled_ns") != planned_end:
        window["errors"].append("missing scheduled phase-boundary samples; interior windows cannot replace load onset")
    if abs(first["at_ns"] - planned_start) > tolerance_ns or abs(last["at_ns"] - planned_end) > tolerance_ns:
        window["errors"].append("phase-boundary scheduling precision insufficient")
    window.update(start_ns=first["at_ns"], end_ns=last["at_ns"],
                  cpu_sec=last["cpu_sec"] - first["cpu_sec"],
                  max_boundary_skew_ns=max(s["boundary_skew_ns"] for s in rows),
                  cpu_resolution_ns=first.get("cpu_resolution_ns"))
    values = [s.get("verified_bytes") for s in rows]
    if any(type(v) is not int or v < 0 for v in values):
        window["errors"].append("missing verified-byte boundary sample")
    elif any(a > b for a, b in zip(values, values[1:])):
        window["errors"].append("verified-byte counter reset")
    else:
        window["verified_bytes"] = values[-1] - values[0]
    if any(a["cpu_sec"] > b["cpu_sec"] for a, b in zip(rows, rows[1:])):
        window["errors"].append("worker CPU counter reset")
    if any(a["at_ns"] >= b["at_ns"] for a, b in zip(rows, rows[1:])):
        window["errors"].append("monotonic clock moved backwards")
    if window["max_boundary_skew_ns"] > tolerance_ns:
        window["errors"].append("measurement boundary precision insufficient")
    result["worker_cpu_sec"] = window["cpu_sec"]
    result["sample_window_sec"] = (window["end_ns"] - window["start_ns"]) / 1e9
    return result


def worker_schedule(start, phases):
    duration = phases["baseline"] + phases["loaded"] + phases["recovery"]
    boundaries = (start, start + phases["baseline"], start + phases["baseline"] + phases["loaded"], start + duration)
    return sorted({*boundaries, *(start + index * .5 for index in range(math.ceil(duration / .5)))})


def worker_resources(timeline, start, phases, tolerance_ns=BOUNDARY_TOLERANCE_NS):
    loaded_start = start + phases["baseline"]
    recovery_start = loaded_start + phases["loaded"]
    end = recovery_start + phases["recovery"]
    return {phase: resources_for_phase(timeline, begin, finish, tolerance_ns)
            for phase, begin, finish in (("baseline", start, loaded_start), ("loaded", loaded_start, recovery_start),
                                         ("recovery", recovery_start, end))}


def effective_configuration(a, environment=None):
    """Shared parent/loader config constructor; unknown build settings stay null."""
    environment = os.environ if environment is None else environment
    udp_enabled = environment.get("L2_UDP_PROBES", "0") == "1"
    effective_config = {
        "schema_version": 1, "heavy_conns": a.conns, "request_bytes": 256,
        "request_rate_hz": float(environment.get("L2_LIGHT_RATE_HZ", "100")),
        "light_mode": environment.get("L2_LIGHT_MODE", "closed-loop"),
        "max_generator_lag_sec": float(environment.get("L2_MAX_GENERATOR_LAG_SECS", ".02")),
        "light_conns": int(environment.get("L2_LIGHT_CONNS", "1")),
        "churn_requests": int(environment.get("L2_CHURN_REQUESTS", "0")),
        "udp_enabled": udp_enabled, "udp_rate_hz": float(environment.get("L2_UDP_RATE_HZ", "100")),
        "udp_payload_bytes": 1032, "request_timeout_sec": a.request_timeout,
        "phases_sec": {"baseline": a.baseline, "loaded": a.loaded, "recovery": a.recovery},
        "direction": "heavy-download/light-echo", "application_port": a.port,
        "memory_mb": int(environment.get("L2_MEMORY_MB", "64")),
        "uplink_delay_ms": float(environment.get("L2_UPLINK_DELAY_MS", "0")),
        "netem_limit_packets": int(environment.get("L2_NETEM_LIMIT_PACKETS", "100000")),
        "mtu": int(environment.get("L2_MTU", "1420")),
        "runtime": environment.get("L2_RUNTIME_ID"), "profile": environment.get("L2_PROFILE_ID"),
        "capture": environment.get("L2_CAPTURE", "0") == "1",
        "boundary_tolerance_ns": BOUNDARY_TOLERANCE_NS,
        "topology": {"wg_ports": 2, "peers": 4, "heavy_peer": 0, "light_peers": [1, 2, 3]},
    }
    from path_contract import client_nodes
    outbound = environment.get("L2_OUTBOUND", "direct")
    nodes = client_nodes(outbound)
    if outbound == "relay":
        effective_config.update(outbound=outbound, probe_port=19002)
        effective_config["topology"]["client_nodes"] = nodes
    from queue_control import configuration
    control = configuration(environment)
    if control is not None:
        effective_config["queue_control"] = control
    if environment.get("L2_EFFECTIVE_CONFIG_JSON"):
        from evidence import read_json
        extra = read_json(Path(environment["L2_EFFECTIVE_CONFIG_JSON"]))
        # Keep both the orchestration manifest and observed load settings; never
        # overwrite observed settings with the caller's requested values.
        effective_config["orchestrator_manifest"] = extra
    return effective_config
