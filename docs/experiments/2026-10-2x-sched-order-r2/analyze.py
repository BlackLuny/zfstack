#!/usr/bin/env python3
"""Analyze saved evidence without changing gates or discarding adverse samples."""
import collections,csv,filecmp,gzip,hashlib,itertools,json,math,pathlib,statistics,struct,sys
OUT=pathlib.Path(__file__).resolve().parent

def rows(name):
 with gzip.open(OUT/(name+'-raw.jsonl.gz'),'rt') as f:
  for line in f:yield json.loads(line)
def save(name,x): (OUT/name).write_text(json.dumps(x,indent=2)+'\n')
def csvsave(name,x):
 if not x:return
 with (OUT/name).open('w') as f:
  w=csv.DictWriter(f,fieldnames=list(dict.fromkeys(k for row in x for k in row)));w.writeheader();w.writerows(x)
def worlds(v):return [v['baseline'],v['loaded']] if 'loaded' in v else [v]
def flows(v):return [f for w in worlds(v) for f in w.get('flows',[])]
def metric(v):
 fs=flows(v); ws=worlds(v);s={k:sum(w.get('scheduler',{}).get(k,0) for w in ws) for k in ['serve_calls','serve_zero','runs','sink_full','round_budget_exhausted','share_retry_visits','share_retry_blocked','share_wakes','share_wake_noop','adaptive_entries','adaptive_exits','adaptive_small_visits']}
 s['serve_zero_rate']=s['serve_zero']/s['serve_calls'] if s['serve_calls'] else None
 s['sink_full_per_run']=s['sink_full']/s['runs'] if s['runs'] else None
 s['round_budget_exhausted_per_run']=s['round_budget_exhausted']/s['runs'] if s['runs'] else None
 s['share_wake_noop_rate']=s['share_wake_noop']/s['share_wakes'] if s['share_wakes'] else None
 effective=sum(f['client_recv']+f['server_recv'] for f in fs)
 bulk=[f for f in fs if f.get('planned_rr_rounds',0)==0 and f.get('expect')=='complete']
 finish=max((f.get('finished_at_ns') or 60_000_000_000 for f in bulk),default=0)
 cpu=v.get('process_cpu_sec',0);wall=v.get('wall_sec',0)
 return dict(local_pressure_kind='both' if s['sink_full'] and s['round_budget_exhausted'] else 'full_only' if s['sink_full'] else 'round_only' if s['round_budget_exhausted'] else 'none',effective_bytes=effective,cpu_sec=cpu,cpu_sec_per_GB=cpu*1e9/effective if effective else None,bulk_completion_ns=finish,bulk_goodput_gbps=sum(f['client_recv']+f['server_recv'] for f in bulk)*8/finish if finish else None,reserved_peak=max((w.get('budget',{}).get('reserved_peak',0) for w in ws),default=0),drops=sum(w.get('link',{}).get('queue_drops',0)+w.get('link',{}).get('random_drops',0) for w in ws),completed_flows=sum(f.get('finished_at_ns') is not None for f in fs),planned_flows=len(fs),cpu_busy_fraction=cpu/wall if wall else None,cpu_per_virtual_sec=cpu/(finish/1e9) if finish else None,**s)
def users(v):
 for wi,w in enumerate(worlds(v)):
  groups=w.get('users',[]);by_flow=collections.defaultdict(list);ports=collections.defaultdict(set);by_service=collections.defaultdict(list)
  for f in w.get('flows',[]):by_flow[(f['port'],f['peer'],f['class'])].append(f)
  for u in groups:ports[u['peer']].add(u['port'])
  for item in w.get('scheduler',{}).get('peer_service',[]):by_service[item['peer']].append(item)
  for u in groups:
   alias=len(ports[u['peer']])>1 and v.get('topology')!='shard-per-port'
   gaps=[item['max_pending_gap_ns'] for item in by_service[u['peer']] if v.get('topology')!='shard-per-port' or item.get('shard')==u['port']]
   ff=by_flow[(u['port'],u['peer'],u['class'])]
   samples=[len(f.get('sim_latency_us',[])) for f in ff if f.get('planned_rr_rounds')]
   scope='unidentifiable: censored requests' if u.get('censored_rr_rounds') else 'not applicable: bulk' if not samples else 'per-flow sample estimate with fewer than100 requests' if min(samples)<100 else 'L0 per-flow request samples'
   yield dict(p99_scope=scope,world=wi,port=u['port'],peer=u['peer'],user_class=u['class'],flows=u['flows'],planned_rr=u.get('planned_rr_rounds',0),censored_rr=u.get('censored_rr_rounds',0),p99_us=u.get('worst_flow_p99_us') if not u.get('censored_rr_rounds') else None,mean_latency_ns=u.get('completed_mean_latency_ns'),max_pending_gap_ns=max(gaps,default=None) if not alias else None,gap_scope='unavailable: original observer aggregates aliased PeerIds across ifaces' if alias else 'pending unsent per Peer on one iface; includes cwnd/rwnd/pacing waits',finished_at_us=u.get('finished_at_us'),received_bytes=u['client_recv_bytes'])

