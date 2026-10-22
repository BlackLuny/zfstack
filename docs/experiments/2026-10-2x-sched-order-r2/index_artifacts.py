#!/usr/bin/env python3
"""Checksum every delivery path, caching identical hard-linked archive bytes."""
import hashlib,json,pathlib
out=pathlib.Path(__file__).resolve().parent
cache={};files=[]
for p in sorted(out.rglob('*')):
 if not p.is_file() or '__pycache__' in p.parts or p.suffix=='.pyc':continue
 name=str(p.relative_to(out))
 if name in ['SHA256SUMS','artifact-index.json']:continue
 st=p.stat();key=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
 if key not in cache:
  h=hashlib.sha256()
  with p.open('rb') as f:
   while b:=f.read(1<<20):h.update(b)
  cache[key]=h.hexdigest()
 files.append({'path':name,'bytes':st.st_size,'sha256':cache[key]})
x={'schema':1,'root':'docs/experiments/2026-10-2x-sched-order-r2','scope':'All delivery files except caches and the two index files. SHA256SUMS additionally verifies artifact-index.json. Hard-linked paths are all listed.','paths':len(files),'unique_inodes':len(cache),'logical_bytes':sum(f['bytes'] for f in files),'files':files}
p=out/'artifact-index.json';p.write_text(json.dumps(x,indent=2)+'\n')
(out/'SHA256SUMS').write_text(''.join(f"{f['sha256']}  {f['path']}\n" for f in files)+hashlib.sha256(p.read_bytes()).hexdigest()+'  artifact-index.json\n')
print(json.dumps({'paths':len(files),'logical_bytes':x['logical_bytes'],'unique_inodes':len(cache)}))
