#!/usr/bin/env python3
"""One bounded issue-23 TUN case in a fresh, exclusively owned Linux netns.

Install as bench/pareto/issue23_tun_case.py. This runner does not build binaries.
Requires root, iproute2, taskset, explicitly available CPUs, and a hard nofile limit
of at least 4096. Every namespace, process and output belongs to this case.
An outer root operation should additionally use `timeout -k 5s 250s ...`.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import subprocess
import sys
import time
import uuid

EXPERIMENT_ENV = {
    "ZFBENCH_TX_CACHE_TRIM_CHECK_MS", "ZFBENCH_TX_CACHE_TRIM_WARM_MS",
    "ZFBENCH_TX_CACHE_TRIM_COLD_MS", "ZFBENCH_TX_CACHE_TRIM_LARGE",
    "ZFBENCH_TX_CACHE_TRIM_SMALL", "ZFBENCH_TX_CACHE_SAMPLE_MS",
    "ZFBENCH_PACKET_POOL_TRIM_IDLE_MS", "ZFBENCH_PACKET_POOL_TRIM_TARGET",
    "ZFBENCH_GLOBAL_BYTES", "ZFBENCH_PORT_BYTES", "ZFBENCH_PEER_BYTES",
    "ZFBENCH_TUN_PEERS",
}
TX_ENV = sorted(name for name in EXPERIMENT_ENV if name.startswith("ZFBENCH_TX_CACHE_TRIM_"))
PACKET_ENV = ["ZFBENCH_PACKET_POOL_TRIM_IDLE_MS", "ZFBENCH_PACKET_POOL_TRIM_TARGET"]
BUDGET_ENV = ["ZFBENCH_GLOBAL_BYTES", "ZFBENCH_PORT_BYTES", "ZFBENCH_PEER_BYTES"]
TARGET = "198.18.0.2:5201"
UPSTREAM = "127.0.0.1:5201"
DEVICE = "zfcT"
# Three owned children can each need a 2s TERM + 2s KILL wait, followed by
# bounded namespace inspection/deletion. Keep this inside the total case cap.
CLEANUP_RESERVE = 20.0


class CaseError(RuntimeError):
    pass


def exclusive_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")


def identity(path):
    info = os.stat(path)
    return {"device": info.st_dev, "inode": info.st_ino}


def file_hash(path, check=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            if check:
                check()
            digest.update(chunk)
    return digest.hexdigest()


def parse_positive_list(value, name, allow_zero=False):
    try:
        items = [float(item) for item in value.split(",")]
    except ValueError as error:
        raise CaseError(f"invalid {name}") from error
    if not items or any(not (0 <= item <= 240) if allow_zero else not (0 < item <= 240) for item in items):
        raise CaseError(f"invalid {name}")
    return items


def parse_experiment(items):
    values = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or name not in EXPERIMENT_ENV or name in values:
            raise CaseError(f"unknown or duplicate experiment variable: {name}")
        if not re.fullmatch(r"[0-9]+", value) or int(value) > (1 << 64) - 1:
            raise CaseError(f"{name} must be a u64 decimal integer")
        values[name] = value
    for group in (TX_ENV, PACKET_ENV, BUDGET_ENV):
        supplied = sum(name in values for name in group)
        if supplied not in (0, len(group)):
            raise CaseError(f"set every variable in the group or none: {group}")
    if TX_ENV[0] in values:
        check = int(values["ZFBENCH_TX_CACHE_TRIM_CHECK_MS"])
        warm = int(values["ZFBENCH_TX_CACHE_TRIM_WARM_MS"])
        cold = int(values["ZFBENCH_TX_CACHE_TRIM_COLD_MS"])
        if not check or not cold or warm > cold or max(check, warm, cold) > ((1 << 64) - 1) // 1_000_000:
            raise CaseError("invalid TX trim timing")
    if PACKET_ENV[0] in values and not int(values[PACKET_ENV[0]]):
        raise CaseError("PacketPool idle milliseconds must be positive")
    if BUDGET_ENV[0] in values:
        global_bytes, port_bytes, peer_bytes = [int(values[name]) for name in BUDGET_ENV]
        if not 0 < peer_bytes <= port_bytes <= global_bytes:
            raise CaseError("budgets require 0 < peer <= port <= global")
    if "ZFBENCH_TX_CACHE_SAMPLE_MS" in values and int(values["ZFBENCH_TX_CACHE_SAMPLE_MS"]) < 1000:
        raise CaseError("TX sampler interval must be >= 1000 ms")
    if "ZFBENCH_TUN_PEERS" in values and values["ZFBENCH_TUN_PEERS"] != "4":
        raise CaseError("ZFBENCH_TUN_PEERS must be 4")
    return values


def parse_cpus(value):
    if not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", value):
        raise argparse.ArgumentTypeError("CPUs must be a comma-separated list of nonnegative integers")
    values = [int(item) for item in value.split(",")]
    if len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("CPU list contains duplicates")
    return set(values)


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--loader", required=True, type=Path)
    parser.add_argument("--outdir", required=True, type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--source-sha", required=True, help="Declared tested source SHA; binary hash is measured independently")
    parser.add_argument("--pair-id", "--pair", dest="pair", type=int, required=True)
    parser.add_argument("--pair-order", choices=("AB", "BA"), required=True)
    parser.add_argument("--pair-position", type=int, choices=(0, 1), required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--build-flags-json", "--build-flags", required=True,
                        help="JSON object copied from the verified build manifest")
    parser.add_argument("--proxy-cpus", type=parse_cpus, default=parse_cpus("0,1"))
    parser.add_argument("--loader-cpus", type=parse_cpus, default=parse_cpus("2,3,4,5"))
    parser.add_argument("--mtu", type=int, choices=(512, 1500, 65535), default=1500)
    parser.add_argument("--idle-secs", "--idle", default="5")
    parser.add_argument("--idle-count", "--idlecount", type=int, default=0)
    parser.add_argument("--flows", type=int, default=8)
    parser.add_argument("--peer-mode", choices=("single", "fanout", "noisy"), default="single")
    parser.add_argument("--pressure-hold-secs", type=float, default=0.0)
    parser.add_argument("--rr-samples", "--rrsamples", type=int, default=1000)
    parser.add_argument("--rr-sizes", default="64,512,1400,4096")
    parser.add_argument("--rr-interval-ms", type=float, default=0.0)
    parser.add_argument("--bulk-bytes-per-flow", "--bulkbytes", type=int, default=2 << 20)
    parser.add_argument("--mixed-rr-interval-ms", "--mixedRR", type=float)
    parser.add_argument("--low-secs", type=float, default=0.0)
    parser.add_argument("--low-interval-ms", type=float, default=1000.0)
    parser.add_argument("--direction", choices=("down", "up", "both"), default="both")
    parser.add_argument("--settle-secs", type=float, default=1.0)
    parser.add_argument("--socket-timeout-secs", type=float, default=10.0)
    parser.add_argument("--case-timeout-secs", type=float, default=240.0)
    parser.add_argument("--experiment-env", "--env", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--diagnostic", action="store_true", help="This case must not enter performance statistics")
    parser.add_argument("--tcpdump", action="store_true", help="Diagnostic capture only; requires --diagnostic")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        raise CaseError("requires root on the explicitly authorized Linux server")
    if not (40 <= args.case_timeout_secs <= 240):
        raise CaseError("case timeout must be in 40..240 seconds, including cleanup")
    if not 0 <= args.idle_count <= 1000 or not 1 <= args.flows <= 64 or args.rr_samples < 1 or args.bulk_bytes_per_flow < 1:
        raise CaseError("invalid idle count, flows, RR samples or bulk bytes")
    numeric = [args.rr_interval_ms, args.low_secs, args.settle_secs]
    if any(not 0 <= value <= 240000 for value in numeric):
        raise CaseError("invalid nonnegative duration")
    if not 0 < args.socket_timeout_secs <= 30 or not 0 < args.low_interval_ms <= 240000:
        raise CaseError("invalid socket or low-traffic interval")
    if args.mixed_rr_interval_ms is not None and not 1 <= args.mixed_rr_interval_ms <= 240000:
        raise CaseError("mixed RR interval must be >= 1 ms")
    if args.tcpdump and not args.diagnostic:
        raise CaseError("tcpdump is allowed only with --diagnostic, never in a performance case")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.source_sha):
        raise CaseError("source-sha must be a full 40-character commit SHA")
    args.idle_values = parse_positive_list(args.idle_secs, "idle-secs", allow_zero=True)
    try:
        args.size_values = [int(value) for value in args.rr_sizes.split(",")]
    except ValueError as error:
        raise CaseError("invalid RR sizes") from error
    if not args.size_values or any(not 1 <= size <= 65536 for size in args.size_values):
        raise CaseError("RR sizes must be in 1..65536")
    # Reject configurations whose planned sleeps already consume the budget.
    # Workload duration is additionally constrained by the absolute deadline.
    sleeps = sum(args.idle_values) + len(args.idle_values) * args.settle_secs + args.low_secs
    paced_rr = (len(args.idle_values) + 1) * len(args.size_values) * args.rr_samples * args.rr_interval_ms / 1000.0
    if sleeps + paced_rr + CLEANUP_RESERVE + 20 >= args.case_timeout_secs:
        raise CaseError("planned sleeps/RR pacing leave less than 20s for setup and workload; split this case")
    args.experiment = parse_experiment(args.experiment_env)
    if not 0 <= args.pressure_hold_secs <= 5 or (args.pressure_hold_secs and args.peer_mode != "noisy"):
        raise CaseError("pressure hold requires noisy mode and must be in 0..5 seconds")
    if args.peer_mode != "single":
        if not args.diagnostic or args.mixed_rr_interval_ms is None or args.flows < 4:
            raise CaseError("multi-peer requires diagnostic mode, mixed RR and at least four flows")
        if args.experiment.get("ZFBENCH_TUN_PEERS") != "4" or "ZFBENCH_TX_CACHE_SAMPLE_MS" not in args.experiment:
            raise CaseError("multi-peer requires explicit ZFBENCH_TUN_PEERS=4 and sampler")
    elif "ZFBENCH_TUN_PEERS" in args.experiment:
        raise CaseError("ZFBENCH_TUN_PEERS requires multi-peer workload")
    if "ZFBENCH_TX_CACHE_SAMPLE_MS" in args.experiment and not args.diagnostic:
        raise CaseError("TX cache sampler requires --diagnostic; sampling must not enter performance statistics")
    if args.proxy_cpus & args.loader_cpus:
        raise CaseError("proxy and loader CPU sets must be disjoint")
    try:
        args.build_flags = json.loads(args.build_flags_json)
    except json.JSONDecodeError as error:
        raise CaseError("build-flags-json must be valid JSON") from error
    if not isinstance(args.build_flags, dict) or not args.build_flags:
        raise CaseError("build-flags-json must be a nonempty object from the build manifest")
    try:
        json.dumps(args.build_flags, allow_nan=False)
    except ValueError as error:
        raise CaseError("build flags cannot contain NaN or infinity") from error
    if args.pair < 0:
        raise CaseError("pair-id must be nonnegative")
    args.run_id = args.run_id or uuid.uuid4().hex
    return args


class Runner:
    def __init__(self, args):
        self.args = args
        self.started = time.monotonic()
        self.work_deadline = self.started + args.case_timeout_secs - CLEANUP_RESERVE
        self.hard_deadline = self.started + args.case_timeout_secs
        self.ns = "zf23-" + str(os.getpid()) + "-" + uuid.uuid4().hex[:12]
        self.ns_path = Path("/run/netns") / self.ns
        self.ns_identity = None
        self.owns_namespace = False
        self.children = {}
        self.logs = []
        self.commands = []
        self.cleanup_log = []
        self.errors = []
        self.capture_stats = None
        self.result = {"type": "runner_result", "schema_version": 1, "ok": False,
                       "workload_passed": False}
        self.host_netns = identity("/proc/self/ns/net")
        # Deliberately do not inherit credentials, PYTHONPATH, LD_PRELOAD, or any
        # ZFBENCH_* variables. The only experiment values are explicit CLI input.
        self.base_env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                         "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
                         "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        self.proxy_env = dict(self.base_env, **args.experiment)
        self.out = args.outdir.resolve()
        self.out.mkdir(parents=True, exist_ok=False)
        self.events = (self.out / "events.jsonl").open("x", buffering=1)
        self.ip = shutil.which("ip", path=self.base_env["PATH"])
        self.taskset = shutil.which("taskset", path=self.base_env["PATH"])
        self.tcpdump = shutil.which("tcpdump", path=self.base_env["PATH"]) if args.tcpdump else None
        # Treatment knobs differ across variants; workload, budgets and sampler
        # settings must not. case_id therefore excludes only treatment settings.
        controlled_env = {key: value for key, value in args.experiment.items()
                          if key not in TX_ENV and key not in PACKET_ENV}
        self.case_config = {
            "mtu": args.mtu, "idle_secs": args.idle_values, "idle_count": args.idle_count,
            "idle_connections": "closed", "flows": args.flows, "rr_samples": args.rr_samples,
            "rr_sizes": args.size_values, "rr_interval_ms": args.rr_interval_ms,
            "bulk_bytes_per_flow": args.bulk_bytes_per_flow, "direction": args.direction,
            "peer_mode": args.peer_mode, "pressure_hold_secs": args.pressure_hold_secs,
            "mixed_rr_interval_ms": args.mixed_rr_interval_ms, "low_secs": args.low_secs,
            "low_interval_ms": args.low_interval_ms, "settle_secs": args.settle_secs,
            "socket_timeout_secs": args.socket_timeout_secs,
            "proxy_cpus": sorted(args.proxy_cpus), "loader_cpus": sorted(args.loader_cpus),
            "proxy_flags": ["client-profile", "proxy-workers=1", "splice", "vnet-hdr", "driver-thread"],
            "controlled_env": controlled_env, "diagnostic": args.diagnostic, "tcpdump": args.tcpdump,
        }
        case_json = json.dumps(self.case_config, sort_keys=True, separators=(",", ":"), allow_nan=False)
        case_digest = hashlib.sha256(case_json.encode()).hexdigest()
        self.case_id = "issue23-" + case_digest[:20]
        self.meta = {"schema_version": 1, "tool": "issue23_tun_case", "label": args.label,
                     "variant": args.variant, "pair": args.pair, "pair_id": args.pair,
                     "pair_order": args.pair_order, "pair_position": args.pair_position, "run_id": args.run_id,
                     "case_id": self.case_id, "case_config_sha256": case_digest, "case_config": self.case_config,
                     "source_sha": args.source_sha, "declared_source_sha": args.source_sha,
                     "build_flags": args.build_flags, "build_flags_provenance": "caller-provided verified build manifest",
                     "expected_proxy_exit_codes": [-int(signal.SIGTERM)],
                     "proxy_shutdown_policy": "success requires runner SIGTERM after loader success; failure cleanup also terminates owned children; SIGKILL and early exit never pass",
                     "case_timeout_sec": args.case_timeout_secs, "namespace": self.ns,
                     "host_netns": self.host_netns, "mode": "diagnostic" if args.diagnostic else "performance",
                     "eligible_for_performance": not args.diagnostic,
                     "started_unix_ns": time.time_ns(), "base_environment": self.base_env,
                     "runner_argv": list(sys.argv),
                     "proxy_environment": self.proxy_env, "proxy_env": self.proxy_env, "loader_environment": self.base_env,
                     "inherited_zfbench_names_removed": sorted(name for name in os.environ if name.startswith("ZFBENCH_")),
                     "requested": {key: str(value) if isinstance(value, Path) else sorted(value) if isinstance(value, set) else value for key, value in vars(args).items()},
                     "cpu_affinity": {"proxy": sorted(args.proxy_cpus), "loader": sorted(args.loader_cpus)},
                     "pool_accounting_note": "live connections and PacketPool in_use are not free cache; global_cached_bytes excludes baseline PacketPool free buffers"}

    def event(self, kind, **values):
        row = {"type": kind, "monotonic_ns": time.monotonic_ns(), "unix_ns": time.time_ns(), **values}
        self.events.write(json.dumps(row, allow_nan=False) + "\n")

    def check(self):
        if time.monotonic() >= self.work_deadline:
            raise CaseError(f"case work deadline reached; reserving the final {CLEANUP_RESERVE:g}s for cleanup")

    def ensure_namespace(self):
        if not self.owns_namespace or identity(self.ns_path) != self.ns_identity:
            raise CaseError("owned namespace disappeared or its identity changed")

    def command(self, argv, check=True, cleanup=False, timeout=5.0):
        if not cleanup:
            self.check()
            available = self.work_deadline - time.monotonic()
        else:
            available = self.hard_deadline - time.monotonic()
        if available <= 0:
            raise CaseError("no time remains for command")
        argv = [str(value) for value in argv]
        record = {"argv": argv, "env_ref": "base_environment", "start_monotonic_ns": time.monotonic_ns()}
        self.commands.append(record)
        try:
            result = subprocess.run(argv, capture_output=True, text=True, env=self.base_env,
                                    timeout=max(0.05, min(timeout, available)))
            record.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                          end_monotonic_ns=time.monotonic_ns())
        except subprocess.TimeoutExpired:
            record.update(timeout=True, end_monotonic_ns=time.monotonic_ns())
            raise CaseError(f"command timed out: {argv}")
        if check and result.returncode != 0:
            raise CaseError(f"command failed ({result.returncode}): {argv}: {result.stderr.strip()}")
        return result

    def ns_command(self, tail, **kwargs):
        self.ensure_namespace()
        return self.command([self.ip, "-n", self.ns, *tail], **kwargs)

    def ns_exec(self, tail, **kwargs):
        self.ensure_namespace()
        return self.command([self.ip, "netns", "exec", self.ns, *tail], **kwargs)

    def json_ns(self, tail):
        return json.loads(self.ns_command(tail).stdout)

    def preflight(self):
        if not self.ip or not self.taskset or (self.args.tcpdump and not self.tcpdump):
            raise CaseError("missing required ip/taskset/tcpdump; no software is installed by this runner")
        self.binary = self.args.binary.resolve(strict=True)
        self.loader = self.args.loader.resolve(strict=True)
        if not self.binary.is_file() or not os.access(self.binary, os.X_OK) or not self.loader.is_file():
            raise CaseError("binary must be executable and loader must be a regular file")
        if not self.args.proxy_cpus.union(self.args.loader_cpus).issubset(os.sched_getaffinity(0)):
            raise CaseError("requested CPUs are not all allowed; affinity is not silently changed")
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 4096:
            raise CaseError("hard nofile limit is below 4096; refusing to raise the hard limit")
        self.nofile_hard = hard
        machine_bytes = Path("/etc/machine-id").read_bytes().strip()
        if not machine_bytes:
            raise CaseError("machine-id is empty; cannot identify the comparison machine")
        self.meta.update(machine_id="sha256:" + hashlib.sha256(machine_bytes).hexdigest(),
                         machine_id_source="SHA256 of /etc/machine-id; raw identifier is not recorded",
                         host_uname=dict(zip(("sysname", "nodename", "release", "version", "machine"), os.uname())),
                         binary=str(self.binary), loader=str(self.loader), runner=str(Path(__file__).resolve()),
                         binary_sha256=file_hash(self.binary, self.check), loader_sha256=file_hash(self.loader, self.check),
                         runner_sha256=file_hash(__file__, self.check),
                         parent_nofile={"soft": soft, "hard": hard}, child_nofile={"soft": 4096, "hard": hard},
                         tools={"ip": self.ip, "taskset": self.taskset, "tcpdump": self.tcpdump})
        self.meta["proxy_binary_sha256"] = self.meta["binary_sha256"]
        if self.ns_path.exists():
            raise CaseError("unique namespace candidate already exists; refusing to reuse it")
        self.event("preflight", **self.meta)

    def child_preexec(self):
        resource.setrlimit(resource.RLIMIT_NOFILE, (4096, self.nofile_hard))

    def spawn(self, role, argv, env):
        self.ensure_namespace()
        self.check()
        stdout = (self.out / (role + ".stdout")).open("xb")
        stderr = (self.out / (role + ".stderr")).open("xb")
        self.logs.extend([stdout, stderr])
        proc = subprocess.Popen([str(value) for value in argv], stdout=stdout, stderr=stderr, env=env,
                                start_new_session=True, preexec_fn=self.child_preexec)
        self.children[role] = proc
        self.meta[role] = {"argv": [str(value) for value in argv], "environment": env,
                           "pid": proc.pid, "owned_process_group": proc.pid}
        self.event("child_started", role=role, **self.meta[role])
        return proc

    def wait_ready(self, proc, predicate, description, seconds=20.0):
        deadline = min(self.work_deadline, time.monotonic() + seconds)
        while time.monotonic() < deadline:
            self.check()
            if proc.poll() is not None:
                raise CaseError(f"{description}: process exited {proc.returncode}")
            if predicate():
                return
            time.sleep(0.05)
        raise CaseError(f"{description}: readiness timeout")

    def verify_child(self, role, cpus):
        proc = self.children[role]
        self.wait_ready(proc, lambda: os.sched_getaffinity(proc.pid) == cpus, role + " affinity", seconds=3)
        netns = identity(f"/proc/{proc.pid}/ns/net")
        if netns != self.ns_identity:
            raise CaseError(f"{role} is not in the owned namespace")
        limits = Path(f"/proc/{proc.pid}/limits").read_text()
        match = re.search(r"^Max open files\s+(\S+)\s+(\S+)", limits, re.MULTILINE)
        if not match or match.group(1) != "4096":
            raise CaseError(f"{role} soft nofile limit was not set to 4096")
        self.meta[role].update(actual_netns=netns, actual_cpus=sorted(os.sched_getaffinity(proc.pid)),
                               actual_nofile={"soft": match.group(1), "hard": match.group(2)},
                               actual_cmdline=Path(f"/proc/{proc.pid}/cmdline").read_bytes().split(b"\0")[:-1])
        self.meta[role]["actual_cmdline"] = [item.decode(errors="replace") for item in self.meta[role]["actual_cmdline"]]

    def setup(self):
        self.command([self.ip, "netns", "add", self.ns])
        self.owns_namespace = True
        self.ns_identity = identity(self.ns_path)
        self.meta["namespace_identity"] = self.ns_identity
        self.event("namespace_created", namespace=self.ns, identity=self.ns_identity)
        exclusive_json(self.out / "ownership.json", {"namespace": self.ns, "identity": self.ns_identity,
                                                      "creator_pid": os.getpid(), "created_unix_ns": time.time_ns()})
        self.ns_command(["link", "set", "lo", "up"])
        argv = [self.ip, "netns", "exec", self.ns, self.taskset, "-c", ",".join(map(str, sorted(self.args.proxy_cpus))), str(self.binary),
                "--tun-proxy", "--client-profile", "--proxy-upstream", UPSTREAM,
                "--tun-mtu", str(self.args.mtu), "--proxy-workers", "1", "--splice", "--vnet-hdr", "--driver-thread"]
        proxy = self.spawn("proxy", argv, self.proxy_env)
        self.wait_ready(proxy, lambda: "zfbench tun-proxy: ready on zfcT" in (self.out / "proxy.stderr").read_text(errors="replace"), "proxy")
        self.verify_child("proxy", self.args.proxy_cpus)
        link = self.json_ns(["-j", "link", "show", "dev", DEVICE])
        if len(link) != 1 or link[0]["mtu"] != self.args.mtu:
            raise CaseError("TUN MTU does not match requested MTU")
        self.ifindex = link[0]["ifindex"]
        # These commands act ONLY on this newly created namespace and interface.
        mode = self.ns_command(["link", "set", "dev", DEVICE, "addrgenmode", "none"], check=False)
        flush = self.ns_command(["-6", "addr", "flush", "dev", DEVICE, "scope", "link"], check=False)
        ipv6_rows = self.json_ns(["-6", "-j", "addr", "show", "dev", DEVICE])
        ipv6_addresses = [address for row in ipv6_rows for address in row.get("addr_info", []) if address.get("family") == "inet6"]
        disabled = self.ns_exec(["cat", f"/proc/sys/net/ipv6/conf/{DEVICE}/disable_ipv6"], check=False)
        explicitly_disabled = disabled.returncode == 0 and disabled.stdout.strip() == "1"
        if ipv6_addresses:
            raise CaseError("TUN still has IPv6 addresses after IPv4-only setup")
        # Linux destroys inet6_dev (including its sysctl directory) when an
        # ordinary interface's MTU drops below 1280. Accept only that verified
        # state, never a generic failed command or merely an empty address list.
        unsupported_family = (mode.returncode == 2 and mode.stderr.strip() ==
                              "RTNETLINK answers: Address family not supported by protocol")
        missing_disable_file = (disabled.returncode == 1 and disabled.stderr.strip() ==
                                f"cat: /proc/sys/net/ipv6/conf/{DEVICE}/disable_ipv6: No such file or directory")
        inet6_removed = False
        if link[0]["mtu"] < 1280 and unsupported_family and flush.returncode == 0 and missing_disable_file:
            probe = self.ns_exec([sys.executable, "-B", "-c",
                                  "import json,os; from pathlib import Path; "
                                  "print(json.dumps({'conf_entries': sorted(os.listdir('/proc/sys/net/ipv6/conf')), "
                                  "'if_inet6': Path('/proc/net/if_inet6').read_text()}))"])
            evidence = json.loads(probe.stdout)
            entries = [line.split() for line in evidence["if_inet6"].splitlines() if line.strip()]
            if any(len(row) != 6 for row in entries):
                raise CaseError("malformed /proc/net/if_inet6 evidence")
            current_link = self.json_ns(["-j", "link", "show", "dev", DEVICE])
            same_link = (len(current_link) == 1 and current_link[0]["ifindex"] == self.ifindex
                         and current_link[0]["mtu"] == self.args.mtu and current_link[0]["mtu"] < 1280)
            no_inet6_entry = not any(row[-1] == DEVICE or int(row[1], 16) == self.ifindex for row in entries)
            inet6_removed = (same_link and no_inet6_entry and DEVICE not in evidence["conf_entries"]
                             and {"all", "default"}.issubset(evidence["conf_entries"]))
            self.meta["ipv6_mtu_evidence"] = {**evidence, "current_link": current_link,
                                               "same_link": same_link, "no_inet6_entry": no_inet6_entry,
                                               "inet6_removed_verified": inet6_removed}
        exception = None
        if mode.returncode or flush.returncode:
            if (link[0]["mtu"] < 1280 and unsupported_family and flush.returncode == 0
                    and (explicitly_disabled or inet6_removed)):
                exception = ("sub-1280 MTU: inet6_dev absent, verified by sysctl directory and if_inet6"
                             if inet6_removed else "sub-1280 MTU: IPv6 explicitly disabled")
            else:
                raise CaseError("IPv4-only configuration failed without verified sub-1280 IPv6-disabled state")
        if link[0]["mtu"] < 1280 and not (explicitly_disabled or inet6_removed):
            raise CaseError("sub-1280 MTU requires explicit IPv6-disabled or verified inet6_dev-absent state")
        self.ns_command(["-4", "route", "add", "198.18.0.0/15", "dev", DEVICE])
        addresses = self.json_ns(["-4", "-j", "addr", "show", "dev", DEVICE])
        if not any(a.get("local") == "172.19.0.1" and a.get("prefixlen") == 30 for row in addresses for a in row.get("addr_info", [])):
            raise CaseError("unexpected proxy TUN IPv4 address")
        peer_routes = []
        if self.args.peer_mode != "single":
            for peer in range(1, 5):
                source = f"172.19.1.{peer}"
                self.ns_command(["-4", "addr", "add", source + "/32", "dev", "lo"])
                route = self.json_ns(["-4", "-j", "route", "get", "198.18.0.2", "from", source])
                if len(route) != 1 or route[0].get("dev") != DEVICE:
                    raise CaseError("peer source does not route through the owned TUN")
                peer_routes.append({"peer_id": peer, "source": source, "route": route})
        self.meta["peer_routes"] = peer_routes
        route = self.json_ns(["-4", "-j", "route", "get", "198.18.0.2"])
        if len(route) != 1 or route[0].get("dev") != DEVICE:
            raise CaseError("target does not route through the owned TUN")
        self.meta["network"] = {"ifindex": self.ifindex, "link": link, "ipv4_addresses": addresses,
                                "ipv4_routes": self.json_ns(["-4", "-j", "route", "show"]), "target_route": route,
                                "ipv6_addresses": ipv6_rows, "disable_ipv6": disabled.stdout.strip(),
                                "addrgenmode_returncode": mode.returncode, "flush_returncode": flush.returncode,
                                "verified_mtu_exception": exception}
        if self.args.tcpdump:
            capture = self.out / "capture.pcap"
            if capture.exists():
                raise CaseError("capture output already exists")
            dump = self.spawn("tcpdump", [self.ip, "netns", "exec", self.ns, self.tcpdump,
                                         "-i", DEVICE, "-nn", "-s", "0", "-U", "-w", str(capture)], self.base_env)
            self.wait_ready(dump, lambda: "listening on" in (self.out / "tcpdump.stderr").read_text(errors="replace"), "tcpdump", seconds=5)

    def workload(self):
        a = self.args
        loader_argv = [self.ip, "netns", "exec", self.ns, self.taskset, "-c", ",".join(map(str, sorted(self.args.loader_cpus))), sys.executable, "-B", str(self.loader),
                       "--proxy-pid", str(self.children["proxy"].pid), "--device", DEVICE, "--target", TARGET, "--upstream", UPSTREAM,
                       "--mtu", str(a.mtu), "--idle-secs", a.idle_secs, "--idle-connections", "closed", "--idle-count", str(a.idle_count),
                       "--flows", str(a.flows), "--rr-samples", str(a.rr_samples), "--rr-sizes", a.rr_sizes,
                       "--rr-interval-ms", str(a.rr_interval_ms), "--bulk-bytes-per-flow", str(a.bulk_bytes_per_flow),
                       "--peer-mode", a.peer_mode, "--pressure-hold-secs", str(a.pressure_hold_secs),
                       "--direction", a.direction, "--low-secs", str(a.low_secs), "--low-interval-ms", str(a.low_interval_ms),
                       "--settle-secs", str(a.settle_secs), "--timeout-secs", str(a.socket_timeout_secs),
                       "--variant", a.variant, "--pair", str(a.pair), "--source-sha", a.source_sha,
                       "--out", str(self.out / "loader.jsonl")]
        if a.mixed_rr_interval_ms is not None:
            loader_argv += ["--mixed-rr-interval-ms", str(a.mixed_rr_interval_ms)]
        loader = self.spawn("loader", loader_argv, self.base_env)
        self.verify_child("loader", self.args.loader_cpus)
        exclusive_json(self.out / "metadata.json", self.meta)
        while loader.poll() is None:
            self.check()
            if self.children["proxy"].poll() is not None:
                raise CaseError("proxy exited during loader execution")
            if "tcpdump" in self.children and self.children["tcpdump"].poll() is not None:
                raise CaseError("tcpdump exited during diagnostic capture")
            time.sleep(0.1)
        self.result["loader_exit_code"] = loader.returncode
        if loader.returncode != 0:
            raise CaseError(f"loader exited {loader.returncode}")
        self.validate_loader()
        if file_hash(self.binary, self.check) != self.meta["binary_sha256"] or file_hash(self.loader, self.check) != self.meta["loader_sha256"]:
            raise CaseError("binary or loader changed during the case")
        current = self.json_ns(["-j", "link", "show", "dev", DEVICE])
        if len(current) != 1 or current[0]["ifindex"] != self.ifindex:
            raise CaseError("TUN was replaced during the case")
        self.result["workload_passed"] = True

    def validate_loader(self):
        rows = []
        for line in (self.out / "loader.jsonl").read_text().splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise CaseError("non-object loader record")
                rows.append(value)
        log_objects = []
        for name in ("loader.stdout", "loader.stderr"):
            for line in (self.out / name).read_text(errors="replace").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    log_objects.append(value)
        for row in rows + log_objects:
            kind = str(row.get("type", "")).lower()
            if "failure" in kind or "secondary" in kind or row.get("ok") is False:
                raise CaseError(f"loader reported failure/secondary: {row}")
            if any(row.get(key) for key in ("secondary", "secondary_error", "secondary_errors")):
                raise CaseError(f"loader reported secondary error: {row}")
        summaries = [row for row in rows if row.get("type") == "summary"]
        if len(summaries) != 1 or rows[-1].get("type") != "summary" or summaries[0].get("ok") is not True or summaries[0].get("errors"):
            raise CaseError("loader must end in exactly one ok summary with no errors")
        quiet = [row for row in rows if row.get("type") == "quiet"]
        if len(quiet) != len(self.args.idle_values):
            raise CaseError("loader quiet phase count does not match request")
        for row, requested in zip(quiet, self.args.idle_values):
            if abs(float(row.get("requested_sec", -1)) - requested) > 1e-6 or row.get("actual_sec", -1) + 0.05 < requested:
                raise CaseError("loader quiet duration does not match request")
            if row.get("actual_idle_connections") != self.args.idle_count:
                raise CaseError("loader actual idle count differs from request")
        population = [row for row in rows if row.get("type") == "idle_population"]
        if len(population) != 1 or population[0].get("established") != self.args.idle_count:
            raise CaseError("idle population was not fully established")
        liveness = [row for row in rows if row.get("type") == "idle_liveness"]
        if len(liveness) != len(self.args.idle_values) + 2 or any(row.get("verified") != self.args.idle_count for row in liveness):
            raise CaseError("idle liveness verification missing or incomplete")
        if self.args.peer_mode != "single":
            self.validate_peers(rows)
        self.result.update(loader_summary=summaries[0], loader_records=len(rows), quiet_phases=len(quiet))

    def validate_peers(self, rows):
        sources = {f"172.19.1.{peer}" for peer in range(1, 5)}
        bulk = [row for row in rows if row.get("type") == "bulk"]
        directions = 2 if self.args.direction == "both" else 1
        if len(bulk) != (len(self.args.idle_values) + 1) * directions:
            raise CaseError("incomplete multi-peer bulk phases")
        for row in bulk:
            flows = row["per_flow"]
            expected = sources if self.args.peer_mode == "fanout" else {"172.19.1.1"}
            if {flow.get("source") for flow in flows} != expected or len(flows) != self.args.flows:
                raise CaseError("bulk did not use the declared peer identities")
            if any(flow["verified_bytes"] != self.args.bulk_bytes_per_flow for flow in flows):
                raise CaseError("incomplete multi-peer payload verification")
            probes = row.get("mixed_peers", [])
            if len(probes) != 3 or {probe.get("source") for probe in probes} != sources - {"172.19.1.1"}:
                raise CaseError("missing light peer probes")
            if any(probe["fully_overlapping_samples"] < 1 for probe in probes):
                raise CaseError("light peer made no completed progress during bulk")
        samples = [json.loads(line.split(" ", 1)[1])
                   for line in (self.out / "proxy.stderr").read_text().splitlines()
                   if line.startswith("ZFBENCH_PEERS ")]
        seen = set()
        for sample in samples:
            if {peer["peer_id"] for peer in sample["peers"]} != set(range(5)):
                raise CaseError("incomplete peer budget sample")
            for peer in sample["peers"]:
                if not 0 <= peer["physical_bytes"] <= sample["peer_limit_bytes"]:
                    raise CaseError("sampled peer physical ceiling exceeded")
                if peer["peer_id"] == 0 and peer["egress_packets"]:
                    raise CaseError("traffic fell back to reader peer 0")
                if peer["physical_bytes"] > 0 and peer["egress_packets"] > 0:
                    seen.add(peer["peer_id"])
        if seen != {1, 2, 3, 4}:
            raise CaseError("did not observe four real traffic peers with physical leases and egress")
        self.result["peer_validation"] = {"traffic_peers": sorted(seen), "samples": len(samples),
                                          "scope": "sampled hard ceilings and concurrent progress; no minimum bandwidth guarantee"}

    def stop_child(self, role, initial_signal=signal.SIGTERM):
        proc = self.children.get(role)
        if proc is None:
            return
        record = {"role": role, "pid": proc.pid, "signals": [], "was_running": proc.poll() is None}
        try:
            if proc.poll() is None:
                if os.getpgid(proc.pid) != proc.pid:
                    raise CaseError(f"{role} process group changed; refusing unrelated group signal")
                os.killpg(proc.pid, initial_signal)
                record["signals"].append(int(initial_signal))
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if os.getpgid(proc.pid) != proc.pid:
                        raise CaseError(f"{role} group changed before forced stop")
                    os.killpg(proc.pid, signal.SIGKILL)
                    record["signals"].append(int(signal.SIGKILL))
                    proc.wait(timeout=2)
            record["exit_code"] = proc.poll()
        except ProcessLookupError:
            record["exit_code"] = proc.poll()
        except Exception as error:
            record["error"] = str(error)
            self.errors.append(f"cleanup {role}: {error}")
        self.cleanup_log.append(record)

    def cleanup(self):
        # The namespace stays mounted until every owned child has been stopped.
        self.stop_child("loader")
        self.stop_child("tcpdump", signal.SIGINT)
        self.stop_child("proxy")
        if self.args.tcpdump and "tcpdump" in self.children:
            text = (self.out / "tcpdump.stderr").read_text(errors="replace")
            fields = {}
            for key, suffix in [("captured", "packets captured"), ("received_by_filter", "packets received by filter"),
                                ("dropped_by_kernel", "packets dropped by kernel"), ("dropped_by_interface", "packets dropped by interface")]:
                match = re.search(r"(\d+)\s+" + suffix.replace("packets", "packets?"), text)
                fields[key] = int(match.group(1)) if match else None
            fields["zero_kernel_drops"] = fields["dropped_by_kernel"] == 0
            fields["interface_drop_counter_available"] = fields["dropped_by_interface"] is not None
            self.capture_stats = fields
            if fields["captured"] is None or fields["captured"] <= 0 or fields["received_by_filter"] is None:
                self.errors.append("tcpdump capture was empty or complete capture statistics are missing")
            if fields["dropped_by_kernel"] != 0 or fields["dropped_by_interface"] not in (None, 0):
                self.errors.append("tcpdump zero-drop evidence missing or drops observed")
        if self.owns_namespace:
            try:
                self.ensure_namespace()
                remaining = self.command([self.ip, "netns", "pids", self.ns], cleanup=True, timeout=2)
                if remaining.stdout.strip():
                    raise CaseError("processes remain in the owned namespace; refusing to kill untracked PIDs: " + remaining.stdout.strip())
                result = self.command([self.ip, "netns", "del", self.ns], cleanup=True, timeout=3)
                deleted = not self.ns_path.exists()
                self.cleanup_log.append({"namespace": self.ns, "identity": self.ns_identity,
                                         "delete_returncode": result.returncode, "deleted": deleted})
                if not deleted:
                    raise CaseError("owned namespace still exists after delete")
                self.owns_namespace = False
            except Exception as error:
                self.errors.append(f"namespace cleanup: {error}")
                self.cleanup_log.append({"namespace": self.ns, "error": str(error)})
        if identity("/proc/self/ns/net") != self.host_netns:
            self.errors.append("runner host network namespace identity changed")
            self.cleanup_log.append({"error": "runner host network namespace identity changed"})
        for stream in self.logs:
            stream.close()

    def run(self):
        old_handlers = {}
        def interrupted(signum, frame):
            raise CaseError(f"runner interrupted by signal {signum}")
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
            old_handlers[signum] = signal.signal(signum, interrupted)
        signal.setitimer(signal.ITIMER_REAL, max(0.1, self.hard_deadline - time.monotonic()))
        try:
            self.preflight()
            self.setup()
            self.workload()
        except Exception as error:
            self.errors.append(str(error))
            self.event("case_failure", error=str(error))
        finally:
            try:
                self.cleanup()
            except Exception as error:
                self.errors.append(f"cleanup interrupted: {error}")
                self.cleanup_log.append({"error": f"cleanup interrupted: {error}"})
            signal.setitimer(signal.ITIMER_REAL, 0)
            elapsed = time.monotonic() - self.started
            proxy = self.children.get("proxy")
            proxy_exit = proxy.poll() if proxy else None
            proxy_cleanup = next((row for row in self.cleanup_log if row.get("role") == "proxy"), {})
            if self.result.get("workload_passed") and (
                proxy_exit not in self.meta["expected_proxy_exit_codes"]
                or not proxy_cleanup.get("was_running")
                or proxy_cleanup.get("signals") != [int(signal.SIGTERM)]
            ):
                self.errors.append(f"proxy shutdown exit {proxy_exit} did not follow the pre-approved runner SIGTERM path")
            namespace_removed = not self.ns_path.exists()
            children_reaped = all(proc.poll() is not None for proc in self.children.values())
            cleanup_ok = namespace_removed and children_reaped and not self.owns_namespace and not any("error" in row for row in self.cleanup_log)
            if not cleanup_ok:
                self.errors.append("cleanup did not remove the namespace and reap every owned child")
            self.result.update(ok=bool(self.result.get("workload_passed")) and not self.errors,
                               case_id=self.case_id, run_id=self.args.run_id, pair_id=self.args.pair,
                               pair_order=self.args.pair_order, pair_position=self.args.pair_position,
                               machine_id=self.meta.get("machine_id"), build_flags=self.args.build_flags,
                               source_sha=self.args.source_sha, proxy_binary_sha256=self.meta.get("proxy_binary_sha256"),
                               loader_sha256=self.meta.get("loader_sha256"), proxy_env=self.proxy_env,
                               proxy_exit_code=proxy_exit, expected_proxy_exit_codes=self.meta["expected_proxy_exit_codes"],
                               loader_exit_code=self.children["loader"].poll() if "loader" in self.children else None,
                               cleanup_ok=cleanup_ok, namespace_removed=namespace_removed, children_reaped=children_reaped,
                               errors=self.errors, label=self.args.label, variant=self.args.variant,
                               namespace=self.ns, namespace_still_owned=self.owns_namespace,
                               started_unix_ns=self.meta["started_unix_ns"], finished_unix_ns=time.time_ns(),
                               elapsed_sec=elapsed, cleanup=self.cleanup_log, tcpdump=self.capture_stats,
                               eligible_for_performance=not self.args.diagnostic,
                               within_case_budget=elapsed <= self.args.case_timeout_secs)
            if not self.result["within_case_budget"]:
                self.result["ok"] = False
                self.errors.append("case exceeded total budget")
            exclusive_json(self.out / "commands.json", self.commands)
            exclusive_json(self.out / "final_metadata.json", self.meta)
            exclusive_json(self.out / "result.json", self.result)
            self.event("case_finished", ok=self.result["ok"], elapsed_sec=elapsed)
            self.events.close()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
        print(json.dumps({"type": "runner_result", **self.result, "outdir": str(self.out)}))
        return 0 if self.result["ok"] else 1


def main():
    return Runner(arguments()).run()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"ok": False, "type": "runner_failure", "error": str(error)}), file=sys.stderr)
        sys.exit(1)
