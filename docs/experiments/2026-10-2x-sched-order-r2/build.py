#!/usr/bin/env python3
"""Build isolated immutable production and noinline attribution binaries."""
import gzip,hashlib,json,os,pathlib,subprocess,sys
OUT=pathlib.Path(__file__).resolve().parent
BIN=pathlib.Path(os.environ.get('ZF_R2_BIN_DIR','/workspace/scratch/sched-r2-bin'));BIN.mkdir(parents=True,exist_ok=True)
stages=sys.argv[1:] or ['control','gate','bare']
for spec in stages:
 root_stage,_,label=spec.partition(':');stage=label or root_stage
 root=pathlib.Path('/workspace/scratch/sched-r2-'+root_stage)
 if any((BIN/(stage+'-'+k)).exists() for k in ['prod','attr']):raise SystemExit('immutable binaries exist for '+stage)
 env=os.environ.copy();env.update(CARGO_TARGET_DIR='/workspace/scratch/sched-r2-target-'+stage,CARGO_PROFILE_RELEASE_LTO='fat',CARGO_PROFILE_RELEASE_CODEGEN_UNITS='1',CARGO_PROFILE_RELEASE_PANIC='abort',CARGO_PROFILE_RELEASE_DEBUG='1')
 sources=[p for folder in ['src','harness/src','harness/examples'] for p in (root/folder).rglob('*.rs')]+[root/'Cargo.lock',root/'Cargo.toml',root/'harness/Cargo.toml']
 record={'stage':stage,'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),'source_hashes':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},'build_env':{k:v for k,v in env.items() if k.startswith('CARGO_PROFILE')},'binaries':[]}
 patch=subprocess.check_output(['git','diff','HEAD','--','src','harness'],cwd=root)
 for untracked in subprocess.check_output(['git','ls-files','--others','--exclude-standard','--','src','harness'],cwd=root,text=True).splitlines():
  patch+=subprocess.run(['git','diff','--no-index','--','/dev/null',untracked],cwd=root,capture_output=True).stdout
 with gzip.open(OUT/(stage+'-source.patch.gz'),'wb') as f:f.write(patch)
 original={}
 for kind in ['prod','attr']:
  if kind=='attr':
   for name in ['src/conn.rs','src/shard.rs']:
    p=root/name;s=p.read_text();original[p]=s
    for needle in ['    fn schedule(', '    fn serve(', '    fn new_data_len(', '    pub(crate) fn wants_tx(']:
     s=s.replace(needle,'    #[inline(never)]\n'+needle)
    p.write_text(s)
   record['attribution_only_diff']='inline(never) for schedule, serve, wants_tx and new_data_len when present; primary gates use prod only'
   with gzip.open(OUT/(stage+'-attribution-source.patch.gz'),'wb') as f:f.write(subprocess.check_output(['git','diff','HEAD','--','src','harness'],cwd=root))
  log=OUT/'logs'/(stage+'-'+kind+'-build.log')
  with log.open('w') as f:
   run=subprocess.run(['cargo','build','--locked','--release','-p','zfharness','--example','sched_r2'],cwd=root,env=env,stdout=f,stderr=subprocess.STDOUT)
  if run.returncode:raise SystemExit(f'build failed {stage} {kind}; {log}')
  target=BIN/(stage+'-'+kind)
  if target.exists():raise SystemExit('refusing to overwrite '+str(target))
  target.write_bytes((pathlib.Path(env['CARGO_TARGET_DIR'])/'release/examples/sched_r2').read_bytes());target.chmod(0o755)
  record['binaries'].append({'kind':kind,'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
  print(stage,kind,'built',flush=True)
 for p,s in original.items():p.write_text(s)
 (OUT/(stage+'-build.json')).write_text(json.dumps(record,indent=2)+'\n')
