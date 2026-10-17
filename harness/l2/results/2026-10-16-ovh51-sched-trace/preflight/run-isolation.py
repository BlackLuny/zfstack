import pathlib,subprocess,json,time,os
T=pathlib.Path('/root/zfstack-qa-pr75-20261016-ovh51-01');S=T/'source-trace';O=T/'sched-trace-20261016';P=S/'harness/l2/results/2026-10-16-ovh51-sched-trace';PY=T/'qa-venv/bin/python'
traces=json.loads((O/'trace-driver-finished.json').read_text())['records']
assert len(traces)==3 and all(r['returncode']==0 and r['analysis_returncode']==0 for r in traces)
assert not (O/'isolation').exists()
assert subprocess.run(['pgrep','-x','irqbalance'],stdout=subprocess.DEVNULL).returncode==1
assert subprocess.run(['systemctl','is-active','irqbalance'],capture_output=True,text=True).stdout.strip()=='inactive'
argv=[str(PY),str(P/'isolation.py'),'--expected-sha','85ac164a2b762a87f00ceac4f56727c5cbc91128','--task-dir',str(T),'--output',str(O/'isolation'),'--domain','zfstack-qa-baremetal-51.75.146.55-2280','--run-id','sched-isolation','--allow-irq-affinity']
plan={'before_execution_ns':time.time_ns(),'argv':argv,'user_authorization':'允许执行 C 臂并恢复 IRQ 设置','tracing':False}
(O/'isolation-launch.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2)+'\n')
r=subprocess.run(argv,cwd=S,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'))
(O/'isolation-driver-finished.json').write_text(json.dumps({'returncode':r.returncode,'finished_ns':time.time_ns()},indent=2)+'\n')
