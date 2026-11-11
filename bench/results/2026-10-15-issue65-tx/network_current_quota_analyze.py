from pathlib import Path
import collections,json,random,statistics as S,sys
r=Path(__file__).resolve().parent;tag=sys.argv[1] if len(sys.argv)>1 else 'network';meta=json.loads((r/(tag+'-meta.json')).read_text());assert meta.get('finished')
rows=[json.loads(x) for x in (r/(tag+'.jsonl')).read_text().splitlines()];assert len(rows)==sum((n+1)*2 for _,n,_ in meta['cases'])
groups=collections.defaultdict(dict);definitions={c:(n,args) for c,n,args in meta['cases']}
assert meta['namespace_net']!=meta['host_net'] and meta['namespace_pid']!=meta['host_pid']
for row in rows:
 assert row['warmup']==(row['rep']<0)
 assert row['command']==['taskset','-c','0-3',meta['binaries'][row['variant']]['path'],*meta['common'],*definitions[row['case']][1]]
 assert row['tun_tx_queue_len']==(100000 if row['case']=='up8' else 10000) and 'pfifo' in row['qdisc'] and 'limit 10000p' in row['qdisc']
 m=row['measurement'];assert row['exit_code']==0 and m['ok'] and not m['errors'];assert m['server_app']['pattern_errors']==m['server_app']['bad_headers']==0
 assert row['variant'] not in groups[(row['case'],row['rep'])];groups[(row['case'],row['rep'])][row['variant']]=m
 assert m['link']['softnet_backlog_drops']==0
 assert isinstance(m['link']['tun_a'],dict)
 for side in ['tun_a','tun_b']:
  if m['link'].get(side) is not None:
   assert all(m['link'][side][f]==0 for f in ['qdisc_drops','rx_dropped','tx_dropped'])
 if row['case']=='connect1000':assert m['results']['success']==m['results']['attempted']==4000 and not m['results']['failures']
assert set(groups)=={(c,rep) for c,n,_ in meta['cases'] for rep in range(-1,n)}
assert all(set(g)=={'T','U'} for g in groups.values())
def get(obj,path):
 for field in path.split('.'):obj=obj[field]
 return obj
# Report fields using the exact paths in this frozen zfbench schema.
metrics={'mixed64':['results.rr_loaded.p99_ms','results.rr_loaded.p999_ms','cpu.cpu_sec_per_effective_GB','cpu.process_window_sec'], 'connect1000':['results.latency_connect.p99_ms','results.latency_total.p99_ms','results.latency_total.p999_ms','cpu.stack_thread_window_sec','cpu.process_window_sec'], 'down8':['results.goodput_mbps_window','cpu.cpu_sec_per_effective_GB','cpu.stack_thread_window_sec'],'up8':['results.goodput_mbps_window','cpu.cpu_sec_per_effective_GB','cpu.stack_thread_window_sec']}
# A schema mismatch is a failure; it must be explicitly resolved from raw data.
report={}
for case,n,_ in meta['cases']:
 gs=[g for (c,rep),g in sorted(groups.items()) if c==case and rep>=0];assert len(gs)==n and all(set(g)=={'T','U'} for g in gs)
 report[case]={}
 for field in metrics[case]:
  a=[get(g['T'],field) for g in gs];b=[get(g['U'],field) for g in gs]
  vals=[100*(y/x-1) for x,y in zip(a,b)];rng=random.Random(65);boot=sorted(S.median(rng.choices(vals,k=n)) for _ in range(10000))
  report[case][field]={'n':n,'T_median':S.median(a),'U_median':S.median(b),'median_pct':S.median(vals),'ci95_pct':[boot[250],boot[9750]],'pairs_pct':vals}
(r/(tag+'-summary.json')).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
