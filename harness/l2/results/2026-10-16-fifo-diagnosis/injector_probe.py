"""Diagnostic-only shim; original injector implementation is copied unchanged."""
import ctypes, gzip, json, os, resource, time
from pathlib import Path
import fifo_injector_impl as I
ctypes.CDLL(None).prctl(15, b'qa75-inject', 0, 0, 0)
events=[]
trace=Path('/tracing/instances/qa75/trace_marker')
def stamp():
 r=resource.getrusage(resource.RUSAGE_THREAD)
 return [time.monotonic_ns(),time.thread_time_ns(),r.ru_nvcsw,r.ru_nivcsw]
def event(label,b,a,extra=None):
 if a[0]-b[0]>1_000_000 or label=='command':
  if len(events)<100000: events.append({'op':label,'begin':b,'end':a,'extra':extra})
def mark(s):
 try: trace.write_text(s+'\n')
 except OSError: pass
for name in ['emit','admit','read_batch']:
 old=getattr(I.Injector,name)
 def wrapped(self,*a,_old=old,_name=name,**kw):
  b=stamp(); first=self.seq; queued=len(self.fifo); head=self.fifo[0][0] if self.fifo else None
  try:return _old(self,*a,**kw)
  finally:event(_name,b,stamp(),{'seq_before':first,'seq_after':self.seq,'queued':queued,'head_due':head})
 setattr(I.Injector,name,wrapped)
old_epoll=I.select.epoll
class Poll:
 def __init__(self,*a,**kw):self.inner=old_epoll(*a,**kw)
 def __getattr__(self,n):return getattr(self.inner,n)
 def poll(self,*a,**kw):
  b=stamp()
  try:return self.inner.poll(*a,**kw)
  finally:event('epoll',b,stamp(),{'timeout':a[0] if a else None})
I.select.epoll=Poll
old_command=I.command
def command(args,**kw):
 mutation=('del' in args or 'replace' in args or 'add' in args) and args[0]=='tc'
 before=None
 if mutation:
  before=old_command(['ip','-s','-s','-j','link','show','dev','eth0']);mark('before '+ ' '.join(args))
 b=stamp()
 try:return old_command(args,**kw)
 finally:
  a=stamp()
  if mutation:
   mark('after '+' '.join(args));after=old_command(['ip','-s','-s','-j','link','show','dev','eth0'])
   event('command',b,a,{'argv':args,'link_before':json.loads(before),'link_after':json.loads(after)})
I.command=command
old_install=I.Injector.install
class Sender:
 def __init__(self,s):self.s=s
 def __getattr__(self,n):return getattr(self.s,n)
 def send(self,data):
  b=stamp()
  try:return self.s.send(data)
  finally:event('send',b,stamp(),{'bytes':len(data)})
def install(self):
 old_install(self);self.sender=Sender(self.sender);mark('injector installed')
I.Injector.install=install
try:I.main()
finally:
 spec=json.loads(Path(__import__('sys').argv[2]).read_text())
 p=Path(spec['report']).parent/'diagnostic-events.json.gz'
 p.write_bytes(gzip.compress(json.dumps({'clock_fields':['monotonic_ns','thread_cpu_ns','voluntary_switches','involuntary_switches'],'events':events}).encode(),mtime=0))
