import hashlib, json, os, pathlib, random, subprocess
root=pathlib.Path(os.environ.get('ZF_QUEUE_EVIDENCE',str(pathlib.Path(__file__).resolve().parent)))
sha=(root/'source-sha-final').read_text().strip()
# Clean acceptance grid plus independent queue and scheduler controls.
cases=[(mtu,budget,trim,4096,4096,'normal',10) for mtu in [1500,512] for budget in [1,2] for trim in [False,True]]
cases += [(512,1,trim,4096,4096,'onecpu',10) for trim in [False,True]]
cases += [(512,1,trim,256,4096,'normal',5) for trim in [False,True]]
cases += [(512,1,trim,4096,0,affinity,5) for trim in [False,True] for affinity in ['normal','onecpu']]
random.Random(23).shuffle(cases)
env={'cpu_affinity':sorted(os.sched_getaffinity(0)), 'cpu_max':pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().strip(), 'kernel':os.uname().release}
for field in ['cpu.stat']:
 env[field+'_before']=pathlib.Path('/sys/fs/cgroup',field).read_text()
(root/'environment-final.json').write_text(json.dumps(env,indent=2)+'\n')
failures=[]
for mtu,budget,trim,capacity,tun_queue,affinity,rounds in cases:
 name=f'final-{mtu}-{budget}-{int(trim)}-q{capacity}-t{tun_queue}-{affinity}'
 cmd=['docker','exec','zfstack-queue-diagnosis-rig']
 if affinity=='onecpu':cmd+=['taskset','-c',str(env['cpu_affinity'][0])]
 cmd+=['python3','-B','/repo/bench/pareto/shared_budget_case.py','--binary','/evidence/bin/final','--outdir',f'/evidence/runs/{name}','--source-sha',sha,'--mtu',str(mtu),'--global-mib',str(budget),'--rounds',str(rounds),'--ingress-capacity',str(capacity),'--tun-queue-len',str(tun_queue)]
 if trim:cmd+=['--trim']
 if capacity==256 or tun_queue==0:cmd+=['--allow-ingress-drops']
 p=subprocess.run(cmd,capture_output=True,text=True,timeout=240)
 (root/(name+'.log')).write_text(p.stdout+p.stderr)
 r=json.loads((root/'runs'/name/'result.json').read_text())
 rows=[json.loads(line) for line in (root/'runs'/name/'probe.jsonl').read_text().splitlines()]
 rounds_done=[row for row in rows if row['type']=='round_done']
 print(name,r['ok'],'drops',sum(x['ingress_queue_drops'] for x in rounds_done),'kernel',sum(x['tun_tx_dropped'] for x in rounds_done),'high',max((max(x['ingress_high_water']) for x in rounds_done),default=0),r['errors'],flush=True)
 if not r['ok']:failures.append(name)
for field in ['cpu.stat']:
 env[field+'_after']=pathlib.Path('/sys/fs/cgroup',field).read_text()
(root/'environment-final.json').write_text(json.dumps(env,indent=2)+'\n')
if failures:raise RuntimeError(failures)
