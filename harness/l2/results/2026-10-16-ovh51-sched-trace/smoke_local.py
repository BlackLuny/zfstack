#!/usr/bin/env python3
"""Local-only smoke of arm.py's tracer, process map and snapshots plus analyze_trace.py.

Runs two real udp_probe.py generators against a loopback UDP echo for a few
seconds under the same private tracefs instance, then analyzes the output with
the frozen analyzer. It exercises real kernel trace formats; it is not an arm.
Run as root on a disposable kernel (the repository's TCG VM), never on a shared host.
"""
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
L2 = HERE.parents[1]
sys.path.insert(0, str(HERE))
import arm  # noqa: E402
import analyze_trace  # noqa: E402


def echo(stop, port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", port))
        sock.settimeout(.1)
        while not stop.is_set():
            try:
                data, peer = sock.recvfrom(2048)
            except socket.timeout:
                continue
            sock.sendto(data, peer)


def main():
    output = Path(sys.argv[1])
    output.mkdir()
    rig = output / "baseline/rig"
    rig.mkdir(parents=True)
    stop, port = threading.Event(), 19501
    threading.Thread(target=echo, args=(stop, port), daemon=True).start()
    (output / "host-before.json").write_text(json.dumps(arm.host_snapshot(), indent=2) + "\n")
    tracer, sampler = arm.Tracer(output), arm.ProcessMap()
    sampler.start()
    tracer.start()
    try:
        start = time.monotonic() + 1
        children = [subprocess.Popen([sys.executable, str(L2 / "udp_probe.py"), "--host", "127.0.0.1", "--port", str(port),
                                      "--flow", str(flow), "--start", str(start), "--baseline", "2", "--loaded", "1",
                                      "--recovery", "1", "--request-timeout", "1", "--out", str(rig / f"udp-{flow}.json")],
                                     cwd=L2, env={"L2_UDP_RATE_HZ": "100", "PATH": "/usr/bin:/bin"}) for flow in (0, 1)]
        codes = [child.wait(timeout=60) for child in children]
    finally:
        stats = tracer.stop()
        tracer.remove()
        sampler.stop.set()
        sampler.join()
        stop.set()
    (output / "processes.json").write_text(json.dumps(sorted(sampler.seen.values(), key=lambda r: r["first_seen_ns"]), indent=2) + "\n")
    result = analyze_trace.analyze(output)
    summary = {"generator_exit_codes": codes, "instance_removed": not tracer.path.exists(), "trace_stats": stats,
               "valid": result["valid"], "invalid_reasons": result["invalid_reasons"],
               "generator_pids": result["generator_pids"], "generator_lag": result["generator_lag"],
               "class_counts": result["class_counts"], "late_samples": result["late_samples"][:5],
               "generator_cpu_residency_ms": result["generator_cpu_residency_ms"]}
    (output / "smoke-summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: summary[k] for k in ("valid", "invalid_reasons", "generator_pids", "class_counts", "instance_removed")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
