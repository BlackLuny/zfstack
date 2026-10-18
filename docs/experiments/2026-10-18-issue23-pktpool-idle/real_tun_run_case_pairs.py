#!/usr/bin/env python3
"""Frozen real-TUN alternating paired A/B with independent baseline A/A controls.

All network devices are created/destroyed by the existing issue23_tun_case.py
inside a unique owned Linux network namespace. Root needed. This driver
does not modify production source or install software.

Run separately per cell (only one driver may run on a host at a time).
Example:
python3 run_case_pairs.py --cell highpps512 --out /path/results/highpps512
  --base /path/main/target/release/zfbench
  --fixed /path/pr/target/release/zfbench
  --aa /path/main-independent/target/release/zfbench
  --runner /path/bench/pareto/issue23_tun_case.py
  --loader /path/bench/pareto/issue23_tun_load.py

These diagnostic results are not an independent certified QA environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

BASE_SHA = "f467635a032e0c5ace36536d348263d8f47ee1d0"
FIX_SHA = "6e8333e30e3a4fc7fc1a69dac7e2551e77c38ab0"
BUILD_FLAGS = {"profile":"release","lto":"fat","codegen_units":1,"panic":"abort",
               "features":["packet-pool-idle-trim"],"rustc":"1.95.0","locked":True,
               "source_build_jobs":2}

# RTT/queue is real local TUN, no artificial loss; no WireGuard.
CELLS = {
    "highpps512": {"mtu":512,"idle":"2","idle_count":0,"flows":8,"rr_samples":160,
                   "rr_sizes":"64,512","bulk_bytes":32*1024*1024,"direction":"both",
                   "mixed_rr":2,"settle":0.5,"trim":False,"timeout":210},
    "mixed1500": {"mtu":1500,"idle":"2","idle_count":0,"flows":8,"rr_samples":160,
                  "rr_sizes":"64,512,1400","bulk_bytes":64*1024*1024,"direction":"both",
                  "mixed_rr":2,"settle":0.5,"trim":True,"timeout":210},
    "bursts1500": {"mtu":1500,"idle":"3","idle_count":0,"flows":16,"rr_samples":160,
                   "rr_sizes":"64,512","bulk_bytes":16*1024*1024,"direction":"both",
                   "mixed_rr":2,"settle":0.5,"trim":True,"timeout":210},
}
SOURCE = {"base":BASE_SHA, "aa":BASE_SHA, "fixed":FIX_SHA}
def sha(path):
    h = hashlib.sha256()
    with open(path,"rb") as s:
        for b in iter(lambda:s.read(1024*1024),b""):
            h.update(b)
    return h.hexdigest()
def append(path,row):
    with open(path, "a", encoding="utf-8") as s:
        s.write(json.dumps(row,sort_keys=True,separators=(",",":"),allow_nan=False)+"\n")

def host_interference():
    counts={}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            name=(entry/"comm").read_text().strip()
        except (OSError,PermissionError):
            continue
        if name in {"rustc","cc1","cc1plus","clang","clang++","gcc","g++","cargo","ld.lld"}:
            counts[name]=counts.get(name,0)+1
    pressure_path=Path("/proc/pressure/cpu")
    return {"compiler_counts":counts,"loadavg":list(os.getloadavg()),
            "cpu_pressure":pressure_path.read_text().strip() if pressure_path.exists() else None,
            "at_unix_ns":time.time_ns()}

def percentiles(values,pct):
    xs=sorted(values)
    return xs[min(len(xs)-1,max(0,int((len(xs)-1)*pct)))] if xs else None

def metrics(dir):
    rows=[json.loads(line) for line in (dir/"loader.jsonl").read_text().splitlines() if line.strip()]
    bulks=[r for r in rows if r.get("type")=="bulk"]
    assert len(bulks)==4, ("bulk count",len(bulks))
    summaries=[r for r in rows if r.get("type")=="summary"]
    assert len(summaries)==1 and summaries[0].get("ok") is True
    assert all(r["verified_bytes"]>0 and r["proxy_cpu_sec_per_GB"]>=0 for r in bulks)
    out={}
    out["proxy_cpu_sec_per_GB"]=sum(r["proxy_cpu_sec"] for r in bulks)/(sum(r["verified_bytes"] for r in bulks)/1e9)
    out["total_verified_bytes"]=sum(r["verified_bytes"] for r in bulks)
    out["total_tun_rx_packets"]=sum(r["tun_delta"].get("rx_packets",0) for r in bulks)
    out["total_tun_tx_packets"]=sum(r["tun_delta"].get("tx_packets",0) for r in bulks)
    out["bulk_proxy_cpu_sec"]=sum(r["proxy_cpu_sec"] for r in bulks)
    out["bulk_generator_cpu_sec"]=sum(r["generator_process_cpu_sec"] for r in bulks)
    for direction in ("down","up"):
        group=[r for r in bulks if r["direction"]==direction]
        if not group: continue
        out[direction+"_mbps"]=sum(r["verified_bytes"] for r in group)*8/1e6/sum(r["window_wall_sec"] for r in group)
        out[direction+"_cpu_sec_per_GB"]=sum(r["proxy_cpu_sec"] for r in group)/(sum(r["verified_bytes"] for r in group)/1e9)
        mixed=[]
        for r in group:
            m=r.get("mixed_rr")
            if m:
                mixed.extend(t["rtt_us"] for t in m.get("raw_samples",[]))
        out[direction+"_mixed_rr_n"]=len(mixed)
        out[direction+"_mixed_rr_p99_us"]=percentiles(mixed,.99) if len(mixed)>=100 else None
    rr=[r for r in rows if r.get("type")=="rr" and r.get("size")==64]
    normal=[s["rtt_us"] for r in rr for s in r.get("raw_samples",[])]
    out["unloaded_rr_64_n"]=len(normal)
    out["unloaded_rr_64_p99_us"]=percentiles(normal,.99) if len(normal)>=100 else None
    quiet=[r for r in rows if r.get("type")=="quiet"]
    out["quiet_wire_silence"]=all(r.get("complete_wire_silence") for r in quiet)
    out["quiet_cpu_sec"]=sum(r.get("proxy_cpu_sec",0) for r in quiet)
    snapshots=[r for r in rows if r.get("type")=="snapshot"]
    rss=[r["sample"]["proxy"].get("rss_bytes") for r in snapshots if r.get("sample",{}).get("proxy",{}).get("rss_bytes") is not None]
    out["rss_max_bytes_samples"]=max(rss) if rss else None
    out["rss_final_bytes_sample"]=rss[-1] if rss else None
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--cell",choices=sorted(CELLS),required=True)
    ap.add_argument("--out",type=Path,required=True)
    for key in ("base","fixed","aa","runner","loader"):
        ap.add_argument("--"+key,type=Path,required=True)
    ap.add_argument("--pairs",type=int,default=12)
    ap.add_argument("--aa-pairs",type=int,default=4)
    ap.add_argument("--warmup-pairs",type=int,default=1)
    args=ap.parse_args()
    assert 1<=args.pairs<=48 and 0<=args.aa_pairs<=12 and 0<=args.warmup_pairs<=6
    config=CELLS[args.cell]
    files={key:getattr(args,key).resolve(strict=True) for key in ("base","fixed","aa","runner","loader")}
    hashes={k:sha(v) for k,v in files.items()}
    out=args.out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    header={"schema":1,"cell":args.cell,"case":config,"build_flags":BUILD_FLAGS,
            "sources":SOURCE,"files":{k:{"path":str(v),"sha256":hashes[k]} for k,v in files.items()},
            "pairs":args.pairs,"aa_pairs":args.aa_pairs,"warmup_pairs":args.warmup_pairs,
            "host":{"uname":dict(zip(("sysname","nodename","release","version","machine"),os.uname())),"cpu_allowed":sorted(os.sched_getaffinity(0)),
                    "loadavg_at_start":os.getloadavg(),"kernel_flags":Path("/proc/cpuinfo").read_text().split("\n")[0],
                    "cpu_governor":Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip()},
            "start_ns":time.time_ns(),"control":"only bare source change, no instrumentation in timed path",
            "order":"alternating AB/BA single paired runs; independent AA controls inserted at regular block positions; no ABBA claim"}
    (out/"frozen-plan.json").write_text(json.dumps(header,indent=2,sort_keys=True)+"\n")
    # Append-only until all runs are complete; never overwrite a result.
    result_log=out/"cases.jsonl"
    n=0
    def run(arm,category,block,position,order):
        nonlocal n
        for k,v in files.items():
            if sha(v)!=hashes[k]:
                raise RuntimeError("input binary/source changed during run: "+k)
        n+=1
        label=f"{n:03d}-{category}-b{block:02d}-{position}-{arm}"
        case_dir=out/label
        runid=f"{args.cell}-{label}"
        cmd=[sys.executable,"-B",str(files["runner"]),
            "--binary",str(files[arm]),"--loader",str(files["loader"]),
            "--outdir",str(case_dir),"--label",label,"--variant",arm,
            "--source-sha",SOURCE[arm],"--pair-id",str(block),"--pair-order",order,
            "--pair-position",str(position),"--run-id",runid,
            "--build-flags-json",json.dumps(BUILD_FLAGS,separators=(",",":")),
            "--proxy-cpus","0,1","--loader-cpus","2,3,4,5","--mtu",str(config["mtu"]),
            "--idle-secs",config["idle"],"--idle-count",str(config["idle_count"]),
            "--flows",str(config["flows"]),"--rr-samples",str(config["rr_samples"]),
            "--rr-sizes",config["rr_sizes"],"--bulk-bytes-per-flow",str(config["bulk_bytes"]),
            "--direction",config["direction"],"--mixed-rr-interval-ms",str(config["mixed_rr"]),
            "--settle-secs",str(config["settle"]),"--low-secs","0",
            "--case-timeout-secs",str(config["timeout"])]
        if config["trim"]:
            cmd+=["--experiment-env","ZFBENCH_PACKET_POOL_TRIM_IDLE_MS=1000",
                  "--experiment-env","ZFBENCH_PACKET_POOL_TRIM_TARGET=2"]
        before_host=host_interference()
        print(f"RUN {label} starting {time.strftime('%H:%M:%S')}Z compilers={before_host['compiler_counts']}",flush=True)
        started=time.monotonic()
        with open(out/(label+"-stdout.log"),"wb") as s,open(out/(label+"-stderr.log"),"wb") as e:
            p=subprocess.run(["timeout","-k","5s",str(config["timeout"]+10),*cmd],
                             stdout=s,stderr=e,timeout=config["timeout"]+20)
        elapsed=time.monotonic()-started
        after_host=host_interference()
        try:
            res=json.loads((case_dir/"result.json").read_text())
        except Exception as err:
            print(f"ERROR case lacked a result.json: {label}: {err}",flush=True)
            raise
        if p.returncode!=0 or not res.get("ok") or not res.get("cleanup_ok") or not res.get("namespace_removed"):
            print("ERROR",label,p.returncode,res.get("errors"),flush=True)
            raise RuntimeError("invalid/failed case "+label)
        data=metrics(case_dir)
        record={"id":label,"cell":args.cell,"variant":arm,"category":category,"block":block,
                "position":position,"order":order,"valid":True,"source_sha":SOURCE[arm],
                "binary_sha256":hashes[arm],"runner_elapsed_sec":elapsed,"run_id":runid,
                "case_id":res["case_id"],"loader_sha256":res["loader_sha256"],
                "metrics":data,"host_before":before_host,"host_after":after_host,
                "result_sha256":sha(case_dir/"result.json"),
                "loader_jsonl_sha256":sha(case_dir/"loader.jsonl")}
        append(result_log,record)
        print(f"PASS {label} {elapsed:.2f}s down={data.get('down_mbps',0):.0f} Mbps "
              f"up={data.get('up_mbps',0):.0f} Mbps CPU={data['proxy_cpu_sec_per_GB']:.3f}s/GB "
              f"mixedDownP99={data.get('down_mixed_rr_p99_us')} mixedUpP99={data.get('up_mixed_rr_p99_us')}",flush=True)

    # A/A controls on two independent baseline builds. Continue without taking
    # CPU-sensitive measurements if some compilation is still running elsewhere.
    for pair in range(args.warmup_pairs):
        run("base","warmup",pair,0,"AB")
        run("fixed","warmup",pair,1,"AB")
    # Four A/A controls spread over the AB series, not only a prelude.
    for block in range(args.pairs):
        if args.aa_pairs and block in sorted({int((i+.5)*args.pairs/args.aa_pairs) for i in range(args.aa_pairs)}):
            index=block
            order=["base","aa"] if (block%2==0) else ["aa","base"]
            for pos,arm in enumerate(order):
                run(arm,"aa",index,pos,"AB" if order[0]=="base" else "BA")
        order=["base","fixed"] if block%2==0 else ["fixed","base"]
        for pos,arm in enumerate(order):
            run(arm,"ab",block,pos,"AB" if order[0]=="base" else "BA")
    for k,v in files.items():
        assert sha(v)==hashes[k], "input changed before completion"
    final={"ok":True,"cases":n,"cell":args.cell,"ended_ns":time.time_ns(),
           "file_sha256":sha(result_log),"full_cmdline":sys.argv,
           "max_load_average":os.getloadavg()}
    (out/"final.json").write_text(json.dumps(final,indent=2,sort_keys=True)+"\n")
    print("COMPLETE",json.dumps(final,sort_keys=True),flush=True)

if __name__=="__main__":
    main()
