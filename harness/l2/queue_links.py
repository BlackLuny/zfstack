"""Owned-namespace link counters for the queue-control precondition only."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

ROLES = {"worker", "target", "client0", "client1", "client2", "client3"}


def check_nodes(nodes):
    if not isinstance(nodes, dict) or set(nodes) != ROLES or len(set(nodes.values())) != len(ROLES):
        raise ValueError("queue links require six distinct owned endpoint namespaces")
    prefixes = set()
    for role, namespace in nodes.items():
        match = re.fullmatch(r"(rig-[a-f0-9]{6})-(worker|target|client|fwd[1-4])", namespace)
        if not match or (role in ("worker", "target") and match[2] != role) or (role.startswith("client") and match[2] not in ("client", "fwd1", "fwd2", "fwd3", "fwd4")):
            raise ValueError("queue link namespace is outside the owned topology")
        prefixes.add(match[1])
    if len(prefixes) != 1:
        raise ValueError("queue link namespaces span different topologies")


def snapshot(nodes):
    from isolation import verify
    if verify():
        raise ValueError("queue link sampling requires verified QA isolation")
    check_nodes(nodes)
    report = {"schema_version": 1, "clock": "CLOCK_MONOTONIC", "begin_ns": time.monotonic_ns(), "nodes": {}, "errors": []}
    for role, namespace in sorted(nodes.items()):
        row = {"namespace": namespace, "begin_ns": time.monotonic_ns()}
        try:
            row["namespace_inode"] = os.stat(Path("/var/run/netns") / namespace).st_ino
            run = subprocess.run(["ip", "netns", "exec", namespace, "ip", "-s", "-s", "-j", "link", "show"],
                                 capture_output=True, timeout=3)
            if run.returncode or len(run.stdout) > 65536 or len(run.stderr) > 65536:
                raise ValueError("bounded ip link command failed or exceeded output budget")
            row["links"] = json.loads(run.stdout)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            report["errors"].append(role + ": " + str(error))
        row["end_ns"] = time.monotonic_ns()
        report["nodes"][role] = row
    report["end_ns"] = time.monotonic_ns()
    return report


def _integer(value):
    return type(value) is int and 0 <= value < 2**64


def validate(report, *, before_ns=None, after_ns=None):
    """Raw ip counters, never supplied deltas/flags; missing is not zero."""
    errors = []
    try:
        if (not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1
                or report.get("clock") != "CLOCK_MONOTONIC" or set(report) != {"schema_version", "clock", "before", "after"}):
            raise ValueError("missing/invalid raw queue link samples")
        observed = []
        for phase in ("before", "after"):
            shot = report[phase]
            if (type(shot.get("schema_version")) is not int or shot["schema_version"] != 1 or shot.get("clock") != "CLOCK_MONOTONIC"
                    or shot.get("errors") != [] or not _integer(shot.get("begin_ns"))
                    or not _integer(shot.get("end_ns")) or shot["end_ns"] < shot["begin_ns"]):
                raise ValueError("queue link snapshot failed or has invalid clock")
            nodes = shot["nodes"]
            check_nodes({role: row["namespace"] for role, row in nodes.items()})
            identities, per_node = set(), {}
            for role, row in nodes.items():
                inode = row.get("namespace_inode")
                if not _integer(inode) or not inode or inode in identities:
                    raise ValueError("missing/duplicate queue link namespace identity")
                identities.add(inode)
                if (not _integer(row.get("begin_ns")) or not _integer(row.get("end_ns"))
                        or not shot["begin_ns"] <= row["begin_ns"] <= row["end_ns"] <= shot["end_ns"]):
                    raise ValueError("queue link observation is outside its snapshot clock")
                links = row.get("links")
                if not isinstance(links, list) or not links or len(links) > 16:
                    raise ValueError("missing/invalid raw queue link interface list")
                entries = {}
                for link in links:
                    name, index, address, link_type = link.get("ifname"), link.get("ifindex"), link.get("address"), link.get("link_type")
                    if (not isinstance(name, str) or not name or name in entries or not _integer(index) or not index
                            or not isinstance(link_type, str) or not link_type
                            or (address is not None and (not isinstance(address, str) or not address))
                            or (link_type != "none" and address is None)):
                        raise ValueError("missing/duplicate queue link interface identity")
                    stats = link.get("stats64")
                    if not isinstance(stats, dict) or set(stats) != {"rx", "tx"}:
                        raise ValueError("missing raw queue link stats64 counters")
                    for direction in ("rx", "tx"):
                        values = stats[direction]
                        if (not isinstance(values, dict) or not {"bytes", "packets", "errors", "dropped"} <= set(values)
                                or any(not _integer(value) for value in values.values())):
                            raise ValueError("missing/invalid queue link counters")
                    entries[name] = {"identity": (index, address, link_type, link.get("link_index")), "stats": stats}
                if not {"lo", "eth0"} <= set(entries) or (role.startswith("client") and "wg0" not in entries):
                    raise ValueError("queue link snapshot omits a required physical/WG interface")
                per_node[role] = {"identity": (row["namespace"], inode), "links": entries}
            observed.append(per_node)
        if report["before"]["end_ns"] >= report["after"]["begin_ns"]:
            raise ValueError("queue link snapshots overlap or reverse time")
        if before_ns is not None and report["before"]["end_ns"] >= before_ns:
            raise ValueError("queue link pre-snapshot did not precede the load")
        if after_ns is not None and report["after"]["begin_ns"] < after_ns:
            raise ValueError("queue link post-snapshot did not cover the full load/recovery")
        for role in sorted(ROLES):
            first, last = observed[0][role], observed[1][role]
            if first["identity"] != last["identity"] or set(first["links"]) != set(last["links"]):
                raise ValueError("queue link namespace/interface identity changed: " + role)
            for name, start in first["links"].items():
                finish = last["links"][name]
                if start["identity"] != finish["identity"]:
                    raise ValueError("queue link interface replaced: " + role + "/" + name)
                for direction in ("rx", "tx"):
                    a, b = start["stats"][direction], finish["stats"][direction]
                    if set(a) != set(b):
                        raise ValueError("queue link counter set changed")
                    for key in a:
                        delta = b[key] - a[key]
                        if delta < 0:
                            errors.append(f"queue link counter reset: {role}/{name}/{direction}.{key}")
                        elif delta and (key in ("dropped", "errors") or key.endswith("_errors")):
                            errors.append(f"queue link {role}/{name}/{direction}.{key} increased by {delta}; latency-only injection is unqualified")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        errors.append(str(error))
    return sorted(set(errors))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", action="append", required=True)
    args = parser.parse_args()
    nodes = {}
    for item in args.node:
        role, namespace = item.split("=", 1)
        if role in nodes:
            parser.error("duplicate namespace role")
        nodes[role] = namespace
    report = snapshot(nodes)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
