"""Opt-in test-side egress queue; no product scheduler or queue changes.

The control delays worker eth0 only inside QA's private namespaces. It proves
that an absolute latency contract detects a deep shared egress queue; it does
not claim to recreate the implementation or historical cause of issue #44.

`netem-delay` is the retained v3 root-qdisc injector. Its show/delete restore
is not atomic under load, so it can only re-judge archived reports. New runs
use `fifo-delay-line` (schema 4, `fifo_injector.py`).
"""
import gzip
import json
import math
import os
import re
import select
import subprocess
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
import fifo_ledger
from queue_links import validate as link_errors

KINDS = ("netem-delay", fifo_ledger.KIND)
LEDGER_NAME = "queue-ledger.bin.gz"
DRAIN_GUARD_SEC = fifo_ledger.DRAIN_GUARD_SEC
DRAIN_RATE_BPS = 2_000_000_000
DRAIN_STRATEGY = {"kind": "netem-zero-delay-bounded-fifo-rate", "delay_ms": 0, "rate_bps": DRAIN_RATE_BPS}


def _queue_shape(row, config, *, draining=False):
    """Check actual tc JSON and its independent text rendering, not claims.

    tc omits the delay option when the kernel delay is zero. JSON rate is
    bytes/second, whereas text renders bits/second; both must match exactly.
    """
    root = _root(row)
    options = root.get("options")
    if (len(row["qdiscs"]) != 1 or root.get("kind") != "netem" or root.get("handle") != "44:"
            or not isinstance(options, dict) or type(options.get("limit")) is not int
            or options["limit"] != config["limit_packets"]
            or set(options) - {"limit", "delay", "rate", "ecn", "gap"}
            or options.get("ecn", False) is not False or type(options.get("gap", 0)) is not int
            or options.get("gap", 0) != 0):
        raise ValueError("queue kind, child, packet limit or impairment differs from frozen injection")
    delay = options.get("delay", {"delay": 0})
    expected_delay = 0 if draining else config["delay_ms"] / 1000
    if (not isinstance(delay, dict) or set(delay) - {"delay", "jitter", "correlation"}
            or type(delay.get("delay")) not in (int, float) or delay["delay"] != expected_delay
            or any(type(delay.get(key, 0)) not in (int, float) or delay.get(key, 0) != 0 for key in ("jitter", "correlation"))):
        raise ValueError("raw queue delay or jitter differs from frozen injection/drain")
    text = row.get("text")
    if not isinstance(text, str) or not text.startswith("qdisc netem 44: root "):
        raise ValueError("missing raw netem text rendering")
    header = text.splitlines()[0]
    if not re.fullmatch(r"qdisc netem 44: root (?:refcnt [0-9]+ )?limit [0-9]+"
                        r"(?: delay [0-9.]+(?:ns|us|ms|s))?(?: rate [0-9.]+[kKMGT]?bit)?", header):
        raise ValueError("unrecognized text queue options; only fixed delay and drain rate are allowed")
    match = re.search(r"\blimit ([0-9]+)\b", header)
    if not match or int(match[1]) != config["limit_packets"]:
        raise ValueError("text queue packet limit differs")
    matches = re.findall(r"\bdelay ([0-9.]+)(ns|us|ms|s)\b", header)
    if (not matches and not draining) or len(matches) > 1:
        raise ValueError("missing or ambiguous text queue delay")
    if matches:
        amount, unit = matches[0]
        observed = Decimal(amount) * {"ns": Decimal(".000001"), "us": Decimal(".001"), "ms": Decimal(1), "s": Decimal(1000)}[unit]
        if observed != Decimal(0 if draining else str(config["delay_ms"])):
            raise ValueError("text queue delay differs from frozen injection/drain")
    elif re.search(r"\bdelay\b", header):
        raise ValueError("unparseable text queue delay")
    if re.search(r"\b(loss|reorder|duplicate|corrupt|gap|slot|ecn|distribution)\b", header):
        raise ValueError("unexpected text queue impairment")
    if not draining:
        if "rate" in options or re.search(r"\brate\b", header):
            raise ValueError("active delay-only injection must not contain a rate")
        return
    rate = options.get("rate")
    if (not isinstance(rate, dict) or set(rate) - {"rate", "packetoverhead", "cellsize", "celloverhead"}
            or type(rate.get("rate")) is not int or rate["rate"] != DRAIN_RATE_BPS // 8
            or any(type(rate.get(key, 0)) is not int or rate.get(key, 0) != 0
                   for key in ("packetoverhead", "cellsize", "celloverhead"))):
        raise ValueError("raw drain rate is not the frozen bounded FIFO strategy")
    matches = re.findall(r"\brate ([0-9.]+)([kKMGT]?)bit\b", header)
    if len(matches) != 1 or Decimal(matches[0][0]) * {"": 1, "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}[matches[0][1]] != DRAIN_RATE_BPS:
        raise ValueError("text drain rate is not the frozen bounded FIFO strategy")

def configuration(environment=None):
    env = os.environ if environment is None else environment
    if not env.get("L2_QUEUE_DELAY_MS"):
        return None
    value = {"kind": env.get("L2_QUEUE_KIND") or "netem-delay", "node": "worker", "interface": "eth0", "phase": "loaded",
             "delay_ms": float(env["L2_QUEUE_DELAY_MS"]),
             "limit_packets": int(env.get("L2_QUEUE_LIMIT_PACKETS", "100000"))}
    validate_config(value)
    return value


def validate_config(value):
    if (not isinstance(value, dict) or set(value) != {"kind", "node", "interface", "phase", "delay_ms", "limit_packets"}
            or value.get("kind") not in KINDS
            or any(value.get(k) != v for k, v in {"node": "worker", "interface": "eth0", "phase": "loaded"}.items())
            or type(value.get("delay_ms")) not in (int, float) or not math.isfinite(value["delay_ms"])
            or not 1 <= value["delay_ms"] <= 2000 or type(value.get("limit_packets")) is not int
            or not 100 <= value["limit_packets"] <= 1_000_000):
        raise ValueError("invalid bounded test-side egress queue configuration")
    return value


def _root(row):
    roots = [q for q in row.get("qdiscs", []) if isinstance(q, dict) and q.get("root") is True]
    if len(roots) != 1:
        raise ValueError("queue evidence needs exactly one root qdisc")
    return roots[0]


def ledger_blob(directory):
    """Bounded raw FIFO ledger bytes from a rig output, or None if absent."""
    path = Path(directory) / LEDGER_NAME
    if not path.is_file():
        return None
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("FIFO ledger exceeds its archive bound")
    return path.read_bytes()


def validate_evidence(report, config, ledger=None):
    """Recompute injection preconditions from raw snapshots/ledger, never `ok`."""
    errors = []
    try:
        validate_config(config)
        if config["kind"] == fifo_ledger.KIND:
            if not isinstance(report, dict):
                raise ValueError("missing typed FIFO queue control report")
            return sorted(set(link_errors(report.get("link_samples")) + fifo_ledger.validate(report, config, ledger)))
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int
                or report["schema_version"] != 3 or report.get("config") != config):
            raise ValueError("queue control configuration differs from frozen case")
        strategy = report.get("drain_strategy")
        if (strategy != DRAIN_STRATEGY or type(strategy.get("delay_ms")) is not int
                or type(strategy.get("rate_bps")) is not int):
            raise ValueError("missing or changed frozen v3 queue drain strategy")
        errors.extend(link_errors(report.get("link_samples")))
        if report.get("clock") != "CLOCK_MONOTONIC" or report.get("errors") != []:
            errors.append("queue control execution is incomplete")
        rows = report.get("samples")
        if not isinstance(rows, list) or len(rows) < 4:
            raise ValueError("missing queue control snapshots")
        times = [r.get("at_ns") for r in rows]
        if any(type(t) is not int or t < 0 for t in times) or times != sorted(set(times)):
            errors.append("invalid queue control monotonic sequence")
        before, restored = [r for r in rows if r.get("phase") == "before"], [r for r in rows if r.get("phase") == "restored"]
        active = [r for r in rows if r.get("phase") == "active"]
        if len(before) != 1 or len(restored) != 1 or len(active) < 2:
            raise ValueError("queue control lacks before/active/restored evidence")
        original, final = _root(before[0]), _root(restored[0])
        if (original.get("kind"), original.get("handle"), original.get("options")) != (final.get("kind"), final.get("handle"), final.get("options")):
            errors.append("queue control did not restore the original root")
        if original.get("kind") != "noqueue":
            errors.append("queue control was not confined to a fresh veth root")
        planned = report.get("planned", {})
        begin, end, recovery = planned.get("start_ns"), planned.get("end_ns"), planned.get("recovery_start_ns")
        if type(begin) is not int or type(end) is not int or type(recovery) is not int or begin >= end:
            raise ValueError("missing frozen queue control interval")
        if abs(recovery - end - round((config["delay_ms"] / 1000 + DRAIN_GUARD_SEC) * 1e9)) > 1:
            errors.append("queue drain guard differs from the frozen interval")
        if abs(active[0]["at_ns"] - begin) > 250_000_000 or not any(r["at_ns"] >= end - 250_000_000 for r in active):
            errors.append("queue control did not cover the declared loaded window")
        if restored[0]["at_ns"] <= end or restored[0]["at_ns"] >= recovery:
            errors.append("queue control was not restored before the recovery phase")
        actual = report.get("actual", {})
        if (any(type(actual.get(key)) is not int for key in ("start_ns", "end_ns", "restored_ns"))
                or not before[0]["at_ns"] < actual["start_ns"] <= active[0]["at_ns"]
                or not active[-1]["at_ns"] <= actual["end_ns"] < restored[0]["at_ns"]
                or actual["restored_ns"] != restored[0]["at_ns"]
                or abs(actual["start_ns"] - begin) > 250_000_000 or abs(actual["end_ns"] - end) > 250_000_000):
            errors.append("queue actual start/end/restoration timestamps are missing or inconsistent")
        queued = 0
        previous = None
        for row in active:
            _queue_shape(row, config)
            root = _root(row)
            values = {key: root.get(key) for key in ("bytes", "packets", "drops", "backlog")}
            if any(type(v) is not int or v < 0 for v in values.values()):
                errors.append("missing or invalid queue counters")
                continue
            if values["drops"]:
                errors.append("queue control dropped packets; latency-only control is invalid")
            if previous and any(values[k] < previous[k] for k in ("bytes", "packets", "drops")):
                errors.append("queue control counters reset")
            queued = max(queued, values["backlog"])
            previous = values
        if not queued or not previous or not previous["packets"]:
            errors.append("queue control did not observe real queued and transmitted traffic")
        draining = [r for r in rows if r.get("phase") == "draining"]
        cleanup = [r for r in rows if r.get("phase") == "cleanup"]
        if not draining or len(cleanup) != 1 or _root(draining[-1]).get("backlog") != 0 or _root(cleanup[0]).get("backlog") != 0:
            errors.append("queue control did not prove a drained queue before removal")
        for row in [*draining, *cleanup]:
            _queue_shape(row, config, draining=True)
            root = _root(row)
            if (root.get("kind") != "netem" or root.get("handle") != "44:"
                    or type(root.get("drops")) is not int or root["drops"] != 0):
                errors.append("queue control drain changed ownership or dropped packets")
            values = {key: root.get(key) for key in ("bytes", "packets", "drops", "backlog")}
            if any(type(value) is not int or value < 0 for value in values.values()):
                errors.append("missing or invalid drained queue counters")
            elif previous and any(values[key] < previous[key] for key in ("bytes", "packets", "drops")):
                errors.append("queue control counters reset during drain")
            else:
                previous = values
        if report.get("cleanup") != "PASS":
            errors.append("queue control cleanup did not complete")
    except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as error:
        errors.append(str(error))
    return sorted(set(errors))


