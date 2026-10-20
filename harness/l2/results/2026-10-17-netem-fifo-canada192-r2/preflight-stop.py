import json,os,subprocess,time
from pathlib import Path
T=Path(__file__).resolve().parent
record={}
def command(argv):
 r=subprocess.run(argv,capture_output=True,text=True,timeout=30)
 return {'argv':argv,'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
def sample():
 rows={}
 for p in Path('/proc').glob('[0-9]*/stat'):
  try:
   text=p.read_text(); fields=text[text.rfind(')')+2:].split();rows[int(p.parent.name)]=(text[text.find('(')+1:text.rfind(')')],int(fields[11])+int(fields[12]))
  except (OSError,ValueError,IndexError):pass
 return rows
try:
 subprocess.run(['python3',str(T/'snapshot.py'),str(T/'preflight/host-before.json')],check=True)
 rows=[command(c) for c in [['hostname'],['systemd-detect-virt'],['lscpu'],['python3','--version'],['pkg-config','--modversion','openssl'],['clang','--version'],['ls','-l','/usr/lib/llvm-14/lib/libclang.so']]]
 (T/'preflight/identity-tools.json').write_text(json.dumps(rows,indent=2)+'\n')
 subprocess.run(['python3',str(T/'services.py'),'stop'],check=True)
 for proc in ['cargo','rustc']:assert subprocess.run(['pgrep','-x',proc],stdout=subprocess.DEVNULL).returncode==1,proc
 scopes=command(['systemctl','list-units','--type=scope','--state=active','--no-legend','--plain']);assert 'rig-watch-' not in scopes['stdout']
 before=sample();start=time.monotonic();time.sleep(10);after=sample();elapsed=time.monotonic()-start
 hz=os.sysconf('SC_CLK_TCK');cpu=[]
 for pid,(name,ticks) in after.items():
  if pid in before:cpu.append({'pid':pid,'comm':name,'cpu_percent':round((ticks-before[pid][1])/hz/elapsed*100,2)})
 record={'elapsed_seconds':elapsed,'top_cpu':sorted(cpu,key=lambda v:v['cpu_percent'],reverse=True)[:20],
         'namespaces':command(['ip','netns','list']),'active_scopes':scopes,
         'lease_entries':[p.name for p in Path('/var/lock/zfstack-qa').glob('*')],
         'loadavg':Path('/proc/loadavg').read_text()}
 (T/'preflight/quiet-cpu-sample.json').write_text(json.dumps(record,indent=2)+'\n')
 print(json.dumps({k:v for k,v in record.items() if k!='active_scopes'},indent=2),flush=True)
except BaseException:
 if (T/'service-control.json').exists():
  code=subprocess.run(['python3',str(T/'services.py'),'restore']).returncode
  if code==0:subprocess.run(['systemctl','stop','zfstack-pr75-canada02-restore-20261017.timer'])
 raise
