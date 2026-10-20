#!/usr/bin/env python3
"""Independently recalculate tail and paired throughput/cost summaries."""
import collections
import hashlib
import gzip
import json
import math
from pathlib import Path

OUT=Path(__file__).resolve().parents[1]/"docs/experiments/2026-10-20-issue69-tail"


def percentile(xs,p):return sorted(xs)[math.ceil(len(xs)*p/100)-1] if xs else None


def load_case(path):
    f=path/"result.json"
    if f.exists(): raw=f.read_text()
    elif f.with_suffix(".json.gz").exists(): raw=gzip.decompress(f.with_suffix(".json.gz").read_bytes()).decode()
    else:return None
    if not raw:return None
    x=json.loads(raw);identity=json.loads((path/"identity.json").read_text())
    if "returncode" not in identity:return None
    users=[]
    for u in x["results"]["loaded_users"]:
        raw=u["samples"]["raw_ms"]
        assert len(raw)==u["samples"]["n"]
        p99=percentile(raw,99) if len(raw)>=100 else None
        if p99 is not None:assert math.isclose(p99,u["samples"]["p99_ms"],abs_tol=1e-8)
        users.append({"peer":u["peer"],"n":len(raw),"p50":percentile(raw,50),"p95":percentile(raw,95),"p99":p99,"max":max(raw)})
    cpu=x["cpu"];gb=cpu["window_effective_bytes"]/1e9
    resource=json.loads((path/"resources.json").read_text()) if (path/"resources.json").exists() else {}
    arrivals=None
    arrivals_path=path/"arrivals.json"
    if arrivals_path.exists() or arrivals_path.with_suffix(".json.gz").exists():
        raw_arrivals=arrivals_path.read_text() if arrivals_path.exists() else gzip.decompress(arrivals_path.with_suffix(".json.gz").read_bytes()).decode()
        lanes=json.loads(raw_arrivals);reasons=[];completed=0;max_lag=0
        for lane in lanes:
            rows=lane["arrivals"];n=lane["count"]
            if n not in (75,300) or len(rows)!=n or lane["hz"]!=25 or lane["period_ns"]!=40_000_000:reasons.append("schedule shape")
            for i,r in enumerate(rows):
                if r["seq"]!=i or r["planned_ns"]!=5_000_000+i*40_000_000:reasons.append("arrival identity/time")
                if r["started_ns"] is not None:
                    lag=r["started_ns"]-r["planned_ns"];max_lag=max(max_lag,lag)
                    if lag<0 or lag>20_000_000:reasons.append("generator lag")
                if r["outcome"]!="completed":reasons.append(r["outcome"]);continue
                completed+=1
                if not r["planned_ns"]<=r["started_ns"]<=r["completed_ns"]<=r["planned_ns"]+3_000_000_000:reasons.append("request deadline")
            if n==300:
                peer=int(lane["local"].split(":")[0].split(".")[2]);u=next(v for v in x["results"]["loaded_users"] if v["peer"]==peer)
                values=[(r["completed_ns"]-r["planned_ns"])/1e6 for r in rows if r["outcome"]=="completed"]
                if len(values)!=len(u["samples"]["raw_ms"]) or any(not math.isclose(a,b,abs_tol=1e-7) for a,b in zip(values,u["samples"]["raw_ms"])):reasons.append("raw latency mismatch")
        if len(lanes)!=6 or sorted(l["count"] for l in lanes)!=[75,75,75,300,300,300]:reasons.append("missing lane")
        if x["results"].get("open_loop_hz")!=25:reasons.append("arrival mode")
        arrivals={"valid":not reasons,"failures":reasons,"completed":completed,"scheduled":sum(l["count"] for l in lanes),"max_generator_lag_ms":max_lag/1e6}
    return {"case":path.name,"ok":x["ok"],"returncode":identity["returncode"],"binary_sha256":identity["binary_sha256"],"arrivals":arrivals,
        "stack":x["config"]["stack"],"queue_bytes":x["config"]["queue_bytes_down"],
        "discipline":x["link"]["down"].get("queue_discipline","legacy-fifo"),"users":users,
        "mbps":x["results"]["heavy"]["goodput_mbps_window"],"bytes":cpu["window_effective_bytes"],"seconds":cpu["window_wall_sec"],
        "worst_p99_ms":max((u["p99"] for u in users if u["p99"] is not None),default=None),"worst_max_ms":max(u["max"] for u in users),
        "stack_cpu_per_GB":cpu["cpu_sec_per_effective_GB"],"process_cpu_per_GB":cpu["process_window_sec"]/gb,
        "link_cpu_per_GB":(cpu["link_up_window_sec"]+cpu["link_down_window_sec"])/gb,
        "max_rss_kib":resource.get("max_rss_kib"),"light_requests":sum(u["n"] for u in users),
        "max_queue_bytes":x["link"]["down"]["max_queue_bytes"],
        "sink_errors":x["link"]["down"]["sink_errors"]+x["link"]["up"]["sink_errors"],
        "live_connections_end":x["stack_thread"]["stack_stats"]["live_conns"]}


