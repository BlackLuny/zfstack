import json,os,subprocess,time
from pathlib import Path
P=Path(__file__).resolve().parent
record={'started_unix_ns':time.time_ns()}
try:
 assert json.loads((P/'gate/gate.json').read_text())['gate']=='PASS'
 for unit in ['rc-worker.service','zfc-rig-watch.service']:
  assert subprocess.run(['systemctl','is-active','--quiet',unit]).returncode!=0
 for label,script in [('tools','prepare_tools.py'),('build-formal','build_and_run.py')]:
  with (P/(label+'-driver.log')).open('xb') as log:
   code=subprocess.run(['python3',str(P/script)],cwd=P,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),stdout=log,stderr=subprocess.STDOUT).returncode
  record[label+'_returncode']=code
  if code: raise RuntimeError(label+' failed; no retry')
except BaseException as e:
 record['error']=repr(e)
finally:
 record['restore_returncode']=subprocess.run(['python3',str(P/'services.py'),'restore']).returncode
 if record['restore_returncode']==0:subprocess.run(['systemctl','stop','zfstack-pr75-canada-restore-20261017.timer'])
 record['finished_unix_ns']=time.time_ns()
 (P/'build-driver-exit.json').write_text(json.dumps(record,indent=2)+'\n')
 print(json.dumps(record,indent=2),flush=True)
