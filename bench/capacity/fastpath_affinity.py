#!/usr/bin/env python3
"""Audit controlled placement without rebuilding either measured executable."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def descendants(pid):
    # Some managed kernels omit /proc/<pid>/task/<tid>/children. Build the
    # tree from PPIDs without inspecting unrelated processes' command lines.
    tree={}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:
            fields=(entry/'stat').read_text().rsplit(')',1)[1].split()
            tree.setdefault(int(fields[1]),[]).append(int(entry.name))
        except (FileNotFoundError,ProcessLookupError):
            continue
    pending=[pid];seen=set()
    while pending:
        p=pending.pop()
        if p in seen:continue
        seen.add(p);pending.extend(tree.get(p,[]))
    return seen


def run(ledger, resources, binary, args):
    start=time.monotonic();cpu=time.process_time();threads={};errors=[];samples=[]
    os.sched_setaffinity(0,{4})
    process=subprocess.Popen(['taskset','-c','2-3','python3','/work/bench/tail69_exec.py',resources,binary]+args,start_new_session=True)
    def interrupted(sig, frame):
        raise InterruptedError(f'placement observer received signal {sig}')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        while process.poll() is None:
            elapsed=time.monotonic()-start;sample=[]
            for pid in sorted(descendants(process.pid)):
                try:
                    argv=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                    proxy=b'--tun-proxy' in argv
                    for task in Path(f'/proc/{pid}/task').iterdir():
                        tid=int(task.name);comm=(task/'comm').read_text().strip()
                        wanted={0} if proxy and comm=='zfc-driver' else {1} if proxy else {2,3}
                        key=str(tid);mask=os.sched_getaffinity(tid)
                        if key not in threads:
                            threads[key]=dict(pid=pid,tid=tid,name=comm,proxy=proxy,requested=sorted(wanted),first_seen_sec=elapsed,observations=0)
                            if proxy and elapsed>2:errors.append(f'proxy thread {tid} first seen after 2 s')
                        threads[key].update(name=comm,proxy=proxy,requested=sorted(wanted))
                        if mask!=wanted:
                            if elapsed>2:errors.append(f'affinity changed after 2 s: {tid} {sorted(mask)}')
                            os.sched_setaffinity(tid,wanted)
                        actual=sorted(os.sched_getaffinity(tid))
                        if actual!=sorted(wanted):errors.append(f'affinity assignment failed: {tid}')
                        threads[key]['observations']+=1
                        threads[key]['last_observed_sec']=elapsed
                        threads[key]['last_affinity']=actual
                        sample.append(dict(tid=tid,actual=actual))
                except (FileNotFoundError,ProcessLookupError):
                    continue
            if elapsed>2:samples.append(dict(elapsed_sec=elapsed,threads=sample))
            time.sleep(.02 if elapsed<2 else .20)
        rc=process.returncode
    except (OSError,InterruptedError) as error:
        errors.append(str(error));rc=1
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL);process.wait()
        roles={t['name'] for t in threads.values() if t['proxy']}
        if not {'zfc-driver','zfc-tun-rx'}<=roles:errors.append('required driver/TUN reader not observed')
        if not samples:errors.append('no post-warmup affinity samples')
        result=dict(valid=rc==0 and not errors,errors=sorted(set(errors)),returncode=rc,
            policy=dict(observer=[4],harness=[2,3],driver=[0],reader_and_other_proxy=[1],deadline_sec=2),
            threads=threads,samples=samples,observer_cpu_sec=time.process_time()-cpu,wall_sec=time.monotonic()-start)
        with Path(ledger).open('x') as f:json.dump(result,f,indent=2);f.write('\n')
    return rc if rc else int(bool(errors))


if __name__=='__main__':
    raise SystemExit(run(sys.argv[1],sys.argv[2],sys.argv[3],sys.argv[4:]))
