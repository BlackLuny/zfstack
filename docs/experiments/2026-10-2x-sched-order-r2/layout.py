#!/usr/bin/env python3
import json,pathlib,re,subprocess,os
out=pathlib.Path(__file__).resolve().parent
sizes={}
for stage in ['bare','control','gate','drr']:
 root=pathlib.Path('/workspace/scratch/sched-r2-'+stage);p=root/'src/shard.rs';original=p.read_text()
 code='''\n#[cfg(test)]\nmod r2_memory_layout { use super::*; #[test] fn measure() { eprintln!("R2_LAYOUT Conn={} Slot={} Sched={} PeerReady={} Drr={} Iface={} StackConfig={} ShardStats={}",std::mem::size_of::<Conn>(),std::mem::size_of::<Slot>(),std::mem::size_of::<Sched>(),std::mem::size_of::<VecDeque<(u32,u32)>>(),std::mem::size_of::<Drr>(),std::mem::size_of::<Iface>(),std::mem::size_of::<StackConfig>(),std::mem::size_of::<ShardStats>()); } }\n'''
 try:
  p.write_text(original+code.replace('size_of::<VecDeque<(u32,u32)>>()', 'size_of::<PeerReady>()') if stage=='drr' else original+code)
  env=os.environ.copy();env['CARGO_TARGET_DIR']='/workspace/scratch/sched-r2-layout-'+stage
  log=out/'logs'/(stage+'-layout.log')
  with log.open('w') as f:r=subprocess.run(['cargo','test','--locked','--lib','--features','test-peer','r2_memory_layout','--','--nocapture'],cwd=root,env=env,stdout=f,stderr=subprocess.STDOUT)
  assert r.returncode==0,stage
  line=next(x for x in log.read_text().splitlines() if x.startswith('R2_LAYOUT'))
  sizes[stage]={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',line)}
 finally:p.write_text(original)
line=next(x for x in (out/'logs/adaptive-tests-v2.log').read_text().splitlines() if x.startswith('R2_LAYOUT'))
sizes['adaptive']={k:int(v) for k,v in re.findall(r'(\w+)=(\d+)',line)}
sizes['drr_without_adaptive']=sizes.pop('drr')
sizes['notes']=['drr_without_adaptive measured from the c9 cherry-pick checkout; no adaptive field in Conn/Slot/PeerReady.', 'A uses the original e21 observation state: Slot +8B/connection and ShardStats +48B/shard relative to main. No packed sparse commit is imported.','B adds48B per iface versus c9; no new connection or peer state. Adaptive configuration adds48B/shard. DRR itself changes peer value32->48B.','Existing uncharged HashMap/container capacity is not RSS or ledger peak; real steady RSS requires L1.']
(out/'memory-layout.json').write_text(json.dumps(sizes,indent=2)+'\n')
print(json.dumps(sizes))
