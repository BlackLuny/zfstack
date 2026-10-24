#!/usr/bin/env python3
"""Reduce raw zfbench JSON into summary.json and TABLES.md."""

from __future__ import annotations

import json
import statistics
from pathlib import Path

OUT = Path(__file__).resolve().parent
RAW = OUT / "raw"


def pct(values, p):
    values = [v for v in values if v is not None]
    if not values:
        return None
    ordered = sorted(values)
    rank = int((p / 100.0) * len(ordered) + 0.999999)
    idx = min(len(ordered), max(1, rank)) - 1
    return ordered[idx]


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def jain(values):
    values = [v for v in values if v is not None and v >= 0]
    if len(values) < 2:
        return None
    total = sum(values)
    if total <= 0:
        return None
    return (total * total) / (len(values) * sum(v * v for v in values))


def g(doc, *path):
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def per_gb(cpu_sec, nbytes):
    if cpu_sec is None or not nbytes:
        return None
    return cpu_sec / (nbytes / 1e9)


def flow_shares(doc):
    flows = g(doc, "results", "per_flow") or []
    warmup = g(doc, "config", "warmup") or 0
    shares = []
    for flow in flows:
        series = flow.get("mbps_per_sec") or []
        shares.append(sum(series[int(warmup):]))
    return shares


def metrics(meta: dict, doc: dict) -> dict:
    results = doc.get("results") or {}
    cpu = doc.get("cpu") or {}
    nbytes = cpu.get("window_effective_bytes") or 0
    link_cpu = None
    if cpu.get("link_up_window_sec") is not None or cpu.get("link_down_window_sec") is not None:
        link_cpu = (cpu.get("link_up_window_sec") or 0) + (cpu.get("link_down_window_sec") or 0)
    users = (meta.get("arrivals") or {}).get("users") or []
    user_means = [u.get("mean_ms") for u in users]
    user_p99 = [u.get("p99_ms") for u in users]
    user_cens = [u.get("p99_censored_ms") for u in users]
    completed = sum(u.get("completed") or 0 for u in users)
    planned = sum(u.get("n") or 0 for u in users)
    misses = sum(u.get("missed") or 0 for u in users)
    timeouts = sum(u.get("timed_out") or 0 for u in users)
    shares = flow_shares(doc)
    positive = [s for s in user_means if s]
    row = {
        "id": meta["id"],
        "cell": meta["cell"],
        "ok": bool(doc.get("ok")),
        "returncode": meta.get("returncode"),
        "errors": doc.get("errors") or [],
        "goodput_mbps": results.get("goodput_mbps_window")
        or g(results, "heavy", "goodput_mbps_window"),
        "stack_cpu_s_per_gb": cpu.get("cpu_sec_per_effective_GB"),
        "process_cpu_s_per_gb": per_gb(cpu.get("process_window_sec"), nbytes),
        "link_cpu_s_per_gb": per_gb(link_cpu, nbytes),
        "kernel_threads_s_per_gb": per_gb(cpu.get("kernel_server_threads_total_sec"), nbytes),
        "proxy_cpu_s_per_gb": g(doc, "proxy", "cpu_sec_per_GB"),
        "rss_mib": (meta.get("hwm_kb") or 0) / 1024,
        "proxy_hwm_mib": ((g(doc, "proxy", "hwm_kb") or 0) / 1024) if g(doc, "proxy", "hwm_kb") else None,
        "proxy_rss_mib": ((g(doc, "proxy", "rss_kb_end") or 0) / 1024) if g(doc, "proxy", "rss_kb_end") else None,
        "rss_bytes_per_conn": results.get("rss_bytes_per_conn"),
        "idle_conns": results.get("conns") if meta["cell"]["test"] == "idle" else None,
        "rr_p50_ms": g(results, "rr_loaded", "p50_ms"),
        "rr_p99_ms": g(results, "rr_loaded", "p99_ms"),
        "rr_mean_ms": g(results, "rr_loaded", "mean_ms"),
        "rr_n": g(results, "rr_loaded", "n"),
        "idle_p50_ms": g(results, "rr_idle", "p50_ms"),
        "idle_p99_ms": g(results, "rr_idle", "p99_ms"),
        "user_mean_ms": mean(user_means),
        "user_worst_p99_ms": max(user_p99) if any(v is not None for v in user_p99) else None,
        "user_best_mean_ms": min(positive) if positive else None,
        "user_worst_mean_ms": max(positive) if positive else None,
        "latency_jain": jain(user_means),
        "user_p99_censored_worst_ms": max(user_cens) if any(v is not None for v in user_cens) else None,
        "arrivals_planned": planned,
        "arrivals_completed": completed,
        "generator_misses": misses,
        "timeouts": timeouts,
        "flow_jain": jain(shares),
        "flow_min_share": (min(shares) / sum(shares)) if shares and sum(shares) > 0 else None,
        "discipline": g(doc, "link", "down", "queue_discipline"),
        "queue_limit": g(doc, "link", "down", "params", "queue_limit_bytes"),
        "late_mean_us": g(doc, "link", "down", "timing_late_mean_us"),
        "late_max_us": g(doc, "link", "down", "timing_late_max_us"),
        "random_drops": g(doc, "link", "down", "random_drops"),
        "bottleneck_drops": g(doc, "link", "down", "bottleneck_drops"),
    }
    if row["user_best_mean_ms"] and row["user_worst_mean_ms"]:
        row["latency_max_over_min"] = row["user_worst_mean_ms"] / row["user_best_mean_ms"]
    else:
        row["latency_max_over_min"] = None
    return row


