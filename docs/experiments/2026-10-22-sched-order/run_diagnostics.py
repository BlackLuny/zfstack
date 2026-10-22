#!/usr/bin/env python3
"""Retain complete rows (including failures), never filter incomplete flows."""
import gzip, hashlib, json, pathlib, subprocess, sys, os
ROOT = pathlib.Path(__file__).resolve().parents[3]
OUT = pathlib.Path(__file__).resolve().parent
plan = json.loads((OUT / 'frozen-plan.json').read_text())
stage, bindir = sys.argv[1:3]
bindir = pathlib.Path(bindir)
inputs = {'local_burst_bytes': 65536, 'local_refill_ns': 1000000, 'fairness_link_bps': 0, 'fairness_link_delay_ns':1000000,'fairness_limits':[536870912,536870912,134217728,4096],'mode': 'off', 'plan_sha256':hashlib.sha256((OUT/'frozen-plan.json').read_bytes()).hexdigest()}
frozen = OUT/'frozen-inputs.json'
if not frozen.exists():
 frozen.write_text(json.dumps(inputs,indent=2)+'\n')
 (OUT/'frozen-inputs.sha256').write_text(hashlib.sha256(frozen.read_bytes()).hexdigest()+'  frozen-inputs.json\n')
cases = [('matrix',bindir/(stage+'-harness'),['l0','--full','--app-drive','event-only','--seed','2201'])]
cases += [(name,bindir/(stage+'-fairness'),[name,'off']) for name in plan['l0']['fairness']]
cases += [(x['name'],bindir/(stage+'-pareto'),x['args']) for x in plan['pareto_probe_cases']]
with gzip.open(OUT/(stage+'-diagnostics.jsonl.gz'),'wt') as f:
 for name,binary,args in cases:
  command = ['taskset','-c',str(min(os.sched_getaffinity(0))),str(binary),*args]
  try:
   r = subprocess.run(command,cwd=ROOT,capture_output=True,text=True,timeout=180)
   row = {'case':name,'binary_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),'argv':command,'returncode':r.returncode,'stderr':r.stderr,'result':json.loads(r.stdout) if r.stdout else None}
  except (subprocess.TimeoutExpired,json.JSONDecodeError) as e: row={'case':name,'argv':command,'error':str(e),'failed':True}
  f.write(json.dumps(row)+'\n'); f.flush()
  print(name, row.get('returncode',row.get('error')),flush=True)