def packet_frames(row):
 with gzip.open(OUT/row['packet_capture'],'rb') as f:
  while h:=f.read(32):
   assert len(h)==32
   link,t,tag,peer,n=struct.unpack('<QQIQI',h);pkt=f.read(n);assert len(pkt)==n
   yield link,t,tag,peer,pkt

def brief(frame):
 link,t,tag,peer,p=frame;ip=(p[0]&15)*4 if p[0]>>4==4 else 40
 return dict(link=link,at_ns=t,tag=tag,peer=peer,src_port=int.from_bytes(p[ip:ip+2],'big'),dst_port=int.from_bytes(p[ip+2:ip+4],'big'),seq=int.from_bytes(p[ip+4:ip+8],'big'),ack=int.from_bytes(p[ip+8:ip+12],'big'),flags=p[ip+13],length=len(p))
def stream_stats(row):
 counters=collections.Counter();conn={};n=0
 for x in packet_frames(row):
  l,t,tag,peer,p=x;ip=(p[0]&15)*4 if p[0]>>4==4 else 40
  frame=struct.pack('<QQIQI',l,t,tag,peer,len(p))+p
  counters[hashlib.sha256(frame).digest()]+=1;n+=1
  key=(l,tag,peer,p[12:20] if ip!=40 else p[8:40],p[ip:ip+4])
  conn.setdefault(key,hashlib.sha256()).update(frame)
 return n,counters,{k:h.hexdigest() for k,h in conn.items()}
def packet_compare(a,b):
 pa,pb=OUT/a['packet_capture'],OUT/b['packet_capture']
 exact=filecmp.cmp(pa,pb,shallow=False)
 if not exact and a['packet_sha256']==b['packet_sha256']:
  exact=True
  with gzip.open(pa,'rb') as fa,gzip.open(pb,'rb') as fb:
   while True:
    x,y=fa.read(1<<20),fb.read(1<<20)
    if x!=y:exact=False;break
    if not x:break
 out={'byte_for_byte_ordered_stream_equal':exact,'sha256_A':a['packet_sha256'],'sha256_B':b['packet_sha256']}
 if not exact:
  n,ca,ka=stream_stats(a);m,cb,kb=stream_stats(b)
  out.update(packets_A=n,packets_B=m,identical_multiset_including_timestamps=ca==cb,identical_per_connection_order_and_time=ka==kb)
  out['first_differences']=[];out['different_positions']=0
  for i,(x,y) in enumerate(itertools.zip_longest(packet_frames(a),packet_frames(b))):
   if x!=y:
    out['different_positions']+=1
    if len(out['first_differences'])<8:out['first_differences'].append({'index':i,'A':brief(x) if x else None,'B':brief(y) if y else None})
 return out

def output_compare(a,b):
 def fields(v):return [(f['id'],f['client_sent'],f['client_recv'],f['server_sent'],f['server_recv'],f.get('finished_at_ns'),f['sim_latency_us'],f['write_wait_us'],f['censored_rr_rounds']) for f in flows(v)]
 p=packet_compare(a,b);p['flow_completion_and_request_outputs_equal']=fields(a['result'])==fields(b['result'])
 return p

