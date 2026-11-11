"""One fixed counter-response calibration, outside all FIFO timing attempts."""
import json
import signal
import subprocess as S
import sys
import time
from pathlib import Path

P = Path(__file__).resolve().parent
out = P / "accounting-calibration"
out.mkdir()
container = "zfstack-pr75-sched"


def mapping(label):
    code = ('import json,sys,time\nprint("ready",flush=True)\n'
            'for line in sys.stdin: print(json.dumps([time.monotonic_ns(),time.time_ns()]),flush=True)')
    proc = S.Popen(["docker", "exec", "-i", container, "python3", "-u", "-c", code],
                   stdin=S.PIPE, stdout=S.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    rows = []
    for _ in range(80):
        begin = time.monotonic_ns()
        proc.stdin.write("p\n")
        proc.stdin.flush()
        guest, real = json.loads(proc.stdout.readline())
        end = time.monotonic_ns()
        rows.append({"host_before": begin, "host_after": end, "guest_mono": guest,
                     "guest_real": real, "offset_low": begin-guest, "offset_high": end-guest})
    proc.stdin.close()
    assert proc.wait() == 0
    (out / (label + ".json")).write_text(json.dumps(rows, indent=2)+"\n")


vm = [int(line.split()[0]) for line in S.check_output(["ps", "-axo", "pid,comm"], text=True).splitlines()
      if line.strip().endswith("/com.apple.Virtualization.VirtualMachine")]
assert len(vm) == 1
mapping("clock-before")
probe = S.Popen([sys.executable, str(P / "host_probe.py"), str(vm[0]), str(out / "host-vcpu.json.gz")],
                stdout=S.PIPE, text=True)
(out / "observer-ready.json").write_text(probe.stdout.readline())
code = '''import json,os,resource,time
os.sched_setaffinity(0,{1})
rows=[]
for phase in ["idle","spin","idle","spin","idle"]:
 b=time.monotonic_ns(); c=time.process_time_ns(); r=resource.getrusage(resource.RUSAGE_SELF)
 if phase=="idle": time.sleep(.5); value=None
 else:
  value=0
  for i in range(5000000): value=(value+i)&0xffffffff
 a=time.monotonic_ns(); d=time.process_time_ns(); s=resource.getrusage(resource.RUSAGE_SELF)
 rows.append({"phase":phase,"begin_ns":b,"end_ns":a,"guest_cpu_ns":d-c,"iterations":5000000 if phase=="spin" else 0,"result":value,"switches":[s.ru_nvcsw-r.ru_nvcsw,s.ru_nivcsw-r.ru_nivcsw]})
print(json.dumps(rows,indent=2))
'''
result = S.run(["docker", "exec", container, "python3", "-c", code], capture_output=True, check=True)
(out / "guest-phases.json").write_bytes(result.stdout)
(out / "guest.stderr").write_bytes(result.stderr)
probe.send_signal(signal.SIGTERM)
assert probe.wait(timeout=20) == 0
mapping("clock-after")
print("accounting calibration complete")
