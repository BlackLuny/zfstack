#!/usr/bin/env python3
"""Immutable job plans; all samples, outputs, profiles and failures retained."""
import concurrent.futures,gzip,hashlib,json,os,pathlib,shutil,subprocess,sys,time
OUT=pathlib.Path(__file__).resolve().parent; ROOT=OUT.parents[2]
BIN=pathlib.Path(os.environ.get('ZF_R2_BIN_DIR','/workspace/scratch/sched-r2-bin'));TOOLS=pathlib.Path('/workspace/scratch/sched-r2-tools/root')
PLAN=json.loads((OUT/'frozen-plan.json').read_text());PHASE=sys.argv[1]
TAG=os.environ.get('ZF_R2_A_BINARY_TAG','2')
CASES=[('matrix/'+c if c in PLAN['matrix'] else c) for c in PLAN['A_cases']]
BCASES=[('matrix/'+c if c in PLAN['matrix'] else c) for c in PLAN['B_cases']]
jobs=[]
def add(case,stage,mode='off',tick=1000,block=0,arm='A',comparison='',kind='prod',warmup=False):
 binary=BIN/(stage+(TAG if stage in ('control','gate','bare') else '')+'-'+kind)
 jobs.append(dict(index=len(jobs),case=case,stage=stage,mode=mode,tick_us=tick,block=block,arm=arm,comparison=comparison,kind=kind,warmup=warmup,argv=[str(binary),case,mode,str(tick)],binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest()))
if PHASE in ['functional-A','functional-A2','instructions-A','attribution-A']:
 for case in CASES:
  for stage in ['control','gate','bare']:
   for repeat in range(PLAN['instruction_repeats'] if PHASE in ('instructions-A','attribution-A') else 1):add(case,stage,block=repeat,kind='attr' if PHASE=='attribution-A' else 'prod')
elif PHASE in ['functional-B','instructions-B']:
 for case in BCASES:
  for mode in ['drr-max','drr-16k','adaptive-16k','adaptive-32k']:
   for repeat in range(3 if PHASE=='instructions-B' else 1):add(case,'adaptive',mode,block=repeat)
 for case in ['mixed-16','mixed-64']:
  for mode in ['adaptive-fast','adaptive-slow','adaptive-k4']:
   for repeat in range(3 if PHASE=='instructions-B' else 1):add(case,'adaptive',mode,block=repeat,comparison='sensitivity')
  for mode in PLAN['fine_clock']['modes']:
   for repeat in range(3 if PHASE=='instructions-B' else 1):add(case,'adaptive',mode,tick=100,block=repeat,comparison='fine')
elif PHASE in ['functional-C','instructions-C']:
 for case in ['mixed-16','mixed-64']:
  for stage in ['control','gate']:
   for repeat in range(3 if PHASE=='instructions-C' else 1):add(case,stage,tick=100,block=repeat,comparison='fine-true-control')
elif PHASE=='cpu-C':
 for case in ['mixed-16','mixed-64']:
  for mode in ['drr-max','drr-16k','adaptive-16k','adaptive-32k','aa']:
   for block in range(-1,6):
    for arm in ('AB' if block<0 or block%2==0 else 'BA'):
     add(case,'control' if arm=='A' or mode=='aa' else 'adaptive','off' if arm=='A' or mode=='aa' else mode,tick=100,block=block,arm=arm,comparison='fine-control-'+mode,warmup=block<0)
elif PHASE=='cpu-A':
 for case in CASES:
  for comparison in ['gate','aa']:
   for block in range(-1,20):
    for arm in ('AB' if block<0 or block%2==0 else 'BA'):
     add(case,'gate' if comparison=='gate' and arm=='B' else 'control',block=block,arm=arm,comparison=comparison,warmup=block<0)
elif PHASE=='cpu-B':
 for case in PLAN['fairness']:
  for mode in ['drr-16k','adaptive-16k','adaptive-32k','aa']:
   for block in range(-1,20):
    for arm in ('AB' if block<0 or block%2==0 else 'BA'):
     add(case,'adaptive',mode if arm=='B' and mode!='aa' else 'drr-max',block=block,arm=arm,comparison=mode,warmup=block<0)
 for case in ['mixed-16','mixed-64']:
  for mode in ['adaptive-fast','adaptive-slow','adaptive-k4',*PLAN['fine_clock']['modes']]:
   tick=100 if mode in PLAN['fine_clock']['modes'] else 1000
   for block in range(-1,6):
    for arm in ('AB' if block<0 or block%2==0 else 'BA'):
     add(case,'adaptive',mode if arm=='B' else 'drr-max',tick=tick,block=block,arm=arm,comparison=('fine-' if tick==100 else 'sensitivity-')+mode,warmup=block<0)
