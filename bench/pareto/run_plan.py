#!/usr/bin/env python3
"""Execute one already-frozen formal case serially; never builds binaries.

All cooperating build/test/benchmark jobs must honor the same physical-domain
lease. An exclusive host assignment remains an orchestrator responsibility.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import sys

from analyze import validate_row
from environment import snapshot, validity
from perf_gate import canonical, decode_json, digest, read_json, validate_plan

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
DEFAULT_OUTPUT_BYTES = 1024 * 1024
MAX_ENVIRONMENT_BYTES = 16 * 1024 * 1024


def file_hash(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def execute(command, timeout, env, sample_interval=.1, lease=None, cpu=None,
            output=None, root=None, output_limit=DEFAULT_OUTPUT_BYTES):
    """Use the QA supervisor; sample the actual exec PID, never its supervisor.

    The exec wrapper pins itself and waits for one pre-exec sample. It retains
    its PID through exec, so cgroup/affinity observations refer to the DUT. The
    pre-exec sample is labelled and cannot satisfy in-run sampling by itself.
    Raw output goes to a bounded regular file, not an unbounded PIPE buffer.
    """
    from qa.common import EvidenceError
    from qa.process import run_child

    if type(output_limit) is not int or not 1 <= output_limit <= MAX_OUTPUT_BYTES:
        raise ValueError("invalid frozen output byte limit")
    if type(sample_interval) not in (int, float) or not math.isfinite(sample_interval) or not 0 < sample_interval <= 10:
        raise ValueError("invalid environment sampling interval")
    if output is None:
        with tempfile.TemporaryDirectory(prefix="pareto-run-") as temporary:
            return execute(command, timeout, env, sample_interval, lease, cpu,
                           Path(temporary) / "output.log", root, output_limit)
    output = Path(output).resolve()
    root = Path(root or Path.cwd()).resolve()
    start, samples, sample_errors = time.monotonic_ns(), [], []
    interruption, lifecycle = None, None
    stop = threading.Event()

    class CheckedLease:
        def check(self):
            nonlocal interruption
            if lease is not None:
                try:
                    lease.check()
                except Exception as error:
                    interruption = repr(error)
                    raise EvidenceError(str(error)) from error

    with tempfile.TemporaryDirectory(prefix=".probe-control-", dir=output.parent) as temporary:
        control = Path(temporary)
        identity, go = control / "pid.json", control / "go"
        payload = {"argv": command, "cpu": cpu, "output_limit": output_limit,
                   "pid_path": str(identity), "go_path": str(go)}
        (control / "exec.json").write_bytes(canonical(payload))

        def observe():
            retained_bytes = 0
            def retain(value):
                nonlocal retained_bytes
                retained_bytes += len(canonical(value))
                if retained_bytes > MAX_ENVIRONMENT_BYTES:
                    raise ValueError("environment observations exceeded byte budget")
                samples.append(value)
            try:
                while not stop.is_set() and not identity.exists():
                    stop.wait(.005)
                if stop.is_set(): return
                record = read_json(identity)
                pid = record["pid"]
                if type(pid) is not int or pid <= 0:
                    raise ValueError("invalid DUT PID handshake")
                first = snapshot(pid)
                retain({**first, "phase": "pre_exec"})
                go.touch(exist_ok=False)
                # On Linux /proc/PID/exe proves that exec actually occurred.
                # macOS is diagnostic-only and never admitted by main().
                while not stop.wait(min(sample_interval, .01)):
                    if sys.platform == "linux":
                        try: actual = Path(f"/proc/{pid}/exe").resolve(strict=True)
                        except (FileNotFoundError, ProcessLookupError): break
                        if actual != Path(command[0]).resolve(): continue
                    observed = snapshot(pid)
                    if sys.platform == "linux" and observed.get("affinity") is None:
                        break  # Process exited during the observation.
                    retain({**observed, "phase": "running"})
                    if stop.wait(sample_interval): break
            except Exception as error:
                sample_errors.append(f"DUT environment sampling failed: {type(error).__name__}: {error}")
                # Do not strand a child waiting for a failed observer. The
                # sampling error makes this run invalid, not a silent omission.
                go.touch(exist_ok=True)

        observer = threading.Thread(target=observe, name="pareto-environment", daemon=True)
        observer.start()
        try:
            lifecycle = run_child([sys.executable, "-I", str(Path(__file__).with_name("probe_exec.py")),
                                   str(control / "exec.json")], root, output, timeout, os.environ.copy(), CheckedLease())
        except BaseException as error:
            interruption = repr(error)
            lifecycle = {"status": "cancelled", "exit_code": None, "start_ns": start,
                         "end_ns": time.monotonic_ns(), "cleanup_status": "UNKNOWN",
                         "cleanup_scope": "unknown", "cleanup_limits": ["supervisor did not return a cleanup record"],
                         "lifecycle_detail": repr(error)}
        finally:
            stop.set()
            observer.join(timeout=5)
            if observer.is_alive(): sample_errors.append("environment observer did not finish")
    if not any(sample.get("phase") == "running" for sample in samples):
        sample_errors.append("missing in-run DUT environment sample")
    if output.exists():
        with output.open("rb") as stream:
            raw = stream.read(output_limit + 1)
        size = output.stat().st_size
    else:
        raw, size = b"", 0
        sample_errors.append("supervisor output artifact missing")
    if size >= output_limit or len(raw) > output_limit:
        sample_errors.append("probe output reached frozen byte limit")
        if lifecycle["status"] == "completed": lifecycle["status"] = "failed"
    # Invalid UTF-8 is retained with replacement but never admitted as JSON.
    try: stdout = raw[:output_limit].decode("utf-8")
    except UnicodeDecodeError:
        stdout = raw[:output_limit].decode("utf-8", errors="replace")
        sample_errors.append("probe output is not valid UTF-8")
    execution = {key: lifecycle[key] for key in ("status", "exit_code", "start_ns", "end_ns")}
    execution.update(clock="CLOCK_MONOTONIC", cleanup=lifecycle["cleanup_status"])
    environment_errors = validity(samples, env) + sample_errors
    if lifecycle["cleanup_scope"] != "linux-subreaper" or lifecycle["cleanup_limits"]:
        environment_errors.append("complete Linux descendant containment unavailable")
    return {"stdout": stdout, "stderr": "", "samples": samples, "interruption": interruption,
            "execution": execution, "lifecycle": lifecycle,
            "output": {"path": output.name, "size_bytes": size, "limit_bytes": output_limit,
                       "sha256": file_hash(output) if output.exists() else None, "streams": "stdout+stderr"},
            "environment_errors": sorted(set(environment_errors))}


def functional_outcome(objects, probe, admission_errors):
    """Malformed/missing observations block; observed contract violations fail."""
    for m in objects:
        if not isinstance(m, dict): continue
        if m.get("ok") is False or m.get("all_budget_leases_released") is False:
            return "FAIL"
        if type(m.get("final_reserved_bytes")) is int and m["final_reserved_bytes"] > 0:
            return "FAIL"
        if (type(m.get("checksum_u64")) is int and type(m.get("expected_checksum_u64")) is int
                and m["checksum_u64"] != m["expected_checksum_u64"]):
            return "FAIL"
    return "PASS" if probe is not None and not admission_errors else "NOT_RUN"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--expected-plan-hash", required=True)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--environment", type=Path, required=True)
    p.add_argument("--lease-directory", type=Path, required=True)
    p.add_argument("--physical-domain", required=True)
    p.add_argument("--generation", required=True)
    p.add_argument("--outdir", type=Path, required=True)
    a = p.parse_args()
    if sys.platform != "linux" or not hasattr(os, "sched_setaffinity"):
        p.error("formal pinned execution requires a native Linux measurement node")
    def interrupted(signum, frame):
        raise InterruptedError(f"runner received signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    plan, env = read_json(a.plan), read_json(a.environment)
    errors = validate_plan(plan)
    if digest(plan) != a.expected_plan_hash: errors.append("plan differs from trusted expected hash")
    if digest(env) != plan.get("environment_hash"): errors.append("environment manifest hash mismatch")
    config = plan.get("config", {})
    if not isinstance(config.get("args"), list) or not config["args"] or not all(isinstance(x, str) for x in config["args"]): errors.append("config.args must be a nonempty string array")
    if type(config.get("cpu")) is not int or config["cpu"] < 0: errors.append("config.cpu must be fixed")
    if type(config.get("timeout_sec")) not in (int, float) or not math.isfinite(config["timeout_sec"]) or config["timeout_sec"] <= 0: errors.append("config.timeout_sec must be fixed")
    output_limit = config.get("max_output_bytes", DEFAULT_OUTPUT_BYTES)
    if type(output_limit) is not int or not 1 <= output_limit <= MAX_OUTPUT_BYTES: errors.append("invalid frozen output byte limit")
    sample_interval = config.get("sample_interval_sec", .1)
    if type(sample_interval) not in (int, float) or not math.isfinite(sample_interval) or not 0 < sample_interval <= 10: errors.append("invalid frozen sampling interval")
    if not isinstance(config.get("expected_measurement"), dict) or not config["expected_measurement"]:
        errors.append("freeze expected_measurement workload counters before running")
    binaries = {"A": a.baseline.resolve(), "B": a.candidate.resolve()}
    for arm, binary in binaries.items():
        if file_hash(binary) != plan.get("binary_sha256", {}).get(arm): errors.append(f"{arm}: binary hash mismatch")
    if errors: p.error("; ".join(errors))
    # Shared QA ownership protocol; no local stale takeover or alternate lock.
    from qa.lease import Lease
    with Lease(a.lease_directory, a.physical_domain, a.expected_plan_hash, a.generation) as lease:
        a.outdir.mkdir(parents=True, exist_ok=False)
        (a.outdir / "plan.json").write_bytes(canonical(plan) + b"\n")
        (a.outdir / "environment.json").write_bytes(canonical(env) + b"\n")
        with (a.outdir / "runs.jsonl").open("x", buffering=1) as output:
            for block_index, block in enumerate(plan["blocks"]):
                for position, arm in enumerate(block["order"]):
                    if digest(read_json(a.plan)) != a.expected_plan_hash or file_hash(binaries[arm]) != plan["binary_sha256"][arm]:
                        raise RuntimeError("frozen input changed; retained partial experiment is invalid")
                    command = [str(binaries[arm]), *config["args"]]
                    run = execute(command, config["timeout_sec"], env, sample_interval=sample_interval,
                                  lease=lease, cpu=config["cpu"], output=a.outdir / f"probe-{block_index}-{position}.log",
                                  output_limit=output_limit)
                    objects, parse_errors = [], []
                    for line in run["stdout"].splitlines():
                        try:
                            value = decode_json(line)
                            objects.append(value)
                        except (ValueError, TypeError, RecursionError):
                            parse_errors.append("unparseable probe output")
                    variant = "baseline" if arm == "A" else "candidate"
                    probe, probe_errors = validate_row({"variant": variant, "pair": 0, "position": position,
                                                       "binary_sha256": plan["binary_sha256"][arm], "measurements": objects,
                                                       "exit_code": run["execution"]["exit_code"]}, 0, {},
                                                      {"name": plan["case_id"], "args": config["args"]})
                    probe_errors.extend(parse_errors)
                    if probe:
                        for field, expected in config["expected_measurement"].items():
                            if probe["measurement"].get(field) != expected: probe_errors.append(f"frozen workload differs: {field}")
                    failures = probe_errors
                    functional = functional_outcome(objects, probe, failures)
                    env_errors = run["environment_errors"]
                    if run["execution"]["status"] != "completed":
                        env_errors.append("probe execution did not complete successfully")
                    if run["execution"]["cleanup"] != "PASS":
                        env_errors.append("owned process cleanup was not verified")
                    row = {"run_id": f"{a.expected_plan_hash}:{block['block_id']}:{position}",
                           "plan_hash": a.expected_plan_hash, "build_manifest_hash": plan["build_manifest_hash"],
                           "environment_hash": plan["environment_hash"], "config_hash": digest(config),
                           "case_id": plan["case_id"], "case_version": plan["case_version"],
                           "block_id": block["block_id"], "position": position, "arm": arm,
                           "binary_sha256": plan["binary_sha256"][arm], "command": command,
                           "functional": {"status": functional, "errors": failures},
                           "validity": {"functional": "VALID" if functional in ("PASS", "FAIL") else "UNKNOWN",
                                        "performance": "INVALID" if env_errors or failures else "VALID", "reasons": env_errors + failures},
                           "execution": run["execution"], "metrics": probe["metrics"] if probe else {},
                           "observations": {"measurements": objects, "environment_samples": run["samples"],
                                            "stdout": run["stdout"], "stderr": run["stderr"], "interruption": run["interruption"],
                                            "process_lifecycle": run["lifecycle"], "output_artifact": run["output"]}}
                    output.write(canonical(row).decode() + "\n")
                    print(json.dumps({"block_id": block["block_id"], "position": position, "arm": arm,
                                      "functional": row["functional"], "validity": row["validity"]}), flush=True)
                    if run["execution"]["cleanup"] != "PASS":
                        lease.quarantine("unexpected surviving benchmark descendants; inspect before reuse")
                        return 2
                    if run["execution"]["status"] != "completed":
                        return 2  # Never splice a replacement arm into this block.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
