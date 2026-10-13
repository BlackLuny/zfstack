#!/usr/bin/env python3
"""Repeat the failing large-idle-population case; include main with budget-only host patch."""
import json
from run_matrix import run
for pair in range(3):
 variants=['main','off','on'] if pair%2==0 else ['on','off','main']
 for position,variant in enumerate(variants):
  extra=['--flows','64','--idle-count','1000','--rr-samples','100','--bulk-bytes-per-flow',str(1<<20)]
  if variant=='main':
   flags={'rust':'1.94.1','profile':'release (default Cargo settings)','features':['zfstack'],'diagnostic_patch':'main-pressure64.patch: only budget ceilings changed, fixed 64/32/32 MiB'}
   extra+=['--binary','/evidence/bin/main-pressure64','--build-flags',json.dumps(flags)]
  else:
   for k,v in [('GLOBAL',64<<20),('PORT',32<<20),('PEER',32<<20)]:extra+=['--env',f'ZFBENCH_{k}_BYTES={v}']
  run('pressure-confirm64',variant,512,pair,position,'AB' if pair%2==0 else 'BA',extra,True)
