"""User-authorized temporary stop and restoration of compile/rig services only."""
import json,subprocess,time,sys,re
from pathlib import Path
T=Path(__file__).resolve().parent
STATE=T/'service-control.json'
NAMES=['rc-worker.service','zfc-rig-watch.service']
def run(argv):
 p=subprocess.run(argv,capture_output=True,text=True,timeout=660)
 return {'argv':argv,'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr,'finished_ns':time.time_ns()}
def info(name):
 p=run(['systemctl','show',name,'-p','Id','-p','ActiveState','-p','SubState','-p','MainPID','-p','UnitFileState','-p','ControlGroup'])
 assert p['returncode']==0,p
 return dict(l.split('=',1) for l in p['stdout'].splitlines() if '=' in l)
def save(data):STATE.write_text(json.dumps(data,indent=2)+'\n')
if sys.argv[1]=='stop':
 assert not STATE.exists()
 data={'authorized_by_user':True,'started_ns':time.time_ns(),'original':{n:info(n) for n in NAMES},'operations':[]}
 scopes=run(['systemctl','list-units','--type=scope','--state=active','--no-legend','--plain'])
 data['rig_scopes']=[l.split()[0] for l in scopes['stdout'].splitlines() if l.split() and re.fullmatch(r'rig-watch-[a-f0-9]+\.scope',l.split()[0])];save(data)
 # Backup recovery is owned by this task. Normal completion restores sooner.
 backup=run(['systemd-run','--unit=zfstack-pr75-canada-restore-20261017','--on-active=3h','/usr/bin/python3',str(Path(__file__).resolve()),'restore']);data['operations'].append(backup);save(data);assert backup['returncode']==0
 for n in ['zfc-rig-watch.service',*data['rig_scopes'],'rc-worker.service']:
  result=run(['systemctl','stop',n]);data['operations'].append(result);save(data)
  if result['returncode']:raise RuntimeError('stop failed; preserve state and restore')
 data['stopped']={n:info(n) for n in NAMES};save(data)
 assert all(v['ActiveState']=='inactive' for v in data['stopped'].values())
 print(json.dumps({'stopped':data['stopped'],'rig_scopes':data['rig_scopes']}),flush=True)
elif sys.argv[1]=='restore':
 data=json.loads(STATE.read_text());records=[]
 for n in NAMES:
  if data['original'][n]['ActiveState']=='active' and info(n)['ActiveState']!='active':records.append(run(['systemctl','start',n]))
 data.setdefault('restore_operations',[]).extend(records);data['restored']={n:info(n) for n in NAMES};data['restored_ns']=time.time_ns();save(data)
 assert all(data['restored'][n]['ActiveState']==data['original'][n]['ActiveState'] and data['restored'][n]['UnitFileState']==data['original'][n]['UnitFileState'] for n in NAMES)
 print(json.dumps({'restored':data['restored']}),flush=True)
