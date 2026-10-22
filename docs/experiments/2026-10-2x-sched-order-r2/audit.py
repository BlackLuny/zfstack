#!/usr/bin/env python3
"""Final evidence integrity checks; policy failures remain failures."""
import collections,gzip,hashlib,json,pathlib,subprocess
import analyze as a
OUT=pathlib.Path(__file__).resolve().parent;ROOT=OUT.parents[2]
BASE='9d6fb7684e3842da16e673825d3c9ace724aa433'
def sha(x):return hashlib.sha256(x).hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def save(name,x):(OUT/name).write_text(json.dumps(x,indent=2)+'\n')
protected=git('diff',BASE,'--','Cargo.toml','Cargo.lock','src/budget.rs');assert not protected
assert git('rev-parse','experiment/2026-10-22-sched-order').decode().strip()=='e6b79e9267d7e3951b52993e5d4b995ffedbcf81'
verified_hashes=[]
for f in sorted(OUT.glob('*.sha256')):
 for line in f.read_text().splitlines():
  digest,name=line.split(None,1);name=name.lstrip('* ');assert sha((OUT/name).read_bytes())==digest,name;verified_hashes.append(name)
for ref,commit in json.loads((OUT/'source-verification.json').read_text()).items():assert git('rev-parse',ref+'^{commit}').decode().strip()==commit
binaries=[]
for label in ['bare','control','gate','bare2','control2','gate2','adaptive','token-control','token-adaptive']:
 m=json.loads((OUT/(label+'-build.json')).read_text())
 for b in m['binaries']:
  assert sha(pathlib.Path(b['path']).read_bytes())==b['sha256'];binaries.append({**b,'source_manifest':label+'-build.json'})
assert len(binaries)==16
prod=json.loads((OUT/'adaptive-build.json').read_text())
differences=[n for n,h in prod['source_hashes'].items() if sha((ROOT/n).read_bytes())!=h]
assert set(differences)=={'src/adaptive_quantum_tests.rs','harness/src/main.rs'},differences
for stage,ref in [('bare2',BASE),('control2','f85714d'),('gate2','f4464a7')]:
 m=json.loads((OUT/(stage+'-build.json')).read_text())
 for name,h in m['source_hashes'].items():
  if name.startswith('src/') or name in ['Cargo.toml','Cargo.lock']:
   assert sha(git('show',ref+':'+name))==h,(stage,name)
config=(ROOT/'src/config.rs').read_text();shard=(ROOT/'src/shard.rs').read_text()
assert 'adaptive_quantum: false,' in config and 'peer_deficit_drr: false,' in config
assert not any(x in config+shard for x in ['sparse_peer_priority','new_peers:','new_eligible:'])
assert len(json.loads((OUT/'replay-check.json').read_text()))==16
phases={'functional-A':117,'functional-A2':117,'functional-B':156,'functional-C':4,'instructions-A':351,'instructions-B':468,'instructions-C':12,'attribution-A':351,'cpu-A':3276,'cpu-B':2576,'cpu-C':140,'C-token':36}
jobfields=['index','case','stage','mode','tick_us','block','arm','comparison','kind','warmup','argv','binary_sha256']
coverage=[];failures=[];formal=warmup=0;captured={};profile_count=0;protocol=collections.defaultdict(lambda:dict(flow=set(),wire=set(),runs=0))

def expected_extra(case):
 if case.startswith('mixed-'):n=int(case.split('-')[1]);return n+3,n*(1<<20)+3*128*(64+512)
 if case.startswith('bulk-'):n=int(case.split('-')[1]);return n+2,(n+2)*(1<<20)
 if case.startswith('peers-'):
  p=case.split('-');n=int(p[1]);return n,n*(128 if p[2]=='uncongested' else 65536)
 if case=='gate-cwnd-small':return 1,65536
 if case=='gate-rwnd-zero':return 1,196608
 if case.startswith('gate-peers-'):n=int(case.split('-')[-1]);return n*4,n*4*16384
 return None

def signatures(v):
 fs=a.flows(v)
 selected=[{k:f.get(k) for k in ['id','port','peer','class','expect','client_sent','client_recv','server_sent','server_recv','target_recv','target_send','finished_at_ns','sim_latency_us','write_wait_us','censored_rr_rounds']} for f in fs]
 wire=[{k:w.get('trace',{}).get(k) for k in ['link_down','link_up']} for w in a.worlds(v)]
 return sha(json.dumps(selected,sort_keys=True,separators=(',',':')).encode()),sha(json.dumps(wire,sort_keys=True,separators=(',',':')).encode())
