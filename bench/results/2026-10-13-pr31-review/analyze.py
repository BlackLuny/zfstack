#!/usr/bin/env python3
"""Paired fixed-work summaries; keep diagnostic/failure evidence out of performance means."""
import collections, hashlib, json, math, pathlib, random, statistics
ROOT=pathlib.Path(__file__).resolve().parent

def rows(path):return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
def pctile(x,q):
 if not x:return None
 x=sorted(x);return x[max(0,math.ceil(q*len(x))-1)]
def cpu_stat(s):return dict((k,int(v)) for k,v in (line.split() for line in s.splitlines()))
def metrics(log):
 out={}
 for direction in ['down','up']:
  bulk=[r for r in log if r['type']=='bulk' and r['direction']==direction]
  byte=sum(r['verified_bytes'] for r in bulk); wall=sum(r['window_wall_sec'] for r in bulk); cpu=sum(r['proxy_cpu_sec'] for r in bulk)
  out[direction+'.cpu_s_GB']=cpu/(byte/1e9)
  out[direction+'.goodput_Mbps']=byte*8/wall/1e6
  out[direction+'.cpu_cores']=cpu/wall
  mixed=[]
  for r in bulk:
   m=r.get('mixed_rr')
   if m:
    # Includes late completion of requests initiated under load. The old
    # loader's fully-contained filter is biased against slow requests.
    mixed += [p['rtt_us'] for p in m['raw_samples'] if m['bulk_begin_monotonic_ns']<=p['begin_monotonic_ns']<m['bulk_end_monotonic_ns']]
  out[direction+'.mixed_max_us']=max(mixed) if mixed else None
  out[direction+'.mixed_p99_us']=pctile(mixed,.99) if len(mixed)>=100 else None
  out[direction+'.mixed_n']=len(mixed)
 for r in log:
  if r['type']=='rr':
   prefix=f'rr{r["size"]}.phase{r["phase"]}'
   out[prefix+'.p99_us']=r['latency']['p99_us']
   out[prefix+'.first_us']=r['first_request']['rtt_us']
 quiet=[r for r in log if r['type']=='quiet']
 if quiet:
  out['quiet.rss_after_bytes']=quiet[-1]['after']['proxy']['rss_bytes']
  out['quiet.rss_delta_bytes']=quiet[-1]['after']['proxy']['rss_bytes']-quiet[-1]['before']['proxy']['rss_bytes']
  out['quiet.cpu_sec']=sum(r['proxy_cpu_sec'] for r in quiet)
 return out

def interval(values,seed):
 rng=random.Random(seed)
 estimates=sorted(statistics.median(rng.choices(values,k=len(values))) for _ in range(10000))
 return [pctile(estimates,.025),pctile(estimates,.975)]

