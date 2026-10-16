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

stage=Path(sys.argv[1]) if len(sys.argv)>1 else P/'collected/gate'; result={}
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
    lifecycle=read(d/'lifecycle.json'); links=read(d/'link-counters.json')
    drops={node:{direction:links['after'][node][direction]['dropped']-before[direction]['dropped'] for direction in ('rx','tx')} for node,before in links['before'].items()}
    checks={'udp_payload_intact':all(r[3] for r in receiver['udp']),
            'receiver_socket_no_loss':not receiver['udp_mib_delta'].get('RcvbufErrors') and not receiver['udp_mib_delta'].get('InErrors'),
            'no_sender_backpressure':sender['blocked']==0,
            'link_zero_drops':not any(n for row in drops.values() for n in row.values()),
            'qdisc_restored':[r.get('kind') for r in report.get('kernel',{}).get('qdisc_restored',[])]==['noqueue'],
            'cleanup_pass':report.get('cleanup')=='PASS'}
    if mode!='overflow':
        checks.update(udp_exactly_once_in_order=seq==list(range(sender['sent'])),
                      tcp_stream_intact=receiver['tcp_bytes']==sender['tcp_bytes'] and not receiver['tcp_errors'],
                      all_frames_delivered=bool(ins) and not lost,
                      gso_frames_preserved=any(r[3] & L.GSO_TYPE_MASK for r in ins))
    if mode=='full':
        plan=report['planned']
        active=[r[2]-r[1] for r in receiver['udp'] if plan['start_ns']+50_000_000<=r[1]<plan['end_ns']]
        recovery=[r[2]-r[1] for r in receiver['udp'] if r[1]>=plan['recovery_start_ns']]
        checks.update(oracle_pass=not errors,active_delay_applied=bool(active) and min(active)>=500_000_000,
                      recovery_transparent=bool(recovery) and max(recovery)<125_000_000)
    elif mode=='cancel':
        checks.update(cancel_recorded=report.get('cancelled') is True,
                      oracle_rejects_cancel=any('cancelled' in e for e in errors),
                      udp_no_duplicates=len(set(seq))==len(seq),
                      redirect_kept_until_clients_reaped_and_measurement_ended=report['actual']['removed_ns']>=max(lifecycle['sender_reaped_ns'],lifecycle['measurement_end_ns']))
    else:
        udp_lost=sum(ins[i][2]==42 and ins[i][1]==1074 for i in lost)
        checks.update(overflow_recorded=bool(lost),oracle_rejects_overflow=any('lost or never released' in e for e in errors),
                      udp_loss_equals_ledger_overflow=udp_lost>0 and len(set(seq))==len(seq) and sender['sent']-len(seq)==udp_lost)
        value['ledger_udp_lost']=udp_lost
    assert checks==summary['checks'],(mode,checks,summary['checks'])
    assert all(checks.values()),(mode,checks)
    value['checks_independently_match']=True
    result[mode]=value
for p in sorted(stage.glob('control-*.json')):
    summary=read(p);d=p.with_suffix('');recv=read(d/'receiver.json');send=read(d/'sender.json')
    plan=summary['planned_ns'];v=order(recv['udp'],{'start_ns':plan['start'],'end_ns':plan['end'],'recovery_start_ns':plan['recovery']})
    assert v['overtaken']==summary['udp_overtaken']
    result[p.stem]={**v,'sent':send['sent'],'received':len(recv['udp']),
                    'missing':len(set(range(send['sent']))-{r[0] for r in recv['udp']}),
                    'remove_after_recovery_ns':summary['actual_ns']['remove'][1]-plan['recovery']}
(P/'gate-recomputed.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))

execution=read(stage/'execution.json')
assert [(r['label'],r['returncode']) for r in execution]==[('capabilities',0),('full',0),('cancel',0),('overflow',0)]
assert all(v['strict_increasing'] and not v['duplicates'] for v in result.values())
assert read(stage/'gate.json')['gate']=='PASS'
print('Independent Canadian gate: PASS; every check recomputed, no controls needed.')
