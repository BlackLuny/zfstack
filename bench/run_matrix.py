#!/usr/bin/env python3
"""Run a zfbench matrix and produce a Markdown report.

Each run is a fresh `zfbench` process; TUN/netns leftovers are removed before
every run and on exit (including Ctrl-C). Raw JSON goes to
bench/results/<date>-<name>/, the report to report.md in the same directory.

Stack specs:
  kernel            kernel server, system default congestion control
  kernel-<cc>       kernel server with TCP_CONGESTION=<cc> (e.g. kernel-cubic)
  smoltcp-cubic | smoltcp-bbr | smoltcp-reno | zfstack

Examples:
  sudo ./run_matrix.py --name s0-e1                  # default matrix
  sudo ./run_matrix.py --quick --name smoke          # 10 s runs, 1 rep, small matrix
  sudo ./run_matrix.py --tests down --rtts 80 --losses 0 --queues 0.25 --flows 1 \
        --stacks kernel-cubic,smoltcp-bbr --reps 5 --name shallow80
  ./run_matrix.py --report-only results/2026-09-24-s0-e1
"""

import argparse
import datetime as dt
import itertools
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

child = None


def cleanup(binpath):
    try:
        subprocess.run([str(binpath), "--cleanup"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass
    # belt and braces, in case the binary itself is broken
    subprocess.run(["ip", "netns", "del", "zfbns"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ip", "link", "del", "zfbA"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fmt_num(x):
    if x is None:
        return "–"
    if float(x).is_integer() and abs(x) < 1e6:
        return f"{int(x)}"
    if abs(x) >= 100:
        return f"{x:.0f}"
    if abs(x) >= 10:
        return f"{x:.1f}"
    return f"{x:.2f}"


def parse_list(s, conv=str):
    return [conv(x) for x in s.split(",") if x.strip() != ""]


def stack_args(spec, args):
    if spec == "kernel":
        return ["--stack", "kernel"]
    if spec.startswith("kernel-"):
        return ["--stack", "kernel", "--kernel-cc", spec[len("kernel-"):]]
    return ["--stack", spec]


def build_runs(a):
    """List of (cell dict, stack spec)."""
    cells = []
    for test in a.tests:
        if test in ("down", "up"):
            for rtt, loss, q, fl in itertools.product(a.rtts, a.losses, a.queues, a.flows):
                cells.append(dict(test=test, rtt=rtt, loss=loss, queue=q, flows=fl))
        elif test == "mixed":
            for rtt, q in itertools.product(a.mixed_rtts, a.mixed_queues):
                cells.append(dict(test="mixed", rtt=rtt, loss=0.0, queue=q, flows=1))
        elif test == "connect":
            cells.append(dict(test="connect", rtt=a.connect_rtt, loss=0.0, queue=a.queues[0], flows=1))
        else:
            sys.exit(f"unknown test {test}")
    return cells


def cell_key(c):
    return f"{c['test']}_rtt{c['rtt']:g}_loss{c['loss']:g}_q{c['queue']:g}_f{c['flows']}"


def run_one(a, outdir, cell, spec, rep):
    global child
    key = f"{cell_key(cell)}__{spec}__r{rep}"
    jpath = outdir / f"{key}.json"
    if jpath.exists() and a.resume:
        return "skipped"
    cmd = [str(a.bin), "--test", cell["test"], "--rate-mbps", str(a.rate),
           "--rtt-ms", str(cell["rtt"]),
           # random loss goes on the data direction: server->client for down/mixed,
           # client->server for up
           "--loss-up" if cell["test"] == "up" else "--loss", str(cell["loss"]),
           "--queue-bdp", str(cell["queue"]), "--flows", str(cell["flows"]),
           "--secs", str(a.secs), "--warmup", str(a.warmup),
           "--conns", str(a.conns), "--concurrency", str(a.concurrency),
           "--seed", str(a.seed + rep), "--label", key, "--json", str(jpath)]
    if a.client_cc:
        cmd += ["--client-cc", a.client_cc]
    cmd += stack_args(spec, a)
    cmd += a.extra
    cleanup(a.bin)
    timeout = a.secs + 120 if cell["test"] != "connect" else 300
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
    # rc=1 = correctness failure: the JSON exists and is flagged ok=false.
    if rc not in (0, 1) or not jpath.exists():
        # record the crash/timeout so the report can show it
        jpath.with_suffix(".failed").write_text(f"{status}\n")
    return f"{status} ({time.time() - t0:.0f}s)"


# ---------------------------------------------------------------- report

def load_results(outdir):
    res = []
    for p in sorted(outdir.glob("*.json")):
        if p.name == "meta.json":
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        key, spec, rep = p.stem.split("__")
        res.append((key, spec, d))
    failed = []
    for p in sorted(outdir.glob("*.failed")):
        key, spec, rep = p.stem.split("__")
        failed.append((key, spec))
    return res, failed


def med_range(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None, None
    return statistics.median(vals), min(vals), max(vals)


def cell_str(vals, n_fail=0):
    m, lo, hi = med_range(vals)
    if m is None:
        s = "–"
    elif len(vals) == 1:
        s = fmt_num(m)
    else:
        s = f"{fmt_num(m)} [{fmt_num(lo)}–{fmt_num(hi)}]"
    if n_fail:
        s += f" ⚠{n_fail}"
    return s


def ratio_str(vals, base_vals):
    m, _, _ = med_range(vals)
    b, _, _ = med_range(base_vals)
    if m is None or not b:
        return "–"
    return f"{m / b:.2f}"


METRICS = {
    "down": [
        ("goodput Mbps", lambda d: d["results"].get("goodput_mbps_mean")),
        ("zero-tput s", lambda d: d["results"].get("zero_throughput_secs")),
        ("bottleneck drops", lambda d: d["link"]["down"]["bottleneck_drops"]),
        ("stack CPU s/GB", lambda d: d["cpu"].get("cpu_sec_per_effective_GB")),
    ],
    "up": [
        ("goodput Mbps", lambda d: d["results"].get("goodput_mbps_mean")),
        ("zero-tput s", lambda d: d["results"].get("zero_throughput_secs")),
        ("bottleneck drops", lambda d: d["link"]["up"]["bottleneck_drops"]),
        ("stack CPU s/GB", lambda d: d["cpu"].get("cpu_sec_per_effective_GB")),
    ],
    "mixed": [
        ("RR P50 ms", lambda d: (d["results"].get("rr_loaded") or {}).get("p50_ms")),
        ("RR P99 ms", lambda d: (d["results"].get("rr_loaded") or {}).get("p99_ms")),
        ("RR P99.9 ms", lambda d: (d["results"].get("rr_loaded") or {}).get("p999_ms")),
        ("idle RR P50 ms", lambda d: (d["results"].get("rr_idle") or {}).get("p50_ms")),
        ("goodput Mbps", lambda d: d["results"].get("goodput_mbps_mean")),
    ],
    "connect": [
        ("success", lambda d: d["results"].get("success")),
        ("refused", lambda d: d["results"].get("failures", {}).get("refused", 0)),
        ("timeout", lambda d: d["results"].get("failures", {}).get("timeout", 0)),
        ("reset", lambda d: d["results"].get("failures", {}).get("reset", 0)),
        ("P50 ms", lambda d: d["results"].get("latency_total", {}).get("p50_ms")),
        ("P99 ms", lambda d: d["results"].get("latency_total", {}).get("p99_ms")),
    ],
}
# metric used for the ratio column (higher = better for goodput/success, lower for latency)
RATIO_METRIC = {"down": 0, "up": 0, "mixed": 1, "connect": 0}


def make_report(outdir, baseline):
    res, failed = load_results(outdir)
    meta = {}
    if (outdir / "meta.json").exists():
        meta = json.loads((outdir / "meta.json").read_text())
    stacks = []
    for _, spec, _ in res:
        if spec not in stacks:
            stacks.append(spec)
    order = meta.get("stacks") or stacks
    stacks = [s for s in order if s in stacks] + [s for s in stacks if s not in order]
    by = {}
    for key, spec, d in res:
        by.setdefault(key, {}).setdefault(spec, []).append(d)
    fails = {}
    for key, spec in failed:
        fails[(key, spec)] = fails.get((key, spec), 0) + 1
    bad = {}
    for key, spec, d in res:
        if not d.get("ok", False):
            bad[(key, spec)] = bad.get((key, spec), 0) + 1

    L = []
    L.append(f"# zfbench report: {outdir.name}\n")
    if meta:
        L.append(f"- generated: {dt.datetime.now().isoformat(timespec='seconds')}")
        L.append(f"- matrix: `{json.dumps(meta.get('args', {}), ensure_ascii=False)}`")
        env = meta.get("env", {})
        for k, v in env.items():
            L.append(f"- {k}: `{v}`")
    L.append("")
    L.append("Cells show **median [min–max]** over repetitions. ⚠n = n runs crashed/timed out or "
             "failed a correctness check (see the .log / JSON `errors`). "
             f"Ratio columns = median(stack) / median({baseline}).\n")
    L.append("Goodput = bytes delivered to the receiving application, mean of the per-second "
             "samples after warmup. Rates are IP-level on the emulated bottleneck (MTU 1420, "
             "so the TCP payload ceiling at 200 Mbps is ~192-194 Mbps).\n")

    tests = []
    for key in by:
        t = key.split("_")[0]
        if t not in tests:
            tests.append(t)
    for t in ["down", "up", "mixed", "connect"]:
        if t not in tests:
            continue
        L.append(f"## {t}\n")
        keys = [k for k in by if k.split("_")[0] == t]

        def sort_key(k):
            c = next(iter(by[k].values()))[0]["config"]
            return (c["rtt_ms"], max(c["loss"], c.get("loss_up", 0)), -c["queue_bdp"], c["flows"])
        keys.sort(key=sort_key)
        for mi, (mname, mf) in enumerate(METRICS[t]):
            L.append(f"### {t}: {mname}\n")
            hdr = ["RTT ms", "loss", "queue k", "flows"] + stacks
            ratio_stacks = [s for s in stacks if s != baseline] if baseline in stacks else []
            if mi == RATIO_METRIC[t]:
                hdr += [f"{s}/{baseline}" for s in ratio_stacks]
            L.append("| " + " | ".join(hdr) + " |")
            L.append("|" + "---|" * len(hdr))
            for k in keys:
                any_d = next(iter(by[k].values()))[0]
                c = any_d["config"]
                row = [f"{c['rtt_ms']:g}", f"{max(c['loss'], c.get('loss_up', 0)) * 100:g}%", f"{c['queue_bdp']:g}", str(c["flows"])]
                vals = {}
                for s in stacks:
                    ds = by[k].get(s, [])
                    v = []
                    for d in ds:
                        try:
                            v.append(mf(d))
                        except (KeyError, TypeError):
                            v.append(None)
                    vals[s] = v
                    nf = fails.get((k, s), 0) + bad.get((k, s), 0)
                    row.append(cell_str(v, nf) if ds or nf else "")
                if mi == RATIO_METRIC[t]:
                    for s in ratio_stacks:
                        row.append(ratio_str(vals[s], vals.get(baseline, [])))
                L.append("| " + " | ".join(row) + " |")
            L.append("")
    # Emulator health
    L.append("## Emulator health\n")
    lates = [d["link"]["down"]["timing_late_mean_us"] for _, _, d in res]
    lmax = [d["link"]["down"]["timing_late_max_us"] for _, _, d in res]
    tund = sum((d["link"]["tun_a"] or {}).get("qdisc_drops", 0) + (d["link"]["tun_a"] or {}).get("tx_dropped", 0)
               for _, _, d in res)
    sn = sum(d["link"].get("softnet_backlog_drops", 0) for _, _, d in res)
    if lates:
        L.append(f"- down-link delivery lateness: mean of means {statistics.mean(lates):.1f} µs, "
                 f"worst max {max(lmax):.0f} µs")
    L.append(f"- drops outside the emulator (TUN qdisc/tx_dropped): {tund}; softnet backlog drops: {sn}")
    errs = [(k, s, d["errors"]) for k, s, d in res if d.get("errors")]
    if errs or failed:
        L.append("\n## Failures\n")
        for k, s, e in errs:
            L.append(f"- `{k}` / {s}: {'; '.join(e[:3])}")
        for k, s in failed:
            L.append(f"- `{k}` / {s}: crashed or timed out (see .log)")
    (outdir / "report.md").write_text("\n".join(L) + "\n")
    return outdir / "report.md"


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="matrix")
    ap.add_argument("--bin", type=Path, default=DEFAULT_BIN)
    ap.add_argument("--stacks", default="kernel-cubic,smoltcp-cubic,smoltcp-bbr")
    ap.add_argument("--baseline", default="smoltcp-cubic", help="stack used as ratio denominator")
    ap.add_argument("--tests", default="down,up,mixed,connect")
    ap.add_argument("--rate", type=float, default=200.0, help="bottleneck Mbit/s")
    ap.add_argument("--rtts", default="1,12,80")
    ap.add_argument("--losses", default="0,0.01", help="probabilities (0.01 = 1%%)")
    ap.add_argument("--queues", default="2,0.25", help="queue sizes in BDP multiples (k)")
    ap.add_argument("--flows", default="1,8")
    ap.add_argument("--mixed-rtts", default="12,80")
    ap.add_argument("--mixed-queues", default="2,0.25")
    ap.add_argument("--connect-rtt", type=float, default=12.0)
    ap.add_argument("--conns", type=int, default=2000)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--secs", type=int, default=30)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--client-cc", default="cubic",
                    help="TCP_CONGESTION of the kernel client (the sender in `up`); '' = system default")
    ap.add_argument("--quick", action="store_true", help="secs=10, warmup=2, reps=1, rtts=12,80, flows=1")
    ap.add_argument("--resume", action="store_true", help="skip runs whose JSON already exists")
    ap.add_argument("--outdir", type=Path, help="explicit output directory (default results/<date>-<name>)")
    ap.add_argument("--report-only", type=Path, metavar="DIR")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("extra", nargs="*", help="extra zfbench args after --")
    a = ap.parse_args()

    if a.report_only:
        print(make_report(a.report_only, a.baseline))
        return
    if a.quick:
        a.secs, a.warmup, a.reps, a.rtts, a.flows = 10, 2, 1, "12,80", "1"
    a.stacks = parse_list(a.stacks)
    a.tests = parse_list(a.tests)
    a.rtts = parse_list(a.rtts, float)
    a.losses = parse_list(a.losses, float)
    a.queues = parse_list(a.queues, float)
    a.flows = parse_list(a.flows, int)
    a.mixed_rtts = parse_list(a.mixed_rtts, float)
    a.mixed_queues = parse_list(a.mixed_queues, float)

    cells = build_runs(a)
    runs = [(rep, c, s) for rep in range(a.reps) for c in cells for s in a.stacks]
    est = sum((a.secs + 3) if c["test"] != "mixed" else (a.secs + 6) for _, c, _ in runs if c["test"] != "connect")
    est += sum(15 for _, c, _ in runs if c["test"] == "connect")
    print(f"{len(runs)} runs ({len(cells)} cells x {len(a.stacks)} stacks x {a.reps} reps), "
          f"estimated {est / 60:.0f} min", file=sys.stderr)
    if a.dry_run:
        for rep, c, s in runs:
            print(rep, cell_key(c), s)
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
        "zfstack_commit": subprocess.run(["git", "-C", str(HERE.parent), "rev-parse", "HEAD"],
                                         capture_output=True, text=True).stdout.strip(),
        "smoltcp_rev": "8014f8b21e12faf89b3b453ceea32027344721af",
    }
    for k in ("net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem", "net.ipv4.tcp_congestion_control"):
        try:
            env[k] = " ".join(Path("/proc/sys/" + k.replace(".", "/")).read_text().split())
        except OSError:
            pass
    meta = {"args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(a).items()},
            "stacks": a.stacks, "env": env, "started": dt.datetime.now().isoformat(timespec="seconds")}
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))

    def on_sig(signum, frame):
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
        cleanup(a.bin)
        print("\ninterrupted; writing partial report", file=sys.stderr)
        print(make_report(outdir, a.baseline), file=sys.stderr)
        sys.exit(130)

    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    try:
        for i, (rep, c, s) in enumerate(runs):
            st = run_one(a, outdir, c, s, rep)
            print(f"[{i + 1}/{len(runs)}] {cell_key(c)} {s} r{rep}: {st}", file=sys.stderr)
    finally:
        cleanup(a.bin)
    meta["finished"] = dt.datetime.now().isoformat(timespec="seconds")
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2))
    print(make_report(outdir, a.baseline))


if __name__ == "__main__":
    main()
