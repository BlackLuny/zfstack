#!/usr/bin/env python3
"""Client-mode matrix (docs/design/0008 §6): zfstack vs sing-box TUN stacks.

usage (root): client_matrix.py ZFBENCH SINGBOX OUTDIR REPS GROUP...
groups: mtu65535 w1 mtu9000 mtu4064 mtu1500 connect idle mixed

Runs every cell REPS times, interleaving the cells in each repetition, keeps
the raw JSON of every run in OUTDIR and prints medians.
"""
import json,os,subprocess,sys,statistics as st
B=sys.argv[1]; SB=sys.argv[2]
out=sys.argv[3]; reps=int(sys.argv[4]); groups=sys.argv[5:]
os.makedirs(out,exist_ok=True)
ZF='--client-proxy zfstack-client --splice --vnet-hdr --driver-thread'
ZFMT='--client-proxy zfstack-client --splice --vnet-hdr'
ZFT='--client-proxy zfstack-client --splice --tso --driver-thread'
ZFP='--client-proxy zfstack-client --splice --driver-thread'        # plain TUN (no offload), like Android/iOS
BULK='--secs 10 --warmup 3'
G={
 'mtu65535': [(p+' '+t, a+' '+t+' '+BULK) for t in ['--test down','--test up','--test down --flows 8','--test up --flows 8']
              for p,a in [('zfstack',ZF),('zfstack-mt',ZFMT),('singbox-go','--client-proxy singbox-go'),('singbox-gvisor','--client-proxy singbox-gvisor'),('singbox-system','--client-proxy singbox-system')]],
 'w1':       [(p+' w1 '+t, a+' --proxy-workers 1 '+t+' '+BULK) for t in ['--test down','--test up']
              for p,a in [('zfstack',ZF),('singbox-go','--client-proxy singbox-go')]],
 'mtu9000':  [(p+' 9000 '+t, a+' --tun-mtu 9000 '+t+' '+BULK) for t in ['--test down','--test up']
              for p,a in [('zfstack-tso',ZFT),('zfstack-plain',ZFP),('singbox-go','--client-proxy singbox-go')]],
 'mtu4064':  [(p+' 4064 '+t, a+' --tun-mtu 4064 '+t+' '+BULK) for t in ['--test down','--test up']
              for p,a in [('zfstack-tso',ZFT),('zfstack-plain',ZFP),('singbox-go','--client-proxy singbox-go')]],
 'mtu1500':  [(p+' 1500 '+t, a+' --tun-mtu 1500 '+t+' '+BULK) for t in ['--test down','--test up']
              for p,a in [('zfstack-tso',ZFT),('zfstack-plain',ZFP),('singbox-go','--client-proxy singbox-go')]],
 'connect':  [(p+' connect', a+' --test connect --conns 4000 --concurrency 64') for p,a in [('zfstack',ZF),('singbox-go','--client-proxy singbox-go'),('singbox-system','--client-proxy singbox-system')]],
 'idle':     [(p+' idle', a+' --test idle --conns 4000') for p,a in [('zfstack',ZF),('singbox-go','--client-proxy singbox-go'),('singbox-system','--client-proxy singbox-system')]],
 'mixed':    [(p+' mixed', a+' --test mixed '+BULK) for p,a in [('zfstack',ZF),('singbox-go','--client-proxy singbox-go')]],
}
cells=[c for g in groups for c in G[g]]
res={}
for r in range(reps):
    for name,args in cells:
        o=subprocess.run([B,'--singbox',SB]+args.split(),capture_output=True,text=True).stdout
        fn=os.path.join(out,name.replace(' ','_').replace('--','')+f'__r{r}.json')
        i=o.find('{')
        try:
            d=json.loads(o[i:]); open(fn,'w').write(o[i:])
        except Exception:
            res.setdefault(name,[]).append(None); continue
        res.setdefault(name,[]).append(d)
def med(v):
    v=[x for x in v if x is not None]
    return st.median(v) if v else None
def f(x,n=2): return '-' if x is None else f"{x:.{n}f}"
for name,_ in cells:
    ds=[d for d in res.get(name,[]) if d]
    ok=sum(1 for d in ds if d['ok']); n=len(res.get(name,[]))
    ds=[d for d in ds if d['ok']]
    if not ds: print(f"{name:40s} FAILED ({n} runs)"); continue
    p=lambda k: med([d['proxy'][k] for d in ds])
    r0=ds[0]['results']; t=ds[0]['config']['test']
    if t in ('down','up'):
        g=med([d['results']['goodput_mbps_window']/1000 for d in ds])
        print(f"{name:40s} {g:6.2f} Gbps  {f(p('cpu_sec_per_GB'),3)} s/GB  util {f(p('cpu_util'))}  sys {f(med([d['cpu']['system_busy_sec_per_GB'] for d in ds]))} s/GB  hwm {p('hwm_kb')/1024:5.1f} MB  ok {ok}/{n}")
    elif t=='connect':
        lt=lambda k: med([d['results']['latency_total'][k] for d in ds])
        print(f"{name:40s} cps {f(med([d['results']['conn_per_sec'] for d in ds]),0)}  succ {med([d['results']['success'] for d in ds])}  p50 {f(lt('p50_ms'))} p99 {f(lt('p99_ms'))} ms  cpu {f(p('cpu_window_sec'))} s  hwm {p('hwm_kb')/1024:5.1f} MB  ok {ok}/{n}")
    elif t=='idle':
        print(f"{name:40s} rss/conn {f(med([d['results']['rss_bytes_per_conn'] for d in ds]),0)} B  idle {p('rss_kb_idle')/1024:5.1f} MB  connect {f(med([d['results']['connect_secs'] for d in ds]))} s  ok {ok}/{n}")
    elif t=='mixed':
        rl=lambda k: med([d['results']['rr_loaded'].get(k) for d in ds])
        g=med([d['results']['goodput_mbps_window']/1000 for d in ds])
        print(f"{name:40s} bulk {g:6.2f} Gbps  RR loaded p50 {f(rl('p50_ms'),3)} p99 {f(rl('p99_ms'),3)} ms  delta p99 {f(med([d['results']['rr_delta_p99_ms'] for d in ds]),3)}  ok {ok}/{n}")