if '--functional' in sys.argv:
 a=list(rows('functional-A2'));b=list(rows('functional-B'));c=list(rows('functional-C'));cmp=[];userrows=[];cases=[]
 ga=collections.defaultdict(dict);gb=collections.defaultdict(dict)
 for r in a:ga[r['case']][r['stage']]=r
 for case,arms in ga.items():
  for label,x,y in [('A-gate',arms['control'],arms['gate']),('D-counters',arms['bare'],arms['control'])]:cmp.append(dict(comparison=label,case=case,**output_compare(x,y)))
 for r in b:gb[(r['case'],r['tick_us'])][r['mode']]=r
 for (case,tick),arms in gb.items():
  if 'drr-max' not in arms:continue
  base=arms['drr-max'];m=metric(base['result']);quiet=m['sink_full']==m['round_budget_exhausted']==0
  for mode,r in arms.items():
   if mode.startswith('adaptive'):
    cmp.append(dict(comparison='B-'+mode,case=case,tick_us=tick,baseline_has_zero_local_pressure=quiet,**output_compare(base,r)))
 gc=collections.defaultdict(dict)
 for r in c:gc[r['case']][r['stage']]=r
 for case,arms in gc.items():
  cmp.append(dict(comparison='C-gate',case=case,tick_us=100,**output_compare(arms['control'],arms['gate'])))
  for mode,r in gb[(case,100)].items():
   cmp.append(dict(comparison='C-control-vs-'+mode,case=case,tick_us=100,**output_compare(arms['control'],r)))
 for phase,rr in [('A',a),('B',b),('C',c)]:
  for r in rr:
   cases.append(dict(phase=phase,case=r['case'],stage=r['stage'],mode=r['mode'],tick_us=r['tick_us'],ok=r['returncode']==0,**metric(r['result'])))
   for u in users(r['result']):userrows.append(dict(phase=phase,case=r['case'],stage=r['stage'],mode=r['mode'],tick_us=r['tick_us'],**u))
 save('functional-comparisons.json',cmp);csvsave('functional-cases.csv',cases)
 with gzip.open(OUT/'user-summary.jsonl.gz','wt') as f:
  for u in userrows:f.write(json.dumps(u)+'\n')
 with gzip.open(OUT/'user-summary.csv.gz','wt') as f:
  w=csv.DictWriter(f,fieldnames=list(userrows[0]));w.writeheader();w.writerows(userrows)
 csvsave('light-users.csv',[u for u in userrows if u['user_class']=='light' and u['case'].startswith('mixed-')])
 print(json.dumps({'functional_cases':len(cases),'comparisons':len(cmp),'users':len(userrows),'packet_differences':sum(not c['byte_for_byte_ordered_stream_equal'] for c in cmp)}))
 raise SystemExit()

