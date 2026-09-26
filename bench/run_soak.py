#!/usr/bin/env python3
"""Stability soak on the isolated TUN emulator (no WAN, no real network).

Compares kernel / smoltcp / zfstack for disconnects, stalls, resume-after-pause,
connection-churn failures and RSS growth. Each run is a fresh zfbench process.

Examples:
  sudo bench/run_soak.py --quick --name soak-smoke
  sudo bench/run_soak.py --name soak
  bench/run_soak.py --report-only bench/results/2026-09-26-soak
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BIN = HERE.parent / "target" / "release" / "zfbench"

# Isolated-emulator stability cells. Times are wall seconds of traffic.
# --quick replaces secs/warmup/pause/idle with the values in QUICK_*.
SCENARIOS = [
    dict(name="down-clean-12", test="down", rtt=12.0, loss=0.0, queue=2.0, flows=1, secs=180, warmup=5),
    dict(name="down-loss-12", test="down", rtt=12.0, loss=0.01, queue=2.0, flows=1, secs=120, warmup=5),
    dict(name="down-shallow-80", test="down", rtt=80.0, loss=0.0, queue=0.25, flows=1, secs=120, warmup=5),
    dict(name="down-loss80-8f", test="down", rtt=80.0, loss=0.01, queue=2.0, flows=8, secs=120, warmup=5),
    dict(name="up-clean-12", test="up", rtt=12.0, loss=0.0, queue=2.0, flows=1, secs=120, warmup=5),
    dict(name="up-loss-12", test="up", rtt=12.0, loss=0.01, queue=2.0, flows=1, secs=120, warmup=5),
    dict(name="mixed-12", test="mixed", rtt=12.0, loss=0.0, queue=2.0, flows=1, secs=120, warmup=5),
    dict(name="pause-12", test="down", rtt=12.0, loss=0.0, queue=2.0, flows=1, secs=40, warmup=3,
         pause_after=8, pause_for=8),
    dict(name="idlehold-256", test="down", rtt=12.0, loss=0.0, queue=2.0, flows=1, secs=60, warmup=5,
         idle_hold=256),
    dict(name="churn-12", test="churn", rtt=12.0, loss=0.0, queue=2.0, flows=1, secs=60, warmup=2,
         concurrency=64),
]

QUICK = dict(secs=15, warmup=2, pause_after=4, pause_for=4, idle_hold=32, churn_secs=12)

child = None


def zfstack_commit():
    try:
        out = subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        out = ""
    return out


def cleanup(binpath):
    try:
        subprocess.run([str(binpath), "--cleanup"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass
    subprocess.run(["ip", "netns", "del", "zfbns"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "del", "zfbA"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "del", "zfbwg"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stack_args(spec):
    if spec == "kernel":
        return ["--stack", "kernel"]
    if spec.startswith("kernel-"):
        return ["--stack", "kernel", "--kernel-cc", spec[len("kernel-"):]]
    return ["--stack", spec]


def apply_quick(sc, quick):
    if not quick:
        return dict(sc)
    out = dict(sc)
    if sc["test"] == "churn":
        out["secs"] = QUICK["churn_secs"]
        out["warmup"] = 1
    elif sc["name"].startswith("pause"):
        out["secs"] = 16
        out["warmup"] = 2
        out["pause_after"] = QUICK["pause_after"]
        out["pause_for"] = QUICK["pause_for"]
    elif sc["name"].startswith("idlehold"):
        out["secs"] = QUICK["secs"]
        out["warmup"] = QUICK["warmup"]
        out["idle_hold"] = QUICK["idle_hold"]
    else:
        out["secs"] = QUICK["secs"]
        out["warmup"] = QUICK["warmup"]
    return out


def run_one(a, outdir, sc, spec):
    global child
    key = f"{sc['name']}__{spec}__r0"
    jpath = outdir / f"{key}.json"
    if jpath.exists() and a.resume:
        return "skipped"
    cmd = [str(a.bin), "--test", sc["test"], "--rate-mbps", str(a.rate),
           "--rtt-ms", str(sc["rtt"]),
           "--loss-up" if sc["test"] == "up" else "--loss", str(sc["loss"]),
           "--queue-bdp", str(sc["queue"]), "--flows", str(sc["flows"]),
           "--secs", str(sc["secs"]), "--warmup", str(sc.get("warmup", 3)),
           "--seed", str(a.seed), "--label", key, "--json", str(jpath),
           "--client-cc", a.client_cc]
    if sc.get("pause_after"):
        cmd += ["--pause-after-secs", str(sc["pause_after"]),
                "--pause-for-secs", str(sc["pause_for"])]
    if sc.get("idle_hold"):
        cmd += ["--idle-hold", str(sc["idle_hold"])]
    if sc["test"] == "churn":
        cmd += ["--concurrency", str(sc.get("concurrency", 64)),
                "--connect-timeout-ms", "3000", "--rr-size", "1024"]
    cmd += stack_args(spec)
    cleanup(a.bin)
    timeout = sc["secs"] + 180
    t0 = time.time()
    with open(outdir / f"{key}.log", "w") as log:
        log.write(" ".join(cmd) + "\n")
        log.flush()
        child = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log, start_new_session=True)
        try:
            rc = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            rc = "timeout"
        finally:
            child = None
    cleanup(a.bin)
    status = "ok" if rc == 0 else f"rc={rc}"
    if rc not in (0, 1) or not jpath.exists():
        jpath.with_suffix(".failed").write_text(f"{status}\n")
    return f"{status} ({time.time() - t0:.0f}s)"


def rss_slope_kb_per_min(series, warmup=8):
    s = list(series or [])[warmup:]
    if len(s) < 16:
        return None
    head = statistics.mean(s[:8])
    tail = statistics.mean(s[-8:])
    dt_min = (len(s) - 8) / 60.0
    if dt_min <= 0:
        return None
    return (tail - head) / dt_min


def budget(d):
    st = (d.get("stack_thread") or {}).get("stack_stats") or {}
    return st.get("budget") or {}


def classify(d, spec, sc):
    """Return a list of (kind, detail) findings. Confirmed only after compare."""
    out = []
    if not d.get("ok", False):
        for e in d.get("errors") or ["ok=false"]:
            el = e.lower()
            kind = "disconnect" if any(k in el for k in ("eof", "reset", "read error", "write error",
                                                         "no count reply", "broken pipe")) else "correctness"
            out.append((kind, e))
    r = d.get("results") or {}
    stall = r.get("stall") or {}
    clean = sc["loss"] == 0 and not sc.get("pause_after")
    if sc["test"] in ("down", "up", "mixed") and clean:
        if (stall.get("max_zero_streak") or 0) >= 2:
            out.append(("stall", f"max_zero_streak={stall.get('max_zero_streak')} "
                        f"events={stall.get('stall_events_ge2s')} first={stall.get('first_zero_sec')}"))
    if sc.get("pause_after"):
        resume = r.get("resume_goodput_mbps")
        if resume is None:
            out.append(("stuck-after-pause", "no resume_goodput_mbps"))
        elif resume < 20:
            out.append(("stuck-after-pause", f"resume_goodput_mbps={resume:.2f}"))
    slope = rss_slope_kb_per_min(r.get("rss_kb_per_sec") or [])
    # Single long flow: buffers should plateau. >8 MiB/min after warmup is a leak suspect.
    if sc["test"] in ("down", "up") and not sc.get("idle_hold") and slope is not None and slope > 8192:
        out.append(("rss-growth", f"{slope:.0f} kB/min"))
    if sc["test"] == "churn":
        success = r.get("success") or 0
        fail_n = r.get("failure_n") or 0
        if success == 0:
            out.append(("churn-dead", f"0 success, failures={r.get('failures')}"))
        elif fail_n and success / max(success + fail_n, 1) < 0.95 and spec.startswith("zfstack"):
            out.append(("churn-errors", f"success={success} failures={r.get('failures')}"))
    b = budget(d)
    if b and spec.startswith("zfstack"):
        # After the client has gone, leftover *active* conns (not TIME_WAIT) are a leak.
        if sc["test"] in ("churn", "connect") and (b.get("active_conns") or 0) > 0:
            out.append(("conn-leak", f"active_conns={b.get('active_conns')} tw={b.get('time_wait_reserved')}"))
    if sc["test"] == "mixed":
        loaded = r.get("rr_loaded") or {}
        if (loaded.get("n") or 0) == 0:
            out.append(("rr-dead", "no loaded RR samples"))
        p99 = loaded.get("p99_ms")
        if p99 is not None and p99 > 2000:
            out.append(("rr-stuck", f"RR P99={p99:.0f} ms"))
    return out


def load_results(outdir):
    res = []
    for p in sorted(outdir.glob("*.json")):
        if p.name in ("meta.json", "findings.json"):
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        parts = p.stem.split("__")
        if len(parts) < 2:
            continue
        res.append((parts[0], parts[1], d))
    failed = []
    for p in sorted(outdir.glob("*.failed")):
        parts = p.stem.split("__")
        if len(parts) >= 2:
            failed.append((parts[0], parts[1], p.read_text().strip()))
    return res, failed


def fmt(x, nd=1):
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def make_report(outdir, stacks):
    res, failed = load_results(outdir)
    meta = {}
    if (outdir / "meta.json").exists():
        meta = json.loads((outdir / "meta.json").read_text())
    scenarios = {s["name"]: s for s in (meta.get("scenarios") or SCENARIOS)}
    by = {}
    for name, spec, d in res:
        by.setdefault(name, {})[spec] = d

    findings = []
    for name, spec, d in res:
        sc = scenarios.get(name) or {"test": d.get("config", {}).get("test"), "loss": d.get("config", {}).get("loss", 0)}
        for kind, detail in classify(d, spec, sc):
            findings.append({"scenario": name, "stack": spec, "kind": kind, "detail": detail, "ok": d.get("ok")})
    for name, spec, status in failed:
        findings.append({"scenario": name, "stack": spec, "kind": "crash", "detail": status, "ok": False})

    L = []
    L.append(f"# zfbench soak: {outdir.name}\n")
    L.append("Isolated TUN emulator (10.201.0.0/24). No WAN, no WireGuard, no external network.\n")
    if meta:
        L.append(f"- generated: {dt.datetime.now().isoformat(timespec='seconds')}")
        env = meta.get("env", {})
        for k in ("uname", "nproc", "zfstack_commit", "quick"):
            if k in env or k in meta:
                L.append(f"- {k}: `{env.get(k, meta.get(k))}`")
        L.append(f"- stacks: {', '.join(stacks)}")
        L.append("")

    L.append("## Findings (auto-classified; confirm before filing)\n")
    if not findings:
        L.append("No automatic findings. Compare stacks in the tables below before concluding.\n")
    else:
        L.append("| scenario | stack | kind | detail |")
        L.append("|---|---|---|---|")
        for f in findings:
            L.append(f"| {f['scenario']} | {f['stack']} | {f['kind']} | {f['detail']} |")
        L.append("")

    # Side-by-side tables
    L.append("## Streams (goodput / stall / RSS)\n")
    L.append("| scenario | stack | ok | Mbps | zero-s | max streak | RSS Δ kB | slope kB/min | leftover active |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for name in [s["name"] for s in (meta.get("scenarios") or SCENARIOS)]:
        if name not in by:
            continue
        if (scenarios.get(name) or {}).get("test") in ("churn", "connect"):
            continue
        for spec in stacks:
            d = by[name].get(spec)
            if not d:
                L.append(f"| {name} | {spec} | – | | | | | | |")
                continue
            r = d.get("results") or {}
            stall = r.get("stall") or {}
            mbps = r.get("goodput_mbps_mean")
            if name.startswith("pause"):
                mbps = r.get("resume_goodput_mbps") if r.get("resume_goodput_mbps") is not None else mbps
            slope = rss_slope_kb_per_min(r.get("rss_kb_per_sec") or [])
            b = budget(d)
            L.append(
                f"| {name} | {spec} | {'yes' if d.get('ok') else 'NO'} | {fmt(mbps)} | "
                f"{stall.get('zero_throughput_secs', r.get('zero_throughput_secs', '–'))} | "
                f"{stall.get('max_zero_streak', '–')} | {r.get('rss_kb_delta', (d.get('mem') or {}).get('rss_kb_delta', '–'))} | "
                f"{fmt(slope, 0)} | {b.get('active_conns', '–')} |"
            )
    L.append("")

    L.append("## Churn\n")
    L.append("| scenario | stack | ok | success | failures | conn/s | P99 ms | RSS Δ kB | active / TW |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for name in [s["name"] for s in (meta.get("scenarios") or SCENARIOS) if s.get("test") == "churn"]:
        for spec in stacks:
            d = by.get(name, {}).get(spec)
            if not d:
                continue
            r = d.get("results") or {}
            b = budget(d)
            L.append(
                f"| {name} | {spec} | {'yes' if d.get('ok') else 'NO'} | {r.get('success')} | "
                f"{r.get('failures')} | {fmt(r.get('conn_per_sec'))} | "
                f"{fmt((r.get('latency_total') or {}).get('p99_ms'))} | {r.get('rss_kb_delta')} | "
                f"{b.get('active_conns', '–')} / {b.get('time_wait_reserved', '–')} |"
            )
    L.append("")

    L.append("## Mixed RR\n")
    L.append("| scenario | stack | ok | bulk Mbps | idle P50 | load P50 | load P99 | load P99.9 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for name in [s["name"] for s in (meta.get("scenarios") or SCENARIOS) if s.get("test") == "mixed"]:
        for spec in stacks:
            d = by.get(name, {}).get(spec)
            if not d:
                continue
            r = d.get("results") or {}
            idle = r.get("rr_idle") or {}
            load = r.get("rr_loaded") or {}
            L.append(
                f"| {name} | {spec} | {'yes' if d.get('ok') else 'NO'} | {fmt(r.get('goodput_mbps_mean'))} | "
                f"{fmt(idle.get('p50_ms'))} | {fmt(load.get('p50_ms'))} | "
                f"{fmt(load.get('p99_ms'))} | {fmt(load.get('p999_ms'))} |"
            )
    L.append("")

    if failed:
        L.append("## Crashes / timeouts\n")
        for name, spec, status in failed:
            L.append(f"- `{name}` / {spec}: {status}")
        L.append("")

    (outdir / "report.md").write_text("\n".join(L) + "\n")
    (outdir / "findings.json").write_text(json.dumps(findings, indent=2) + "\n")
    return outdir / "report.md", findings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="soak")
    ap.add_argument("--bin", type=Path, default=DEFAULT_BIN)
    ap.add_argument("--stacks", default="kernel-cubic,smoltcp-cubic,zfstack-cubic,zfstack-bbr")
    ap.add_argument("--rate", type=float, default=200.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--client-cc", default="cubic")
    ap.add_argument("--quick", action="store_true", help="~15 s cells, for harness smoke")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--outdir", type=Path)
    ap.add_argument("--report-only", type=Path)
    ap.add_argument("--only", default="", help="comma-separated scenario names")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    a.stacks = [s for s in a.stacks.split(",") if s]

    if a.report_only:
        path, _ = make_report(a.report_only, a.stacks)
        print(path)
        return

    scenarios = [apply_quick(s, a.quick) for s in SCENARIOS]
    if a.only:
        want = set(a.only.split(","))
        scenarios = [s for s in scenarios if s["name"] in want]
        if not scenarios:
            sys.exit(f"no scenarios match --only {a.only}")

    runs = [(s, spec) for s in scenarios for spec in a.stacks]
    est = sum(s["secs"] + 4 for s, _ in runs)
    print(f"{len(runs)} soak runs, estimated {est / 60:.0f} min (isolated TUN emulator)", file=sys.stderr)
    if a.dry_run:
        for s, spec in runs:
            print(s["name"], spec, f"{s['secs']}s")
        return
    if os.geteuid() != 0:
        sys.exit("must run as root (TUN / netns)")
    if not a.bin.exists():
        sys.exit(f"{a.bin} not found; build with: cargo build --release -p zfbench")

    outdir = a.outdir or HERE / "results" / f"{dt.date.today().isoformat()}-{a.name}"
    outdir.mkdir(parents=True, exist_ok=True)
    env = {
        "uname": subprocess.run(["uname", "-a"], capture_output=True, text=True).stdout.strip(),
        "nproc": os.cpu_count(),
        "zfstack_commit": zfstack_commit(),
        "quick": a.quick,
        "link": "isolated TUN emulator 10.201.0.0/24 (no WAN)",
    }
    meta = {
        "args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()},
        "stacks": a.stacks,
        "scenarios": scenarios,
        "env": env,
        "started": dt.datetime.now().isoformat(timespec="seconds"),
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))

    def on_sig(signum, frame):
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
        cleanup(a.bin)
        print("\ninterrupted; writing partial report", file=sys.stderr)
        print(make_report(outdir, a.stacks)[0], file=sys.stderr)
        sys.exit(130)

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    try:
        for i, (s, spec) in enumerate(runs):
            st = run_one(a, outdir, s, spec)
            print(f"[{i + 1}/{len(runs)}] {s['name']} {spec}: {st}", file=sys.stderr)
    finally:
        cleanup(a.bin)
    meta["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    path, findings = make_report(outdir, a.stacks)
    print(path)
    print(f"{len(findings)} automatic findings", file=sys.stderr)


if __name__ == "__main__":
    main()
