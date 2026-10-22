#!/usr/bin/env python3
"""Apply complete source bundles only in the task-owned replay scratch worktree."""
import gzip,hashlib,json,pathlib,subprocess
OUT=pathlib.Path(__file__).resolve().parent
root=pathlib.Path('/workspace/scratch/sched-r2-replay-check')
assert root.name=='sched-r2-replay-check' and (root/'.git').is_file()
results=[]
for source in json.loads((OUT/'reproduction-sources.json').read_text())['sources']:
 m=json.loads((OUT/(source['stage']+'-build.json')).read_text())
 for patch in source['patches']:
  subprocess.run(['git','reset','--hard',source['base_commit']],cwd=root,check=True,stdout=subprocess.DEVNULL)
  subprocess.run(['git','clean','-fd','--','src','harness'],cwd=root,check=True,stdout=subprocess.DEVNULL)
  data=gzip.decompress((OUT/patch['path']).read_bytes())
  subprocess.run(['git','apply','--allow-empty','--check','-'],input=data,cwd=root,check=True)
  subprocess.run(['git','apply','--allow-empty','-'],input=data,cwd=root,check=True)
  checked=0
  for name,digest in m['source_hashes'].items():
   text=(root/name).read_bytes()
   if patch['kind']=='attr' and name in ['src/conn.rs','src/shard.rs']:
    for needle in ['    fn schedule(', '    fn serve(', '    fn new_data_len(', '    pub(crate) fn wants_tx(']:text=text.replace(b'    #[inline(never)]\n'+needle.encode(),needle.encode())
   assert hashlib.sha256(text).hexdigest()==digest,(source['stage'],patch['kind'],name)
   checked+=1
  results.append({'stage':source['stage'],'kind':patch['kind'],'apply_clean':True,'source_hashes_verified':checked})
subprocess.run(['git','reset','--hard'],cwd=root,check=True,stdout=subprocess.DEVNULL)
subprocess.run(['git','clean','-fd','--','src','harness'],cwd=root,check=True,stdout=subprocess.DEVNULL)
(OUT/'replay-check.json').write_text(json.dumps(results,indent=2)+'\n')
print(json.dumps({'complete_source_bundles_apply_cleanly':len(results)}))