def main():
 records=[json.loads(p.read_text()) for p in sorted((ROOT/'records').glob('*.json'))]
 summary={'scope':'Linux TUN + whole proxy, not core-only or WireGuard. CPU uses /proc ticks; intervals are descriptive, no multiple-comparison correction.','records':len(records),'failures':[],'performance':[],'diagnostics':[],'stress':[],'outcomes':[],'sampled_budget_checks':[]}
 groups=collections.defaultdict(lambda:collections.defaultdict(list))
 for record in records:
  result=record['result']
  path=ROOT/'runs'/record['name']
  log=rows(path/'loader.jsonl') if (path/'loader.jsonl').exists() else []
  summary['outcomes'].append({'name':record['name'],'group':record['group'] if result else record['group']+'-preflight','started':bool(result),'ok':bool(result and result['ok']),'cleanup_ok':bool(result and result['cleanup_ok']),'elapsed_sec':record['elapsed']})
  samples=[json.loads(line.split('ZFBENCH_TX_CACHE ',1)[1]) for line in (path/'proxy.stderr').read_text().splitlines() if line.startswith('ZFBENCH_TX_CACHE ')] if (path/'proxy.stderr').exists() else []
  if samples:
   summary['sampled_budget_checks'].append({'name':record['name'],'samples':len(samples),'note':'sampled bounds only; independent reads cannot prove continuous conservation', 'exceeded_samples':sum(any(s[a]>s[b] for a,b in [('global_reserved_bytes','global_limit_bytes'),('port_physical_bytes','port_limit_bytes'),('peer_physical_bytes','peer_limit_bytes')]) for s in samples),'max_reserve_failures_by_level':[max(s['reserve_failures_by_level'][i] for s in samples) for i in range(3)],'pressure_reclaims':max(s['pressure_reclaims'] for s in samples)})
  if not result or not result['ok'] or record['exit_code']:
   summary['failures'].append({'name':record['name'],'errors':result['errors'] if result else [record['stderr'].splitlines()[-1]],'started':bool(result),'workload_errors':[r for r in log if r['type']=='failure'],'cleanup_ok':bool(result and result['cleanup_ok'])})
   continue
  path=ROOT/'runs'/record['name'];meta=json.loads((path/'final_metadata.json').read_text());log=rows(path/'loader.jsonl')
  if record['group'] in ['aa','main-off','off-on']:
   assert result['eligible_for_performance'] and not meta['case_config']['diagnostic']
   assert 'ZFBENCH_TX_CACHE_SAMPLE_MS' not in meta['proxy_environment'] and not meta['case_config']['tcpdump']
   assert all(r['complete_wire_silence'] for r in log if r['type']=='quiet')
   assert result['cleanup_ok']
   record['metrics']=metrics(log);record['case_config']=meta['case_config'];record['hashes']={k:meta[k] for k in ['binary_sha256','loader_sha256','runner_sha256']}
   groups[(record['group'],record['mtu'])][record['pair']].append(record)
  elif record['group'].startswith(('cold-','low-rate','stress-')):
   samples=[json.loads(line.split('ZFBENCH_TX_CACHE ',1)[1]) for line in (path/'proxy.stderr').read_text().splitlines() if line.startswith('ZFBENCH_TX_CACHE ')]
   events=rows(path/'events.jsonl')
   start=next(e['monotonic_ns'] for e in events if e['type']=='child_started' and e['role']=='proxy')
   item={'name':record['name'],'sampler_alignment':'relative to proxy child-start event; sampler starts slightly later during initialization, approximate alignment only','phases':[]}
   for r in log:
    if r['type'] not in ['quiet','low_traffic']:continue
    begin=(r['before']['monotonic_ns']-start)/1e6;end=(r['after']['monotonic_ns']-start)/1e6
    # Avoid boundary/reburst ambiguity from the sampler initialization offset.
    selected=[s for s in samples if begin+500<s['elapsed_ms']<end-500]
    item['phases'].append({'kind':r['type'],'seconds':r.get('requested_sec'),'rss_before':r['before']['proxy']['rss_bytes'],'rss_after':r['after']['proxy']['rss_bytes'],'wire_silent':r.get('complete_wire_silence'),'sample_n':len(selected),'first':selected[0] if selected else None,'last':selected[-1] if selected else None,'max_large_cached':max((s['large_cached'] for s in selected),default=None),'min_large_cached':min((s['large_cached'] for s in selected),default=None)})
   summary['stress' if record['group'].startswith('stress-') else 'diagnostics'].append(item)
 for (group,mtu),pairs in sorted(groups.items()):
  expected=6 if group=='aa' else 12
  item={'group':group,'mtu':mtu,'expected_pairs':expected,'complete':len(pairs)==expected,'pairs':len(pairs),'metrics':{},'throttled_usec':0}
  measurements=collections.defaultdict(lambda:[[],[]])
  for pair,recs in sorted(pairs.items()):
   assert len(recs)==2,(group,mtu,pair)
   assert recs[0]['case_config']==recs[1]['case_config']
   assert recs[0]['hashes']['loader_sha256']==recs[1]['hashes']['loader_sha256']
   assert recs[0]['hashes']['runner_sha256']==recs[1]['hashes']['runner_sha256']
   if group=='aa':
    recs.sort(key=lambda r:r['position'],reverse=bool(pair%2));assert recs[0]['hashes']['binary_sha256']==recs[1]['hashes']['binary_sha256']
   else:
    baseline=group.split('-')[0];recs.sort(key=lambda r:r['variant']!=baseline)
   for rec in recs:item['throttled_usec']+=cpu_stat(rec['after']['cpu_stat'])['throttled_usec']-cpu_stat(rec['before']['cpu_stat'])['throttled_usec']
   for key in recs[0]['metrics']:
    a,b=recs[0]['metrics'][key],recs[1]['metrics'][key]
    if a is None or b is None:continue
    measurements[key][0].append(a);measurements[key][1].append(b)
  for key,(a,b) in measurements.items():
   vals=[100*(y/x-1) for x,y in zip(a,b) if x>0]
   item['metrics'][key]={'baseline_median':statistics.median(a),'candidate_median':statistics.median(b),'n':len(a),'paired_median_percent':statistics.median(vals) if vals else None,'descriptive_ci95':interval(vals,20261013) if len(vals)>=6 else None,'paired_values_percent':vals,'paired_delta_median':statistics.median([y-x for x,y in zip(a,b)])}
  summary['performance'].append(item)
 (ROOT/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
 print('Records',len(records),'failures',len(summary['failures']))
 for item in summary['performance']:
  print(item['group'],item['mtu'],'pairs',item['pairs'],'throttle',item['throttled_usec'])
  for key in ['down.cpu_s_GB','up.cpu_s_GB','down.goodput_Mbps','up.goodput_Mbps','rr64.phase1.p99_us']:
   m=item['metrics'][key];print(' ',key,round(m['paired_median_percent'],2),m['descriptive_ci95'])

if __name__=='__main__':main()
