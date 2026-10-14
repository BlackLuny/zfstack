#!/usr/bin/env python3
"""Pipelined UDP echo alongside L2 TCP load; validates every offered datagram."""
import argparse,json,select,socket,struct,time
from pathlib import Path
from load import pattern,quantiles,wait_until

def run(a):
    finish=a.start+a.baseline+a.loaded+a.recovery
    pending={}; samples=[]; errors=[]; seq=0; received=0
    def phase(t):
        return 'baseline' if t<a.start+a.baseline else 'loaded' if t<a.start+a.baseline+a.loaded else 'recovery'
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.connect((a.host,a.port));sock.setblocking(False)
        wait_until(a.start);next_send=a.start
        while time.monotonic()<finish or pending:
            now=time.monotonic()
            if now<finish and now>=next_send:
                data=struct.pack('!II',a.flow,seq)+pattern(a.flow)*4
                sock.send(data);pending[seq]=(data,now,phase(now));seq+=1
                next_send=max(next_send+.01,now+.001)
            ready,_,_=select.select([sock],[],[],.002)
            if ready:
                data=sock.recv(2048);end=time.monotonic()
                if len(data)<8:
                    errors.append('short datagram');break
                flow,index=struct.unpack('!II',data[:8]);item=pending.pop(index,None)
                if flow!=a.flow or item is None or item[0]!=data:
                    errors.append('unexpected/corrupt datagram');break
                received+=1;samples.append({'phase':item[2],'seq':index,'at':item[1],'latency_ms':(end-item[1])*1000})
            expired=[i for i,(_,t,_) in pending.items() if time.monotonic()-t>a.request_timeout]
            for i in expired:
                pending.pop(i);errors.append(f'datagram timeout seq={i}')
    result={'peer':a.flow,'sent':seq,'received':received,'bytes':received*(8+1024),'errors':errors,'samples':samples,
            'phases':{p:quantiles([s['latency_ms'] for s in samples if s['phase']==p]) for p in ('baseline','loaded','recovery')}}
    Path(a.out).write_text(json.dumps(result,indent=2)+'\n')
    return bool(errors) or received!=seq

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--host',required=True);p.add_argument('--port',type=int,default=19001)
    p.add_argument('--flow',type=int,required=True);p.add_argument('--start',type=float,required=True)
    p.add_argument('--baseline',type=float,required=True);p.add_argument('--loaded',type=float,required=True);p.add_argument('--recovery',type=float,required=True)
    p.add_argument('--request-timeout',type=float,default=3);p.add_argument('--out',required=True)
    raise SystemExit(run(p.parse_args()))
