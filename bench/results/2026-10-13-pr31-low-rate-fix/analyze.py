#!/usr/bin/env python3
"""Functional low-rate retention regression, not a throughput benchmark."""
import json,pathlib,statistics,hashlib
R=pathlib.Path(__file__).resolve().parent
summary={'scope':'diagnostic real TUN only; no performance/non-inferiority claim','cases':[],'low_rate':[],'cold':[]}
for f in sorted((R/'records').glob('*.json')):
 r=json.loads(f.read_text());result=r['result'];assert result and result['ok'] and result['cleanup_ok'],r['name']
 assert not result['eligible_for_performance']
 p=R/'runs'/r['name'];meta=json.loads((p/'final_metadata.json').read_text())
 rows=[json.loads(s) for s in (p/'loader.jsonl').read_text().splitlines()]
 samples=[json.loads(s.split(' ',1)[1]) for s in (p/'proxy.stderr').read_text().splitlines() if s.startswith('ZFBENCH_TX_CACHE ')]
 events=[json.loads(s) for s in (p/'events.jsonl').read_text().splitlines()]
 start=next(e['monotonic_ns'] for e in events if e['type']=='child_started' and e['role']=='proxy')
 summary['cases'].append({'name':r['name'],'ok':True,'cleanup_ok':True,'source_sha':result['source_sha'],'binary_sha256':meta['binary_sha256'],'verified_bulk_bytes':sum(x['verified_bytes'] for x in rows if x['type']=='bulk'),'sample_n':len(samples)})
 for s in samples:
  for a,b in [('global_reserved_bytes','global_limit_bytes'),('port_physical_bytes','port_limit_bytes'),('peer_physical_bytes','peer_limit_bytes')]:assert s[a]<=s[b],(r['name'],a,s)
 if r['group'].startswith('low-rate'):
  phase=next(x for x in rows if x['type']=='low_traffic')
  begin=(phase['before']['monotonic_ns']-start)/1e6;end=(phase['after']['monotonic_ns']-start)/1e6
  stable=[s for s in samples if begin+12000<s['elapsed_ms']<end-1000]
  assert len(stable)>=40
  item={'name':r['name'],'mtu':r['mtu'],'requests':phase['requests'],'rss_before':phase['before']['proxy']['rss_bytes'],'rss_after':phase['after']['proxy']['rss_bytes'],'stable_samples':len(stable),'large_min':min(s['large_cached'] for s in stable),'large_max':max(s['large_cached'] for s in stable),'large_median':statistics.median(s['large_cached'] for s in stable),'last':stable[-1]}
  if 'after' in r['group']:
   assert item['large_max']<=2,item
   assert item['last']['idle_trimmed_blocks']==14,item
  else:assert item['large_median']==16,item
  summary['low_rate'].append(item)
 if r['group']=='cold-after':
  phase=next(x for x in rows if x['type']=='quiet');assert phase['complete_wire_silence']
  begin=(phase['before']['monotonic_ns']-start)/1e6;end=(phase['after']['monotonic_ns']-start)/1e6
  stable=[s for s in samples if begin+34000<s['elapsed_ms']<end-1000];assert stable
  assert all(s['payload_bytes']==s['descriptor_bytes']==s['packet_cached']==s['packet_in_use']==0 for s in stable)
  summary['cold'].append({'name':r['name'],'stable_samples':len(stable),'last':stable[-1]})
assert len(summary['cases'])==10
assert len(summary['low_rate'])==4 and len(summary['cold'])==2
(R/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
print('10/10 cases passed payload, cleanup and retention assertions; sampled budget bounds held')
for r in summary['low_rate']:print(r['name'],r['large_min'],r['large_max'],r['last']['payload_bytes'],r['last']['idle_trimmed_blocks'])
