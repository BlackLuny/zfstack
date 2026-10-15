"""Separate test-endpoint process accounting; never subtracted from worker CPU.

Only numeric /proc counters and PID/starttime/comm/exe/netns identity are read.
No command lines, environments, keys or runtime configurations are collected.
"""
import math
import os
from pathlib import Path
import threading
import time

from path_contract import client_nodes


def expected_endpoints(config):
    nodes = client_nodes(config.get("outbound", "direct"))
    expected = {"generator-heavy-0": {"role": "heavy_generator", "namespace_role": nodes[0]},
                "target-business": {"role": "tcp_udp_target", "namespace_role": "target"}}
    for index in (1, 2, 3):
        expected[f"generator-light-{index}"] = {"role": "light_generator", "namespace_role": nodes[index]}
    if config.get("udp_enabled"):
        for index in range(4):
            expected[f"generator-udp-{index}"] = {"role": "udp_generator", "namespace_role": nodes[index]}
    if config.get("outbound") == "relay":
        expected.update({"target-probe": {"role": "tcp_udp_probe_target", "namespace_role": "target"},
                         "relay-forwarder": {"role": "private_tun_forwarder", "namespace_role": "fwd1"}})
    if config.get("mode") == "idle":
        expected["generator-idle"] = {"role": "idle_generator", "namespace_role": nodes[1]}
    return expected


def boundaries(start, phases):
    result, begin = {}, start
    for phase in ("baseline", "loaded", "recovery"):
        end = begin + phases[phase]
        result[phase] = {"start_ns": round(begin * 1e9), "end_ns": round(end * 1e9)}
        begin = end
    return result


def sample_schedule(start, phases):
    window = boundaries(start, phases)
    duration = sum(phases.values())
    return sorted({*(value for row in window.values() for value in row.values()),
                   *(round((start + index * .5) * 1e9) for index in range(math.ceil(duration / .5)))})


