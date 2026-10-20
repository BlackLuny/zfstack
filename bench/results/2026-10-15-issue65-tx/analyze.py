from pathlib import Path
import json,random,statistics as S,collections,sys
r=Path(__file__).resolve().parent
out={}
for tag in sys.argv[1:]:
 meta=json.loads((r/(tag+'-meta.json')).read_text());assert meta.get('finished') and meta['binary_hashes_unchanged']
 rows=[json.loads(x) for x in (r/(tag+'.jsonl')).read_text().splitlines()];assert len(rows)==len(meta['cases'])*(meta['pairs']+1)*len(meta['variants'])
 groups=collections.defaultdict(dict);seen=set();definitions={c['name']:c for c in meta['cases']}
 for row in rows:
  key=(row['case'],row['rep'],row['variant']);assert key not in seen;seen.add(key)
  assert row['warmup']==(row['rep']<0)
  assert row['command']==['taskset','-c','3',meta['variants'][row['variant']],*definitions[row['case']]['args']]
  m=row['measurement'];assert row['exit_code']==0 and m['ok'] and m['checksum_u64']==m['expected_checksum_u64'] and m['final_reserved_bytes']==0
  assert m['schema_version']==2 and m['all_budget_leases_released'] and m['work_cpu_ns']>0
  args=definitions[row['case']]['args']
  assert m['case']==args[0]
  for flag,field in [('--iterations','iterations'),('--payload','payload_bytes'),('--flows','flows')]:
   if flag in args:assert m[field]==int(args[args.index(flag)+1]),(tag,field)
  if m['case'].startswith('tx-'):
   n=m['payload_bytes'];expected=((n//251)*31375+(n%251)*(n%251-1)//2)*m['iterations']%(1<<64)
   assert m['checksum_u64']==expected
   assert m['sampled_peak_reserved_bytes'] is None
  assert m['operations']==m['iterations'] and m['effective_bytes']==m['payload_bytes']*m['iterations'] if m['case'].startswith('tx-') else m['all_budget_leases_released']
  assert row['binary_sha256']==meta['binary_sha256'][row['variant']]
  key=(row['case'],row['rep']);assert row['variant'] not in groups[key];groups[key][row['variant']]=m
 assert seen=={(c['name'],rep,v) for c in meta['cases'] for rep in range(-1,meta['pairs']) for v in meta['variants']}
 for g in groups.values():
  assert set(g)==set(meta['variants'])
  for f in ['case','iterations','payload_bytes','operations','effective_bytes','checksum_u64','expected_checksum_u64','verification_method','tx_api','connections_checked','flows','peers','server_ingress','pacing','timing_scope','cpu_clock','real_network_io','virtual_clock_used_for_rate']:
   assert len({m.get(f) for m in g.values()})==1,(tag,f)
  if next(iter(g.values()))['case'].startswith('tx-'):
   for f in ['warmup_reserved_bytes','retained_reserved_bytes_before_drop','tx_source_alignment_mod64','tx_payload_alignment_mod64']:
    assert len({m.get(f) for m in g.values()})==1,(tag,f)
 cases={}
 for case in sorted({c for c,_ in groups}):
  gs=[g for (c,rep),g in sorted(groups.items()) if c==case and rep>=0];assert len(gs)==meta['pairs']
  cs={}
  for a,b in meta.get('comparisons',[]):
   vals=[(g[b]['work_cpu_ns']/g[a]['work_cpu_ns']-1)*100 for g in gs]
   rng=random.Random(65);draws=sorted(S.median(rng.choices(vals,k=len(vals))) for _ in range(10000))
   cs[b+'/'+a]={'n':len(vals),'median_pct':S.median(vals),'ci95_pct':[draws[250],draws[9750]],'pairs_pct':vals}
  cases[case]={'warmup_alignments_mod64':{v:sorted({(g[v].get('tx_source_alignment_mod64'),g[v].get('tx_payload_alignment_mod64')) for g in gs}) for v in meta['variants']},'comparisons':cs,'ns_per_op':{v:S.median(g[v]['work_cpu_ns_per_operation'] for g in gs) for v in meta['variants']},'ledgers':{v:{f:gs[0][v].get(f) for f in ['warmup_reserved_bytes','retained_reserved_bytes_before_drop']} for v in meta['variants']}}
 out[tag]={'runs':len(rows),'cases':cases}
 print(tag)
 for c,e in cases.items(): print(c,{k:(round(v['median_pct'],3),[round(x,3) for x in v['ci95_pct']]) for k,v in e['comparisons'].items()})
(r/'summary.json').write_text(json.dumps(out,indent=2)+'\n')
