#!/usr/bin/env python3
"""Reconstruct complete patches, including untracked common example files."""
import gzip,hashlib,json,pathlib,subprocess
OUT=pathlib.Path(__file__).resolve().parent
records=[]
for label in ['bare','control','gate','bare2','control2','gate2','adaptive','token-control','token-adaptive']:
 m=json.loads((OUT/(label+'-build.json')).read_text());stage=label.rstrip('2');root=pathlib.Path('/workspace/scratch/sched-r2-'+stage)
 original={};manifest_hashes={}
 try:
  for name,digest in m['source_hashes'].items():
   p=root/name;data=p.read_bytes()
   if hashlib.sha256(data).hexdigest()!=digest:
    # The only old-snapshot difference is the documented arrival correction.
    assert label in ['bare','control','gate'] and name=='harness/examples/sched_r2.rs',(label,name)
    original[p]=data;data=data.replace(b'200000',b'150000').replace(b'from_millis(200)',b'from_millis(150)');p.write_bytes(data)
   assert hashlib.sha256(data).hexdigest()==digest,(label,name)
   manifest_hashes[name]=digest
  def patch():
   data=subprocess.check_output(['git','diff','HEAD','--','src','harness'],cwd=root)
   untracked=subprocess.check_output(['git','ls-files','--others','--exclude-standard','--','src','harness'],cwd=root,text=True).splitlines()
   for name in untracked:
    data+=subprocess.run(['git','diff','--no-index','--','/dev/null',name],cwd=root,capture_output=True).stdout
   return data
  entry={'stage':label,'base_commit':m['git_commit'],'prod_sources_match_manifest':True,'patches':[]}
  for kind in (['prod'] if label.startswith('token-') else ['prod','attr']):
   if kind=='attr':
    for name in ['src/conn.rs','src/shard.rs']:
     p=root/name;original.setdefault(p,p.read_bytes());s=p.read_text()
     for needle in ['    fn schedule(', '    fn serve(', '    fn new_data_len(', '    pub(crate) fn wants_tx(']:s=s.replace(needle,'    #[inline(never)]\n'+needle)
     p.write_text(s)
   data=patch();name=label+'-'+kind+'-reproduction.patch.gz'
   with gzip.GzipFile(filename='',mode='wb',mtime=0,fileobj=open(OUT/name,'wb')) as f:f.write(data)
   entry['patches'].append({'kind':kind,'path':name,'uncompressed_sha256':hashlib.sha256(data).hexdigest(),'compressed_sha256':hashlib.sha256((OUT/name).read_bytes()).hexdigest()})
  records.append(entry)
 finally:
  for p,data in original.items():p.write_bytes(data)
# Existing patch snapshots remain as captured; the complete bundles include the new driver.
(OUT/'reproduction-sources.json').write_text(json.dumps({'note':'Complete patches against each declared commit, including untracked common driver files omitted by plain git diff in initial build logging. Original patch snapshots preserved. Diagnostic attr patches add only noinline.','sources':records},indent=2)+'\n')
print(json.dumps({'source_snapshots_verified':len(records),'complete_patches':sum(len(r['patches']) for r in records)}))
