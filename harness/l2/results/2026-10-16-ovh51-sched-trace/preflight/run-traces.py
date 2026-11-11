import pathlib,subprocess,json,time,os
T=pathlib.Path('/root/zfstack-qa-pr75-20261016-ovh51-01');S=T/'source-trace';O=T/'sched-trace-20261016';P=S/'harness/l2/results/2026-10-16-ovh51-sched-trace';PY=T/'qa-venv/bin/python'
O.mkdir(exist_ok=False)
records=[]
for n in range(1,4):
 label=f'trace-{n}'
 argv=[str(PY),str(P/'arm.py'),'--expected-sha','85ac164a2b762a87f00ceac4f56727c5cbc91128','--task-dir',str(T),'--output',str(O/label),'--label',label,'--domain','zfstack-qa-baremetal-51.75.146.55-2280','--run-id','sched-'+label,'--trace']
 r={'label':label,'started_ns':time.time_ns(),'argv':argv}
 with (O/(label+'.log')).open('xb') as f:
  r['returncode']=subprocess.run(argv,cwd=S,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),stdout=f,stderr=subprocess.STDOUT).returncode
 records.append(r);(O/'trace-execution.json').write_text(json.dumps(records,indent=2)+'\n')
 print(label,'arm rc',r['returncode'],flush=True)
 if r['returncode']!=0:break
 with (O/(label+'-analysis.log')).open('xb') as f:
  r['analysis_returncode']=subprocess.run([str(PY),str(P/'analyze_trace.py'),str(O/label),'--output',str(O/label/'analysis.json')],cwd=S,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),stdout=f,stderr=subprocess.STDOUT).returncode
 (O/'trace-execution.json').write_text(json.dumps(records,indent=2)+'\n')
 print(label,'analysis rc',r['analysis_returncode'],flush=True)
 if r['analysis_returncode']!=0:break
(O/'trace-driver-finished.json').write_text(json.dumps({'finished_ns':time.time_ns(),'records':records},indent=2)+'\n')