def read_snapshot(pid, scheduled_ns, identity=None, *, proc=Path("/proc"), ticks=None):
    """A zombie retains CPU counters, but has no observable resident memory."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("endpoint PID is missing")
    ticks = os.sysconf("SC_CLK_TCK") if ticks is None else ticks
    begin = time.monotonic_ns()
    root = Path(proc) / str(pid)
    fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
    state, starttime = fields[0], int(fields[19])
    total = int(fields[11]) + int(fields[12])
    status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines())
    zombie = state in ("Z", "X")
    if zombie:
        if not identity:
            raise ValueError("endpoint exited before any live identity observation")
        observed = {**identity, "pid": pid, "starttime_ticks": starttime}
        rss = peak = None
        source = "retained-zombie"
    else:
        observed = {"pid": pid, "starttime_ticks": starttime, "comm": (root / "comm").read_text().strip(),
                    "exe": os.readlink(root / "exe"), "netns": os.readlink(root / "ns/net")}
        rss, peak = int(status["VmRSS"].split()[0]), int(status["VmHWM"].split()[0])
        source = "live"
    if identity and observed != identity:
        raise ValueError("endpoint PID/starttime/executable/namespace identity changed")
    end = time.monotonic_ns()
    return observed, {"scheduled_ns": scheduled_ns, "at_ns": end, "read_duration_ns": end - begin,
                      "boundary_error_ns": end - scheduled_ns, "pid": pid, "starttime_ticks": starttime,
                      "identity_observation": source, "state": state, "cpu_ticks": total,
                      "ticks_per_sec": ticks, "cpu_sec": total / ticks,
                      "cpu_resolution_ns": math.ceil(1e9 / ticks), "rss_kib": rss, "peak_rss_kib": peak}


class EndpointSampler:
    def __init__(self, config, start, pids, output, *, reader=read_snapshot):
        self.config, self.start, self.pids, self.output, self.reader = config, start, pids, Path(output), reader
        self.stop = threading.Event()
        self.report = {"schema_version": 1, "clock": "CLOCK_MONOTONIC",
                       "scope": "separate process diagnostics; target TCP/UDP share one process; never subtract from worker CPU",
                       "planned_boundaries": boundaries(start, config["phases_sec"]),
                       "boundary_tolerance_ns": config["boundary_tolerance_ns"], "errors": [],
                       "endpoints": {name: {**role, "identity": None, "samples": []}
                                     for name, role in expected_endpoints(config).items()}}
        self.thread = threading.Thread(target=self._run, name="qa-endpoint-accounting", daemon=True)

    def _run(self):
        import json
        try:
            for scheduled in sample_schedule(self.start, self.config["phases_sec"]):
                if self.stop.wait(max(0, (scheduled - time.monotonic_ns()) / 1e9)):
                    self.report["errors"].append("endpoint sampler cancelled before all phase boundaries")
                    break
                for name, record in self.report["endpoints"].items():
                    try:
                        identity, row = self.reader(self.pids.get(name), scheduled, record["identity"])
                        record["identity"] = identity
                    except (OSError, ValueError, KeyError, IndexError) as error:
                        row = {"scheduled_ns": scheduled, "at_ns": time.monotonic_ns(), "error": str(error)}
                    record["samples"].append(row)
        except Exception as error:
            self.report["errors"].append("endpoint sampler failed: " + str(error))
        finally:
            self.output.write_text(json.dumps(self.report, indent=2, allow_nan=False) + "\n")

    def begin(self):
        self.thread.start()

    def finish(self, cancelled=False):
        if cancelled:
            self.stop.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.report["errors"].append("endpoint sampler did not converge")
            self.stop.set()
            # A failed observer cannot skip generator/process cleanup or erase
            # independently observed payload outcomes. Missing raw stays invalid.
        return self.report


def validate_evidence(report, result, config):
    """Recompute every endpoint phase delta from the retained raw timeline."""
    errors, diagnostics = [], {}
    try:
        start = result.get("start_at")
        if type(start) not in (int, float) or not math.isfinite(start) or start < 0:
            raise ValueError("missing endpoint phase clock origin")
        expected = expected_endpoints(config)
        planned = boundaries(start, config["phases_sec"])
        schedule = sample_schedule(start, config["phases_sec"])
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1
                or report.get("clock") != "CLOCK_MONOTONIC" or report.get("planned_boundaries") != planned
                or report.get("errors") != [] or set(report.get("endpoints", {})) != set(expected)):
            raise ValueError("missing/incomplete endpoint CPU/RSS identity, phase or role evidence")
        tolerance = report.get("boundary_tolerance_ns")
        if type(tolerance) is not int or tolerance <= 0 or tolerance != config["boundary_tolerance_ns"]:
            raise ValueError("endpoint boundary tolerance differs from the frozen case")
        pids = set()
        for name, role in expected.items():
            record = report["endpoints"][name]
            identity, rows = record.get("identity"), record.get("samples")
            if any(record.get(key) != value for key, value in role.items()):
                errors.append(name + ": role or namespace differs")
            if (not isinstance(identity, dict) or set(identity) != {"pid", "starttime_ticks", "comm", "exe", "netns"}
                    or type(identity.get("pid")) is not int or identity["pid"] <= 0 or identity["pid"] in pids
                    or type(identity.get("starttime_ticks")) is not int or identity["starttime_ticks"] <= 0
                    or any(not isinstance(identity.get(key), str) or not identity[key] for key in ("comm", "exe", "netns"))):
                errors.append(name + ": missing/duplicate endpoint process identity")
                continue
            pids.add(identity["pid"])
            if not isinstance(rows, list) or [row.get("scheduled_ns") for row in rows] != schedule:
                errors.append(name + ": endpoint samples omit the frozen phase schedule")
                continue
            previous = None
            for row in rows:
                prefix = name + ": "
                required_ints = ("at_ns", "read_duration_ns", "cpu_ticks", "ticks_per_sec", "cpu_resolution_ns")
                if row.get("error") or any(type(row.get(key)) is not int or row[key] < 0 for key in required_ints) or row.get("ticks_per_sec") == 0:
                    errors.append(prefix + "missing/invalid endpoint counter snapshot")
                    continue
                if (type(row.get("pid")) is not int or type(row.get("starttime_ticks")) is not int
                        or row.get("pid") != identity["pid"] or row.get("starttime_ticks") != identity["starttime_ticks"]
                        or type(row.get("cpu_sec")) not in (int, float) or not math.isfinite(row["cpu_sec"])
                        or row["cpu_sec"] != row["cpu_ticks"] / row["ticks_per_sec"]
                        or row["cpu_resolution_ns"] != math.ceil(1e9 / row["ticks_per_sec"])):
                    errors.append(prefix + "endpoint identity or CPU conversion changed")
                if (type(row.get("boundary_error_ns")) is not int or row["boundary_error_ns"] != row["at_ns"] - row["scheduled_ns"]
                        or abs(row["boundary_error_ns"]) > tolerance or row["read_duration_ns"] > tolerance):
                    errors.append(prefix + "endpoint phase sampling precision insufficient")
                zombie = row.get("state") in ("Z", "X")
                if zombie:
                    if (row.get("identity_observation") != "retained-zombie" or row.get("rss_kib") is not None
                            or row.get("peak_rss_kib") is not None or previous is None):
                        errors.append(prefix + "exited endpoint RSS/identity was fabricated")
                elif (row.get("identity_observation") != "live" or not isinstance(row.get("state"), str) or len(row["state"]) != 1
                      or any(type(row.get(key)) is not int or row[key] < 0 for key in ("rss_kib", "peak_rss_kib"))):
                    errors.append(prefix + "missing live endpoint RSS")
                if previous and (row["at_ns"] <= previous["at_ns"] or row["cpu_ticks"] < previous["cpu_ticks"]
                                 or row["ticks_per_sec"] != previous["ticks_per_sec"]):
                    errors.append(prefix + "endpoint CPU/clock counter reset")
                previous = row
            phases = {}
            for phase, interval in planned.items():
                selected = [row for row in rows if interval["start_ns"] <= row["scheduled_ns"] <= interval["end_ns"]]
                first, last = selected[0], selected[-1]
                if any(type(row.get("cpu_sec")) not in (int, float) for row in (first, last)):
                    phases[phase] = {"cpu_sec": None, "rss_peak_kib": None}
                    continue
                phases[phase] = {"cpu_sec": last["cpu_sec"] - first["cpu_sec"],
                                 "rss_peak_kib": max((row["rss_kib"] for row in selected if type(row.get("rss_kib")) is int), default=None),
                                 "start_ns": first["at_ns"], "end_ns": last["at_ns"],
                                 "start_error_ns": first["at_ns"] - interval["start_ns"],
                                 "end_error_ns": last["at_ns"] - interval["end_ns"],
                                 "sample_count": len(selected)}
            diagnostics[name] = {**role, "phases": phases}
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
        errors.append("endpoint evidence: " + str(error))
    return sorted(set(errors)), diagnostics
