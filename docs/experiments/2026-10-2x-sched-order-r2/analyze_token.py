#!/usr/bin/env python3
"""Latency/capacity sensitivity only; capture-enabled CPU is not qualified."""
import collections,csv,gzip,json,pathlib,statistics
import analyze as a
OUT=pathlib.Path(__file__).resolve().parent
allrows=list(a.rows('C-token'));groups=collections.defaultdict(list);users=[]
for r in allrows:
 assert r['returncode']==0 and r['result']['ok']
 for u in a.users(r['result']):users.append(dict(case=r['case'],stage=r['stage'],mode=r['mode'],repeat=r['block'],tick_us=100,model='credit-carry64KiB',**u))
 groups[(r['case'],r['stage'],r['mode'])].append(r)
summary=[]
for (case,stage,mode),rs in groups.items():
 assert len(rs)==3
 m=[a.metric(r['result']) for r in rs]
 row={'case':case,'stage':stage,'mode':mode,'repeats':3,'source_manifest':stage+'-build.json','capture_enabled':True,'CPU_and_Ir_qualification':False,'distinct_packet_streams':len({r['packet_sha256'] for r in rs})}
 for field in ['bulk_completion_ns','bulk_goodput_gbps','reserved_peak','drops','serve_calls','serve_zero','sink_full','round_budget_exhausted']:
  vs=[x[field] for x in m];row[field+'_min']=min(vs);row[field+'_max']=max(vs)
 summary.append(row)
a.save('C-token-summary.json',summary);a.csvsave('C-token-cases.csv',summary);a.csvsave('C-token-users.csv',users)
base=[]
for phase in ['functional-A2','functional-C']:
 for r in a.rows(phase):
  if r['case'] in ['mixed-16','mixed-64'] and r['stage']=='control':base.append({'case':r['case'],'model':'reset1ms' if phase=='functional-A2' else 'reset100us',**a.metric(r['result'])})
for r in allrows:
 if r['stage']=='token-control' and r['block']==0:base.append({'case':r['case'],'model':'carry100us',**a.metric(r['result'])})
a.csvsave('C-model-capacity.csv',base)
print(json.dumps({'runs':len(allrows),'per_user_rows':len(users),'groups':len(summary),'all_three_packet_streams_deterministic':all(r['distinct_packet_streams']==1 for r in summary)}))
