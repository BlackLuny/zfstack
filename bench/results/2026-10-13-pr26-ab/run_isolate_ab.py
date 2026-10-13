#!/usr/bin/env python3
"""Serial AB/BA runner for zfbench --test isolate. Records failed lights as data."""
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


def cleanup(binary):
    subprocess.run(
        ["sudo", "env", f"PATH={os.environ.get('PATH', '')}", str(binary), "--cleanup"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--pairs", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=False)
    binaries = {"baseline": Path(args.baseline).resolve(), "candidate": Path(args.candidate).resolve()}
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in binaries.items()}
    cases = json.loads(Path(args.cases).read_text())
    metadata = {
        "argv": vars(args),
        "binary_sha256": hashes,
        "cases": cases,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "order": "AB, BA, AB, BA ...",
        "initial_environment": snapshot(),
        "notes": "exit 1 (correctness / light timeout) is retained as a measured outcome; exit 3 stops the runner",
    }
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2))
    env = os.environ.copy()
    env["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin:" + env.get("PATH", "")
    with (out / "runs.jsonl").open("x", buffering=1) as records:
        for case in cases:
            for pair in range(-1, args.pairs):
                order = ["baseline", "candidate"] if pair % 2 == 0 else ["candidate", "baseline"]
                for position, variant in enumerate(order):
                    binary = binaries[variant]
                    json_path = out / f"{case['name']}.p{pair}.{variant}.json"
                    command = [
                        "sudo",
                        "env",
                        f"PATH={env['PATH']}",
                        str(binary),
                        *case["args"],
                        "--json",
                        str(json_path),
                    ]
                    cleanup(binary)
                    before = snapshot()
                    started = time.monotonic()
                    try:
                        completed = subprocess.run(
                            command,
                            text=True,
                            capture_output=True,
                            timeout=args.timeout,
                            env=env,
                        )
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
                    cleanup(binary)
                    after = snapshot()
                    result = None
                    if json_path.exists():
                        try:
                            result = json.loads(json_path.read_text())
                        except json.JSONDecodeError:
                            result = None
                    row = {
                        "case": case["name"],
                        "pair": pair,
                        "warmup": pair < 0,
                        "position": position,
                        "variant": variant,
                        "command": command,
                        "binary_sha256": hashes[variant],
                        "exit_code": code,
                        "subprocess_wall_sec": elapsed,
                        "before": before,
                        "after": after,
                        "result": result,
                        "stderr": stderr,
                        "stdout": stdout,
                    }
                    records.write(json.dumps(row) + "\n")
                    lights = []
                    if result:
                        for user in (result.get("results") or {}).get("loaded_users") or []:
                            samples = user.get("samples") or {}
                            lights.append(
                                {
                                    "user": user.get("user"),
                                    "n": samples.get("n"),
                                    "p50_ms": samples.get("p50_ms"),
                                    "p99_ms": samples.get("p99_ms"),
                                    "max_ms": samples.get("max_ms"),
                                }
                            )
                    print(
                        json.dumps(
                            {
                                "case": case["name"],
                                "pair": pair,
                                "variant": variant,
                                "exit_code": code,
                                "ok": None if result is None else result.get("ok"),
                                "errors": None if result is None else result.get("errors"),
                                "heavy_mbps": None
                                if result is None
                                else (result.get("results") or {}).get("heavy", {}).get("goodput_mbps_window"),
                                "lights": lights,
                                "cpu_sec_per_GB": None if result is None else (result.get("cpu") or {}).get("cpu_sec_per_effective_GB"),
                            }
                        ),
                        flush=True,
                    )
                    if code not in (0, 1) or result is None:
                        raise RuntimeError(f"setup/crash for {case['name']}/{variant} exit={code}; inspect retained output")
    metadata["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    metadata["final_environment"] = snapshot()
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
