"""Collect task-owned evidence; formal runtime keys/configs stay excluded."""
import hashlib,json,subprocess,tarfile,time
from pathlib import Path
P=Path(__file__).resolve().parent
subprocess.run(['python3',str(P/'snapshot.py'),str(P/'preflight/host-final.json')],check=True)
selected=[]
for name in ['gate','preflight','evidence']:
 for f in sorted((P/name).rglob('*')):
  if f.is_file() and not f.is_symlink():selected.append((f,str(f.relative_to(P))))
for name in ['services.py','service-control.json','run-gate.py','gate-driver.log','gate-launch.json','gate-launch-result.json','build-and-formal-plan.json','build-input-transfer.json','tool-inputs.json','prepare_tools.py','build_and_run.py','run-build.py','snapshot.py','collect_evidence.py','tools-driver.log','build-formal-driver.log','build-driver-exit.json','preflight-stop.py','source-transfer.json','orchestrator.log','post-restoration-check.json']:
 f=P/name
 if f.is_file():selected.append((f,name))
for f in sorted((P/'build').glob('*/*')):
 if f.is_file() and f.suffix in ['.json','.log','.lock']:selected.append((f,str(f.relative_to(P))))
index=[{'path':name,'size_bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()} for f,name in selected]
with (P/'collected-artifacts.tar.gz').open('xb') as stream,tarfile.open(fileobj=stream,mode='w:gz') as t:
 for f,name in selected:t.add(f,arcname=name,recursive=False)
(P/'collected-artifacts.index.json').write_text(json.dumps({'files':index},indent=2)+'\n')
print('collected',len(index),'files', (P/'collected-artifacts.tar.gz').stat().st_size,'bytes')