class QueueControl:
    def __init__(self, config, namespace, start, end, output):
        validate_config(config)
        from isolation import verify
        if verify() or not isinstance(namespace, str) or not re.fullmatch(r"rig-[a-f0-9]{6}-worker", namespace):
            raise ValueError("egress queue control requires verified QA isolation and the owned worker namespace")
        self.config, self.namespace, self.output = config, namespace, Path(output)
        # The entire loaded request population is retained. Only injection ends
        # early enough to drain before recovery; this guard is fixed in v1.
        self.start, self.end, self.installed = start, end - config["delay_ms"] / 1000 - DRAIN_GUARD_SEC, False
        if self.end <= self.start:
            raise ValueError("loaded phase is too short for the frozen queue drain guard")
        self.stop = threading.Event()
        self.report = {"schema_version": 3, "drain_strategy": dict(DRAIN_STRATEGY),
                       "scope": "test-side shared worker egress queue; finite-rate FIFO drain, not delay-only/historical root-cause reproduction",
                       "config": config, "clock": "CLOCK_MONOTONIC", "planned": {"start_ns": round(start * 1e9), "end_ns": round(self.end * 1e9), "recovery_start_ns": round(end * 1e9)},
                       "samples": [], "errors": [], "cleanup": "UNKNOWN"}
        self.thread = threading.Thread(target=self._run, name="qa-egress-queue", daemon=True)

    def _command(self, args):
        run = subprocess.run(["ip", "netns", "exec", self.namespace, "tc", *args], capture_output=True, timeout=3)
        if run.returncode or len(run.stdout) + len(run.stderr) > 65536:
            raise ValueError("bounded queue command failed: " + " ".join(args[:4]))
        return run.stdout.decode("utf-8")

    def _sample(self, phase):
        qdiscs = json.loads(self._command(["-j", "-s", "qdisc", "show", "dev", "eth0"]))
        text = self._command(["-s", "qdisc", "show", "dev", "eth0"])
        row = {"phase": phase, "at_ns": time.monotonic_ns(), "qdiscs": qdiscs, "text": text}
        self.report["samples"].append(row)
        return _root(row)

    def _run(self):
        try:
            if self._sample("before").get("kind") != "noqueue":
                raise ValueError("refuse to replace an existing worker root qdisc")
            if self.stop.wait(max(0, self.start - time.monotonic())):
                raise ValueError("queue control cancelled before injection")
            self._command(["qdisc", "add", "dev", "eth0", "root", "handle", "44:", "netem", "limit",
                           str(self.config["limit_packets"]), "delay", f"{self.config['delay_ms']}ms"])
            self.installed = True
            self.report["actual"] = {"start_ns": time.monotonic_ns()}
            while True:
                self._sample("active")
                remaining = self.end - time.monotonic()
                if remaining <= 0 or self.stop.wait(min(.1, remaining)):
                    break
            # Keep the qdisc and its queued skbs. A nonzero rate makes netem
            # clamp new deadlines to its last pending deadline, preventing
            # zero-delay newcomers from overtaking the old delayed backlog.
            # This finite-rate drain is a distinct v3 injection implementation.
            self._command(["qdisc", "change", "dev", "eth0", "root", "handle", "44:", "netem", "limit",
                           str(self.config["limit_packets"]), "delay", "0ms", "rate", f"{DRAIN_RATE_BPS}bit"])
            self.report["actual"]["end_ns"] = time.monotonic_ns()
            deadline = time.monotonic() + self.config["delay_ms"] / 1000 + 3
            while self._sample("draining").get("backlog") != 0:
                if time.monotonic() >= deadline:
                    raise ValueError("injected egress queue did not drain")
                time.sleep(.05)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.report["errors"].append(str(error))
        finally:
            try:
                if self.installed:
                    root = self._sample("cleanup")
                    if root.get("kind") != "netem" or root.get("handle") != "44:":
                        raise ValueError("queue ownership changed; refuse to remove an unknown root")
                    self._command(["qdisc", "del", "dev", "eth0", "root", "handle", "44:"])
                    self.installed = False
                self._sample("restored")
                self.report.setdefault("actual", {})["restored_ns"] = self.report["samples"][-1]["at_ns"]
                self.report["cleanup"] = "PASS"
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.report["errors"].append(str(error))
                self.report["cleanup"] = "FAIL"
            self.output.write_text(json.dumps(self.report, indent=2) + "\n")

    def begin(self):
        self.thread.start()

    def finish(self, cancelled=False):
        if cancelled:
            self.stop.set()
        self.thread.join(timeout=12)
        if self.thread.is_alive():
            raise ValueError("queue control cleanup thread did not converge")
        return self.report


