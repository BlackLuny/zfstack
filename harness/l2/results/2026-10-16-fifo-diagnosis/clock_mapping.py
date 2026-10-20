import subprocess,time,json,sys
from pathlib import Path
code='import time,json,sys\nprint("ready",flush=True)\nfor line in sys.stdin: print(json.dumps([time.monotonic_ns(),time.time_ns()]),flush=True)'
p=subprocess.Popen(['docker','exec','-i','zfstack-pr75-diag','python3','-u','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
assert p.stdout.readline().strip()=='ready';rows=[]
for _ in range(40):
 b=time.monotonic_ns();p.stdin.write('ping\n');p.stdin.flush();g,real=json.loads(p.stdout.readline());a=time.monotonic_ns();rows.append({'host_before':b,'host_after':a,'guest_mono':g,'guest_real':real,'offset_low':b-g,'offset_high':a-g})
p.stdin.close();p.wait();Path(sys.argv[1]).write_text(json.dumps(rows,indent=2)+'\n');print('mapping interval ms',(min(x['offset_high'] for x in rows)-max(x['offset_low'] for x in rows))/1e6)
