#!/usr/bin/env python3
import concurrent.futures
from run_matrix import run, require, ROOT, SHA
BEFORE='2e579eb50d079e9c5ae10caaeaf54fa68ef15ecf'
common=['--rr-samples','100','--bulk-bytes-per-flow',str(2<<20)]
jobs=[]
for mtu in [1500,512]:
 for binary in ['before','after']:
  args=common+['--binary','/evidence/bin/'+binary,'--source-sha',BEFORE if binary=='before' else SHA['on'],'--idle-secs','0','--low-secs','60','--low-interval-ms','1000']
  jobs.append(('low-rate-'+binary,'on',mtu,0,0,'AB',args,True,True))
for mtu in [1500,512]:
 jobs.append(('cold-after','on',mtu,0,0,'AB',common+['--binary','/evidence/bin/after','--idle-secs','40'],True,True))
 jobs.append(('sustained-after','on',mtu,0,0,'AB',common+['--binary','/evidence/bin/after','--bulk-bytes-per-flow',str(128<<20)],True,False))
 args=common+['--binary','/evidence/bin/after','--flows','8','--idle-count','100']
 for k,v in [('GLOBAL',8<<20),('PORT',4<<20),('PEER',4<<20)]:args+=['--env',f'ZFBENCH_{k}_BYTES={v}']
 jobs.append(('quota-after','on',mtu,0,0,'AB',args,True,False))
(ROOT/'runs').mkdir(exist_ok=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
 for record in ex.map(lambda job:run(*job),jobs):require(record)
