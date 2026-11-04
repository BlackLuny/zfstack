#!/usr/bin/env python3
"""Local comparison of PR 94's bench host against main and other stacks.

Production src/ is identical on main and PR 94. The measured stack difference
is the link-emulator queue: fifo versus peer byte DRR, which PR 94 evaluated
and did not promote to a production default. Kernel, smoltcp and zfstack share
that emulator. sing-box is a client TUN proxy, so its numbers are a separate
path. This harness has no lwIP adapter.

One zfbench at a time. Failed runs are kept and are not overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
RAW = OUT / "raw"
RATE = 100.0
# 99% of the 100 Mbps IP-byte bottleneck. Zero leaves the new pacer off.
PACE_BPS = 12_375_000
# down/mixed: total seconds, of which warmup is discarded.
# isolate: measurement window is secs - warmup.
SECS = 12
WARMUP = 4
OPEN_HZ = 25


def qbytes(rtt_ms: float, mult: float) -> int:
    bdp = RATE * 1e6 / 8.0 * (rtt_ms / 1000.0) * mult
    return max(250_000, int(round(bdp)))


def add(rows, **kw):
    reps = kw.pop("reps")
    kw.setdefault("pace", "na")
    kw.setdefault("driver", "shard")
    kw.setdefault("workers", 0)
    for rep in range(reps):
        row = dict(kw)
        row["rep"] = rep
        rows.append(row)


def zf(bin_name, pace, driver="shard", stack="zfstack-cubic"):
    return {"bin": bin_name, "stack": stack, "pace": pace, "driver": driver}


def plan(which: str) -> list[dict]:
    """Retest of updated PR 94: production iface pacing, default off.

    Link discipline stays fifo, so this is not the earlier bench peer-DRR
    comparison. `on` sets the pacer to 99% of the 100 Mbps bottleneck.
    """
    rows: list[dict] = []
    off = zf("pr94", "off")
    on = zf("pr94", "on")
    old = zf("main", "off")
    cross = [
        {"bin": "pr94", "stack": "kernel-cubic", "pace": "na", "driver": "shard"},
        {"bin": "pr94", "stack": "smoltcp-cubic", "pace": "na", "driver": "shard"},
        off,
        on,
    ]

    def put(group, arm, test, rtt, loss, flows, lights, heavy, queue_mult, reps):
        if which == "smoke" and not (group == "fair" and arm["pace"] in ("off", "on") and rtt == 80 and loss == 0 and lights == 3 and heavy == 8 and test == "isolate"):
            if not (which == "smoke" and group == "server" and arm["stack"] == "kernel-cubic" and test == "down" and rtt == 80 and loss == 0 and flows == 1):
                if not (which == "smoke" and group == "client" and test == "down" and arm["stack"] in ("zfstack-client", "singbox-go") and flows == 1):
                    return
        mult = queue_mult
        add(rows, group=group, bin=arm["bin"], stack=arm["stack"], pace=arm["pace"], driver=arm["driver"],
            discipline="fifo" if group != "client" else "none", test=test, rtt=rtt, loss=loss,
            flows=flows if flows is not None else (heavy or 1), lights=lights, heavy=heavy,
            queue=qbytes(rtt, mult) if group != "client" else 0, queue_mult=mult, reps=reps)

    if which in ("all", "fair", "smoke"):
        arms = [old, off, on]
        for rtt in (20, 80, 250):
            for loss, reps in ((0.0, 2), (0.01, 1)):
                for arm in arms:
                    put("fair", arm, "isolate", rtt, loss, None, 3, 8, 1, reps)
        if which != "smoke":
            for lights in (1, 8):
                for arm in (off, on):
                    put("fair", arm, "isolate", 80, 0.0, None, lights, 8, 1, 2)
                    put("fair", arm, "isolate", 20, 0.0, None, lights, 8, 1, 1)
                    put("fair", arm, "isolate", 250, 0.0, None, lights, 8, 1, 1)
                    put("fair", arm, "isolate", 80, 0.01, None, lights, 8, 1, 1)
                put("fair", old, "isolate", 80, 0.0, None, lights, 8, 1, 1)
            for heavy in (1, 32):
                for rtt, loss, reps in ((20, 0.0, 1), (80, 0.0, 2), (250, 0.0, 1), (80, 0.01, 1)):
                    for arm in (off, on):
                        put("fair", arm, "isolate", rtt, loss, None, 3, heavy, 2 if heavy == 1 else 1, reps)
            for loss, reps in ((0.0, 2), (0.01, 1)):
                for arm in (zf("pr94", "off", stack="zfstack-bbr"), zf("pr94", "on", stack="zfstack-bbr")):
                    put("fair", arm, "isolate", 80, loss, None, 3, 8, 1, reps)
            for rtt, reps in ((20, 1), (80, 2), (250, 1)):
                for arm in (zf("pr94", "off", driver="tokio"), zf("pr94", "on", driver="tokio")):
                    put("fair", arm, "isolate", rtt, 0.0, None, 3, 8, 1, reps)

    if which in ("all", "server", "smoke"):
        for rtt in (20, 80, 250):
            for loss, reps in ((0.0, 2 if rtt == 80 else 1), (0.01, 1)):
                for arm in cross:
                    put("server", arm, "down", rtt, loss, 1, None, None, 2, reps)
                    if which != "smoke":
                        put("server", arm, "mixed", rtt, loss, 1, None, None, 1, 2 if loss == 0 else 1)
        if which != "smoke":
            for loss, reps in ((0.0, 2), (0.01, 1)):
                for arm in cross:
                    put("server", arm, "down", 80, loss, 8, None, None, 2, reps)
            for rtt in (20, 250):
                for arm in cross:
                    put("server", arm, "down", rtt, 0.0, 8, None, None, 2, 1)
            bbr = [
                {"bin": "pr94", "stack": "smoltcp-bbr", "pace": "na", "driver": "shard"},
                zf("pr94", "off", stack="zfstack-bbr"),
                zf("pr94", "on", stack="zfstack-bbr"),
            ]
            for loss, reps in ((0.0, 2), (0.01, 1)):
                for arm in bbr:
                    put("server", arm, "down", 80, loss, 1, None, None, 2, reps)
                    if loss == 0.0:
                        put("server", arm, "mixed", 80, loss, 1, None, None, 1, reps)
            put("server", old, "down", 80, 0.0, 1, None, None, 2, 2)
            put("server", old, "mixed", 80, 0.0, 1, None, None, 1, 2)

    if which in ("all", "client", "smoke"):
        for proxy in ("zfstack-client", "singbox-go", "singbox-gvisor"):
            arm = {"bin": "pr94", "stack": proxy, "pace": "na", "driver": "client"}
            native = [("down", 1, 0, 2), ("down", 8, 0, 1), ("up", 1, 0, 1), ("mixed", 1, 0, 2), ("idle", 1, 0, 1)]
            if which == "smoke":
                if proxy == "singbox-gvisor":
                    continue
                native = [("down", 1, 0, 1)]
            for test, flows, workers, reps in native:
                add(rows, group="client", bin="pr94", stack=proxy, pace="na", driver="client", discipline="none",
                    test=test, rtt=0, loss=0.0, flows=flows, lights=None, heavy=None, queue=0, queue_mult=0,
                    workers=workers, reps=reps)
            if which != "smoke":
                add(rows, group="client", bin="pr94", stack=proxy, pace="na", driver="client", discipline="none",
                    test="down", rtt=0, loss=0.0, flows=1, lights=None, heavy=None, queue=0, queue_mult=0,
                    workers=1, reps=1)
        if which != "smoke":
            add(rows, group="client", bin="pr94", stack="direct", pace="na", driver="client", discipline="none",
                test="down", rtt=0, loss=0.0, flows=1, lights=None, heavy=None, queue=0, queue_mult=0, workers=0, reps=1)

    rows.sort(key=lambda r: (r["group"], r["rep"], r["rtt"], r["loss"], r["test"], str(r.get("lights")), str(r.get("heavy")), r["stack"], r["pace"], r["driver"], r["bin"]))
    return rows


def cell_id(c: dict) -> str:
    loss = f"{c['loss']:.2f}"
    lights = "na" if c.get("lights") is None else str(c["lights"])
    heavy = "na" if c.get("heavy") is None else str(c["heavy"])
    workers = c.get("workers", 0)
    pace = c.get("pace", "na")
    driver = c.get("driver", "shard")
    return (
        f"{c['group']}_{c['bin']}_{c['stack']}_{c['discipline']}_{pace}_{driver}_{c['test']}"
        f"_r{c['rtt']:g}_l{loss}_f{c['flows']}_u{lights}_h{heavy}_w{workers}_n{c['rep']}"
    )


def parse_stack(spec: str) -> tuple[str, str | None]:
    if spec.startswith("kernel-"):
        return "kernel", spec.split("-", 1)[1]
    return spec, None


def argv_for(c: dict, binary: Path, json_path: Path) -> list[str]:
    stack, kernel_cc = parse_stack(c["stack"])
    cmd = [str(binary), "--label", cell_id(c), "--json", str(json_path), "--client-cc", "cubic",
           "--connect-timeout-ms", "8000", "--seed", str(1000 + c["rep"] * 17 + int(c["rtt"]) + int(c["loss"] * 1000))]
    if c["group"] == "client":
        cmd += ["--client-proxy", c["stack"], "--test", c["test"], "--proxy-workers", str(c.get("workers", 0))]
        if c["test"] != "idle":
            cmd += ["--flows", str(c["flows"]), "--secs", "10" if c["rtt"] == 0 else "12",
                    "--warmup", "2" if c["rtt"] == 0 else "4", "--rr-interval-ms", "40",
                    "--rr-size", "1024", "--idle-rr-secs", "2"]
        else:
            cmd += ["--conns", "400"]
        if c["stack"] == "zfstack-client":
            cmd += ["--splice", "--vnet-hdr", "--tso", "--driver-thread"]
        if c["stack"].startswith("singbox"):
            cmd += ["--singbox", os.environ["SINGBOX"]]
        return cmd
    cmd += ["--stack", stack, "--test", c["test"], "--rate-mbps", str(RATE), "--rtt-ms", str(c["rtt"]),
            "--loss", str(c["loss"]), "--loss-up", str(c["loss"]), "--queue-bytes", str(c["queue"]),
            "--flows", str(c["flows"] or 1), "--secs", str(SECS), "--warmup", str(WARMUP),
            "--rr-interval-ms", "40", "--listen-pool", "64"]
    if kernel_cc:
        cmd += ["--kernel-cc", kernel_cc]
    if c.get("pace") == "on":
        cmd += ["--iface-egress-rate", str(PACE_BPS)]
    if c.get("driver") == "tokio":
        cmd += ["--tokio-stack"]
    if c["test"] == "mixed":
        cmd += ["--rr-size", "1024", "--idle-rr-secs", "2"]
    elif c["test"] == "isolate":
        cmd += ["--rr-size", "256", "--idle-rr-secs", "0", "--ports", "1", "--lights", str(c["lights"]),
                "--heavy-conns", str(c["heavy"]), "--placement", "same-iface", "--topology", "shared-shard"]
    return cmd


def env_for(c: dict, arrivals: Path | None) -> dict:
    env = os.environ.copy()
    for key in ("ZFBENCH_QUEUE_DISCIPLINE", "ZFBENCH_CONNECT_PARALLEL", "ZFBENCH_CLIENT_NETEM",
                "ZFBENCH_RR_OPEN_HZ", "ZFBENCH_RR_ARRIVALS"):
        env.pop(key, None)
    if c["group"] != "client":
        env["ZFBENCH_QUEUE_DISCIPLINE"] = c["discipline"]
        env["ZFBENCH_CONNECT_PARALLEL"] = "32"
    if c["test"] == "isolate":
        env["ZFBENCH_RR_OPEN_HZ"] = str(OPEN_HZ)
        if arrivals is not None:
            env["ZFBENCH_RR_ARRIVALS"] = str(arrivals)
    if c["group"] == "client" and c["rtt"] > 0:
        env["ZFBENCH_CLIENT_NETEM"] = f"{c['rtt']:g}:{c['loss']}"
    return env


def snapshot_hwm(root: int) -> int:
    children: dict[int, list[int]] = {}
    hwm: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "stat").read_text()
            status = (entry / "status").read_text()
        except OSError:
            continue
        rest = raw.rsplit(")", 1)[-1].split()
        ppid = int(rest[1]) if len(rest) > 1 else 0
        children.setdefault(ppid, []).append(pid)
        for line in status.splitlines():
            if line.startswith("VmHWM:"):
                hwm[pid] = int(line.split()[1])
    peak = 0
    stack = [root]
    seen = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        peak = max(peak, hwm.get(pid, 0))
        stack.extend(children.get(pid, []))
    return peak


def sample_hwm(pid: int, stop: list[bool]) -> int:
    hwm = 0
    while not stop[0]:
        hwm = max(hwm, snapshot_hwm(pid))
        time.sleep(0.25)
    return hwm


def kill_group(pid: int) -> None:
    # sudo is root, so the user-owned runner cannot signal that group itself.
    subprocess.run(["sudo", "-n", "kill", "-9", f"-{pid}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "-n", "kill", "-9", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reap_leftovers() -> None:
    subprocess.run(["sudo", "-n", "pkill", "-9", "sing-box"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sudo", "-n", "pkill", "-9", "-x", "zfbench"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_cell(c: dict, binaries: dict[str, Path]) -> None:
    import threading
    cid = cell_id(c)
    dest = RAW / f"{cid}.json"
    meta_path = RAW / f"{cid}.meta.json"
    if dest.exists() and dest.stat().st_size > 0:
        try:
            json.loads(dest.read_text())
            print(f"skip {cid}", flush=True)
            return
        except json.JSONDecodeError:
            dest.unlink()
    binary = binaries[c["bin"]]
    arrivals = RAW / f"{cid}.arrivals.json"
    err_path = RAW / f"{cid}.err"
    cmd = ["sudo", "-n", "--preserve-env=ZFBENCH_QUEUE_DISCIPLINE,ZFBENCH_CONNECT_PARALLEL,ZFBENCH_CLIENT_NETEM,ZFBENCH_RR_OPEN_HZ,ZFBENCH_RR_ARRIVALS"]
    cmd += argv_for(c, binary, dest)
    env = env_for(c, arrivals if c["test"] == "isolate" else None)
    subprocess.run(["sudo", "-n", str(binary), "--cleanup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reap_leftovers()
    t0 = time.time()
    err_file = open(err_path, "w")
    # stdout is discarded. --json is the result. A new session lets us kill
    # sudo, zfbench and a proxy that would otherwise keep the pipes open.
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=err_file, start_new_session=True)
    stop = [False]
    hwm_box = {}

    def watch():
        hwm_box["kb"] = sample_hwm(proc.pid, stop)

    thread = threading.Thread(target=watch)
    thread.start()
    timeout = 75
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc.pid)
        rc = -9
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    stop[0] = True
    thread.join()
    err_file.close()
    reap_leftovers()
    subprocess.run(["sudo", "-n", str(binary), "--cleanup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wall = time.time() - t0
    stderr = err_path.read_text(errors="replace") if err_path.exists() else ""
    arrival_summary = summarize_arrivals(arrivals)
    if arrivals.exists():
        arrivals.unlink()
    meta = {
        "id": cid,
        "cell": {k: c[k] for k in c},
        "returncode": rc,
        "wall_sec": wall,
        "hwm_kb": hwm_box.get("kb", 0),
        "arrivals": arrival_summary,
        "stderr_tail": (stderr or "")[-1500:],
    }
    meta_path.write_text(json.dumps(meta))
    ok = False
    goodput = None
    if dest.exists():
        try:
            doc = json.loads(dest.read_text())
            ok = bool(doc.get("ok"))
            goodput = (doc.get("results") or {}).get("goodput_mbps_window")
            if goodput is None:
                goodput = ((doc.get("results") or {}).get("heavy") or {}).get("goodput_mbps_window")
        except json.JSONDecodeError:
            pass
    print(f"done {cid} rc={rc} ok={ok} mbps={goodput} wall={wall:.1f}s hwm={hwm_box.get('kb', 0)}KiB", flush=True)


def summarize_arrivals(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        users = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"error": "unreadable"}
    out = []
    for user in users:
        arrivals = user.get("arrivals") or []
        outcomes: dict[str, int] = {}
        lats = []
        for item in arrivals:
            outcome = item.get("outcome") or "unknown"
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
            if outcome == "completed" and item.get("completed_ns") is not None and item.get("planned_ns") is not None:
                lats.append((item["completed_ns"] - item["planned_ns"]) / 1e6)
        timeouts = outcomes.get("timed_out", 0) + outcomes.get("unfinished", 0)
        censored = lats + [3000.0] * timeouts
        out.append({
            "n": len(arrivals),
            "completed": outcomes.get("completed", 0),
            "missed": outcomes.get("missed", 0),
            "timed_out": outcomes.get("timed_out", 0),
            "outcomes": outcomes,
            "mean_ms": statistics.fmean(lats) if lats else None,
            "p50_ms": pct(lats, 50),
            "p99_ms": pct(lats, 99),
            "p99_censored_ms": pct(censored, 99),
        })
    return {"users": out}


def pct(values: list[float], p: float):
    if not values:
        return None
    ordered = sorted(values)
    rank = int((p / 100.0) * len(ordered) + 0.999999)
    idx = min(len(ordered), max(1, rank)) - 1
    return ordered[idx]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def write_meta(binaries: dict[str, Path], which: str, count: int) -> None:
    def sysctl(name: str) -> str:
        got = subprocess.run(["sysctl", "-n", name], text=True, capture_output=True)
        return (got.stdout or got.stderr).strip()

    meta = {
        "which": which,
        "count": count,
        "rate_mbps": RATE,
        "secs": SECS,
        "warmup": WARMUP,
        "open_loop_hz": OPEN_HZ,
        "kernel": os.uname().release,
        "nproc": os.cpu_count(),
        "hostname": socket.gethostname(),
        "rustc": subprocess.run(["rustc", "+1.94.1", "--version"], text=True, capture_output=True).stdout.strip(),
        "bins": {name: {"path": str(path), "sha256": sha256(path)} for name, path in binaries.items()},
        "pr94": subprocess.run(["git", "-C", "/tmp/zf-pr94", "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip(),
        "main": subprocess.run(["git", "-C", "/tmp/zf-main", "rev-parse", "HEAD"], text=True, capture_output=True).stdout.strip(),
        "src_identical": subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", "main", "pr-94", "--", "src"]).returncode == 0,
        "tcp_rmem": sysctl("net.ipv4.tcp_rmem"),
        "tcp_wmem": sysctl("net.ipv4.tcp_wmem"),
        "tcp_cc": sysctl("net.ipv4.tcp_congestion_control"),
        "tcp_available_cc": sysctl("net.ipv4.tcp_available_congestion_control"),
        "singbox": os.environ.get("SINGBOX", ""),
        "note": "Updated PR 94 iface pacing. on = 12375000 B/s (99% of 100 Mbps). Link discipline is fifo. Release profile, not fat LTO. Loss is both directions.",
    }
    (OUT / "run-meta.json").write_text(json.dumps(meta, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--which", default="all", choices=["all", "server", "fair", "host", "client", "smoke"])
    parser.add_argument("--pr94", default="/tmp/target-pr94/release/zfbench")
    parser.add_argument("--main", default="/tmp/target-main/release/zfbench")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    os.environ.setdefault("SINGBOX", "/tmp/singbox-bin/sing-box")
    binaries = {"pr94": Path(args.pr94), "main": Path(args.main)}
    needed = {row["bin"] for row in plan(args.which)}
    for name in needed:
        if not binaries[name].exists():
            print(f"missing {name} binary: {binaries[name]}", file=sys.stderr)
            return 3
    RAW.mkdir(parents=True, exist_ok=True)
    subprocess.run(["sudo", "-n", "sysctl", "-w", "net.ipv4.tcp_rmem=4096 1048576 67108864", "net.ipv4.tcp_wmem=4096 1048576 67108864"], check=False)
    rows = plan(args.which)
    if args.limit:
        rows = rows[: args.limit]
    write_meta(binaries, args.which, len(rows))
    print(f"cells {len(rows)}", flush=True)
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}]", flush=True)
        run_cell(row, binaries)
    subprocess.run([sys.executable, str(OUT / "analyze.py")], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
