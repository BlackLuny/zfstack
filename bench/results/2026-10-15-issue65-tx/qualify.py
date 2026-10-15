"""Apply preregistered #65 limits; an interval crossing a limit is inconclusive."""
from pathlib import Path
import json,csv
r=Path(__file__).resolve().parent;s=json.loads((r/'summary.json').read_text());checks=[];table=[]
def upper_check(name,result,limit,kind):
 lo,hi=result['ci95_pct'];status='PASS' if (hi<limit if kind=='target_gain' else hi<=limit) else 'REGRESSION' if lo>limit else 'INCONCLUSIVE'
 checks.append({'name':name,'kind':kind,'limit_pct':limit,'median_pct':result['median_pct'],'ci95_pct':[lo,hi],'status':status})
for api in ['push','write']:
 for w in [1,2]:
  tag=f'current-quota-{api}-window{w}'
  for case,entry in s[tag]['cases'].items():
   v=entry['comparisons']['U/T'];upper_check(tag+'/'+case,v,2,'cpu_guard')
   if case in ['tx-alloc_64','tx-alloc-migrate_64']:
    upper_check(tag+'/'+case+'/improvement',v,0,'target_gain')
   aa=entry['comparisons']['U2/U']
   table.append({'tag':tag,'case':case,'pairs':v['n'],'median_pct':v['median_pct'],'ci95_lo_pct':v['ci95_pct'][0],'ci95_hi_pct':v['ci95_pct'][1],'aa_median_pct':aa['median_pct'],'aa_lo_pct':aa['ci95_pct'][0],'aa_hi_pct':aa['ci95_pct'][1], 'baseline_cpu_ns_per_op':entry['ns_per_op']['T'],'candidate_cpu_ns_per_op':entry['ns_per_op']['U'],'baseline_cpu_ns_per_byte':entry['ns_per_op']['T']/int(case.rsplit('_',1)[1]),'candidate_cpu_ns_per_byte':entry['ns_per_op']['U']/int(case.rsplit('_',1)[1])})
for case,e in s['current-quota-pump-guards']['cases'].items():upper_check('pump-guards/'+case,e['comparisons']['U/T'],2,'cpu_guard')
for case,e in s['current-quota-f56-context']['cases'].items():upper_check('f56-context/'+case,e['comparisons']['U/F56'],2,'f56_guard')
if (r/'network-summary.json').exists():
 net=json.loads((r/'network-summary.json').read_text())
 for case,metrics in net.items():
  for field,v in metrics.items():
   if field=='results.goodput_mbps_window':
    lo,hi=v['ci95_pct'];checks.append({'name':case+'/'+field,'kind':'throughput_guard','limit_pct':-5,'median_pct':v['median_pct'],'ci95_pct':[lo,hi],'status':'PASS' if lo>=-5 else 'REGRESSION' if hi<-5 else 'INCONCLUSIVE'})
   elif field.endswith('.p99_ms'):upper_check(case+'/'+field,v,5,'p99_guard')
   elif field=='cpu.cpu_sec_per_effective_GB' or case=='connect1000' and field=='cpu.stack_thread_window_sec':upper_check(case+'/'+field,v,2,'network_cpu_guard')
with (r/'cpu-table.csv').open('w',newline='') as f:
 writer=csv.DictWriter(f,fieldnames=list(table[0]),lineterminator="\n");writer.writeheader();writer.writerows(table)
# Keep original inconclusive cells visible. A prespecified, longer follow-up
# can resolve that same cell only when both independent windows exclude +2%.
supplement_tags=[f'current-quota-write-tiny-long-window{w}' for w in [1,2]]
resolutions=[]
if all(t in s for t in supplement_tags):
 for case in ['tx-alloc-pooled_1','tx-alloc-pooled_64']:
  results=[s[t]['cases'][case]['comparisons']['U/T'] for t in supplement_tags]
  for t,v in zip(supplement_tags,results):upper_check(t+'/'+case,v,2,'supplement_cpu_guard')
  if all(v['ci95_pct'][1]<=2 for v in results):
   for w in [1,2]:
    name=f'current-quota-write-window{w}/{case}'
    if any(c['name']==name and c['status']=='INCONCLUSIVE' for c in checks):
     resolutions.append({'original_check':name,'supplement_checks':[t+'/'+case for t in supplement_tags],'reason':'Two independent 12-pair windows with 5x iterations; original samples retained separately.'})
