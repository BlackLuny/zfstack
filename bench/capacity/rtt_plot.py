#!/usr/bin/env python3
"""Plot independent RTT validation; error bars are observed ranges, not CIs."""
import argparse
import json
from pathlib import Path
import statistics


def plot(summary, destination):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import ScalarFormatter

    data = json.loads(summary.read_text())
    rows = data["rows"]
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5), layout="constrained")
    panels = [(1, "mbps", "Single-connection useful throughput", "Mbps", False),
              (200, "user_equal_mean_ms", "Loaded light-user mean latency", "ms", True),
              (200, "worst_user_p99_ms", "Loaded worst light-user P99", "ms", True),
              (200, "stack_link_cpu_s_per_gb", "Stack + link-emulator CPU cost", "CPU seconds / GB", False)]
    handles, labels = [], []
    missing = set()
    for ax, (heavy, metric, title, ylabel, logy) in zip(axes.flat, panels):
        for cc, color, marker in (("cubic", "#2563a6", "o"), ("bbr", "#c56318", "s")):
            for arm, style, name in (("q0s0p0", "--", "FIFO / 250 KB"), ("q1s0p0", "-", data.get("candidate_label", "Peer / 2 BDP"))):
                x, y, lower, upper = [], [], [], []
                for rtt in (1,10,50,100,250,500):
                    cell = [r for r in rows if (r["cc"],r["rtt"],r["heavy"],r["arm"]) == (cc,rtt,heavy,arm)]
                    x.append(rtt)
                    if len(cell) != 2 or not all(r["valid"] and r.get(metric) is not None for r in cell):
                        y.append(float("nan")); lower.append(0); upper.append(0)
                        missing.add((cc,rtt,heavy,arm))
                    else:
                        values = [r[metric] for r in cell]
                        mid = statistics.median(values)
                        y.append(mid); lower.append(mid-min(values)); upper.append(max(values)-mid)
                line = ax.errorbar(x, y, yerr=[lower, upper], color=color, linestyle=style,
                                  marker=marker, markersize=4, linewidth=1.5, capsize=3, alpha=.85)
                if ax is axes.flat[0]:
                    handles.append(line); labels.append(f"{cc.upper()}: {name}")
        ax.set(xscale="log", xlabel="Configured base RTT (ms)", ylabel=ylabel, title=title)
        if logy: ax.set_yscale("log")
        elif metric == "mbps": ax.set_ylim(bottom=0)
        ax.set_xticks([1,10,50,100,250,500])
        ax.xaxis.set_major_formatter(ScalarFormatter())
        ax.grid(True, which="major", alpha=.2)
    note = f"; {len(missing)} invalid/incomplete cells left blank" if missing else ""
    fig.suptitle("Independent 1–500 ms validation at 100 Mbps\nTwo observations per arm/cell; bars show range, not confidence intervals" + note, fontsize=12)
    fig.legend(handles, labels, loc="outside lower center", ncol=2, frameon=False, fontsize=9)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for suffix in (".png", ".svg"):
        target = destination.with_suffix(suffix)
        if target.exists(): raise FileExistsError(target)
        fig.savefig(target, dpi=160)
        if suffix == ".svg":
            target.write_text("\n".join(line.rstrip() for line in target.read_text().splitlines()) + "\n")
    plt.close(fig)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("summary", type=Path)
    p.add_argument("destination", type=Path)
    a = p.parse_args()
    plot(a.summary, a.destination)
