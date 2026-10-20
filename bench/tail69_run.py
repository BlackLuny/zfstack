#!/usr/bin/env python3
"""One bounded local attribution case. Never overwrites existing evidence."""
import argparse
import hashlib
import json
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/experiments/2026-10-20-issue69-tail"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("case")
    p.add_argument("--stack", default="zfstack-bbr")
    p.add_argument("--conns", default=200, type=int)
    p.add_argument("--queue-bytes", type=int, default=250000)
    p.add_argument("--secs", type=int, default=14)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--trace", action="store_true")
    p.add_argument("--binary", default="target/aarch64-unknown-linux-musl/release/zfbench")
    p.add_argument("--env", action="append", default=[])
    p.add_argument("--open-hz", type=int, choices=range(1,1001))
    a = p.parse_args()
    dest = OUT / a.case
    dest.mkdir(exist_ok=False)
    binary = ROOT / a.binary
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    env = list(a.env)
    if a.open_hz:
        env += ["ZFBENCH_RR_OPEN_HZ="+str(a.open_hz),"ZFBENCH_RR_ARRIVALS=/work/"+str((dest/"arrivals.json").relative_to(ROOT))]
    if a.trace:
        env.append("ZFBENCH_TAIL_TRACE=/work/" + str((dest / "trace.jsonl").relative_to(ROOT)))
    cmd = ["docker", "exec", "zfstack-issue69-tail", "env"] + env + ["timeout", "180", "python3", "/work/bench/tail69_exec.py",
        "/work/"+str((dest/"resources.json").relative_to(ROOT)), "/work/" + a.binary,
        "--test", "isolate", "--stack", a.stack, "--ports", "1", "--lights", "3", "--heavy-conns", str(a.conns),
        "--rate-mbps", "100", "--rtt-ms", "10", "--secs", str(a.secs), "--warmup", "2", "--idle-rr-secs", "3",
        "--rr-interval-ms", "1", "--rr-size", "256", "--queue-bytes", str(a.queue_bytes), "--seed", str(a.seed)]
    meta = {"args":vars(a),"argv":cmd,"binary_sha256":digest,"start_unix":time.time()}
    (dest / "identity.json").write_text(json.dumps(meta,indent=2)+"\n")
    with (dest / "result.json").open("w") as output, (dest / "stderr").open("w") as err:
        proc = subprocess.run(cmd,stdout=output,stderr=err,timeout=190)
    meta.update(returncode=proc.returncode,end_unix=time.time())
    assert hashlib.sha256(binary.read_bytes()).hexdigest()==digest
    (dest / "identity.json").write_text(json.dumps(meta,indent=2)+"\n")
    x=json.loads((dest / "result.json").read_text())
    print(a.case, x["ok"], x["results"]["heavy"]["goodput_mbps_window"],
          [(u["samples"]["p99_ms"],u["samples"]["max_ms"]) for u in x["results"]["loaded_users"]],flush=True)
    if proc.returncode: raise SystemExit(proc.returncode)


if __name__ == "__main__": main()
