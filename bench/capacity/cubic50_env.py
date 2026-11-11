#!/usr/bin/env python3
"""Observe native-stack placement and scheduling without changing its binary."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

from fastpath_affinity import descendants

ROLES={'zfb-stack':0,'zfb-link-up':1,'zfb-link-down':2}


def parse_stat(text):
    fields=text.rsplit(')',1)[1].split()
    return dict(ppid=int(fields[1]),user_ticks=int(fields[11]),system_ticks=int(fields[12]),
        start_ticks=int(fields[19]),last_cpu=int(fields[36]))


def desired_mask(name,placement):
    return [0,1,2,3] if placement=='original' else [ROLES.get(name,3)]


def sample_thread(task):
    values=parse_stat((task/'stat').read_text());values['affinity']=sorted(os.sched_getaffinity(int(task.name)))
    path=task/'schedstat'
    if path.exists():
        fields=path.read_text().split()
        values['schedstat']=dict(runtime_ns=int(fields[0]),runqueue_ns=int(fields[1]),timeslices=int(fields[2]))
    else:values['schedstat']=None
    status={}
    for line in (task/'status').read_text().splitlines():
        key,sep,value=line.partition(':')
        if sep and key in ['voluntary_ctxt_switches','nonvoluntary_ctxt_switches']:status[key]=int(value.strip())
    values['context_switches']=status
    migrations={}
    path=task/'sched'
    if path.exists():
        for line in path.read_text().splitlines():
            key,sep,value=line.partition(':');key=key.strip()
            if sep and key in ['se.nr_migrations','nr_migrations','nr_switches','nr_voluntary_switches','nr_involuntary_switches']:
                migrations[key]=int(value.strip().split()[0])
    values['scheduler_counts']=migrations
    return values


def run(placement,ledger,resources,binary,args):
    start=time.monotonic();cpu=time.process_time();threads={};errors=[];samples=[];roles={}
    os.sched_setaffinity(0,{4})
    process=subprocess.Popen(['taskset','-c','0-3','python3','/work/bench/tail69_exec.py',resources,binary]+args,start_new_session=True)
    def interrupted(sig,frame):raise InterruptedError(f'observer received signal {sig}')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        while process.poll() is None:
            elapsed=time.monotonic()-start;sample=[]
            for pid in sorted(descendants(process.pid)):
                for task in sorted(Path(f'/proc/{pid}/task').glob('[0-9]*')):
                    try:
                        tid=int(task.name);name=(task/'comm').read_text().strip();wanted=desired_mask(name,placement)
                        before=sample_thread(task);key=str(tid)
                        if name in ROLES and name not in roles:roles[name]=dict(tid=tid,first_seen_sec=elapsed)
                        if placement=='fixed' and before['affinity']!=wanted:
                            if elapsed>2:errors.append(f'late or changed affinity: {tid} {name}')
                            os.sched_setaffinity(tid,set(wanted))
                        observed=sample_thread(task)
                        if elapsed>2 and observed['affinity']!=wanted:errors.append(f'actual mask differs: {tid} {name}')
                        if key not in threads:
                            threads[key]=dict(pid=pid,tid=tid,first_seen_sec=elapsed,first=observed,observations=0,sampled_cpu_changes=0)
                        t=threads[key]
                        if 'last' in t and t['last']['last_cpu']!=observed['last_cpu']:t['sampled_cpu_changes']+=1
                        t.update(name=name,requested=wanted,last_seen_sec=elapsed,last=observed)
                        t['observations']+=1
                        sample.append(dict(tid=tid,name=name,**observed))
                    except (FileNotFoundError,ProcessLookupError):
                        continue
            if elapsed>2:samples.append(dict(elapsed_sec=elapsed,threads=sample))
            time.sleep(.02 if elapsed<2 else .5)
        rc=process.returncode
    except (OSError,InterruptedError,ValueError) as error:
        errors.append(str(error));rc=1
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
        if set(roles)!=set(ROLES):errors.append('required native driver/link roles not observed')
        if any(r['first_seen_sec']>2 for r in roles.values()):errors.append('required role first observed after 2 s')
        if not samples:errors.append('no post-assignment observations')
        result=dict(valid=rc==0 and not errors,returncode=rc,errors=sorted(set(errors)),placement=placement,
            required_roles=roles,threads=threads,samples=samples,observer_affinity=sorted(os.sched_getaffinity(0)),
            observer_cpu_sec=time.process_time()-cpu,wall_sec=time.monotonic()-start,clock_ticks=os.sysconf('SC_CLK_TCK'),
            scope='Thread counters cover observed lifetimes, not exact benchmark windows; sampled CPU changes are not exact migration counts.')
        with Path(ledger).open('x') as f:json.dump(result,f,indent=2);f.write('\n')
    return rc if rc else int(bool(errors))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('placement',choices=['original','fixed']);p.add_argument('ledger');p.add_argument('resources');p.add_argument('binary');p.add_argument('args',nargs=argparse.REMAINDER)
    a=p.parse_args();raise SystemExit(run(a.placement,a.ledger,a.resources,a.binary,a.args))
