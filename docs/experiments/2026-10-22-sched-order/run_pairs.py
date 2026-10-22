#!/usr/bin/env python3
"""Frozen serial AB/BA and A/A measurements; each row keeps complete output."""
import gzip,hashlib,json,os,pathlib,subprocess,sys
OUT=pathlib.Path(__file__).resolve().parent
ROOT=OUT.parents[2]
plan=json.loads((OUT/'frozen-plan.json').read_text())
phase,bindir=sys.argv[1:3];bindir=pathlib.Path(bindir)
cpu=min(os.sched_getaffinity(0))
cases=[('matrix','harness',['l0','--full','--app-drive','event-only','--seed','2201'])]
cases += [(x,'fairness',[x]) for x in plan['l0']['fairness']]
cases += [(x['name'],'pareto',x['args']) for x in plan['pareto_probe_cases']]
comparisons=[('gate','step0','off','step1','off')] if phase=='step1' else [(m,'final','off','final',m) for m in plan['modes'][1:]]
comparisons += [('aa','step0' if phase=='step1' else 'final','off','step0' if phase=='step1' else 'final','off')]
jobs=[]
for comp,astage,amode,bstage,bmode in comparisons:
 for case,kind,args in cases:
  arms={'A':(astage,amode),'B':(bstage,bmode)}
  for block in range(-1,6):
   order='AB' if block<0 or block%2==0 else 'BA'
   for arm in order:
    stage,mode=arms[arm];binary=bindir/(stage+'-'+kind)
    flags=[] if phase=='step1' else (['--sched-mode',mode] if kind=='harness' else [mode] if kind=='fairness' else ['--sched-mode',mode])
    # step0/step1 fairness requires the baseline mode argument.
    if phase=='step1' and kind=='fairness': flags=['off']
    jobs.append({'comparison':comp,'case':case,'kind':kind,'block':block,'warmup':block<0,'arm':arm,'mode':mode,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'argv':['taskset','-c',str(cpu),str(binary),*args,*flags]})
manifest={'schema':1,'phase':phase,'plan_sha256':hashlib.sha256((OUT/'frozen-plan.json').read_bytes()).hexdigest(),'jobs':jobs}
mp=OUT/(phase+'-paired-plan.json')
if mp.exists(): raise SystemExit('refusing to overwrite paired plan')
mp.write_text(json.dumps(manifest,indent=2)+'\n')
(OUT/(phase+'-paired-plan.sha256')).write_text(hashlib.sha256(mp.read_bytes()).hexdigest()+'  '+mp.name+'\n')
with gzip.open(OUT/(phase+'-paired-raw.jsonl.gz'),'wt') as f:
 for i,j in enumerate(jobs):
  assert hashlib.sha256(pathlib.Path(j['argv'][3]).read_bytes()).hexdigest()==j['binary_sha256']
  try:
   r=subprocess.run(j['argv'],cwd=ROOT,capture_output=True,text=True,timeout=180)
   row={**j,'returncode':r.returncode,'stderr':r.stderr,'result':json.loads(r.stdout) if r.stdout else None}
  except subprocess.TimeoutExpired as e: row={**j,'failed':True,'error':'timeout','stdout':e.stdout.decode() if e.stdout else None}
  except json.JSONDecodeError as e: row={**j,'failed':True,'error':str(e),'stdout':r.stdout,'stderr':r.stderr}
  f.write(json.dumps(row)+'\n');f.flush()
  if j['block']==5 and j['arm']==('B' if j['block']%2==0 else 'A'): print(i+1,len(jobs),j['comparison'],j['case'],row.get('returncode',row.get('error')),flush=True)
