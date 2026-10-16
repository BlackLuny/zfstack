#!/usr/bin/env python3
"""Locate where UDP order is lost after the netem FIFO, from retained files only.

Inputs are one self-test output directory (ledger, injector report, receiver
samples) and, optionally, the veth_order_trace.sh ftrace of the same run. The
ledger gives eth0 egress order; the receiver gives socket order; the trace
gives, per frame, the CPU that handed it to the receiving veth backlog
(netif_rx) and the CPU and time it was processed (netif_receive_skb).
"""
import argparse
import collections
import gzip
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import netem_fifo_ledger as ledger  # noqa: E402

UDP_FRAME_BYTES = 1074
LINE = re.compile(r"\[(\d+)\]\s+\S+\s+(\d+)\.(\d+): (netif_rx|netif_receive_skb): dev=(\S+) skbaddr=(\S+) len=(\d+)")


def quantiles(values):
    if not values:
        return None
    values = sorted(values)
    pick = lambda q: values[min(len(values) - 1, int(q * len(values)))]
    return {"n": len(values), "p50": pick(.5), "p99": pick(.99), "max": values[-1]}


def inversions(order):
    """Frames that some earlier-numbered frame overtook, and the overtaken spans."""
    late, best = [], -1
    for value in order:
        if value < best:
            late.append(value)
        best = max(best, value)
    return late


def egress(directory):
    report = json.loads((directory / "queue-control.json").read_text())
    in_rows, out_rows = ledger.decode((directory / "queue-ledger.bin.gz").read_bytes())
    offset = ledger.clock_offset(report["clock_samples"])[0]
    pairs, _, _ = ledger.match(in_rows, out_rows)
    udp = sorted((in_rows[a][0] - offset, out_rows[b][0] - offset) for a, b in pairs if in_rows[a][1] == UDP_FRAME_BYTES)
    return report, udp


def trace(path):
    events = []
    with gzip.open(path, "rt") as stream:
        for line in stream:
            match = LINE.search(line)
            if match:
                cpu, sec, frac, kind, dev, skb, length = match.groups()
                events.append((int(sec) * 1_000_000_000 + int(frac.ljust(9, "0")[:9]), int(cpu), kind, skb))
    events.sort()
    pending, frames = collections.defaultdict(collections.deque), []
    for ns, cpu, kind, skb in events:
        if kind == "netif_rx":
            frame = {"rx_ns": ns, "rx_cpu": cpu}
            pending[skb].append(frame)
            frames.append(frame)
        elif pending[skb]:
            frame = pending[skb].popleft()
            frame.update(recv_ns=ns, recv_cpu=cpu)
    return [frame for frame in frames if "recv_ns" in frame]


def analyze(directory, trace_path):
    report, udp = egress(directory)
    received = json.loads((directory / "receiver.json").read_text())["udp"]
    sent = {row[0]: row for row in received}
    socket_order = [row[0] for row in received]
    if len(udp) != len(received):
        raise SystemExit("UDP frames in the ledger differ from received datagrams")
    egress_ordered = all(a[1] < b[1] for a, b in zip(udp, udp[1:]))
    late = inversions(socket_order)
    result = {"source": str(directory), "udp_frames": len(udp), "egress_in_send_order": egress_ordered,
              "socket_overtaken": len(late),
              "socket_overtaken_after_egress_ns": quantiles([sent[seq][2] - udp[seq][1] for seq in late]),
              "egress_gap_to_overtaker_ns": None}
    gaps = []
    best = -1
    for seq in socket_order:
        if seq < best:
            gaps.append(udp[best][1] - udp[seq][1])
        best = max(best, seq)
    result["egress_gap_to_overtaker_ns"] = quantiles(gaps)
    planned = report["planned"]
    phase = lambda t: ("baseline" if t < planned["start_ns"] else "loaded" if t < planned["end_ns"]
                       else "drain-or-guard" if t < planned["recovery_start_ns"] else "recovery")
    result["socket_overtaken_by_send_phase"] = dict(collections.Counter(phase(sent[seq][1]) for seq in late))
    if trace_path:
        frames = trace(trace_path)
        result["trace_frames"] = len(frames)
        if len(frames) == len(udp):
            processed = sorted(range(len(frames)), key=lambda index: frames[index]["recv_ns"])
            overtaken = inversions(processed)
            same_cpu = [frames[index]["rx_cpu"] == frames[index]["recv_cpu"] for index in range(len(frames))]
            best, crossed = -1, []
            for index in processed:
                if index < best:
                    crossed.append(frames[index]["rx_cpu"] != frames[best]["rx_cpu"])
                best = max(best, index)
            result["trace"] = {
                "processed_on_handoff_cpu": sum(same_cpu), "handoff_cpus": dict(collections.Counter(f["rx_cpu"] for f in frames)),
                "overtaken": len(overtaken), "overtaken_match_socket": overtaken == late,
                "overtaker_on_other_handoff_cpu": sum(crossed),
                "handoff_cpu_changes": sum(a["rx_cpu"] != b["rx_cpu"] for a, b in zip(frames, frames[1:])),
                "backlog_wait_ns": quantiles([frames[index]["recv_ns"] - frames[index]["rx_ns"] for index in overtaken])}
        else:
            result["trace"] = "skipped: traced UDP-sized frames differ from the ledger"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(args.run, args.trace), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