class FifoQueueControl:
    """Parent handle of the persistent FIFO injector in the worker namespace."""

    def __init__(self, config, namespace, measurement_start, start, end, measurement_end, output):
        validate_config(config)
        from isolation import verify
        if (config["kind"] != fifo_ledger.KIND or verify() or not isinstance(namespace, str)
                or not re.fullmatch(r"rig-[a-f0-9]{6}-worker", namespace)):
            raise ValueError("FIFO queue control requires verified QA isolation and the owned worker namespace")
        inject_end = end - config["delay_ms"] / 1000 - DRAIN_GUARD_SEC
        if not measurement_start < start < inject_end < end < measurement_end:
            raise ValueError("loaded phase is too short for the frozen FIFO drain guard")
        self.config, self.namespace, self.output = config, namespace, Path(output)
        self.ready_by = measurement_start - fifo_ledger.INSTALL_LEAD_NS / 1e9
        self.spec = {"config": config, "report": str(self.output / "queue-control.json"),
                     "ledger": str(self.output / LEDGER_NAME),
                     "planned": {"measurement_start_ns": round(measurement_start * 1e9), "start_ns": round(start * 1e9),
                                 "end_ns": round(inject_end * 1e9), "recovery_start_ns": round(end * 1e9),
                                 "measurement_end_ns": round(measurement_end * 1e9)}}
        self.process, self.log, self.errors = None, None, []

    def begin(self):
        spec = self.output / "queue-fifo-spec.json"
        spec.write_text(json.dumps(self.spec, indent=2) + "\n")
        self.log = (self.output / "queue-fifo.log").open("wb")
        try:
            self.process = subprocess.Popen(
                ["ip", "netns", "exec", self.namespace, sys.executable, str(Path(__file__).with_name("fifo_injector.py")),
                 "--spec", str(spec)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log)
        except OSError as error:
            self.errors.append("FIFO injector could not start: " + str(error))
            self.log.close()
            return
        line = b""
        while not line.endswith(b"\n") and len(line) < 128:
            remaining = self.ready_by - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                self.errors.append("FIFO injector was not installed before the measured baseline")
                return
            chunk = os.read(self.process.stdout.fileno(), 128)
            if not chunk:
                self.errors.append("FIFO injector exited before installation")
                return
            line += chunk
        if not line.startswith(b"ready "):
            self.errors.append("FIFO injector reported an invalid readiness line")

    def finish(self, cancelled=False):
        """Release the FIFO path only after the caller reaped its load clients.

        A signal to the injector requests cancellation but cannot establish
        client quiescence. This final control message provides that ownership
        handoff; the injector still preserves the full measurement lifetime.
        """
        if self.process is not None:
            try:
                self.process.stdin.write(b"cancel\n" if cancelled or self.errors else b"finish\n")
                self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=self.config["delay_ms"] / 1000 + 60)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
                self.errors.append("FIFO injector did not exit after its teardown request")
            self.process.stdout.close()
            self.log.close()
        path = Path(self.spec["report"])
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            report = {"schema_version": fifo_ledger.SCHEMA_VERSION, "config": self.config, "errors": [],
                      "cancelled": cancelled, "cleanup": "UNKNOWN"}
            self.errors.append("FIFO injector report is missing")
        if not isinstance(report, dict) or not isinstance(report.get("errors"), list):
            report = {"schema_version": fifo_ledger.SCHEMA_VERSION, "errors": ["malformed FIFO injector report"]}
        if self.process is not None and self.process.returncode and not report["errors"]:
            self.errors.append(f"FIFO injector exited {self.process.returncode}")
        report["errors"].extend(self.errors)
        path.write_text(json.dumps(report, indent=2) + "\n")
        return report
