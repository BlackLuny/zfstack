"""Read-only generator-lag diagnosis; no verdict or threshold changes."""
import gzip,json
from pathlib import Path
P=Path(__file__).resolve().parent
files=list((P/'formal-artifacts').rglob('baseline/rig/raw.json.gz'))
assert len(files)==1
raw=json.loads(gzip.decompress(files[0].read_bytes()))
samples=raw['udp-3.json']['samples'];missed=[s for s in samples if s['status']=='missed'];assert len(missed)==1
m=missed[0];prev=samples[m['seq']-1];nxt=samples[m['seq']+1]
start,end=prev['ended_at'],nxt['started_at']
r={'source':str(files[0].relative_to(P)),'missed':m,'previous':prev,'next':nxt,
   'window_after_last_completion_sec':[start,end],'window_ms':(end-start)*1000,
   'generator_limit_ms':20,'injector_installed':False,'other_flows':{},
   'limits':'No scheduler trace was collected. Other processes continued, ruling out a shared whole-host stop over this entire interval; per-task scheduler latency, CPU-local IRQ/softirq delay, or generator runtime delay are not distinguished.'}
for i in range(3):
 data=raw[f'udp-{i}.json'];inside=[s for s in data['samples'] if s.get('started_at') is not None and start<s['started_at']<end]
 r['other_flows'][str(i)]={'max_generator_lag_ms':max(s.get('generator_lag_ms',0) for s in data['samples']),
                          'starts_in_window':inside,'count':len(inside)}
(P/'baseline-lag-analysis.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({'missed_lag_ms':m['generator_lag_ms'],'window_ms':r['window_ms'],
                  'other_flow_starts':{k:v['count'] for k,v in r['other_flows'].items()}},indent=2))