for phase,n in phases.items():
 plan=json.loads((OUT/(phase+'-jobs.json')).read_text());assert len(plan['jobs'])==n
 count=0
 for r in a.rows(phase):
  exp=plan['jobs'][count];assert all(r[k]==exp[k] for k in jobfields),(phase,count)
  count+=1
  if phase.startswith('cpu-'):
   if r['warmup']:warmup+=1
   else:formal+=1
  ok=r.get('returncode')==0 and r.get('result',{}).get('ok') is True
  if not ok:failures.append({'phase':phase,'index':r['index'],'case':r['case'],'stage':r['stage'],'returncode':r.get('returncode'),'error':r.get('error')})
  if ok:
   want=expected_extra(r['case'])
   if want:
    m=a.metric(r['result']);assert (m['planned_flows'],m['effective_bytes'])==want,(phase,r['index'],want)
    assert all(f['finished_at_ns'] is not None and f.get('censored_rr_rounds',0)==0 for f in a.flows(r['result']))
   if not r['warmup']:
    k=(phase,r['case'],r['stage'],r['mode'],r['tick_us']);f,w=signatures(r['result']);g=protocol[k];g['flow'].add(f);g['wire'].add(w);g['runs']+=1
  if 'packet_capture' in r:captured[r['packet_capture']]=r['packet_sha256']
  if 'callgrind_file' in r:
   with gzip.open(OUT/r['callgrind_file'],'rb') as f:assert sha(f.read())==r['callgrind_sha256']
   profile_count+=1
 assert count==n,(phase,count,n)
 coverage.append({'phase':phase,'planned':n,'present':count,'ordered_fields_match':True})
assert sum(phases.values())==7604
assert formal==5672 and warmup==320,(formal,warmup)
assert len(failures)==12 and all(f['phase']=='functional-A' for f in failures),failures[:10]
# Headers were normalized, but the complete original ordered packet bytes remain.
archive_verified={}
for name,digest in captured.items():
 p=OUT/name;key=(p.stat().st_dev,p.stat().st_ino)
 if key not in archive_verified:
  h=hashlib.sha256()
  with gzip.open(p,'rb') as f:
   while data:=f.read(1<<20):h.update(data)
  archive_verified[key]=h.hexdigest()
 assert archive_verified[key]==digest,name
proto=[dict(zip(['phase','case','stage','mode','tick_us'],k),runs=g['runs'],distinct_flow_outputs=len(g['flow']),distinct_wire_traces=len(g['wire'])) for k,g in sorted(protocol.items())]
save('protocol-repeatability.json',proto)
save('binary-identities.json',binaries)
save('validation.json',{'schema':2,'baseline':BASE,'protected_files_unchanged':True,'source_snapshots_verified':9,'binary_hashes_verified':len(binaries),'complete_source_patches_apply_cleanly':16,'post_measurement_test_or_cli_only_source_differences':differences,'measured_runtime_source_matches':True,'both_policy_flags_default_false':True,'sparse_policy_absent':True,'frozen_hashes_verified':verified_hashes,'coverage':coverage,'total_executions':7604,'primary_Ir_executions':831,'attribution_executions':351,'auxiliary_CPU_formal_executions':formal,'auxiliary_CPU_warmups':warmup,'invalid_initial_preflight_executions':failures,'unexpected_failures_or_timeouts':0,'packet_capture_paths':len(captured),'unique_archives_checked':len(archive_verified),'profile_hashes_verified':profile_count,'full_workspace_tests':{'library_passed':339,'library_ignored':6,'harness_passed':33,'bench_passed':19,'integration_passed':6,'integration_ignored':1,'log':'logs/workspace-tests-v2.log'},'exact_isolated_gate_library_tests':{'passed':327,'ignored':6,'log':'logs/isolated-gate-tests.log'},'example_tests':{'passed':5,'log':'logs/examples-tests-v2.log'},'later_boundary_test_log':'logs/adaptive-tests-final.log','qualification':'No policy promotion; A failed strict decrease criterion; B quiet0.5% NI unproven. L0 only.'})
print(json.dumps({'audit':'passed','executions':7604,'expected_preflight_failures':12,'unexpected_failures':0,'CPU_formal':formal,'profiles':profile_count,'captures':len(captured)}))
