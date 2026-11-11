from pathlib import Path
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
r=Path(__file__).resolve().parent;s=json.loads((r/'summary.json').read_text())
cases=['tx-alloc_64','tx-alloc-migrate_64','tx-alloc-pooled_64','tx-alloc-pooled_1400','tx-alloc-migrate_65536','tx-alloc-pooled_65536']
labels=['64 B fresh','64 B alternate peer','64 B cached same peer','1400 B cached same peer','64 KiB alternate peer','64 KiB cached same peer']
fig,axs=plt.subplots(1,2,figsize=(13,4.7),sharey=True,layout='constrained')
for ax,api in zip(axs,['push','write']):
 for w,color,offset in [(1,'#1679a7',-.13),(2,'#d27924',.13)]:
  cs=s[f'current-quota-{api}-window{w}']['cases'];vals=[cs[c]['comparisons']['U/T'] for c in cases]
  y=[n+offset for n in range(len(cases))];x=[v['median_pct'] for v in vals]
  lo=[v['median_pct']-v['ci95_pct'][0] for v in vals];hi=[v['ci95_pct'][1]-v['median_pct'] for v in vals]
  ax.errorbar(x,y,xerr=[lo,hi],fmt='o',capsize=3,color=color,label=f'Window {w}: 12 pairs')
 ax.axvline(0,color='#555',lw=.8);ax.axvline(2,color='#b45b63',ls=':',lw=1,label='+2% CPU guard')
 ax.set_title('push' if api=='push' else 'write_with');ax.set_xlabel('Candidate / main CPU change (%) - lower is better');ax.grid(axis='x',alpha=.2);ax.legend(fontsize=8)
axs[0].set_yticks(range(len(cases)),labels);axs[0].invert_yaxis()
fig.suptitle('Issue 65: fixed-work TX CPU, full-byte verification included; paired median and 95% CI')
fig.savefig(r/'tx-owner.png',dpi=160);fig.savefig(r/'tx-owner.pdf')
