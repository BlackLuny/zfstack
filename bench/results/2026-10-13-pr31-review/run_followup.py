#!/usr/bin/env python3
"""Functional diagnostics only; no results from here enter paired timing statistics."""
import json
from run_matrix import run, require

for mtu in [1500,512]:
 require(run('final-smoke','on',mtu,0,0,'AB',['--rr-samples','100','--bulk-bytes-per-flow',str(1<<20)]))
 for position,variant in enumerate(['off','on']):
  require(run('sustained',variant,mtu,0,position,'AB',['--rr-samples','100','--bulk-bytes-per-flow',str(512<<20)],True))
 flags={'rust':'1.94.1','profile':'release (default Cargo settings)','features':['zfstack'],'diagnostic_patch':'main-pressure.patch: only budget ceilings changed, fixed 8/4/4 MiB'}
 run('pressure-main-fixed-budget','main',mtu,0,0,'AB',['--binary','/evidence/bin/main-pressure','--build-flags',json.dumps(flags),'--flows','64','--rr-samples','100','--bulk-bytes-per-flow',str(1<<20)],True)
