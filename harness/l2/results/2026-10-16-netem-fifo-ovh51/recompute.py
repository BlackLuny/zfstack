"""Offline recheck against raw ledgers and receiver datagrams, without traffic."""
import collections, json, sys
from pathlib import Path
P=Path(__file__).resolve().parent
sys.path.insert(0,str(P.parents[1]))
import netem_fifo_ledger as L

def read(p): return json.loads(p.read_text())
def order(rows, planned):
    best=-1; bad=[]
    for row in rows:
        if row[0]<best: bad.append(row)
        best=max(best,row[0])
    def phase(t):
        return 'baseline' if t<planned['start_ns'] else 'loaded' if t<planned['end_ns'] else 'drain-or-guard' if t<planned['recovery_start_ns'] else 'recovery'
    return {'overtaken':len(bad),'by_send_phase':dict(collections.Counter(phase(r[1]) for r in bad))}

stage=P/'evidence/stage1'; result={}
for mode in ('full','cancel','overflow'):
    d=stage/mode; summary=read(stage/(mode+'.json'))
    report=read(d/'queue-control.json'); sender=read(d/'sender.json'); receiver=read(d/'receiver.json')
    config={'kind':'netem-fifo','node':'worker','interface':'eth0','phase':'loaded','delay_ms':500.0,'limit_packets':100 if mode=='overflow' else 100000}
    errors=L.validate(report,config,(d/'queue-ledger.bin.gz').read_bytes())
    assert errors==summary['oracle_errors']
    seq=[r[0] for r in receiver['udp']]
    value={'oracle_errors':errors,'summary_failed_checks':[k for k,v in summary['checks'].items() if not v],
           'sent':sender['sent'],'received':len(seq),'unique':len(set(seq)),
           'missing':len(set(range(sender['sent']))-set(seq)),
           'strict_complete_order':seq==list(range(sender['sent'])),
           'strict_increasing':all(a < b for a,b in zip(seq,seq[1:])), 'duplicates':len(seq)-len(set(seq)),**order(receiver['udp'],report['planned']),
           'cleanup':report.get('cleanup'),'receiver_mib':receiver['udp_mib_delta']}
    ins,outs=L.decode((d/'queue-ledger.bin.gz').read_bytes());pairs,lost,extra=L.match(ins,outs)
    value.update(in_frames=len(ins),out_frames=len(outs),lost=len(lost),extra=len(extra),
                 release_pairs_in_admission_order=all(a[0]<b[0] for a,b in zip(pairs,pairs[1:])))
    result[mode]=value
for p in sorted(stage.glob('control-*.json')):
    summary=read(p);d=p.with_suffix('');recv=read(d/'receiver.json');send=read(d/'sender.json')
    plan=summary['planned_ns'];v=order(recv['udp'],{'start_ns':plan['start'],'end_ns':plan['end'],'recovery_start_ns':plan['recovery']})
    assert v['overtaken']==summary['udp_overtaken']
    result[p.stem]={**v,'sent':send['sent'],'received':len(recv['udp']),
                    'missing':len(set(range(send['sent']))-{r[0] for r in recv['udp']}),
                    'remove_after_recovery_ns':summary['actual_ns']['remove'][1]-plan['recovery']}
(P/'recomputed.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