def main():
    cases=[x for p in sorted(OUT.iterdir()) if p.is_dir() and (x:=load_case(p)) is not None]
    planned=json.loads((OUT/"paired-plan.json").read_text())
    by_name={x["case"]:x for x in cases}
    blocks=collections.defaultdict(lambda:collections.defaultdict(list))
    for p in planned["plan"]:
        if p["case"] not in by_name:continue
        x=by_name[p["case"]]
        assert x["binary_sha256"]==planned["binary_sha256"]
        assert x["ok"] and x["returncode"]==0 and x["sink_errors"]==0 and x["live_connections_end"]==0
        assert x["queue_bytes"]==250000 and x["max_queue_bytes"]<=250000
        if p["arm"] in ("A","B"):blocks[p["block"]][p["arm"]].append(x)
    def aggregate(xs):
        goodput=sum(x["bytes"] for x in xs)*8/1e6/sum(x["seconds"] for x in xs)
        return {"mbps":goodput,"p99_range_ms":[min(x["worst_p99_ms"] for x in xs),max(x["worst_p99_ms"] for x in xs)],
            "max_ms":max(x["worst_max_ms"] for x in xs),"stack_cpu_per_GB":sum(x["stack_cpu_per_GB"]*x["bytes"] for x in xs)/sum(x["bytes"] for x in xs),
            "process_cpu_per_GB":sum(x["process_cpu_per_GB"]*x["bytes"] for x in xs)/sum(x["bytes"] for x in xs),
            "link_cpu_per_GB":sum(x["link_cpu_per_GB"]*x["bytes"] for x in xs)/sum(x["bytes"] for x in xs),
            "peak_rss_kib":max(x["max_rss_kib"] for x in xs),"light_requests":sum(x["light_requests"] for x in xs)}
    comparisons=[]
    for block,arms in blocks.items():
        if len(arms["A"])!=2 or len(arms["B"])!=2:continue
        a,b=aggregate(arms["A"]),aggregate(arms["B"])
        comparisons.append({"block":block,"A":a,"B":b,"throughput_ratio":b["mbps"]/a["mbps"],
            "throughput_gate_pass":b["mbps"]/a["mbps"]>=.99,
            "process_cpu_ratio":b["process_cpu_per_GB"]/a["process_cpu_per_GB"]})
    done=sum(p["case"] in by_name for p in planned["plan"])
    fixed=[x for x in cases if x["case"].startswith(("fixed-bbr-abba-","fixed2-bbr-abba-"))]
    if fixed:
        fixed_identity=json.loads((OUT/"fixed-binary.json").read_text())
        assert all(x["binary_sha256"]==fixed_identity["sha256"] for x in fixed)
    fixed_comparisons=[]
    for prefix in ("fixed-bbr-abba-","fixed2-bbr-abba-"):
        block=[x for x in fixed if x["case"].startswith(prefix)]
        if len(block)!=4:continue
        arms={arm:[x for x in block if x["case"].endswith("-"+arm)] for arm in ("A","B")}
        assert len(arms["A"])==len(arms["B"])==2
        a,b=aggregate(arms["A"]),aggregate(arms["B"])
        fixed_comparisons.append({"block":prefix.rstrip("-"),"A":a,"B":b,
            "valid":all(x["ok"] and x["returncode"]==0 and x["sink_errors"]==0 and x["live_connections_end"]==0
                and x["queue_bytes"]==250000 and x["max_queue_bytes"]<=250000 and x["arrivals"] and x["arrivals"]["valid"] for x in block),
            "throughput_ratio":b["mbps"]/a["mbps"],"throughput_gate_pass":b["mbps"]/a["mbps"]>=.99,
            "process_cpu_ratio":b["process_cpu_per_GB"]/a["process_cpu_per_GB"]})
    result={"cases":cases,"comparisons":comparisons,"paired_done":done,"paired_planned":len(planned["plan"]),
        "fixed_comparisons":fixed_comparisons,"paired_complete":done==len(planned["plan"]),"scope":"Test-side bottleneck queue causal prototype, not a production ZFC/TCP fix."}
    (OUT/"summary.json").write_text(json.dumps(result,indent=2)+"\n")
    print('paired',done,'/',len(planned['plan']))
    for x in comparisons:print(x)
    for x in fixed_comparisons:print('fixed',x)


if __name__=="__main__":main()