def load_rows():
    rows = []
    if not RAW.exists():
        return rows
    for meta_path in sorted(RAW.glob("*.meta.json")):
        meta = json.loads(meta_path.read_text())
        doc_path = RAW / f"{meta['id']}.json"
        doc = {}
        if doc_path.exists():
            try:
                doc = json.loads(doc_path.read_text())
            except json.JSONDecodeError:
                doc = {}
        rows.append(metrics(meta, doc))
    return rows


def group_key(row):
    cell = row["cell"]
    return tuple(cell.get(k) for k in (
        "group", "bin", "stack", "discipline", "test", "rtt", "loss", "flows", "lights", "heavy", "queue", "workers"))


def median_groups(rows):
    buckets = {}
    for row in rows:
        buckets.setdefault(group_key(row), []).append(row)
    out = []
    for key, items in buckets.items():
        def med(name):
            vals = [item[name] for item in items if isinstance(item.get(name), (int, float))]
            return statistics.median(vals) if vals else None

        cell = items[0]["cell"]
        out.append({
            "cell": cell,
            "n": len(items),
            "ok_n": sum(1 for item in items if item["ok"]),
            "goodput_mbps": med("goodput_mbps"),
            "stack_cpu_s_per_gb": med("stack_cpu_s_per_gb"),
            "process_cpu_s_per_gb": med("process_cpu_s_per_gb"),
            "link_cpu_s_per_gb": med("link_cpu_s_per_gb"),
            "kernel_threads_s_per_gb": med("kernel_threads_s_per_gb"),
            "proxy_cpu_s_per_gb": med("proxy_cpu_s_per_gb"),
            "rss_mib": med("rss_mib"),
            "proxy_hwm_mib": med("proxy_hwm_mib"),
            "proxy_rss_mib": med("proxy_rss_mib"),
            "rss_bytes_per_conn": med("rss_bytes_per_conn"),
            "rr_p50_ms": med("rr_p50_ms"),
            "rr_p99_ms": med("rr_p99_ms"),
            "rr_mean_ms": med("rr_mean_ms"),
            "idle_p99_ms": med("idle_p99_ms"),
            "user_mean_ms": med("user_mean_ms"),
            "user_worst_p99_ms": med("user_worst_p99_ms"),
            "user_p99_censored_worst_ms": med("user_p99_censored_worst_ms"),
            "latency_jain": med("latency_jain"),
            "latency_max_over_min": med("latency_max_over_min"),
            "flow_jain": med("flow_jain"),
            "flow_min_share": med("flow_min_share"),
            "generator_misses": med("generator_misses"),
            "timeouts": med("timeouts"),
            "arrivals_completed": med("arrivals_completed"),
            "arrivals_planned": med("arrivals_planned"),
            "late_mean_us": med("late_mean_us"),
            "late_max_us": med("late_max_us"),
            "runs": items,
        })
    return out