pump_tags=[f'current-quota-pump-long-window{w}' for w in [1,2]]
if all(t in s for t in pump_tags):
 for case in ['short256_4096','down8']:
  results=[s[t]['cases'][case]['comparisons']['U/T'] for t in pump_tags]
  for t,v in zip(pump_tags,results):upper_check(t+'/'+case,v,2,'supplement_cpu_guard')
  name='pump-guards/'+case
  if all(v['ci95_pct'][1]<=2 for v in results) and any(c['name']==name and c['status']=='INCONCLUSIVE' for c in checks):
   resolutions.append({'original_check':name,'supplement_checks':[t+'/'+case for t in pump_tags],'reason':'Two independent 12-pair windows with 10x iterations; original samples retained separately.'})
if (r/'network-connect-confirmation-summary.json').exists():
 confirmation=json.loads((r/'network-connect-confirmation-summary.json').read_text())
 for case,metrics in confirmation.items():
  for field,v in metrics.items():
   kind='p99_guard' if field.endswith('.p99_ms') else 'network_cpu_guard' if field=='cpu.stack_thread_window_sec' else None
   if kind:
    name='network-connect-confirmation/'+case+'/'+field
    upper_check(name,v,5 if kind=='p99_guard' else 2,kind)
    original=case+'/'+field
    if checks[-1]['status']=='PASS' and any(c['name']==original and c['status']=='INCONCLUSIVE' for c in checks):
     resolutions.append({'original_check':original,'supplement_checks':[name],'reason':'Fixed 192 paired repetitions of the unchanged 4000-attempt/concurrency1000 protocol; original 12 pairs retained separately.'})
if (r/'network-bulk-confirmation-summary.json').exists():
 confirmation=json.loads((r/'network-bulk-confirmation-summary.json').read_text())
 for case,metrics in confirmation.items():
  for field,v in metrics.items():
   name='network-bulk-confirmation/'+case+'/'+field
   if field=='results.goodput_mbps_window':
    lo,hi=v['ci95_pct'];checks.append({'name':name,'kind':'throughput_guard','limit_pct':-5,'median_pct':v['median_pct'],'ci95_pct':[lo,hi],'status':'PASS' if lo>=-5 else 'REGRESSION' if hi<-5 else 'INCONCLUSIVE'})
   elif field=='cpu.cpu_sec_per_effective_GB':upper_check(name,v,2,'network_cpu_guard')
   else:continue
   original=case+'/'+field
   if checks[-1]['status']=='PASS' and any(c['name']==original and c['status']=='INCONCLUSIVE' for c in checks):
    resolutions.append({'original_check':original,'supplement_checks':[name],'reason':'Fixed 24-pair longer steady-state cohort (25-second window); original 12 pairs retained separately, without pooling.'})
qualified={'source_sha'   :'2694c1406b7ebb5d1eef381a0c1a69e27f1ea743','baseline_sha':'88a2e3f7a2e86fb6948223054a377e8a7f831af1','checks':checks,'non_passing':[v for v in checks if v['status']!='PASS'],'primary_runs_including_warmup':sum(s[f'current-quota-{a}-window{w}']['runs'] for a in ['push','write'] for w in [1,2]),'primary_measured_runs':3456,'supplement_runs_including_warmup':sum(s[t]['runs'] for t in supplement_tags+pump_tags if t in s),'supplement_measured_runs':288 if all(t in s for t in supplement_tags+pump_tags) else None,'aa_max_abs_median_pct':max(abs(x['aa_median_pct']) for x in table),'network_included':(r/'network-summary.json').exists(),'resolutions':resolutions,'unresolved':[v for v in checks if v['status']!='PASS' and v['name'] not in {x['original_check'] for x in resolutions}]}
qualified['all_network_cohorts_included']=all((r/f).exists() for f in ['network-summary.json','network-connect-confirmation-summary.json','network-bulk-confirmation-summary.json'])
qualified['performance_verdict']='PASS' if qualified['all_network_cohorts_included'] and not qualified['unresolved'] else 'REGRESSION' if any(x['status']=='REGRESSION' for x in qualified['unresolved']) else 'INCONCLUSIVE'
(r/'qualification.json').write_text(json.dumps(qualified,indent=2)+'\n')
print('Checks:',len(checks),'non-passing:',len(qualified['non_passing']))
for x in qualified['non_passing']:print(x)
