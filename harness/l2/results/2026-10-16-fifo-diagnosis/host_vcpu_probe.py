import ctypes as C,gzip,json,sys,time
from pathlib import Path
pid=int(sys.argv[1]);out=Path(sys.argv[2]);duration=float(sys.argv[3])
l=C.CDLL('/usr/lib/libSystem.B.dylib',use_errno=True);l.proc_pidinfo.argtypes=[C.c_int,C.c_int,C.c_uint64,C.c_void_p,C.c_int]
class T(C.Structure):
 _fields_=[('user',C.c_uint64),('system',C.c_uint64),('cpu',C.c_int),('policy',C.c_int),('state',C.c_int),('flags',C.c_int),('sleep',C.c_int),('curpri',C.c_int),('priority',C.c_int),('maxpriority',C.c_int),('name',C.c_char*64)]
a=(C.c_uint64*256)();n=l.proc_pidinfo(pid,6,0,a,C.sizeof(a));threads=[]
for tid in a[:n//8]:
 t=T();r=l.proc_pidinfo(pid,5,tid,C.byref(t),C.sizeof(t));name=t.name.decode()
 if r==C.sizeof(t) and '.thread.cpu-' in name:threads.append((tid,int(name.rsplit('-',1)[1])))
assert len(threads)==10,threads
rows=[];end=time.monotonic()+duration
while time.monotonic()<end:
 b=time.monotonic_ns();values=[]
 for tid,cpu in threads:
  t=T();r=l.proc_pidinfo(pid,5,tid,C.byref(t),C.sizeof(t))
  if r!=C.sizeof(t):raise RuntimeError('lost host vCPU identity')
  values.append([cpu,t.user,t.system,t.state])
 rows.append([b,time.monotonic_ns(),values]);time.sleep(.001)
out.write_bytes(gzip.compress(json.dumps({'pid':pid,'threads':threads,'rows':rows,'cpu_times_unit':'nanoseconds','clock':'host time.monotonic_ns'}).encode(),mtime=0))
