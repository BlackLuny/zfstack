#!/usr/bin/env python3
"""Run already-built pump probes. Caller must hold the repository's host QA Lease."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time

COMPILER = re.compile(r"^(cargo|rustc|zig|clang(?:\+\+)?(?:-\d+)?|gcc(?:-\d+)?|g\+\+(?:-\d+)?|cc|c\+\+|cc1|cc1plus|ld(?:\.lld)?|lld|rust-lld|cmake|ninja|make)$")

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def compilers():
    found = []
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            name = (path / "comm").read_text().strip()
            command = (path / "cmdline").read_bytes().split(b"\0", 1)[0].decode(errors="replace")
            if COMPILER.fullmatch(name) or COMPILER.fullmatch(Path(command).name):
                found.append({"pid": int(path.name), "comm": name, "program": command})
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    return found

def host_snapshot():
    return {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "hostname": socket.gethostname(), "loadavg": Path("/proc/loadavg").read_text().strip(),
            "cpu": Path("/proc/stat").read_text().splitlines()[0], "compilers": compilers()}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("counts", "time"), required=True)
    parser.add_argument("--cpu", type=int, required=True)
    args = parser.parse_args()
    if args.cpu not in os.sched_getaffinity(0):
        raise SystemExit("Chosen CPU is outside the runner's allowed affinity")
    manifest = json.loads(args.manifest.read_text())
    overlays = {entry["label"]: entry for entry in manifest["overlays"]}
    if args.output.exists():
        raise SystemExit("Refusing to replace any existing measurement directory")
    args.output.mkdir(parents=True)
    plan = []
    if args.mode == "counts":
        for label in ("A-counts", "B-counts"):
            for case, iterations in (("idle1", 1000), ("idle64", 64000), ("consumed", 1), ("tx", 1)):
                plan.append({"phase": "counts", "pair": None, "label": label, "case": case, "iterations": iterations})
    else:
        # All A/A samples precede all A/B samples. Alternating order is fixed;
        # no result-dependent expansion, early stopping, or discarded samples.
        for phase, pairs, first, second in (("AA", 4, "A1", "A2"), ("AB", 12, "A1", "B")):
            for case in ("idle1", "idle64"):
                for pair in range(pairs):
                    order = (first, second) if pair % 2 == 0 else (second, first)
                    for label in order:
                        plan.append({"phase": phase, "pair": pair, "label": label, "case": case, "iterations": 1_000_000})
    binaries = {}
    for label in dict.fromkeys(entry["label"] for entry in plan):
        binary = Path(overlays[label]["path"]) / "target/release/examples/issue53_pump_probe"
        if not binary.is_file():
            raise SystemExit(f"Build required before running: {binary}")
        binaries[label] = {"path": str(binary), "sha256": digest(binary)}
    frozen = {"manifest_sha256": digest(args.manifest), "mode": args.mode, "cpu": args.cpu,
              "thread_count": 1, "plan": plan, "binaries": binaries, "host": host_snapshot(),
              "meaning": "Production ordinary Driver::pump on pre-established Sim connections; no network E2E inference."}
    (args.output / "PLAN.json").write_text(json.dumps(frozen, indent=2) + "\n")
    if frozen["host"]["compilers"]:
        raise SystemExit("BLOCKED: compiler processes present; see PLAN.json; no probe launched")
    with (args.output / "raw.jsonl").open("x") as raw:
        for index, entry in enumerate(plan):
            record = {"slot": index, **entry, "before": host_snapshot()}
            binary = binaries[entry["label"]]
            if record["before"]["compilers"] or digest(Path(binary["path"])) != binary["sha256"]:
                record["status"] = "BLOCKED_COMPILER_OR_BINARY_CHANGE"
                raw.write(json.dumps(record) + "\n"); raw.flush()
                raise SystemExit("BLOCKED before next sample; completed samples retained")
            command = ["taskset", "--cpu-list", str(args.cpu), binary["path"], entry["case"], str(entry["iterations"])]
            record["command"] = command
            started = time.monotonic_ns()
            try:
                completed = subprocess.run(command, text=True, capture_output=True, timeout=60)
                record.update(returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
                if completed.returncode == 0:
                    record["measurement"] = json.loads(completed.stdout)
            except subprocess.TimeoutExpired as error:
                record.update(returncode=None, stdout=str(error.stdout), stderr=str(error.stderr), timed_out=True)
            record["subprocess_wall_ns"] = time.monotonic_ns() - started
            record["after"] = host_snapshot()
            measurement = record.get("measurement", {})
            expected_counted = args.mode == "counts"
            valid = (record.get("returncode") == 0 and measurement.get("instrumented") is expected_counted
                     and measurement.get("reserved_final") == 0 and not record["after"]["compilers"])
            if valid and expected_counted:
                per_pump = 3 if entry["label"] == "A-counts" else (2 if entry["case"] == "consumed" else 1)
                record["expected_pump_queue_locks"] = per_pump * entry["iterations"]
                valid = measurement["pump_queue_locks"] == record["expected_pump_queue_locks"]
            record["status"] = "PASS" if valid else "FAIL_OR_CONTAMINATED"
            raw.write(json.dumps(record) + "\n"); raw.flush()
            print(json.dumps({"slot": index, "phase": entry["phase"], "case": entry["case"], "label": entry["label"], "status": record["status"]}), flush=True)
            if not valid:
                raise SystemExit("Sample failed or was contaminated; raw output retained, no replacement launched")
    print(json.dumps({"completed": len(plan), "output": str(args.output)}))

if __name__ == "__main__":
    main()
