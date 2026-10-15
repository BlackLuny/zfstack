#!/usr/bin/env python3
"""Client-mode matrix: zfstack vs latest sing-box TUN stacks vs C lwIP / hev-socks5-tunnel.

usage (root):
  client_matrix.py ZFBENCH SINGBOX OUTDIR REPS GROUP...

optional env:
  ZFBENCH_LWIP, ZFBENCH_HEV, ZFBENCH_SOCKS5  — C binaries from bench/c-stacks/build.sh

groups:
  core flows8 flows32 mtu1500 mtu9000 mixed connect idle short w1 gvisor all
"""
import json, os, resource, statistics as st, subprocess, sys, time

def raise_nofile(want=262144):
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, want))
    except (ValueError, OSError):
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(want, hard), hard))
        except (ValueError, OSError):
            pass


raise_nofile()

B = sys.argv[1]
SB = sys.argv[2]
out = sys.argv[3]
reps = int(sys.argv[4])
groups = sys.argv[5:]
os.makedirs(out, exist_ok=True)

LWIP = os.environ.get("ZFBENCH_LWIP", "")
HEV = os.environ.get("ZFBENCH_HEV", "")
S5 = os.environ.get("ZFBENCH_SOCKS5", "")

ZF = "--client-proxy zfstack-client --splice --vnet-hdr --driver-thread"
ZFT = "--client-proxy zfstack-client --splice --tso --driver-thread"
ZFP = "--client-proxy zfstack-client --splice --driver-thread"
GO = "--client-proxy singbox-go"
GV = "--client-proxy singbox-gvisor"
SY = "--client-proxy singbox-system"
LW = f"--client-proxy lwip --lwip {LWIP}" if LWIP else None
HV = f"--client-proxy hev --hev {HEV} --socks5-override {S5}" if HEV and S5 else None
BULK = "--secs 10 --warmup 3"


def stacks(*pairs):
    return [(n, a) for n, a in pairs if a]


def cells_for(group):
    zf_go = stacks(("zfstack", ZF), ("singbox-go", GO), ("lwip", LW), ("hev", HV))
    zf_go_sys = stacks(("zfstack", ZF), ("singbox-go", GO), ("singbox-system", SY), ("lwip", LW), ("hev", HV))
    if group == "core":
        return [
            (f"{n} {t}", f"{a} {t} {BULK}")
            for t in ["--test down", "--test up"]
            for n, a in zf_go_sys
        ]
    if group == "flows8":
        return [
            (f"{n} {t}", f"{a} {t} {BULK}")
            for t in ["--test down --flows 8", "--test up --flows 8"]
            for n, a in zf_go
        ]
    if group == "flows32":
        return [(f"{n} down32", f"{a} --test down --flows 32 {BULK}") for n, a in zf_go]
    if group == "mtu1500":
        pairs = stacks(("zfstack-tso", ZFT), ("zfstack-plain", ZFP), ("singbox-go", GO), ("lwip", LW), ("hev", HV))
        return [(f"{n} 1500 {t}", f"{a} --tun-mtu 1500 {t} {BULK}") for t in ["--test down", "--test up"] for n, a in pairs]
    if group == "mtu9000":
        pairs = stacks(("zfstack-tso", ZFT), ("singbox-go", GO), ("lwip", LW))
        return [(f"{n} 9000 {t}", f"{a} --tun-mtu 9000 {t} {BULK}") for t in ["--test down", "--test up"] for n, a in pairs]
    if group == "mixed":
        outc = []
        for n, a in zf_go:
            outc.append((f"{n} mixed", f"{a} --test mixed {BULK}"))
            outc.append((f"{n} mixed8", f"{a} --test mixed --flows 8 {BULK}"))
            outc.append((f"{n} mixed-bi", f"{a} --test mixed-bi {BULK}"))
        return outc
    if group == "connect":
        return [(f"{n} connect", f"{a} --test connect --conns 4000 --concurrency 64") for n, a in zf_go_sys]
    if group == "idle":
        return [(f"{n} idle", f"{a} --test idle --conns 4000") for n, a in zf_go]
    if group == "short":
        return [(f"{n} short", f"{a} --test short --conns 2000 --concurrency 64 --short-bytes 65536") for n, a in zf_go]
    if group == "w1":
        return [
            (f"{n} w1 {t}", f"{a} --proxy-workers 1 {t} {BULK}")
            for t in ["--test down", "--test up"]
            for n, a in stacks(("zfstack", ZF), ("singbox-go", GO))
        ]
    if group == "gvisor":
        return [(f"singbox-gvisor {t}", f"{GV} {t} {BULK}") for t in ["--test down", "--test up"]]
    if group == "all":
        return sum((cells_for(g) for g in ["core", "flows8", "flows32", "mtu1500", "mixed", "connect", "idle", "short", "w1"]), [])
    raise SystemExit(f"unknown group {group}")


cells = [c for g in groups for c in cells_for(g)]
res = {}
t0 = time.time()
for r in range(reps):
    for name, args in cells:
        cmd = [B, "--singbox", SB] + args.split()
        p = subprocess.run(cmd, capture_output=True, text=True)
        fn = os.path.join(out, name.replace(" ", "_").replace("--", "") + f"__r{r}.json")
        i = p.stdout.find("{")
        try:
            d = json.loads(p.stdout[i:])
            open(fn, "w").write(p.stdout[i:])
        except Exception:
            open(fn + ".err", "w").write((p.stderr or "") + "\n" + (p.stdout or "")[:4000])
            res.setdefault(name, []).append(None)
            print(f"{name:48s} FAIL r{r} (see {fn}.err)", flush=True)
            continue
        res.setdefault(name, []).append(d)
        ok = "ok" if d.get("ok") else "ERR"
        print(f"{name:48s} {ok} r{r}", flush=True)


