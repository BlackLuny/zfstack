"""Offline timing localization, without changing the gate or claiming a CPU cause."""
import bisect,collections,json,sys
from pathlib import Path
P=Path(__file__).resolve().parent
sys.path.insert(0,str(P.parents[1]))
import netem_fifo_ledger as L
D=P/'collected/gate/full'
report=json.loads((D/'queue-control.json').read_text())
receiver=json.loads((D/'receiver.json').read_text())['udp']
sender=json.loads((D/'sender.json').read_text())
ins,outs=L.decode((D/'queue-ledger.bin.gz').read_bytes())
pairs,lost,extra=L.match(ins,outs)
assert not lost and not extra
assert not L.validate(report,{'kind':'netem-fifo','node':'worker','interface':'eth0','phase':'loaded','delay_ms':500.0,'limit_packets':100000},(D/'queue-ledger.bin.gz').read_bytes())
by={r[0]:r for r in receiver};assert sorted(by)==list(range(sender['sent'])) and len(by)==len(receiver)
offset,spread,monos=L.clock_offset(report['clock_samples'])
# Frame length alone also selects TCP. Require the recorded 42-byte IPv4/UDP
# header and one flow. No captured payload sequence is stored in the ledger:
# this diagnostic additionally requires a unique timestamp interval mapping.
udp=sorted((a,b) for a,b in pairs if ins[a][1]==1074 and ins[a][2]==42)
assert len(udp)==len(receiver) and len({ins[a][7] for a,b in udp})==1
sent=[by[i][1] for i in range(len(by))]
assert all(a<b for a,b in zip(sent,sent[1:]))
mapped={};gaps=[]
for a,b in udp:
    admission,release=ins[a][0]-offset,outs[b][0]-offset
    seq=bisect.bisect_right(sent,admission)-1
    assert 0<=seq<len(sent) and seq not in mapped
    # Each mirror timestamp lies strictly inside its sender interval, even
    # allowing the entire measured clock-offset spread in either direction.
    assert admission-spread>sent[seq]
    assert seq+1==len(sent) or admission+spread<sent[seq+1]
    mapped[seq]={'admission_ns':admission,'egress_ns':release}
    gaps.append(admission-sent[seq])
assert sorted(mapped)==list(range(len(sent)))
assert all(mapped[i]['egress_ns']<mapped[i+1]['egress_ns'] for i in range(len(sent)-1))
incidents=[];best=-1
for row in receiver:
    seq=row[0]
    if seq<best:
        own,other=mapped[seq],mapped[best]
        emit=[{'seq':r[0],'sent_ns':r[1]} for r in receiver if own['egress_ns']<r[1]<row[2]]
        incidents.append({'overtaken_seq':seq,'overtaker_seq':best,
          'phase_offset_from_loaded_start_ms':(row[1]-report['planned']['start_ns'])/1e6,
          'send_gap_to_overtaker_ms':(by[best][1]-row[1])/1e6,
          'overtaken_admission_ns':own['admission_ns'],'overtaken_egress_ns':own['egress_ns'],
          'overtaken_socket_ns':row[2],'overtaker_egress_ns':other['egress_ns'],'overtaker_socket_ns':by[best][2],
          'overtaken_residence_ms':(own['egress_ns']-own['admission_ns'])/1e6,
          'overtaker_residence_ms':(other['egress_ns']-other['admission_ns'])/1e6,
          'overtaken_egress_to_socket_ms':(row[2]-own['egress_ns'])/1e6,
          'overtaker_egress_to_socket_ms':(by[best][2]-other['egress_ns'])/1e6,
          'egress_gap_ms':(other['egress_ns']-own['egress_ns'])/1e6,
          'socket_overtake_margin_ms':(row[2]-by[best][2])/1e6,
          'other_sends_during_egress_to_socket_interval':emit})
    best=max(best,seq)
result={'gate_unchanged':'FAIL','overtaken':len(incidents),'incidents':incidents,
        'all_udp_timestamp_intervals_unique':True,'udp_frames':len(udp),'udp_flows':1,
        'all_udp_egress_in_mapped_send_order':True,
        'same_length_capture_headers':dict(collections.Counter(r[2] for r in ins if r[1]==1074)),
        'clock_offset_spread_ns':spread,'max_clock_sample_gap_ms':max(b-a for a,b in zip(monos,monos[1:]))/1e6,
        'send_to_admission_ms':{'min':min(gaps)/1e6,'max':max(gaps)/1e6},
        'limits':'Ledger has no payload sequence field. Attribution uses one IPv4/UDP flow and a complete unique sender-interval/timestamp mapping with clock-spread checks. The recorded egress-to-socket interval includes kernel delivery and receiver scheduling; no tracing or CPU identity exists, so no specific CPU, IRQ, kernel function or injector component is established as the cause.'}
(P/'order-analysis.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
