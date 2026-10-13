#!/usr/bin/env python3
"""Plot sampled PacketPool retention (diagnostics, not performance runs)."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

p = argparse.ArgumentParser()
p.add_argument('root', type=Path)
p.add_argument('--outdir', type=Path, default=Path('.'))
a = p.parse_args()
fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True, constrained_layout=True)
for ax, mtu in zip(axes, [1500, 512]):
    for variant, label in [('before', 'main b170aff'), ('after', 'PacketPool decay')]:
        path = a.root / 'runs' / f'low-rate-{mtu}-00-0-{variant}' / 'proxy.stderr'
        rows = [json.loads(s.split(' ', 1)[1]) for s in path.read_text().splitlines()
                if s.startswith('ZFBENCH_TX_CACHE ')]
        ax.step([r['elapsed_ms'] / 1000 for r in rows], [r['packet_cached'] for r in rows],
                where='post', label=label)
    ax.set(title=f'MTU {mtu}', xlabel='Seconds since sampler start', ylim=(-5, 135))
    ax.grid(alpha=.25)
axes[0].set_ylabel('Free packet buffers (separate samples)')
axes[1].legend()
fig.suptitle('Burst → 64-byte request every 250 ms; 3 s observation window')
for ext in ['svg', 'png']:
    fig.savefig(a.outdir / f'packet-cache-retention.{ext}', dpi=150)
