import bisect,gzip,json,re
from pathlib import Path
P=Path(__file__).parent
maps=sum([json.loads((P/n).read_text()) for n in ['clock-mapping-before-stream.json','clock-mapping-after-stream.json']],[])
low=max(r['offset_low'] for r in maps);high=min(r['offset_high'] for r in maps)
print('offset width ns',high-low)
assert low<=high
host=json.loads(gzip.decompress((P/'host-vcpu-full-3.json.gz').read_bytes()))['rows'];ends=[r[1] for r in host];begins=[r[0] for r in host]
lines=gzip.decompress((P/'before-full-3.ftrace.gz').read_bytes()).decode().splitlines();switches=[]
for l in lines:
 m=re.search(r'\[(\d+)\].*? (\d+\.\d+): sched_switch:.*?next_comm=qa75-inject next_pid=',l)
 if m:switches.append((int(float(m[2])*1e9),int(m[1])))
switches.sort();switchtimes=[r[0] for r in switches]
events=json.loads(gzip.decompress((P/'before-full-3/diagnostic-events.json.gz').read_bytes()))['events'];out=[]
for e in events:
 b,a=e['begin'],e['end'];wall=a[0]-b[0]
 if wall<5_000_000 or e['op'] not in ['admit','send','emit','read_batch','epoll']:continue
 si=bisect.bisect_right(switchtimes,b[0])-1
 if si<0:continue
 cpu=switches[si][1];i=bisect.bisect_right(ends,b[0]+low)-1;j=bisect.bisect_left(begins,a[0]+high)
 if i<0 or j>=len(host):continue
 bh=next(x for x in host[i][2] if x[0]==cpu);ah=next(x for x in host[j][2] if x[0]==cpu)
 cost=ah[1]+ah[2]-bh[1]-bh[2]
 total=sum(x[1]+x[2] for x in host[j][2])-sum(x[1]+x[2] for x in host[i][2])
 out.append({'operation':e['op'],'guest_begin_ns':b[0],'guest_end_ns':a[0],'wall_ns':wall,'guest_thread_cpu_ns':a[1]-b[1],'guest_switches':[a[2]-b[2],a[3]-b[3]],'vcpu':cpu,'host_cpu_upper_bound_ns':cost,'host_all_vcpu_cpu_upper_bound_ns':total,'host_sample_window_ns':[host[i][0],host[j][1]],'host_sample_edge_slack_ns':(b[0]+low-host[i][0])+(host[j][1]-a[0]-high),'extra':e['extra']})
result={'offset_low_ns':low,'offset_high_ns':high,'events':out};(P/'host-correlation.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
