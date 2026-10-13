#!/usr/bin/env python3
import argparse, concurrent.futures, hashlib, json, os, pathlib, random, subprocess, time
ROOT=pathlib.Path(os.environ.get('ZF31_EVIDENCE_DIR', str(pathlib.Path(__file__).resolve().parent)))
DOCKER=['docker','--host=unix:///var/run/docker.sock','exec','zfstack-pr31-rig']
SHA={'main':'667e9dbc5a0e4bc86b83fc5c9116a2cb2432eadc','off':'594e380','on':'594e380'}
SHA['off']=SHA['on']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=os.environ.get('ZF31_REPO', '/workspace/zfstack-pr31'),text=True).strip()
FAST={'ZFBENCH_TX_CACHE_TRIM_CHECK_MS':100,'ZFBENCH_TX_CACHE_TRIM_WARM_MS':200,'ZFBENCH_TX_CACHE_TRIM_COLD_MS':1000,'ZFBENCH_TX_CACHE_TRIM_LARGE':2,'ZFBENCH_TX_CACHE_TRIM_SMALL':2,'ZFBENCH_PACKET_POOL_TRIM_IDLE_MS':1000,'ZFBENCH_PACKET_POOL_TRIM_TARGET':0}
SLOW=dict(FAST,ZFBENCH_TX_CACHE_TRIM_CHECK_MS=1000,ZFBENCH_TX_CACHE_TRIM_WARM_MS=5000,ZFBENCH_TX_CACHE_TRIM_COLD_MS=30000,ZFBENCH_PACKET_POOL_TRIM_IDLE_MS=30000)

def snapshot():
 return {name:pathlib.Path(path).read_text() for name,path in [('cpu_stat','/sys/fs/cgroup/cpu.stat'),('cpu_max','/sys/fs/cgroup/cpu.max')]}

def run(group,variant,mtu,pair,position,order,extra=None,diagnostic=False,slow=False):
 name=f'{group}-{mtu}-{pair:02}-{position}-{variant}'
 out=ROOT/'runs'/name
 binary='main' if variant=='main' else 'candidate'
 flags={'rust':'1.94.1','profile':'release','lto':'fat','codegen_units':1,'panic':'abort','features':['zfstack']+([] if binary=='main' else ['packet-pool-idle-trim'])}
 cmd=DOCKER+['python3','-B','/repo/bench/pareto/issue23_tun_case.py','--binary','/evidence/bin/'+binary,'--loader','/repo/bench/pareto/issue23_tun_load.py','--outdir','/evidence/runs/'+name,'--label',group,'--variant',variant,'--source-sha',SHA[variant],'--pair',str(pair),'--pair-order',order,'--pair-position',str(position),'--build-flags',json.dumps(flags),'--loader-cpus','2,3,4','--mtu',str(mtu),'--idle-secs','2','--flows','8','--rr-samples','1000','--rr-sizes','64,512,1400,4096','--bulk-bytes-per-flow',str(8<<20),'--mixed-rr-interval-ms','1']
 env={}
 if variant=='on':env.update(SLOW if slow else FAST)
 if diagnostic:
  cmd+=['--diagnostic'];env['ZFBENCH_TX_CACHE_SAMPLE_MS']=1000
 for key,value in env.items():cmd+=['--env',f'{key}={value}']
 cmd+=extra or []
 before=snapshot(); start=time.monotonic()
 p=subprocess.run(cmd,capture_output=True,text=True,timeout=255)
 result=json.loads((out/'result.json').read_text()) if (out/'result.json').exists() else None
 record={'name':name,'group':group,'variant':variant,'mtu':mtu,'pair':pair,'position':position,'command':cmd,'exit_code':p.returncode,'elapsed':time.monotonic()-start,'before':before,'after':snapshot(),'result':result,'stdout':p.stdout,'stderr':p.stderr}
 (ROOT/'records').mkdir(exist_ok=True)
 (ROOT/'records'/f'{name}.json').write_text(json.dumps(record,indent=2))
 print(name,'PASS' if result and result['ok'] else 'FAIL',round(record['elapsed'],2),flush=True)
 return record

def require(record):
 if record['exit_code'] or not record['result'] or not record['result']['ok']:
  raise RuntimeError(record['name']+': '+str(record['result'] or record['stderr']))

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['smoke','performance','diagnostics','stress']);a=p.parse_args()
 (ROOT/'runs').mkdir(exist_ok=True)
 if a.mode=='smoke':
  for mtu in [1500,512]:
   require(run('smoke','on',mtu,0,0,'AB',['--rr-samples','100','--bulk-bytes-per-flow',str(1<<20)]))
 elif a.mode=='performance':
  for mtu in [1500,512]:
   for pair in range(6):
    for position in range(2):require(run('aa','main',mtu,pair,position,'AB' if pair%2==0 else 'BA'))
   for pair in range(12):
    groups=[('main-off',['main','off']),('off-on',['off','on'])]
    if pair%2:groups.reverse()
    for group,order in groups:
     if pair%2:order.reverse()
     for position,variant in enumerate(order):require(run(group,variant,mtu,pair,position,'AB' if pair%2==0 else 'BA'))
 elif a.mode=='diagnostics':
  jobs=[]
  for mtu in [1500,512]:
   for variant in ['off','on']:
    for group,extra in [('cold-short',['--idle-secs','5,30,60']),('cold-long',['--idle-secs','180']),('low-rate',['--idle-secs','0','--low-secs','60','--low-interval-ms','1000'])]:
     jobs.append((group,variant,mtu,0,0,'AB',extra+['--rr-samples','100','--bulk-bytes-per-flow',str(2<<20)],True,True))
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
   for record in ex.map(lambda job:run(*job),jobs):require(record)
 else:
  # Coverage grid, not a full Cartesian product. Diagnostic samples never enter timing statistics.
  for mtu in [1500,512]:
   for mb,flows,idle in [(8,1,0),(8,8,100),(8,64,0),(64,1,1000),(64,8,100),(64,64,1000)]:
    for position,variant in enumerate(['off','on']):
     extra=['--flows',str(flows),'--idle-count',str(idle),'--rr-samples','100','--bulk-bytes-per-flow',str(1<<20)]
     for key,value in [('GLOBAL',mb<<20),('PORT',mb<<19),('PEER',mb<<19)]:extra+=['--env',f'ZFBENCH_{key}_BYTES={value}']
     record=run(f'stress-{mb}mb-f{flows}-idle{idle}',variant,mtu,0,position,'AB',extra,True)
     # Preserve failures and continue coverage; they remain failures in the report.