def fmt(value, digits=2):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def md_table(headers, records, fields):
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] + ["---:"] * (len(headers) - 1)) + "|",
    ]
    for rec in records:
        cells = []
        for field in fields:
            if isinstance(field, tuple):
                cells.append(fmt(rec.get(field[0]), field[1]))
            else:
                cells.append(fmt(rec.get(field), 2))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def cell_bits(rec):
    cell = rec["cell"]
    return cell


def write_tables(groups):
    def select(**pred):
        chosen = []
        for rec in groups:
            cell = rec["cell"]
            if all(cell.get(key) == value for key, value in pred.items()):
                chosen.append(rec)
        return chosen

    parts = ["# 机器本地对比表", "", "数字是重复次数的中位数。`ok` 不是 全部通过时，表里仍保留测到的值。", ""]
    parts.append("## 服务端下行（100 Mbps，FIFO，队列 2×BDP）")
    parts.append("")
    rows = []
    for rec in select(group="server", test="down", discipline="fifo"):
        cell = rec["cell"]
        rows.append({
            "stack": cell["stack"], "rtt": cell["rtt"], "loss": cell["loss"], "flows": cell["flows"],
            "n": f"{rec['ok_n']}/{rec['n']}", "mbps": rec["goodput_mbps"],
            "stack_cpu": rec["stack_cpu_s_per_gb"], "proc_cpu": rec["process_cpu_s_per_gb"],
            "kern_cpu": rec["kernel_threads_s_per_gb"], "rss": rec["rss_mib"],
            "jain": rec["flow_jain"], "min_share": rec["flow_min_share"],
        })
    rows.sort(key=lambda r: (r["rtt"], r["loss"], r["flows"], r["stack"]))
    parts.append(md_table(
        ["栈", "RTT", "丢包", "流", "通过", "吞吐 Mbps", "栈 CPU s/GB", "进程 CPU s/GB", "内核线程 s/GB", "RSS MiB", "流 Jain", "最小流占比"],
        rows,
        ["stack", ("rtt", 0), ("loss", 2), ("flows", 0), "n", ("mbps", 2), ("stack_cpu", 2), ("proc_cpu", 2), ("kern_cpu", 2), ("rss", 1), ("jain", 3), ("min_share", 3)],
    ))
    parts.append("")
    parts.append("## 服务端混合延迟（1 条下载 + 1 条 1 KiB RR，FIFO，队列 1×BDP）")
    parts.append("")
    rows = []
    for rec in select(group="server", test="mixed"):
        cell = rec["cell"]
        rows.append({
            "stack": f"{cell['stack']}/{cell['discipline']}", "rtt": cell["rtt"], "loss": cell["loss"],
            "n": f"{rec['ok_n']}/{rec['n']}", "mbps": rec["goodput_mbps"],
            "p50": rec["rr_p50_ms"], "p99": rec["rr_p99_ms"], "idle_p99": rec["idle_p99_ms"],
            "stack_cpu": rec["stack_cpu_s_per_gb"], "rss": rec["rss_mib"],
        })
    rows.sort(key=lambda r: (r["rtt"], r["loss"], r["stack"]))
    parts.append(md_table(
        ["栈/队列", "RTT", "丢包", "通过", "吞吐 Mbps", "RR P50", "RR P99", "空载 P99", "栈 CPU s/GB", "RSS MiB"],
        rows,
        ["stack", ("rtt", 0), ("loss", 2), "n", ("mbps", 2), ("p50", 1), ("p99", 1), ("idle_p99", 1), ("stack_cpu", 2), ("rss", 1)],
    ))
    parts.append("")
    parts.append("## 多用户公平（zfstack isolate，25 Hz 开环，256 B）")
    parts.append("")
    rows = []
    for rec in [r for r in groups if r["cell"]["group"] in ("fair", "host") and r["cell"]["test"] == "isolate"]:
        cell = rec["cell"]
        rows.append({
            "bin": cell["bin"], "stack": cell["stack"], "disc": cell["discipline"],
            "rtt": cell["rtt"], "loss": cell["loss"], "lights": cell["lights"], "heavy": cell["heavy"],
            "n": f"{rec['ok_n']}/{rec['n']}", "mbps": rec["goodput_mbps"],
            "mean": rec["user_mean_ms"], "p99": rec["user_worst_p99_ms"], "cens": rec["user_p99_censored_worst_ms"],
            "jain": rec["latency_jain"], "ratio": rec["latency_max_over_min"],
            "miss": rec["generator_misses"], "timeout": rec["timeouts"],
            "stack_cpu": rec["stack_cpu_s_per_gb"], "rss": rec["rss_mib"],
        })
    rows.sort(key=lambda r: (r["bin"], r["stack"], r["rtt"], r["loss"], r["lights"] or 0, r["heavy"] or 0, r["disc"]))
    parts.append(md_table(
        ["宿主", "栈", "队列", "RTT", "丢包", "轻用户", "重连接", "通过", "吞吐", "平均延迟", "最差 P99", "含超时 P99", "延迟 Jain", "最差/最好", "发生器漏发", "超时", "栈 CPU", "RSS"],
        rows,
        ["bin", "stack", "disc", ("rtt", 0), ("loss", 2), ("lights", 0), ("heavy", 0), "n", ("mbps", 2), ("mean", 1), ("p99", 1), ("cens", 1), ("jain", 3), ("ratio", 2), ("miss", 0), ("timeout", 0), ("stack_cpu", 2), ("rss", 1)],
    ))
    parts.append("")
    parts.append("## 客户端 TUN（与上面的 100 Mbps 瓶颈不是同一条路径）")
    parts.append("")
    rows = []
    for rec in select(group="client"):
        cell = rec["cell"]
        rows.append({
            "stack": cell["stack"], "test": cell["test"], "rtt": cell["rtt"], "loss": cell["loss"],
            "flows": cell["flows"], "workers": cell.get("workers", 0),
            "n": f"{rec['ok_n']}/{rec['n']}", "mbps": rec["goodput_mbps"],
            "cpu": rec["proxy_cpu_s_per_gb"], "hwm": rec["proxy_hwm_mib"], "rss": rec["proxy_rss_mib"],
            "per_conn": None if rec["rss_bytes_per_conn"] is None else rec["rss_bytes_per_conn"] / 1024,
            "p99": rec["rr_p99_ms"], "idle_p99": rec["idle_p99_ms"],
        })
    rows.sort(key=lambda r: (r["test"], r["rtt"], r["loss"], r["flows"], r["workers"], r["stack"]))
    parts.append(md_table(
        ["代理", "测试", "netem RTT", "丢包", "流", "workers", "通过", "吞吐 Mbps", "代理 CPU s/GB", "HWM MiB", "RSS MiB", "每连接 KiB", "RR P99", "空载 P99"],
        rows,
        ["stack", "test", ("rtt", 0), ("loss", 2), ("flows", 0), ("workers", 0), "n", ("mbps", 1), ("cpu", 2), ("hwm", 1), ("rss", 1), ("per_conn", 1), ("p99", 1), ("idle_p99", 1)],
    ))
    parts.append("")
    (OUT / "TABLES.md").write_text("\n".join(parts) + "\n")


def main():
    rows = load_rows()
    groups = median_groups(rows)
    (OUT / "summary.json").write_text(json.dumps({"runs": rows, "groups": [
        {k: v for k, v in rec.items() if k != "runs"} | {"runs": [
            {"id": item["id"], "ok": item["ok"], "goodput_mbps": item["goodput_mbps"], "errors": item["errors"][:8]}
            for item in rec["runs"]
        ]} for rec in groups
    ]}, indent=2))
    write_tables(groups)
    print(f"runs {len(rows)} groups {len(groups)}")


if __name__ == "__main__":
    main()
