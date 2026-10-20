#!/usr/bin/env python3
"""Attribute every closed-loop light RR to test-side packet/app timestamps."""
import argparse
import bisect
import collections
import gzip
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument("case",type=Path);a=p.parse_args()
    trace=a.case/"trace.jsonl"
    if trace.exists(): lines=trace.read_text().splitlines()
    else: lines=gzip.decompress(trace.with_suffix(".jsonl.gz").read_bytes()).decode().splitlines()
    rows=[json.loads(line) for line in lines]
    assert rows[-1]["kind"]=="trace_end" and rows[-1]["omitted"]==0
    result=a.case/"result.json"
    result_text=result.read_text() if result.exists() else gzip.decompress(result.with_suffix(".json.gz").read_bytes()).decode()
    size=json.loads(result_text)["config"]["rr_size"]
    groups=collections.defaultdict(list)
    for r in rows:
        if "port" in r:groups[r["peer"],r["port"]].append(r)
    # isolate creates exactly one baseline and one loaded TCP connection per
    # light peer. Use observed SYN order, not elapsed-time guesses.
    phases={}
    for peer in {key[0] for key in groups}:
        connections=sorted((min(r["at_ns"] for r in rs),key) for key,rs in groups.items() if key[0]==peer)
        assert len(connections)==2,(peer,connections)
        for phase,(_,key) in zip(("baseline","loaded"),connections):phases[key]=phase
    results=[]
    for (peer,port),rs in groups.items():
        rs.sort(key=lambda r:r["at_ns"]);times=[r["at_ns"] for r in rs]
        syn=next(r for r in rs if r["kind"]=="packet" and r["stage"]=="core_egress" and r["dir"]=="down" and r["flags"]&2)
        index=0
        starts=collections.deque()
        for r in rs:
            if r["kind"]!="request":continue
            if r["stage"]=="rr_start":starts.append(r["at_ns"]);continue
            if r["stage"]!="rr_end":continue
            start=starts.popleft();end=r["at_ns"]
            response_seq=(syn["seq"]+1+index*size)&0xffffffff;index+=1
            events=rs[bisect.bisect_left(times,start):bisect.bisect_right(times,end)]
            def find(stage,direction=None,payload=False):
                return [e for e in events if e["stage"]==stage and (direction is None or e.get("dir")==direction)
                        and (not payload or e.get("payload",0)>0)]
            ingress=find("core_ingress","up",True)
            writes=find("app_write")
            sends=[e for e in find("core_egress","down",True) if e["seq"]==response_seq]
            # Filter obsolete retransmissions from the preceding request. The
            # response for this RR cannot be emitted before its app write.
            if writes:sends=[e for e in sends if e["at_ns"]>=writes[0]["at_ns"]]
            deliveries=[e for e in find("link_deliver","down",True) if e["seq"]==response_seq]
            drops=find("queue_drop")
            up_arrive=find("link_arrive","up",True);up_deliver=find("link_deliver","up",True)
            down_queue=[e for e in events if e["stage"] in ("link_arrive","queue_drop") and e.get("dir")=="down" and e.get("seq")==response_seq and e.get("payload",0)>0]
            first=lambda xs:xs[0]["at_ns"] if xs else None
            t_in,t_write,t_send=first(ingress),first(writes),first(sends)
            t_deliver=deliveries[-1]["at_ns"] if deliveries else None
            diff=lambda b,c:(b-c)/1e6 if b is not None and c is not None else None
            out={"peer":peer,"port":port,"phase":phases[peer,port],"start_ms":start/1e6,"latency_ms":(end-start)/1e6,
                 "response_seq":response_seq,"response_queue_drops":sum(e.get("dir")=="down" and e.get("seq")==response_seq and e.get("payload",0)>0 for e in drops),
                 "up_link_to_core_ms":diff(t_in,first(up_deliver)),"core_egress_to_link_ms":diff(first(down_queue),t_send),
                 "up_to_core_ms":diff(t_in,start),"core_to_app_write_ms":diff(t_write,t_in),
                 "app_write_to_first_egress_ms":diff(t_send,t_write),"first_egress_to_delivery_ms":diff(t_deliver,t_send),
                 "delivery_to_app_ms":diff(end,t_deliver),"queue_drops":len(drops),
                 "response_copies":len(sends),"response_send_intervals_ms":[(b["at_ns"]-c["at_ns"])/1e6 for c,b in zip(sends,sends[1:])]}
            if out["latency_ms"]>50:
                out["events"]=[{**e,"relative_ms":(e["at_ns"]-start)/1e6} for e in events]
            results.append(out)
    assert results
    loaded=[r for r in results if r["phase"]=="loaded"]
    slow=[r for r in loaded if r["latency_ms"]>50]
    stage_names=["up_to_core_ms","core_to_app_write_ms","app_write_to_first_egress_ms","first_egress_to_delivery_ms","delivery_to_app_ms"]
    summary={"requests":len(results),"loaded_requests":len(loaded),"slow_over_50ms":len(slow),"slow_with_queue_drop":sum(r["queue_drops"]>0 for r in slow),
        "slow_with_response_retransmission":sum(r["response_copies"]>1 for r in slow),
        "over_200ms":sum(r["latency_ms"]>200 for r in loaded),
        "over_200ms_with_dropped_response":sum(r["latency_ms"]>200 and r["response_queue_drops"]>0 for r in loaded),
        "max_stage_ms":{k:max((r[k] for r in loaded if r[k] is not None),default=None) for k in stage_names+["up_link_to_core_ms","core_egress_to_link_ms"]},
        "slowest":sorted(loaded,key=lambda r:-r["latency_ms"])[:20]}
    (a.case/"attribution.json").write_text(json.dumps({"summary":summary,"requests":results},indent=2)+"\n")
    print(json.dumps({k:v for k,v in summary.items() if k!="slowest"},indent=2))
    for r in summary["slowest"][:6]:print({k:v for k,v in r.items() if k!="events"})


if __name__=="__main__":main()