def med(v):
    v = [x for x in v if x is not None]
    return st.median(v) if v else None


def f(x, n=2):
    return "-" if x is None else f"{x:.{n}f}"


print("\n=== medians ===")
summary = []
for name, _ in cells:
    ds = [d for d in res.get(name, []) if d]
    ok = sum(1 for d in ds if d.get("ok"))
    n = len(res.get(name, []))
    ds = [d for d in ds if d.get("ok")]
    row = {"name": name, "ok": ok, "n": n}
    if not ds:
        print(f"{name:48s} FAILED ({n} runs)")
        summary.append(row)
        continue
    p = lambda k: med([d["proxy"][k] for d in ds if d.get("proxy") and d["proxy"].get(k) is not None])
    t = ds[0]["config"]["test"]
    row["hwm_mb"] = (p("hwm_kb") or 0) / 1024
    row["cpu_s_per_gb"] = p("cpu_sec_per_GB")
    if t in ("down", "up"):
        g = med([d["results"]["goodput_mbps_window"] / 1000 for d in ds])
        row["gbps"] = g
        print(
            f"{name:48s} {g:6.2f} Gbps  {f(p('cpu_sec_per_GB'),3)} s/GB  util {f(p('cpu_util'))}  "
            f"sys {f(med([d['cpu'].get('system_busy_sec_per_GB') for d in ds]))} s/GB  "
            f"hwm {row['hwm_mb']:5.1f} MB  ok {ok}/{n}"
        )
    elif t == "connect":
        lt = lambda k: med([d["results"]["latency_total"][k] for d in ds if d["results"].get("latency_total")])
        row["cps"] = med([d["results"]["conn_per_sec"] for d in ds])
        row["p50_ms"] = lt("p50_ms")
        row["p99_ms"] = lt("p99_ms")
        print(
            f"{name:48s} cps {f(row['cps'],0)}  succ {med([d['results']['success'] for d in ds])}  "
            f"p50 {f(lt('p50_ms'))} p99 {f(lt('p99_ms'))} ms  cpu {f(p('cpu_window_sec'))} s  "
            f"hwm {row['hwm_mb']:5.1f} MB  ok {ok}/{n}"
        )
    elif t == "idle":
        row["rss_per_conn"] = med([d["results"].get("rss_bytes_per_conn") for d in ds])
        row["established"] = med([d["results"].get("established") for d in ds])
        row["rss_kb_after"] = med([d["results"].get("rss_kb_after") for d in ds]) or p("rss_kb_end")
        held_kb = row["rss_kb_after"] or 0
        print(
            f"{name:48s} rss/conn {f(row['rss_per_conn'],0)} B  idle { (p('rss_kb_idle') or 0)/1024:5.1f} MB  "
            f"held {held_kb/1024:5.1f} MB  est {f(row['established'],0)}  ok {ok}/{n}"
        )
    elif t in ("mixed", "mixed-bi"):
        rl = lambda k: med([d["results"].get("rr_loaded", {}).get(k) for d in ds])
        g = med([d["results"].get("goodput_mbps_window", 0) / 1000 for d in ds])
        row["gbps"] = g
        row["p50_ms"] = rl("p50_ms")
        row["p99_ms"] = rl("p99_ms")
        print(
            f"{name:48s} bulk {g:6.2f} Gbps  RR loaded p50 {f(rl('p50_ms'),3)} p99 {f(rl('p99_ms'),3)} ms  "
            f"delta p99 {f(med([d['results'].get('rr_delta_p99_ms') for d in ds]),3)}  ok {ok}/{n}"
        )
    elif t == "short":
        row["cps"] = med([d["results"]["conn_per_sec"] for d in ds])
        row["gbps"] = med([d["results"].get("goodput_mbps_window", 0) / 1000 for d in ds])
        lt = lambda k: med([d["results"]["latency_total"][k] for d in ds if d["results"].get("latency_total")])
        row["p50_ms"] = lt("p50_ms")
        row["p99_ms"] = lt("p99_ms")
        print(
            f"{name:48s} cps {f(row['cps'],0)}  {f(row['gbps'],2)} Gbps  p50 {f(lt('p50_ms'))} p99 {f(lt('p99_ms'))} ms  "
            f"{f(p('cpu_sec_per_GB'),3)} s/GB  hwm {row['hwm_mb']:5.1f} MB  ok {ok}/{n}"
        )
    else:
        print(f"{name:48s} ok {ok}/{n}")
    summary.append(row)

prev_path = os.path.join(out, "summary.json")
elapsed = time.time() - t0
all_groups = list(groups)
all_reps = reps
if os.path.exists(prev_path):
    try:
        old = json.loads(open(prev_path).read())
        by_name = {r["name"]: r for r in old.get("rows", [])}
        order = [r["name"] for r in old.get("rows", [])]
        for row in summary:
            by_name[row["name"]] = row
            if row["name"] not in order:
                order.append(row["name"])
        summary = [by_name[n] for n in order]
        elapsed = (old.get("elapsed_sec") or 0) + elapsed
        all_groups = list(dict.fromkeys((old.get("groups") or []) + list(groups)))
        all_reps = max(old.get("reps") or 0, reps)
    except Exception:
        pass
open(prev_path, "w").write(
    json.dumps({"elapsed_sec": elapsed, "reps": all_reps, "groups": all_groups, "rows": summary}, indent=2)
)
print(f"wrote {out} in {elapsed:.0f}s")
