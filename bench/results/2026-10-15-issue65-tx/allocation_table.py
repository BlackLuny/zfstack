"""Validate isolated allocator diagnostics and summarize paired counts."""
from pathlib import Path
import json,csv
r=Path(__file__).resolve().parent;groups={}
for stage,file in [('hot','current-quota-allocation-diagnostics.json'),('cold','current-quota-cold-allocation-diagnostics.json')]:
 rows=json.loads((r/file).read_text());assert len(rows)==96
 for row in rows:
  m=row['measurement'];assert m['ok'] and m['all_budget_leases_released'] and m['checksum_u64']==m['expected_checksum_u64'] and m['final_reserved_bytes']==0
  n=m['payload_bytes'];assert m['iterations']==10000 and m['checksum_u64']==((n//251*31375+(n%251)*(n%251-1)//2)*m['iterations'])%(1<<64)
  api='write_with' if row['api'].startswith('tx_write') else 'push';key=(api,m['case'],n);g=groups.setdefault(key,{})
  assert (stage,row['variant']) not in g;g[stage,row['variant']]=m
out=[]
assert len(groups)==48
for (api,case,n),g in sorted(groups.items()):
 assert set(g)=={(s,v) for s in ['hot','cold'] for v in ['T','U']}
 fields={'hot':['work_alloc_calls','work_free_calls','work_alloc_bytes','warmup_reserved_bytes','retained_reserved_bytes_before_drop'],'cold':['cold_alloc_calls','cold_free_calls','cold_alloc_bytes']}
 row={'api':api,'case':case,'payload_bytes':n,'hot_iterations':10000}
 for stage,fs in fields.items():
  for f in fs:
   assert g[stage,'T'][f]==g[stage,'U'][f],(api,case,n,f)
   for v in ['T','U']:row[f+'_'+v]=g[stage,v][f]
 for v in ['T','U']:
  for f in ['warmup_reserved_bytes','retained_reserved_bytes_before_drop']:assert g['hot',v][f]==g['cold',v][f]
 out.append(row)
with (r/'allocation-table.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(out[0]),lineterminator='\n');w.writeheader();w.writerows(out)
(r/'allocation-summary.json').write_text(json.dumps({'paired_cases':48,'raw_rows':192,'all_counts_and_bytes_match':True,'all_warmup_and_retained_ledgers_match':True,'all_final_reservations_zero':True,'baseline_sha':'88a2e3f7a2e86fb6948223054a377e8a7f831af1','candidate_sha':'2694c1406b7ebb5d1eef381a0c1a69e27f1ea743','counting':'Rust GlobalAlloc API requests; realloc counts as one alloc and one free; requested bytes exclude allocator-internal overhead.'},indent=2)+'\n')
print('Validated 192 diagnostic rows across 48 paired cases.')
