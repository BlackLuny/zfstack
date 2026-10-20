#!/usr/bin/env python3
"""Plot the frozen closed-loop comparisons; fixed-arrival failures stay separate."""
import json
from pathlib import Path
import matplotlib.pyplot as plt
import numpy as np

OUT=Path(__file__).resolve().parents[1]/"docs/experiments/2026-10-20-issue69-tail"
s=json.loads((OUT/"summary.json").read_text());cs=s["comparisons"]
plt.rcParams.update({"font.size":10,"axes.spines.top":False,"axes.spines.right":False})
fig,axes=plt.subplots(1,3,figsize=(12.8,4.5));x=np.arange(len(cs))
colors={"A":"#b9583d","B":"#188881"}
for arm,shift in (("A",-.15),("B",.15)):
    ranges=[c[arm]["p99_range_ms"] for c in cs];ys=[sum(v)/2 for v in ranges]
    axes[0].errorbar(x+shift,ys,yerr=[[(b-a)/2 for a,b in ranges]]*2,fmt='o',capsize=5,color=colors[arm],label=arm+(' FIFO' if arm=='A' else ' Peer queue'))
axes[0].set_yscale('log');axes[0].set_ylim(8,400);axes[0].set_yticks([10,30,100,300],labels=['10','30','100','300']);axes[0].set_ylabel('Worst-user P99 (ms)');axes[0].set_title('Tail falls to 11–13 ms');axes[0].legend(fontsize=8)
for i,c in enumerate(cs):
    delta=(c['throughput_ratio']-1)*100;axes[1].bar(i,delta,color=colors['B'],width=.55);axes[1].text(i,delta-.09,f'{delta:+.3f}%',ha='center',va='top',fontsize=9)
axes[1].axhline(-1,color='#a45236',ls='--',label='Frozen gate: −1%');axes[1].axhline(0,color='#555',lw=.8);axes[1].set_ylim(-1.3,.1);axes[1].set_ylabel('Bulk goodput change, B vs A (%)');axes[1].set_title('Within 1% gate; not zero loss');axes[1].legend(fontsize=8,loc='lower right')
for i,c in enumerate(cs):
    delta=(c['process_cpu_ratio']-1)*100;axes[2].bar(i,delta,color='#aa7b2c',width=.55);axes[2].text(i,delta+.2,f'+{delta:.2f}%',ha='center',fontsize=9)
axes[2].set_ylim(0,10);axes[2].set_ylabel('Process CPU / bulk GB change (%)');axes[2].set_title('More work and higher CPU cost')
for ax in axes:
    ax.set_xticks(x,labels=['BBR\nABBA','BBR\nBAAB','CUBIC\nABBA']);ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
fig.suptitle('Issue 69 · test-side bottleneck queue prototype',fontsize=14,y=.98)
fig.text(.5,.015,'Same 100 Mbps / 250000 B cap / 200 bulk flows · trace off · ranges = 2 runs per arm, not CI\nClosed-loop light completions increased ~3×; CPU is not equal-work overhead. No production ZFC/WG repair claim.',ha='center',fontsize=9)
fig.tight_layout(rect=[0,.105,1,.93]);fig.savefig(OUT/'tail-causal.png',dpi=180);plt.close(fig)
