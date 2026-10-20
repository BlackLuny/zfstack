"""Collect diagnostic evidence only; exclude runtime configuration and WireGuard keys."""
import pathlib,json,hashlib,tarfile,subprocess,time,sys
T=pathlib.Path('/root/zfstack-qa-pr75-20261016-ovh51-01');O=T/'sched-trace-20261016';D=T/'source-trace/harness/l2/results/2026-10-16-ovh51-sched-trace'
sys.path.insert(0,str(D));import arm
host=arm.host_snapshot()
for name,argv in [('processes',['ps','-eo','pid,ppid,pcpu,pmem,comm','--sort=-pcpu']),('containers',['docker','ps','--format','{{.Names}} {{.Status}}']),('namespaces',['ip','netns','list'])]:
 p=subprocess.run(argv,capture_output=True,text=True);host[name]={'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
host['lease_entries']=[p.name for p in pathlib.Path('/var/lock/zfstack-qa').iterdir()]
host['trace_instances']=[p.name for p in pathlib.Path('/sys/kernel/tracing/instances').iterdir()]
(O/'host-final.json').write_text(json.dumps(host,indent=2)+'\n')
selected=[]; retained=[]
topnames={'plan.json','execution.json','analysis.json','trace-stats.json','processes.json','host-before.json','host-after.json','command.log','summary.json'}
for p in sorted(O.rglob('*')):
 if not p.is_file():continue
 rel=p.relative_to(O)
 if 'rig' in rel.parts or 'baseline' in rel.parts:
  if p.name in {f'udp-{n}.json' for n in range(4)} | {'result.json'}: selected.append(p)
  elif p.suffix in {'.json','.log','.csv','.jsonl'} and not any(w in p.name.lower() for w in ['key','config','token']):retained.append(p)
 elif p.name in {'trace.txt.gz', 'late-windows.txt.gz'}:
  (selected if p.stat().st_size<=50_000_000 else retained).append(p)
 elif p.name in topnames or (len(rel.parts)<=2 and p.suffix in {'.json','.log'}):selected.append(p)

def digest(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()
def row(p):return {'path':str(p.relative_to(O)),'size_bytes':p.stat().st_size,'sha256':digest(p)}
index={'created_ns':time.time_ns(),'remote_root':str(O),'files':[row(p) for p in selected],'retained_on_host':[row(p) for p in retained]}
with (T/'sched-evidence.tar.gz').open('xb') as f,tarfile.open(fileobj=f,mode='w:gz') as tar:
 for p in selected:tar.add(p,arcname=str(p.relative_to(O)),recursive=False)
index['archive_sha256']=digest(T/'sched-evidence.tar.gz');index['archive_size_bytes']=(T/'sched-evidence.tar.gz').stat().st_size
(T/'sched-evidence-index.json').write_text(json.dumps(index,indent=2)+'\n')
print(json.dumps({'files':len(selected),'retained_on_host':len(retained),'archive_size_bytes':index['archive_size_bytes'],'archive_sha256':index['archive_sha256']}))
