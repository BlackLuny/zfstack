#!/usr/bin/env python3
"""Validate delay-only L2 qdiscs; queue loss invalidates either comparison arm."""
import argparse
import json
import math
from pathlib import Path


def root_qdisc(path):
    qdiscs = json.loads(path.read_text())
    if not isinstance(qdiscs, list) or any(not isinstance(q, dict) for q in qdiscs):
        raise ValueError("qdisc snapshot must be a JSON array of objects")
    roots = [q for q in qdiscs if q.get("root") is True]
    if len(roots) != 1:
        raise ValueError("expected one root qdisc on wg0")
    return roots[0]


def delta(before, after):
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError("qdisc counters must be objects")
    if (before["kind"], before["handle"]) != (after["kind"], after["handle"]):
        raise ValueError("root qdisc changed during the measurement")
    if before.get("options") != after.get("options"):
        raise ValueError("qdisc options changed during the measurement")
    counts = {}
    for name in ("bytes", "packets", "drops"):
        values = [q[name] for q in (before, after)]
        if any(type(v) is not int or v < 0 for v in values):
            raise ValueError(f"invalid {name} counter")
        counts[name] = values[1] - values[0]
        if counts[name] < 0:
            raise ValueError(f"{name} counter reset during the measurement")
    return counts


def evidence_errors(report, delay_ms):
    # Old no-delay runs remain usable; old delayed runs cannot establish that
    # their implicit 1000-packet queue did not discard ACKs (#37).
    if report is None:
        return ["missing qdisc evidence for delayed run"] if delay_ms else []
    if not isinstance(report, dict):
        return ["invalid qdisc report"]
    errors = []
    if report.get("uplink_delay_ms") != delay_ms:
        errors.append("qdisc delay metadata differs from the workload")
    limit = report.get("limit_packets")
    if delay_ms and (type(limit) is not int or not 0 < limit <= 0xffffffff):
        errors.append("missing or invalid netem packet limit")
    clients = report.get("clients", {})
    if not isinstance(clients, dict):
        return errors + ["invalid qdisc client evidence"]
    if set(clients) != {str(i) for i in range(4)}:
        errors.append("missing qdisc evidence for one or more clients")
    for client, row in clients.items():
        try:
            if not isinstance(row, dict):
                raise ValueError("client evidence must be an object")
            if row.get("error"):
                raise ValueError(row["error"])
            before, after = row["before"], row["after"]
            counts = delta(before, after)
            if delay_ms:
                for q in (before, after):
                    if q["kind"] != "netem" or q["options"]["limit"] != limit:
                        raise ValueError("netem missing or packet limit differs from requested value")
            elif before["kind"] == "netem":
                raise ValueError("unexpected netem in a no-delay run")
            if counts["drops"]:
                errors.append(f"client {client}: qdisc dropped {counts['drops']} packets during the measurement")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"client {client}: invalid qdisc evidence: {exc}")
    return errors


def summarize(path, delay_ms, limit_packets):
    report = {"uplink_delay_ms": delay_ms,
              "limit_packets": limit_packets if delay_ms else None, "clients": {}}
    for i in range(4):
        row = {}
        try:
            row["before"] = root_qdisc(path / f"qdisc-pre-{i}.json")
            row["after"] = root_qdisc(path / f"qdisc-post-{i}.json")
            row["delta"] = delta(row["before"], row["after"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            row["error"] = str(exc)
        report["clients"][str(i)] = row
    report["errors"] = evidence_errors(report, delay_ms)
    report["ok"] = not report["errors"]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--delay-ms", type=float, required=True)
    parser.add_argument("--limit-packets", type=int, required=True)
    parser.add_argument("--check", action="store_true", help="validate settings before changing the rig")
    args = parser.parse_args()
    if not math.isfinite(args.delay_ms) or args.delay_ms < 0:
        parser.error("delay must be finite and nonnegative")
    if not 0 < args.limit_packets <= 0xffffffff:
        parser.error("packet limit must be a positive uint32")
    if args.check:
        print(int(args.delay_ms > 0))
        return
    if args.path is None:
        parser.error("output path is required")
    print(json.dumps(summarize(args.path, args.delay_ms, args.limit_packets), indent=2))


if __name__ == "__main__":
    main()
