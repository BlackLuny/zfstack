#!/usr/bin/env python3
"""Freeze and execute the capacity screening protocol, serially in an owned rig."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
ARMS = {
    "A": {},
    "B": {"ZFBENCH_MAX_SND_BUF": "262144"},
    "C": {"ZFBENCH_PREFETCH_MAX": "65536"},
    "D": {"ZFBENCH_MAX_SND_BUF": "262144", "ZFBENCH_PREFETCH_MAX": "65536"},
}
CLIENT = ["--client-proxy", "zfstack-client", "--splice", "--driver-thread",
          "--proxy-workers", "2", "--tun-mtu", "65535", "--flows", "8", "--secs", "12", "--warmup", "2"]
ISOLATE = ["--test", "isolate", "--stack", "zfstack-cubic", "--ports", "1", "--lights", "3",
           "--rate-mbps", "100", "--secs", "12", "--warmup", "2", "--idle-rr-secs", "2",
           "--rr-size", "256", "--rr-interval-ms", "40", "--seed", "23"]
CASES = {
    "client_down8": CLIENT + ["--test", "down"],
    "client_up8": CLIENT + ["--test", "up"],
    "isolate200_rtt10": ISOLATE + ["--heavy-conns", "200", "--rtt-ms", "10", "--queue-bytes", "250000"],
    "isolate1_rtt80": ISOLATE + ["--heavy-conns", "1", "--rtt-ms", "80", "--queue-bytes", "2000000"],
}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def save(path, value):
    with Path(path).open("x") as f:
        json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
        f.write("\n")


def plan(seed=20261023):
    rng = random.Random(seed)
    cases = list(CASES)
    rng.shuffle(cases)
    rows = []
    for case in cases:
        for pos in range(2):
            rows.append(dict(case=case, block="AA", position=pos, arm="A"))
        candidates = ["B", "C", "D"]
        rng.shuffle(candidates)
        for candidate in candidates:
            for pos, arm in enumerate(["A", candidate, candidate, "A"]):
                rows.append(dict(case=case, block=candidate, position=pos, arm=arm))
    return [dict(index=i, **r) for i, r in enumerate(rows)]


def counters():
    result = {}
    for name in ["cpu.stat", "cpu.max", "memory.current", "memory.max"]:
        p = Path("/sys/fs/cgroup") / name
        result[name] = p.read_text() if p.exists() else None
    result["loadavg"] = Path("/proc/loadavg").read_text()
    return result


def execute(out, name, row, container, binary, digest, extra=None):
    dest = out / name
    dest.mkdir()
    rel = dest.relative_to(ROOT)
    env = dict(ARMS[row["arm"]])
    if row["case"].startswith("isolate"):
        env.update(ZFBENCH_RR_RAW="1", ZFBENCH_RR_OPEN_HZ="25",
                   ZFBENCH_RR_ARRIVALS=f"/work/{rel}/arrivals.json")
    argv = ["docker", "exec", container, "env"] + [f"{k}={v}" for k, v in env.items()]
    argv += ["timeout", "-k", "5", "150", "python3", "/work/bench/tail69_exec.py",
             f"/work/{rel}/resources.json", "taskset", "-c", "0-3", f"/work/{binary.relative_to(ROOT)}"]
    argv += extra if extra is not None else CASES[row["case"]]
    argv += ["--label", name]
    if sha(binary) != digest:
        raise RuntimeError("binary changed before run")
    save(dest / "start.json", dict(row=row, argv=argv, env=env, binary_sha256=digest,
                                  start_unix=time.time(), cgroup=counters()))
    with (dest / "stdout.json").open("x") as stdout, (dest / "stderr.txt").open("x") as stderr:
        try:
            proc = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=165)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = "outer_timeout"
    # Keep each attempt even when the tool or generator reports failure.
    save(dest / "end.json", dict(returncode=rc, end_unix=time.time(), cgroup=counters(),
                                binary_unchanged=sha(binary) == digest))
    print(json.dumps(dict(name=name, returncode=rc)), flush=True)
    if rc in (124, 137, "outer_timeout"):
        raise RuntimeError("bounded attempt timed out; plan is incomplete, no retry")
    if sha(binary) != digest:
        raise RuntimeError("binary changed during run")
    return rc


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("out", type=Path)
    p.add_argument("--container", default="zfstack-capacity-20261023")
    p.add_argument("--binary", type=Path, default=ROOT / "target/release/zfbench")
    a = p.parse_args()
    out, binary = a.out.resolve(), a.binary.resolve()
    out.relative_to(ROOT)
    binary.relative_to(ROOT)
    out.mkdir(parents=True, exist_ok=False)
    protocol = ROOT / "docs/experiments/2026-10-23-capacity/PROTOCOL.md"
    sources = {str(f.relative_to(ROOT)): sha(f) for parent in [ROOT / "src", ROOT / "bench/src"] for f in sorted(parent.rglob("*.rs"))}
    for f in [ROOT / "Cargo.lock", ROOT / "Cargo.toml", ROOT / "bench/Cargo.toml", Path(__file__), protocol]:
        sources[str(f.relative_to(ROOT))] = sha(f)
    manifest = dict(base_sha=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                    binary_sha256=sha(binary), sources=sources, cases=CASES, arms=ARMS,
                    seed=20261023, rows=plan(), host=os.uname()._asdict() if hasattr(os.uname(), "_asdict") else list(os.uname()),
                    affinity=sorted(os.sched_getaffinity(0)), environment=counters())
    save(out / "plan.json", manifest)
    (out / "source.patch").write_bytes(subprocess.check_output(["git", "diff", "--", "bench/src"], cwd=ROOT))
    smoke = ["--stack", "zfstack-cubic", "--test", "down", "--rate-mbps", "20", "--rtt-ms", "10", "--secs", "3", "--warmup", "1"]
    rc = execute(out, "smoke", dict(case="client_down8", arm="A"), a.container, binary, manifest["binary_sha256"], smoke)
    if rc != 0:
        raise SystemExit("setup smoke failed; frozen plan not started")
    for row in manifest["rows"]:
        execute(out, f"{row['index']:03d}-{row['case']}-{row['block']}-{row['position']}-{row['arm']}", row,
                a.container, binary, manifest["binary_sha256"])
    save(out / "complete.json", dict(attempts=len(manifest["rows"]), end_unix=time.time()))


if __name__ == "__main__":
    main()
