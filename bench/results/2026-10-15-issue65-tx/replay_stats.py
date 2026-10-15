"""Recompute statistics in a new directory without changing the evidence."""
from pathlib import Path
import sys,tarfile,subprocess,shutil
r=Path(__file__).resolve().parent;dest=Path(sys.argv[1]).resolve()
assert not dest.exists(),'Choose a new output directory';dest.mkdir(parents=True)
with tarfile.open(r/'raw-evidence.tar.gz') as t:
 for m in t.getmembers():
  p=Path(m.name);assert not p.is_absolute() and '..' not in p.parts and not m.issym() and not m.islnk()
 t.extractall(dest, **({"filter":"data"} if sys.version_info >= (3,12) else {}))
work=dest/'remote'
for f in ['analyze.py','network_current_quota_analyze.py','qualify.py','allocation_table.py']:
 shutil.copy2(r/f,work/f)
tags=['current-quota-preheat','current-quota-push-window1','current-quota-write-window1','current-quota-push-window2','current-quota-write-window2','current-quota-pump-guards','current-quota-f56-context','current-quota-write-tiny-long-window1','current-quota-write-tiny-long-window2','current-quota-pump-long-window1','current-quota-pump-long-window2']
subprocess.run([sys.executable,str(work/'analyze.py'),*tags],check=True,stdout=subprocess.DEVNULL)
subprocess.run([sys.executable,str(work/'network_current_quota_analyze.py')],check=True,stdout=subprocess.DEVNULL)
subprocess.run([sys.executable,str(work/'network_current_quota_analyze.py'),'network-connect-confirmation'],check=True,stdout=subprocess.DEVNULL)
subprocess.run([sys.executable,str(work/'network_current_quota_analyze.py'),'network-bulk-confirmation'],check=True,stdout=subprocess.DEVNULL)
subprocess.run([sys.executable,str(work/'qualify.py')],check=True,stdout=subprocess.DEVNULL)
subprocess.run([sys.executable,str(work/'allocation_table.py')],check=True,stdout=subprocess.DEVNULL)
for f in ['allocation-table.csv','allocation-summary.json','summary.json','network-summary.json','network-connect-confirmation-summary.json','network-bulk-confirmation-summary.json','qualification.json','cpu-table.csv']:
 assert (work/f).read_bytes()==(r/f).read_bytes(),f+' differs'
print('Exact statistical replay matches all summaries, qualification, and CPU table.')