else:raise SystemExit(PHASE)
manifest=OUT/(PHASE+'-jobs.json')
if manifest.exists():raise SystemExit('refusing to overwrite '+str(manifest))
manifest.write_text(json.dumps({'phase':PHASE,'frozen_plan_sha256':hashlib.sha256((OUT/'frozen-plan.json').read_bytes()).hexdigest(),'jobs':jobs},indent=2)+'\n')
manifest.with_suffix('.sha256').write_text(hashlib.sha256(manifest.read_bytes()).hexdigest()+'  '+manifest.name+'\n')
profile=PHASE.startswith(('instructions','attribution'));capture=PHASE.startswith('functional')
(OUT/'profiles').mkdir(exist_ok=True);(OUT/'packets').mkdir(exist_ok=True)
cpus=sorted(os.sched_getaffinity(0));cpu=cpus[0]

def run(j):
 assert hashlib.sha256(pathlib.Path(j['argv'][0]).read_bytes()).hexdigest()==j['binary_sha256']
 env=os.environ.copy();args=j['argv'];prefix=PHASE+'-'+str(j['index']).zfill(4)
 path=OUT/'profiles'/(prefix+'.callgrind')
 if profile:
  env['VALGRIND_LIB']=str(TOOLS/'usr/libexec/valgrind')
  args=[str(TOOLS/'usr/bin/valgrind'),'--tool=callgrind','--cache-sim=no','--branch-sim=no','--compress-strings=no','--compress-pos=no','--callgrind-out-file='+str(path),*args]
 if capture:
  packet=OUT/'packets'/(prefix+'.bin');env['ZF_R2_PACKET_CAPTURE']=str(packet)
 start=time.monotonic()
 try:
  executed=['taskset','-c',str(cpus[1+j['index']%min(2,len(cpus)-1)] if profile and len(cpus)>1 else cpu),*args]
  r=subprocess.run(executed,cwd=ROOT,env=env,capture_output=True,text=True,timeout=3600 if profile else 240)
  row={**j,'returncode':r.returncode,'stderr':r.stderr,'result':json.loads(r.stdout) if r.stdout else None}
 except subprocess.TimeoutExpired as e:row={**j,'failed':True,'error':'timeout','stdout':e.stdout.decode() if isinstance(e.stdout,bytes) else e.stdout}
 except json.JSONDecodeError as e:row={**j,'failed':True,'error':str(e),'stdout':r.stdout,'stderr':r.stderr}
 row['executed_argv']=executed
 row['runner_wall_sec']=time.monotonic()-start
 if profile and path.exists():
  data=path.read_bytes();row['callgrind_summary']=[line for line in data.decode().splitlines() if line.startswith(('summary:','totals:'))]
  row['callgrind_sha256']=hashlib.sha256(data).hexdigest();row['callgrind_file']=str(path.relative_to(OUT))+'.gz'
  with gzip.open(str(path)+'.gz','wb') as f:f.write(data)
  path.unlink()
 if capture and packet.exists():
  digest=hashlib.sha256();size=0
  with packet.open('rb') as src,gzip.GzipFile(filename='',mode='wb',mtime=0,fileobj=open(str(packet)+'.gz','wb')) as dst:
   while data:=src.read(1<<20):digest.update(data);size+=len(data);dst.write(data)
  row.update(packet_capture=str(packet.relative_to(OUT))+'.gz',packet_sha256=digest.hexdigest(),packet_bytes=size);packet.unlink()
 return row

with gzip.open(OUT/(PHASE+'-raw.jsonl.gz'),'wt') as f:
 if profile:
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   for i,row in enumerate(pool.map(run,jobs)):
    f.write(json.dumps(row)+'\n');f.flush()
    print(i+1,len(jobs),row['case'],row['stage'],row['mode'],row.get('returncode',row.get('error')),flush=True)
 else:
  for i,j in enumerate(jobs):
   row=run(j);f.write(json.dumps(row)+'\n');f.flush()
   if capture or (PHASE=='cpu-C' and j['block']==5) or j['block']==19 or (PHASE=='cpu-B' and j['comparison'].startswith(('fine-','sensitivity-')) and j['block']==5):print(i+1,len(jobs),j['comparison'],j['case'],j['stage'],j['mode'],row.get('returncode',row.get('error')),flush=True)
