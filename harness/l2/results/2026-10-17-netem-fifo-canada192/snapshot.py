import json,subprocess,sys,time
from pathlib import Path
rows=[]
for cmd in [['uname','-a'],['free','-h'],['df','-h','/'],['ps','-eo','pid,ppid,comm,pcpu,etime'],['ip','-j','link','show'],['ip','netns','list'],['tc','-j','qdisc','show'],['systemctl','show','rc-worker.service','zfc-rig-watch.service','-p','ActiveState','-p','SubState','-p','UnitFileState','-p','MainPID'],['docker','ps','--format','{{.Names}} {{.Status}}']]:
 r=subprocess.run(cmd,capture_output=True,text=True,timeout=15)
 rows.append({'argv':cmd,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
Path(sys.argv[1]).write_text(json.dumps({'time_unix_ns':time.time_ns(),'commands':rows,'proc':{name:Path('/proc',name).read_text() for name in ['stat','softirqs','interrupts','loadavg']}},indent=2)+'\n')
