#!/usr/bin/env python3
import json,pathlib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
ROOT=pathlib.Path(__file__).resolve().parent
fig,axes=plt.subplots(2,2,figsize=(10,6),sharey=True)
for row,mtu in enumerate([1500,512]):
 for col,group in enumerate(['cold-long','low-rate']):
  ax=axes[row,col]
  for variant,color in [('off','#6b7280'),('on','#007c91')]:
   p=ROOT/'runs'/f'{group}-{mtu}-00-0-{variant}'
   events=[json.loads(x) for x in (p/'events.jsonl').read_text().splitlines()]
   start=next(x['monotonic_ns'] for x in events if x['type']=='child_started' and x['role']=='proxy')
   logs=[json.loads(x) for x in (p/'loader.jsonl').read_text().splitlines()]
   phase=next(x for x in logs if x['type']==('quiet' if group=='cold-long' else 'low_traffic'))
   begin=(phase['before']['monotonic_ns']-start)/1e6
   samples=[json.loads(x.split(' ',1)[1]) for x in (p/'proxy.stderr').read_text().splitlines() if x.startswith('ZFBENCH_TX_CACHE ')]
   samples=[x for x in samples if 500<x['elapsed_ms']-begin<phase['requested_sec']*1000-500]
   xs=[(x['elapsed_ms']-begin)/1000 for x in samples]
   ax.plot(xs,[(x['payload_bytes']+x['descriptor_bytes'])/1048576 for x in samples],color=color,label=f'{variant}: TX free + backing')
   ax.plot(xs,[x['packet_free_lease_bytes_estimate']/1048576 for x in samples],color=color,linestyle='--',label=f'{variant}: packet free (estimate)')
  ax.set_title(f'MTU {mtu}: '+('180 s complete idle' if group=='cold-long' else '60 s, one 64-byte request/s'))
  ax.set_xlabel('Seconds into phase (approximate alignment)');ax.set_ylim(-.04,1.12);ax.grid(alpha=.2)
  if col==0:ax.set_ylabel('Retained free memory (MiB)')
axes[0,0].legend(fontsize=8,loc='center right')
fig.suptitle('PR #31: complete-idle reclaim works; low-rate traffic keeps historical cache')
fig.tight_layout()
fig.savefig(ROOT/'cache-retention.png',dpi=150)
fig.savefig(ROOT/'cache-retention.svg')