if '--instructions' in sys.argv:
 grouped=collections.defaultdict(list);raw=[]
 for phase in (['instructions-A'] if '--a-only' in sys.argv else ['instructions-A','instructions-B','instructions-C']):
  for r in rows(phase):
   m=metric(r['result']);ir=int(next(x.split(':')[1] for x in r['callgrind_summary'] if x.startswith('summary:')))
   x=dict(phase=phase,case=r['case'],stage=r['stage'],mode=r['mode'],tick_us=r['tick_us'],block=r['block'],ok=r['returncode']==0,Ir=ir,Ir_per_GB=ir*1e9/m['effective_bytes'] if m['effective_bytes'] else None,**m)
   grouped[(phase,r['case'],r['stage'],r['mode'],r['tick_us'])].append(x);raw.append(x)
 summary=[];index={}
 for key,rr in grouped.items():
  values=[r['Ir_per_GB'] for r in rr if r['Ir_per_GB'] is not None]
  x=dict(zip(['phase','case','stage','mode','tick_us'],key));x.update(runs_planned=3,runs_present=len(rr),failed_runs=sum(not r['ok'] for r in rr),effective_bytes=rr[0]['effective_bytes'],Ir_mean=statistics.mean(r['Ir'] for r in rr),Ir_per_GB_mean=statistics.mean(values) if values else None,Ir_per_GB_min=min(values,default=None),Ir_per_GB_max=max(values,default=None),variation_pct=(max(values)/min(values)-1)*100 if values else None,serve_zero=rr[0]['serve_zero'],serve_calls=rr[0]['serve_calls'],sink_full=rr[0]['sink_full'],round_budget_exhausted=rr[0]['round_budget_exhausted'])
  summary.append(x);index[key]=x
 comparisons=[]
 for key,x in index.items():
  phase,case,stage,mode,tick=key
  if phase=='instructions-C' and stage=='gate':
   a=index[(phase,case,'control','off',tick)];label='C-gate'
  elif phase=='instructions-A' and stage in ['gate','control']:
   base='control' if stage=='gate' else 'bare';label='A-gate' if stage=='gate' else 'D-counters';a=index[(phase,case,base,'off',tick)]
  elif phase=='instructions-B' and mode!='drr-max':a=index[(phase,case,stage,'drr-max',tick)];label='B-'+mode
  else:continue
  if not a['Ir_per_GB_mean'] or not x['Ir_per_GB_mean']:delta=worst=None
  else:delta=100*(x['Ir_per_GB_mean']/a['Ir_per_GB_mean']-1);worst=100*(x['Ir_per_GB_max']/a['Ir_per_GB_min']-1)
  localzero=a['sink_full']==a['round_budget_exhausted']==0
  row=dict(comparison=label,case=case,tick_us=tick,mean_delta_pct=delta,worst_repeat_increase_pct=worst,variation_A_pct=a['variation_pct'],variation_B_pct=x['variation_pct'],Ir_per_GB_A=a['Ir_per_GB_mean'],Ir_per_GB_B=x['Ir_per_GB_mean'],serve_zero_A=a['serve_zero'],serve_zero_B=x['serve_zero'],baseline_has_zero_local_pressure=localzero,complete=not a['failed_runs'] and not x['failed_runs'],increase_le_0_5pct=worst is not None and worst<=.5,strict_decrease_all_repeats=x['Ir_per_GB_max']<a['Ir_per_GB_min'] if a['Ir_per_GB_min'] else False)
  if label=='A-gate':row['A_instruction_gate_pass']=row['complete'] and row['increase_le_0_5pct'] and (a['serve_zero']==0 or row['strict_decrease_all_repeats'])
  comparisons.append(row)
 if '--a-only' not in sys.argv:
  for key,x in index.items():
   phase,case,stage,mode,tick=key
   if phase!='instructions-B' or tick!=100:continue
   a=index[('instructions-C',case,'control','off',100)]
   comparisons.append(dict(comparison='C-control-vs-'+mode,case=case,tick_us=100,mean_delta_pct=100*(x['Ir_per_GB_mean']/a['Ir_per_GB_mean']-1),worst_repeat_increase_pct=100*(x['Ir_per_GB_max']/a['Ir_per_GB_min']-1),variation_A_pct=a['variation_pct'],variation_B_pct=x['variation_pct'],Ir_per_GB_A=a['Ir_per_GB_mean'],Ir_per_GB_B=x['Ir_per_GB_mean'],serve_zero_A=a['serve_zero'],serve_zero_B=x['serve_zero'],baseline_has_zero_local_pressure=False,complete=not a['failed_runs'] and not x['failed_runs'],increase_le_0_5pct=None,strict_decrease_all_repeats=None))
 prefix='A-' if '--a-only' in sys.argv else ''
 csvsave(prefix+'instruction-runs.csv',raw);save(prefix+'instruction-summary.json',summary);csvsave(prefix+'instruction-comparisons.csv',comparisons);save(prefix+'instruction-comparisons.json',comparisons)
 print(json.dumps({'instruction_runs':len(raw),'groups':len(summary),'comparisons':len(comparisons),'A_passed_cases':sum(c.get('A_instruction_gate_pass',False) for c in comparisons),'A_total_cases':sum(c['comparison']=='A-gate' for c in comparisons)}));raise SystemExit()

if '--attribution' in sys.argv:
 target=['schedule','serve','wants_tx','new_data_len'];output=[]
 for r in rows('attribution-A'):
  self_cost=collections.Counter();child=collections.Counter();calls=collections.Counter();edgecost=collections.Counter();edgecalls=collections.Counter();fn=cfn=None;pending=None
  with gzip.open(OUT/r['callgrind_file'],'rt') as f:
   for line in f:
    line=line.strip()
    if line.startswith('fn='):fn=line[3:];cfn=None;pending=None
    elif line.startswith('cfn='):cfn=line[4:]
    elif line.startswith('calls='):
     n=int(line[6:].split()[0]);calls[cfn]+=n;edgecalls[(fn,cfn)]+=n;pending=cfn
    elif line and (line[0].isdigit() or line[0] in '+-*'):
     cols=line.split()
     try:cost=int(cols[-1])
     except ValueError:continue
     if pending is not None:child[fn]+=cost;edgecost[(fn,pending)]+=cost;pending=None
     else:self_cost[fn]+=cost
  m=metric(r['result']);primary=sum(self_cost.values())
  expected=int(next(x.split(':')[1] for x in r['callgrind_summary'] if x.startswith('summary:')))
  assert primary==expected,(r['case'],r['stage'],r['block'],primary,expected)
  for name in target:
   selected={n for n in set(self_cost)|set(calls) if '::'+name in n and ('Shard::'+name in n if name in ('schedule','serve') else 'Conn::'+name in n) and not (name=='serve' and '::serve_' in n)}
   count=sum(calls[n] for n in selected);own=sum(self_cost[n] for n in selected);inc=sum(self_cost[n]+child[n] for n in selected)
   output.append(dict(case=r['case'],stage=r['stage'],repeat=r['block'],function=name,calls=count,self_Ir=own,inclusive_Ir=inc,self_Ir_per_call=own/count if count else None,inclusive_Ir_per_call=inc/count if count else None,whole_program_Ir_per_call=primary/count if count else None,from_wants_tx_calls=sum(n for (a,b),n in edgecalls.items() if 'Conn::wants_tx' in a and b in selected),from_wants_tx_Ir=sum(n for (a,b),n in edgecost.items() if 'Conn::wants_tx' in a and b in selected),scope='separate noinline attribution build; both client and DUT functions; not the primary optimized-build gate'))
 csvsave('attribution.csv',output);save('attribution.json',output);print(json.dumps({'attribution_rows':len(output)}));raise SystemExit()

