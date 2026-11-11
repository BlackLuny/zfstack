"""Read-only post-run extraction of registered late windows (+50 ms context).
No new measurements. The complete traces remain untouched on the host.
"""
from pathlib import Path
import json,gzip,re,hashlib
O=Path('/root/zfstack-qa-pr75-20261016-ovh51-01/sched-trace-20261016')
rx=re.compile(r'\s(\d+)\.(\d+):\s+\w+:')
for n in (1,2,3):
 D=O/f'trace-{n}';a=json.loads((D/'analysis.json').read_text());ranges=[]
 for p in D.rglob('udp-*.json'):
  if not p.stem.split('-')[1].isdigit():continue
  for s in json.loads(p.read_text())['samples']:
   if s.get('generator_lag_ms',0)<5:continue
   start=round(s['scheduled_at']*1e9);end=round((s.get('started_at') or (s['scheduled_at']+s['generator_lag_ms']/1000))*1e9)
   ranges.append([start-50_000_000,end+50_000_000])
 merged=[]
 for lo,hi in sorted(ranges):
  if merged and lo<=merged[-1][1]:merged[-1][1]=max(hi,merged[-1][1])
  else:merged.append([lo,hi])
 if not merged:continue
 dest=D/'late-windows.txt.gz';count=0
 with gzip.open(D/'trace.txt.gz','rt') as src,gzip.open(dest,'xt') as dst:
  dst.write('# Excerpt of unchanged full trace: registered >=5ms late windows with 50ms context.\n')
  for line in src:
   m=rx.search(line)
   if not m:continue
   t=int(m[1])*1_000_000_000+int(m[2].ljust(9,'0')[:9])
   if any(lo<=t<=hi for lo,hi in merged):dst.write(line);count+=1
 h=hashlib.sha256()
 with (D/'trace.txt.gz').open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 meta={'source':'trace.txt.gz','source_sha256':h.hexdigest(),'source_size_bytes':(D/'trace.txt.gz').stat().st_size,'windows_ns':merged,'context_ns':50_000_000,'lines':count,'excerpt_sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'excerpt_bytes':dest.stat().st_size,'new_measurements':False}
 (D/'late-windows-index.json').write_text(json.dumps(meta,indent=2)+'\n')
 print(D.name,meta,flush=True)
