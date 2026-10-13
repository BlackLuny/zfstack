import json, os, subprocess, pathlib, sys, time
ROOT=pathlib.Path(os.environ.get('ZF_PRESSURE_EVIDENCE', str(pathlib.Path(__file__).resolve().parent)))
SHA={'main':'b170affa14e70d73f3d0834b2e06579dde70ca85','packetpool':'db1f6a50a6cb26b5185a0d9ece6c602e2a005e43'}
def run(label,variant,mtu=1500,mb=8,idle=0,pair=0,extra=()):
 name=f'{label}-{variant}-{mtu}-{mb}-{idle}-{pair}'
 cmd=['docker','exec','zfstack-pressure-rig','python3','-B','/repo/bench/pareto/issue23_tun_case.py','--binary',f'/evidence/bin/{variant}','--loader','/repo/bench/pareto/issue23_tun_load.py','--outdir',f'/evidence/runs/{name}','--label',label,'--variant',variant,'--source-sha',SHA.get(variant,SHA['main']),'--pair',str(pair),'--pair-position','0','--pair-order','AB','--build-flags',json.dumps({'rust':'1.94.1','profile':'release','lto':'fat','codegen_units':1,'panic':'abort','features':['packet-pool-idle-trim']}),'--proxy-cpus','0,1','--loader-cpus','2,3,4','--mtu',str(mtu),'--idle-secs','2','--flows','64','--idle-count',str(idle),'--rr-samples','100','--rr-sizes','64,512,1400,4096','--bulk-bytes-per-flow',str(1<<20),'--mixed-rr-interval-ms','1','--diagnostic','--env','ZFBENCH_TX_CACHE_SAMPLE_MS=1000']
 for k,v in [('GLOBAL',mb<<20),('PORT',mb<<19),('PEER',mb<<19)]:cmd+=['--env',f'ZFBENCH_{k}_BYTES={v}']
 if label.startswith('trace'):
  cmd[cmd.index('/repo/bench/pareto/issue23_tun_case.py')]='/evidence/case_trace.py'
  cmd+=['--loader','/evidence/trace_load.py','--tcpdump']
 cmd+=list(extra)
 start=time.monotonic(); p=subprocess.run(cmd,capture_output=True,text=True,timeout=255)
 result_path=ROOT/'runs'/name/'result.json'; result=json.loads(result_path.read_text()) if result_path.exists() else None
 (ROOT/'records').mkdir(exist_ok=True)
 (ROOT/'records'/f'{name}.json').write_text(json.dumps({'command':cmd,'result':result,'stdout':p.stdout,'stderr':p.stderr,'exit_code':p.returncode,'elapsed':time.monotonic()-start},indent=2))
 print(name, result.get('ok') if result else p.stderr, result.get('errors') if result else '', flush=True)
 return result
if __name__=='__main__':
 for mtu,mb,idle in [(1500,8,0),(512,8,0),(512,64,1000)]:
  for variant in ['main','packetpool']:run('repro',variant,mtu,mb,idle)
