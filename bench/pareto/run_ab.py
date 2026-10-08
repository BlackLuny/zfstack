#!/usr/bin/env python3
"""Serial, pinned AB/BA runner for pareto_probe binaries; never runs builds."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time


def snapshot():
    result = {}
    for name, path in [
        ("loadavg", "/proc/loadavg"),
        ("cpu_stat", "/sys/fs/cgroup/cpu.stat"),
        ("cpu_max", "/sys/fs/cgroup/cpu.max"),
        ("proc_stat", "/proc/stat"),
    ]:
        try:
            value = Path(path).read_text()
            if name == "proc_stat":
                value = "\n".join(line for line in value.splitlines() if line.startswith("cpu"))
            result[name] = value.strip()
        except OSError as error:
            result[name] = str(error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--pairs", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=False)
    binaries = {"baseline": Path(args.baseline).resolve(), "candidate": Path(args.candidate).resolve()}
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
    cases = json.loads(Path(args.cases).read_text())
    metadata = {
        "argv": vars(args), "binary_sha256": hashes,
        "cases": cases, "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "affinity": args.cpu, "order": "AB, BA, AB, BA ...", "initial_environment": snapshot(),
    }
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2))
    with (out / "runs.jsonl").open("x", buffering=1) as records:
        for case in cases:
            for pair in range(-1, args.pairs):
                order = ["baseline", "candidate"] if pair % 2 == 0 else ["candidate", "baseline"]
                for position, variant in enumerate(order):
                    command = ["taskset", "-c", str(args.cpu), str(binaries[variant]), *case["args"]]
                    before = snapshot()
                    started = time.monotonic()
                    try:
                        completed = subprocess.run(command, text=True, capture_output=True, timeout=args.timeout)
                        code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
                    except subprocess.TimeoutExpired as error:
                        code = 124
                        stdout = error.stdout or ""
                        stderr = error.stderr or ""
                        if isinstance(stdout, bytes):
                            stdout = stdout.decode(errors="replace")
                        if isinstance(stderr, bytes):
                            stderr = stderr.decode(errors="replace")
                    elapsed = time.monotonic() - started
                    after = snapshot()
                    objects = []
                    for line in stdout.splitlines():
                        try:
                            objects.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                    row = {
                        "case": case["name"], "pair": pair, "warmup": pair < 0,
                        "position": position, "variant": variant, "command": command,
                        "binary_sha256": hashes[variant], "exit_code": code,
                        "subprocess_wall_sec": elapsed, "before": before, "after": after,
                        "measurements": objects, "stderr": stderr, "stdout": stdout,
                    }
                    records.write(json.dumps(row) + "\n")
                    print(json.dumps({key: row[key] for key in ["case", "pair", "variant", "exit_code", "measurements"]}), flush=True)
                    if code != 0 or len(objects) != 1:
                        raise RuntimeError(f"Invalid run for {case['name']}/{variant}; inspect retained output")
    metadata["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metadata["final_environment"] = snapshot()
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
