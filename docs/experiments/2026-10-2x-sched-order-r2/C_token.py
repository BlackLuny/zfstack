#!/usr/bin/env python3
"""Carry-preserving C sensitivity; isolated model patches, unchanged stack code."""
import gzip,hashlib,json,os,pathlib,subprocess,time
OUT=pathlib.Path(__file__).resolve().parent;ROOT=OUT.parents[2];BIN=pathlib.Path('/workspace/scratch/sched-r2-bin')
stages=[]
for name,stage in [('token-control','control'),('token-adaptive','adaptive')]:
 root=pathlib.Path('/workspace/scratch/sched-r2-token-'+stage);binary=BIN/(name+'-prod')
 assert not binary.exists(),binary
 subprocess.run(['rustfmt','--edition','2021','harness/src/world.rs','harness/examples/sched_r2.rs'],cwd=root,check=True)
 sources=[p for folder in ['src','harness/src','harness/examples'] for p in (root/folder).rglob('*.rs')]+[root/'Cargo.toml',root/'Cargo.lock',root/'harness/Cargo.toml']
 manifest={'stage':name,'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'source_hashes':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'features':['test-peer'],'diagnostic_only':True}
 patch=subprocess.check_output(['git','diff','HEAD','--','src','harness'],cwd=root)
 for p in subprocess.check_output(['git','ls-files','--others','--exclude-standard','--','src','harness'],cwd=root,text=True).splitlines():patch+=subprocess.run(['git','diff','--no-index','--','/dev/null',p],cwd=root,capture_output=True).stdout
 patchfile=name+'-reproduction.patch.gz'
 with gzip.GzipFile(filename='',mode='wb',mtime=0,fileobj=open(OUT/patchfile,'wb')) as f:f.write(patch)
 manifest['complete_source_patch']=patchfile
 env=os.environ.copy();env.update(CARGO_TARGET_DIR='/workspace/scratch/sched-r2-target-'+name,CARGO_PROFILE_RELEASE_LTO='fat',CARGO_PROFILE_RELEASE_CODEGEN_UNITS='1',CARGO_PROFILE_RELEASE_PANIC='abort',CARGO_PROFILE_RELEASE_DEBUG='1')
 manifest['build_env']={k:v for k,v in env.items() if k.startswith('CARGO_PROFILE')}
 with (OUT/'logs'/(name+'-build.log')).open('w') as log:run=subprocess.run(['cargo','build','--locked','--release','-p','zfharness','--example','sched_r2'],cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
 assert run.returncode==0,name
 binary.write_bytes((pathlib.Path(env['CARGO_TARGET_DIR'])/'release/examples/sched_r2').read_bytes());binary.chmod(0o755)
 manifest['binaries']=[{'kind':'prod','path':str(binary),'sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}]
 (OUT/(name+'-build.json')).write_text(json.dumps(manifest,indent=2)+'\n');stages.append(manifest);print(name,'built',flush=True)
jobs=[]
for case in ['mixed-16','mixed-64']:
 for stage,mode in [('token-control','off'),('token-adaptive','off'),('token-adaptive','drr-max'),('token-adaptive','drr-16k'),('token-adaptive','adaptive-16k'),('token-adaptive','adaptive-32k')]:
  binary=BIN/(stage+'-prod')
  for repeat in range(3):jobs.append(dict(index=len(jobs),case=case,stage=stage,mode=mode,tick_us=100,block=repeat,arm='A' if stage=='token-control' else 'B',comparison='C-token',kind='prod',warmup=False,argv=[str(binary),case,mode,'100'],binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest()))
p=OUT/'C-token-jobs.json';assert not p.exists();p.write_text(json.dumps({'phase':'C-token','diagnostic_only':True,'frozen_plan_sha256':hashlib.sha256((OUT/'C-token-plan.json').read_bytes()).hexdigest(),'jobs':jobs},indent=2)+'\n');p.with_suffix('.sha256').write_text(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n')
with gzip.open(OUT/'C-token-raw.jsonl.gz','wt') as raw:
 for j in jobs:
  packet=OUT/'packets'/('C-token-'+str(j['index']).zfill(4)+'.bin');env=os.environ.copy();env['ZF_R2_PACKET_CAPTURE']=str(packet)
  assert hashlib.sha256(pathlib.Path(j['argv'][0]).read_bytes()).hexdigest()==j['binary_sha256']
  start=time.monotonic();cmd=['taskset','-c',str(min(os.sched_getaffinity(0))),*j['argv']]
  r=subprocess.run(cmd,cwd=ROOT,env=env,capture_output=True,text=True,timeout=240)
  row={**j,'diagnostic_only':True,'returncode':r.returncode,'stderr':r.stderr,'result':json.loads(r.stdout),'executed_argv':cmd,'runner_wall_sec':time.monotonic()-start}
  h=hashlib.sha256();size=0
  with packet.open('rb') as src,gzip.GzipFile(filename='',mode='wb',mtime=0,fileobj=open(str(packet)+'.gz','wb')) as dst:
   while data:=src.read(1<<20):h.update(data);size+=len(data);dst.write(data)
  row.update(packet_capture=str(packet.relative_to(OUT))+'.gz',packet_sha256=h.hexdigest(),packet_bytes=size);packet.unlink()
  raw.write(json.dumps(row)+'\n');raw.flush();print(j['index']+1,len(jobs),j['case'],j['stage'],j['mode'],r.returncode,flush=True)
