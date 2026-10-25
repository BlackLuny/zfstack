#!/usr/bin/env python3
"""Render the completed representative study; never plot interim effects."""
import argparse
import json
from pathlib import Path

import common_cases as rig
from common_collect import primary_protected


def plot(directory):
    if not (rig.PREP/'done.json').exists():
        raise RuntimeError('finish measurements before rendering')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    audited = directory/'audited'
    source = audited if (audited/'ordinary-comparisons.json').exists() else directory
    ordinary = json.loads((source/'ordinary-comparisons.json').read_text())
    cases = sorted(ordinary['cells'].items(), key=lambda item:(item[1]['heavy'], item[1]['cc'], item[1]['rtt']))
    fig, axes = plt.subplots(1, 2, figsize=(13, 11), layout='constrained', sharey=True)
    labels = []
    for y, (name, cell) in enumerate(cases):
        labels.append(f"{cell['cc'].upper()} / {cell['rtt']} ms / "+('single' if cell['heavy'] == 1 else '8 bulk + 3 interactive'))
        changes = cell['changes']
        protected = primary_protected(changes)
        clear = changes['mbps']['bootstrap_95_pct'][1] < -3 or changes['stack_cpu_s_per_gb']['bootstrap_95_pct'][0] > 3
        valid = cell['observed_valid'] and cell['candidate_strict_valid']
        color = '#777777' if not valid else '#247451' if protected else '#b33434' if clear else '#aa731b'
        for ax, metric in zip(axes, ['mbps', 'stack_cpu_s_per_gb']):
            value = changes[metric]
            low, high = value['bootstrap_95_pct']
            slow, shigh = value['simultaneous_95_pct']
            ax.hlines(y, slow, shigh, color='#b9c0c7', linewidth=1.3)
            ax.hlines(y, low, high, color=color, linewidth=3)
            ax.plot(value['geometric_mean_pct'], y, 'o', color=color, markersize=4)
    for ax, margin in zip(axes, [-3, 3]):
        ax.axvline(0, color='#777777', linewidth=.8)
        ax.axvline(margin, color='#a53333', linestyle='--', linewidth=1)
        ax.grid(axis='x', alpha=.2)
        ax.set_ylim(len(cases)-.5, -.5)
    axes[0].set(yticks=range(len(cases)), yticklabels=labels, xlabel='Useful throughput change (%)', title='Useful throughput: lower is constrained at -3%')
    axes[1].set(xlabel='Driver / relay CPU per useful GB change (%)', title='Driver CPU cost: upper is constrained at +3%')
    axes[1].legend(handles=[Line2D([0], [0], color='#b9c0c7', label='Simultaneous 95% band'),
        Line2D([0], [0], color='#247451', linewidth=3, label='Per-cell 95% interval')], fontsize=8, loc='lower left')
    fig.suptitle('Frozen V5 off/on: 24 ordinary 100 Mb/s WAN cases\n12 complete round pairs per cell; unchanged estimates after independent placement audit', fontsize=12)
    save_figure(fig, directory/'ordinary-protection')
    plt.close(fig)
    loaded = [(name, cell) for name, cell in cases if cell['heavy'] > 1]
    fig, axes = plt.subplots(1, 2, figsize=(12, 6.8), layout='constrained', sharey=True)
    labels = []
    for y, (name, cell) in enumerate(loaded):
        labels.append(f"{cell['cc'].upper()} / {cell['rtt']} ms")
        for ax, metric in zip(axes, ['user_equal_mean_ms', 'worst_user_p99_ms']):
            value = cell['changes'][metric]
            low, high = value['bootstrap_95_pct']
            ax.hlines(y, low, high, color='#2563a6', linewidth=2)
            ax.plot(value['geometric_mean_pct'], y, 'o', color='#2563a6', markersize=4)
    for ax in axes:
        ax.axvline(0, color='#777777', linewidth=.8)
        ax.axvline(5, color='#a53333', linestyle='--', linewidth=1)
        ax.grid(axis='x', alpha=.2)
        ax.set_ylim(len(loaded)-.5, -.5)
        ax.set_xlabel('Latency change (%)')
    axes[0].set(yticks=range(len(loaded)), yticklabels=labels, title='Interactive user-equal mean')
    axes[1].set_title('Worst interactive user P99')
    fig.suptitle('Eight bulk connections of one peer + three interactive peers\nPlanned-arrival latency, whole-round pointwise 95% intervals', fontsize=12)
    save_figure(fig, directory/'ordinary-latency')
    plt.close(fig)


def save_figure(fig, prefix):
    for suffix in ['png', 'svg']:
        target = prefix.with_suffix('.'+suffix)
        if target.exists():
            raise FileExistsError(target)
        fig.savefig(target, dpi=160, metadata={'Date':'2026-10-25'} if suffix == 'svg' else None)
        if suffix == 'svg':
            target.write_text('\n'.join(line.rstrip() for line in target.read_text().splitlines())+'\n')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    a = p.parse_args()
    plot(a.directory.resolve())
