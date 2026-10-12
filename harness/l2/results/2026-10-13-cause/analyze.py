import collections,gzip,json,pathlib,struct,sys

def packets(path, clock):
 data=gzip.decompress(path.read_bytes())
 endian={b'\xd4\xc3\xb2\xa1':'<',b'\xa1\xb2\xc3\xd4':'>',b'\x4d\x3c\xb2\xa1':'<',b'\xa1\xb2\x3c\x4d':'>'}[data[:4]]
 ns=data[:4] in (b'\x4d\x3c\xb2\xa1',b'\xa1\xb2\x3c\x4d')
 link=struct.unpack_from(endian+'I',data,20)[0]
 offset={1:14,12:0,101:0,113:16,276:20}[link]
 pos=24; rows=[]
 while pos < len(data):
  sec,sub,n,original=struct.unpack_from(endian+'IIII',data,pos);pos+=16
  packet=data[pos:pos+n];pos+=n
  ip=packet[offset:]
  if len(ip)<20 or ip[0]>>4 != 4 or ip[9]!=6: continue
  ih=(ip[0]&15)*4;tcp=ip[ih:];th=(tcp[12]>>4)*4
  src,dst,seq,ack=struct.unpack_from('!HHII',tcp)
  payload=tcp[th:]
  r={'at':sec+sub/(1e9 if ns else 1e6)-clock['realtime']+clock['monotonic'], 'src':src,'dst':dst,'tcp_seq':seq,'ack':ack,'flags':tcp[13],'window_raw':struct.unpack_from('!H',tcp,14)[0],'payload_len':struct.unpack_from('!H',ip,2)[0]-ih-th}
  if len(payload)>=8 and payload[:4]==b'\0\0\0\1':
   r['app_seq']=struct.unpack_from('!I',payload,4)[0]
  rows.append(r)
 return rows

def analyze(p):
 result=json.loads((p/'result.json').read_text());raw=json.loads(gzip.decompress((p/'raw.json.gz').read_bytes()))
 inner=packets(p/'light-inner.pcap.gz',raw['clock.json']);target=packets(p/'light-target.pcap.gz',raw['clock.json'])
 def index(rows):
  d=collections.defaultdict(list)
  for row in rows:
   if 'app_seq' in row:d[(row['app_seq'],'request' if row['dst']==19001 else 'response')].append(row)
  return d
 ip,tp=index(inner),index(target)
 events={r['seq']:r for r in raw['target_events'] if r['peer']==1}
 samples=raw['client-1.json']['samples'];slow=[]
 for s in samples:
  if s['latency_ms']<100: continue
  seq=s['seq'];req=ip[(seq,'request')];response=ip[(seq,'response')];treq=tp[(seq,'request')];tresp=tp[(seq,'response')];ev=events.get(seq)
  row={'seq':seq,'phase':s['phase'],'start_sec':s['at']-result['start_at'],'latency_ms':s['latency_ms'],'inner_request_at_sec':[round(r['at']-result['start_at'],6) for r in req],'inner_request_intervals_ms':[round((b['at']-a['at'])*1000,3) for a,b in zip(req,req[1:])],'target_req_ms':(treq[0]['at']-s['at'])*1000 if treq else None,'target_reply_ms':(tresp[0]['at']-s['at'])*1000 if tresp else None,'inner_reply_ms':(response[0]['at']-s['at'])*1000 if response else None,'server_receive_ms':(ev['received_at']-s['at'])*1000 if ev else None,'server_work_ms':(ev['replied_at']-ev['received_at'])*1000 if ev else None}
  if req:
   end=(req[0]['tcp_seq']+req[0]['payload_len'])&0xffffffff
   acks=[r for r in inner if r['src']==19001 and r['at']>=req[0]['at'] and (r['ack']-end)&0xffffffff < (1<<31)]
   row['request_first_ack_ms']=(acks[0]['at']-s['at'])*1000 if acks else None
   row['request_wire_tcp_seq']=req[0]['tcp_seq']
   rejected=[]
   for request in req[:-1]:
    replies=[r for r in inner if r['src']==19001 and request['at']<=r['at']<=request['at']+.1 and r['ack']==request['tcp_seq'] and r['payload_len']==0]
    rejected.append({'request_at_sec':request['at']-result['start_at'],'dup_ack_delay_ms':(replies[0]['at']-request['at'])*1000 if replies else None,'window_raw':replies[0]['window_raw'] if replies else None})
   row['rejected_attempts']=rejected
  slow.append(row)
 loaded=[s['latency_ms'] for s in samples if s['phase']=='loaded']
 inbound_fraction=[x['target_req_ms']/x['latency_ms'] for x in slow if x['target_req_ms'] is not None]
 summary={'case':p.name,'ok':result['ok'],'heavy_mbps':result['heavy_mbps'],'loaded_n':len(loaded),'loaded_max_ms':max(loaded) if loaded else None,'loaded_p99_ms':result['light_users'][0]['phases']['loaded']['p99_ms'],'slow_over_100ms':len(slow),'requests_retransmitted':sum(len(rs)>1 for (seq,d),rs in ip.items() if d=='request'),'pcap_inner_packets':len(inner),'pcap_target_payloads':len(target),'target_events_peer1':len(events),'slow_request_inbound_fraction_min':min(inbound_fraction) if inbound_fraction else None,'slow_request_inbound_fraction_median':sorted(inbound_fraction)[len(inbound_fraction)//2] if inbound_fraction else None,'capture_stats':{key:raw[key] for key in ('light-capture.log','target-capture.log')},'slow_requests':sorted(slow,key=lambda r:-r['latency_ms'])}
 diag=[]
 last_alloc=None
 for line in (p/'diagnostics.log').read_text().splitlines():
  if line.startswith('L2_RX_ALLOC'):
   last_alloc=dict(item.split('=',1) for item in line.split()[1:])
  if line.startswith('L2_RX_DROP'):
   row=dict(item.split('=',1) for item in line.split()[1:])
   at=int(row['realtime_us'])/1e6-raw['clock.json']['realtime']+raw['clock.json']['monotonic']
   req=ip[(int(row['seq']),'request')]
   match=min(req,key=lambda r:abs(at-r['at'])) if req else None
   row['window_sec']=at-result['start_at']
   row['nearest_request_delta_ms']=(at-match['at'])*1000 if match else None
   row['allocation']=last_alloc
   row['allocation_to_drop_us']=int(row['realtime_us'])-int(last_alloc['realtime_us']) if last_alloc else None
   diag.append(row)
 if diag:
  summary['core_drop_count']=len(diag)
  summary['core_drop_causes']=dict(collections.Counter(r['cause'] for r in diag))
  summary['core_drop_levels']=dict(collections.Counter(r['allocation']['level'] for r in diag if r['allocation']))
  summary['core_drops']=diag
 (p/'analysis.json').write_text(json.dumps(summary,indent=2)+'\n')
 print(json.dumps({k:v for k,v in summary.items() if k not in ('slow_requests','capture_stats','core_drops')}))
 for row in summary['slow_requests'][:3]:print(json.dumps(row))
for p in sorted((pathlib.Path(sys.argv[1]) if len(sys.argv)>1 else pathlib.Path(__file__).parent).iterdir()):
 if p.is_dir():analyze(p)
