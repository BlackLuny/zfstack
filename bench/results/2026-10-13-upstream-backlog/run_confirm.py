from run_pressure import run
TRIM=[]
for k,v in [('CHECK',1000),('WARM',3000),('COLD',10000),('LARGE',2),('SMALL',2)]:TRIM+=['--env',f'ZFBENCH_TX_CACHE_TRIM_{k}_MS={v}' if k in ['CHECK','WARM','COLD'] else f'ZFBENCH_TX_CACHE_TRIM_{k}={v}']
TRIM+=['--env','ZFBENCH_PACKET_POOL_TRIM_IDLE_MS=3000','--env','ZFBENCH_PACKET_POOL_TRIM_TARGET=0']
failed=[]
for mtu,mb,idle in [(1500,8,0),(512,8,0),(1500,64,1000),(512,64,1000)]:
 for pair in range(3):
  for variant in ['main','packetpool']:
   result=run('confirmed',variant,mtu,mb,idle,pair,extra=TRIM if variant=='packetpool' else [])
   if not result or not result['ok']:failed.append((variant,mtu,mb,idle,pair))

if failed:raise RuntimeError(failed)
