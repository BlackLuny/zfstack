import json, os, pathlib, subprocess, sys, time
ROOT=pathlib.Path(os.environ.get('ZF_MULTI_PEER_EVIDENCE', str(pathlib.Path(__file__).resolve().parent)))
SHA='36058e177fb8966e81c7a9777f75acce36472d38'
def run(mode,mtu,peer_mb,trim,pair,label='probe',extra=()):
 name=f'{label}-{mode}-{mtu}-{peer_mb}-{trim}-{pair}'
 cmd=['docker','exec','zfstack-multipeer-rig','python3','-B','/repo/bench/pareto/issue23_tun_case.py','--binary','/evidence/bin/proxy','--loader','/repo/bench/pareto/issue23_tun_load.py','--outdir',f'/evidence/runs/{name}','--label',label,'--variant',str(trim),'--source-sha',SHA,'--pair',str(pair),'--pair-position','0','--pair-order','AB','--build-flags',json.dumps({'rust':'1.94.1','profile':'release','lto':'fat','codegen_units':1,'panic':'abort','features':['packet-pool-idle-trim']}),'--proxy-cpus','0,1','--loader-cpus','2,3,4','--mtu',str(mtu),'--idle-secs','3','--flows','64','--idle-count','16','--rr-samples','50','--rr-sizes','64,512,1400,4096','--bulk-bytes-per-flow',str(1<<20),'--mixed-rr-interval-ms','2','--diagnostic','--peer-mode',mode,'--pressure-hold-secs','2' if mode=='noisy' else '0','--env','ZFBENCH_TX_CACHE_SAMPLE_MS=1000','--env','ZFBENCH_TUN_PEERS=4']
 for k,v in [('GLOBAL',32<<20),('PORT',16<<20),('PEER',peer_mb<<20)]:cmd+=['--env',f'ZFBENCH_{k}_BYTES={v}']
 if trim:
  for k,v in [('CHECK_MS',1000),('WARM_MS',3000),('COLD_MS',10000),('LARGE',2),('SMALL',2)]:cmd+=['--env',f'ZFBENCH_TX_CACHE_TRIM_{k}={v}']
 cmd+=list(extra)
 start=time.monotonic();p=subprocess.run(cmd,capture_output=True,text=True,timeout=255)
 path=ROOT/'runs'/name/'result.json';result=json.loads(path.read_text()) if path.exists() else None
 (ROOT/'records').mkdir(exist_ok=True)
 (ROOT/'records'/f'{name}.json').write_text(json.dumps({'command':cmd,'result':result,'stdout':p.stdout,'stderr':p.stderr,'exit_code':p.returncode,'elapsed_sec':time.monotonic()-start},indent=2))
 print(name, result['ok'] if result else p.stderr, result['errors'] if result else '',flush=True)
 return result
if __name__=='__main__':run(sys.argv[1],int(sys.argv[2]),int(sys.argv[3]),int(sys.argv[4]),int(sys.argv[5]))
