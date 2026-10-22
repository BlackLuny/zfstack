#!/usr/bin/env python3
"""Per-user min/max over every formal repeat; missing users stay in denominators."""
import collections,csv,gzip,json,pathlib
import analyze as a
OUT=pathlib.Path(__file__).resolve().parent
phases=['instructions-A','instructions-B','instructions-C','attribution-A','cpu-A','cpu-B','cpu-C','C-token']
templates={}
for phase in ['functional-A2','functional-B','functional-C','C-token']:
 for r in a.rows(phase):templates[(r['case'],r['stage'],r['mode'],r['tick_us'])]=list(a.users(r['result']))
expected=collections.Counter();groups={}
def core(phase,r):return (phase,r['comparison'],r['case'],r['stage'],r['mode'],r['tick_us'],r['arm'])
def key(c,u):return (*c,u['world'],u['port'],u['peer'],u['user_class'])
for phase in phases:
 plan=json.loads((OUT/(phase+'-jobs.json')).read_text())['jobs']
 for r in plan:
  if not r['warmup']:expected[core(phase,r)]+=1
for c,n in expected.items():
 _,_,case,stage,mode,tick,_=c
 for u in templates[(case,stage,mode,tick)]:groups[key(c,u)]={'planned':n,'present':0,'failed':0,'scope':u['gap_scope'],'p99_scope':u['p99_scope'],'fields':{}}
fields=['flows','planned_rr','censored_rr','p99_us','mean_latency_ns','max_pending_gap_ns','finished_at_us','received_bytes']
for phase in phases:
 for r in a.rows(phase):
  if r['warmup']:continue
  c=core(phase,r)
  for u in a.users(r.get('result') or {}):
   g=groups[key(c,u)];g['present']+=1;g['failed']+=r.get('returncode')!=0
   for f in fields:
    value=u.get(f)
    if value is None:continue
    q=g['fields'].setdefault(f,[value,value,0,0]);q[0]=min(q[0],value);q[1]=max(q[1],value);q[2]+=value;q[3]+=1
rows=[]
for k,g in sorted(groups.items()):
 x=dict(zip(['phase','comparison','case','stage','mode','tick_us','arm','world','port','peer','class'],k))
 x.update(runs_planned=g['planned'],runs_present=g['present'],missing_samples=g['planned']-g['present'],failed_executions=g['failed'],gap_scope=g['scope'],p99_scope=g['p99_scope'])
 for f in fields:
  v=g['fields'].get(f,[None,None,0,0]);x[f+'_min']=v[0];x[f+'_max']=v[1];x[f+'_mean_over_available']=v[2]/v[3] if v[3] else None
 if x['missing_samples'] or x['failed_executions'] or x.get('censored_rr_max',0):
  for name in ['p99_us_min','p99_us_max','p99_us_mean_over_available']:x[name]=None
  x['p99_scope']='unidentifiable: failed/missing/censored requests retained in denominator'
 rows.append(x)
with gzip.open(OUT/'formal-user-ranges.jsonl.gz','wt') as f:
 for x in rows:f.write(json.dumps(x)+'\n')
with gzip.open(OUT/'formal-user-ranges.csv.gz','wt') as f:
 w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
print(json.dumps({'per_user_groups':len(rows),'missing_samples':sum(x['missing_samples'] for x in rows),'failed_samples':sum(x['failed_executions'] for x in rows)}))
