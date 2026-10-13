import json, os, pathlib, subprocess, time
root=pathlib.Path(os.environ.get('ZF_SHARED_BUDGET_EVIDENCE', str(pathlib.Path(__file__).resolve().parent)))
sha='b69bbfb1d2e438dfb23a8fdff5c4c775e5da26c5'
failures=[]
for mtu in [1500,512]:
 for budget in [1,2]:
  for trim in [False,True]:
   name=f'matrix-{mtu}-{budget}-{int(trim)}'
   cmd=['docker','exec','zfstack-shared-budget-rig','python3','-B','/repo/bench/pareto/shared_budget_case.py','--binary','/evidence/bin/probe','--outdir',f'/evidence/runs/{name}','--source-sha',sha,'--mtu',str(mtu),'--global-mib',str(budget),'--rounds','3']
   if trim:cmd+=['--trim']
   p=subprocess.run(cmd,capture_output=True,text=True,timeout=240)
   (root/f'{name}.log').write_text(p.stdout+'\n'+p.stderr)
   r=json.loads((root/'runs'/name/'result.json').read_text())
   print(name,r['ok'],r['errors'],flush=True)
   if not r['ok']:failures.append(name)
print('failures',failures)

if failures:raise RuntimeError(failures)