if '--cpu' in sys.argv:
 grouped=collections.defaultdict(dict)
 for phase in ['cpu-A','cpu-B','cpu-C']:
  for r in rows(phase):
   if r['warmup']:continue
   grouped[(phase,r['case'],r['comparison'],r['tick_us'],r['block'])][r['arm']]={**metric(r['result']),'ok':r['returncode']==0}
 comp=collections.defaultdict(list)
 for (p,c,n,t,b),arms in grouped.items():comp[(p,c,n,t)].append((b,arms))
 out=[]
 for key,pairs in comp.items():
  phase,case,label,tick=key;n=20 if phase!='cpu-C' and (phase=='cpu-A' or not label.startswith(('fine-','sensitivity-'))) else 6
  x=dict(phase=phase,case=case,comparison=label,tick_us=tick,pairs_planned=n,pairs_present=len(pairs),failed_pairs=sum(not a['A']['ok'] or not a['B']['ok'] for b,a in pairs))
  for field in ['cpu_sec_per_GB','bulk_goodput_gbps','bulk_completion_ns']:
   values=[100*(a['B'][field]/a['A'][field]-1) for b,a in pairs if a['A'][field] and a['B'][field] is not None]
   if len(values)==n:
    mean=statistics.mean(values);half=(2.093 if n==20 else 2.571)*statistics.stdev(values)/math.sqrt(n)
    x[field+'_delta_pct']=mean;x[field+'_ci_low_pct']=mean-half;x[field+'_ci_high_pct']=mean+half
   else:x[field+'_delta_pct']=x[field+'_ci_low_pct']=x[field+'_ci_high_pct']=None
  for field in ['cpu_sec_per_GB','bulk_goodput_gbps','bulk_completion_ns']:
   values=[100*(a['B'][field]/a['A'][field]-1) for b,a in pairs if a['A'][field] and a['B'][field] is not None]
   x[field+'_paired_deltas_pct']=values;x[field+'_n_available']=len(values)
  for arm in 'AB':
   for field in ['cpu_sec_per_GB','cpu_busy_fraction','cpu_per_virtual_sec','effective_bytes','bulk_completion_ns','reserved_peak','drops','serve_calls','serve_zero','sink_full','round_budget_exhausted']:
    vs=[a[arm][field] for b,a in pairs if a[arm][field] is not None];x[arm+'_'+field]=statistics.mean(vs) if vs else None
  out.append(x)
 noise={}
 for x in out:
  if x['comparison'] not in ['aa','fine-control-aa','fine-drr-max']:continue
  pp=comp[(x['phase'],x['case'],x['comparison'],x['tick_us'])];noise[(x['phase'],x['case'],x['tick_us'])]=max(abs(100*(a['B']['cpu_sec_per_GB']/a['A']['cpu_sec_per_GB']-1)) for b,a in pp)
 for x in out:
  x['AA_max_abs_pct']=noise.get((x['phase'],x['case'],x['tick_us']))
  lo,hi=x['cpu_sec_per_GB_ci_low_pct'],x['cpu_sec_per_GB_ci_high_pct'];mean=x['cpu_sec_per_GB_delta_pct'];aa=x['AA_max_abs_pct']
  x['cpu_attribution']='no attributable change' if lo is None or lo<=0<=hi or (aa is not None and abs(mean)<=aa) else 'increase' if mean>0 else 'decrease'
 csvsave('cpu-summary.csv',out);save('cpu-summary.json',out);print(json.dumps({'cpu_comparisons':len(out)}));raise SystemExit()
if __name__=='__main__':raise SystemExit('choose --functional, --instructions, --attribution or --cpu')
