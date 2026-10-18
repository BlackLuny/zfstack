#!/usr/bin/env python3
"""Strict post-hoc A/B and independently built A/A analysis.

Check each frozen case count, source/binary identity, result integrity, and
hard correctness status before descriptive paired statistics. Does not
discard outliers or selectively trim noisy samples. A/A is *not* proof
of CPU non-inferiority; physical-host noise and code placement remain.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics

METRICS = [
    ("proxy_cpu_sec_per_GB","s/GB",False),
    ("down_cpu_sec_per_GB","s/GB",False),
    ("up_cpu_sec_per_GB","s/GB",False),
    ("down_mbps","Mbps",True),
    ("up_mbps","Mbps",True),
    ("unloaded_rr_64_p99_us","us",False),
    ("down_mixed_rr_p99_us","us",False),
    ("up_mixed_rr_p99_us","us",False),
    ("rss_max_bytes_samples","bytes",False),
]
LIMITS = {"proxy_cpu_sec_per_GB":("upper",2.0),"down_cpu_sec_per_GB":("upper",2.0),
          "up_cpu_sec_per_GB":("upper",2.0),"down_mbps":("lower",-5.0),
          "up_mbps":("lower",-5.0),
          "down_mixed_rr_p99_us":("upper",5.0),
          "up_mixed_rr_p99_us":("upper",5.0)}
def sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for piece in iter(lambda:f.read(1<<20),b""):
            h.update(piece)
    return h.hexdigest()

def bootstrap(xs,seed,replications=20000):
    rng=random.Random(seed)
    n=len(xs)
    if n<2: return None
    values=sorted(statistics.median(rng.choices(xs,k=n)) for _ in range(replications))
    return [values[int((replications-1)*.025)],values[int((replications-1)*.975)]]

def pct(value): return "n/a" if value is None else f"{value:+.2f}%"

def sample_metrics(records,primary,secondary,kind,seed):
    groups=defaultdict(dict)
    for rec in records:
        if rec["category"]==kind:
            g=groups[rec["block"]]
            if rec["variant"] in g: raise RuntimeError("duplicate arm in block")
            g[rec["variant"]]=rec
    details={}
    for name,unit,higher in METRICS:
        vals=[];base_ns=[];candidate_ns=[];valid_blocks=[];interference_pairs=0
        for block,arms in sorted(groups.items()):
            assert len(arms)==2 and set(arms)=={primary,secondary}, (kind,block,arms.keys())
            b=arms[primary]["metrics"].get(name)
            c=arms[secondary]["metrics"].get(name)
            if b is None or c is None or b<=0: continue
            vals.append(100.0*(c/b-1))
            valid_blocks.append(block)
            base_ns.append(b);candidate_ns.append(c)
            if any(a["host_before"]["compiler_counts"] or a["host_after"]["compiler_counts"] for a in arms.values()):
                interference_pairs+=1
        if vals:
            boot=bootstrap(vals,seed+sum(map(ord,name)))
            median=statistics.median(vals)
            threshold=LIMITS.get(name)
            inference="not_tested"
            if threshold:
                edge,limit=threshold
                inference=("nonstrict_point_pass" if ((edge=="upper" and median<=limit) or (edge=="lower" and median>=limit))
                           else "point_fail_or_noise")
                if boot and len(vals)>=12 and interference_pairs==0:
                    if edge=="upper" and boot[1]<=limit or edge=="lower" and boot[0]>=limit:
                        inference="statistical_noninferiority_only_if_AA_calibrated"
                    else: inference="cannot_qualify_noninferiority"
            details[name]={"unit":unit,"higher_is_better":higher,"blocks":len(vals),
                "median_baseline":statistics.median(base_ns),"median_candidate":statistics.median(candidate_ns),
                "paired_change_pct_median":median,"paired_95ci":boot,
                "raw_paired_pct":vals,"valid_blocks":valid_blocks,
                "compiler_interfered_pairs_at_boundary":interference_pairs,
                "interpretation":inference,
            }
    return {"block_count":len(groups),"metrics":details}

def analyze(root,require_complete=True):
    plan=json.loads((root/"frozen-plan.json").read_text())
    rows=[json.loads(line) for line in (root/"cases.jsonl").read_text().splitlines() if line.strip()]
    valid_ids=set()
    for r in rows:
        assert r["id"] not in valid_ids and r["valid"]
        valid_ids.add(r["id"])
        expected=plan["files"][r["variant"]]["sha256"]
        assert r["binary_sha256"]==expected
        assert r["source_sha"]==plan["sources"][r["variant"]]
        target=root/r["id"]
        rr=json.loads((target/"result.json").read_text())
        assert rr["ok"] and rr["cleanup_ok"] and rr["namespace_removed"] and rr["workload_passed"]
        assert sha(target/"result.json")==r["result_sha256"]
        assert sha(target/"loader.jsonl")==r["loader_jsonl_sha256"]
        assert r["case_id"]==rr["case_id"]
        assert r["loader_sha256"]==rr["loader_sha256"]
    ab=[r for r in rows if r["category"]=="ab"]
    aa=[r for r in rows if r["category"]=="aa"]
    warm=[r for r in rows if r["category"]=="warmup"]
    if require_complete:
        assert len(ab)==2*plan["pairs"] and len(aa)==2*plan["aa_pairs"] and len(warm)==2*plan["warmup_pairs"],(len(ab),len(aa),len(warm))
        assert (root/"final.json").exists() and json.loads((root/"final.json").read_text())["ok"]
    for category in ["ab","aa"]:
        keyset=[r["case_id"] for r in rows if r["category"]==category]
        assert len(set(keyset))<=1, "case config differs across variants"
    result={"case":plan["cell"],"pairs_ab":len(ab)//2,"pairs_aa":len(aa)//2,"warmups":len(warm),
            "all_cases_verified":True,
            "any_compiler_boundary_interference":any(r["host_before"]["compiler_counts"] or r["host_after"]["compiler_counts"] for r in rows),
            "meta_source":{k:v["sha256"] for k,v in plan["files"].items()},
            "ab":sample_metrics(ab,"base","fixed","ab",233),
            "aa":sample_metrics(aa,"base","aa","aa",232),
            "statistical_limit_note":"Exploratory s/GB, throughput, P99 CI. Non-inferiority thresholds not formally pre-registered by this runner. Host scheduler/compilation, A/A build drift, short P99 sample counts can dominate. Never classify an entire PR as clean based only on these.",
            "pr_handling":"Do not merge unless actual A/A and AB on suitable isolated host justify noninferiority or explicit performance tradeoff acceptance."}
    return result

def report(result):
    lines=[
        f"## {result['case']} — physical TUN paired testing",
        f"Validated cases: A/B {result['pairs_ab']} pairs, independent A/A {result['pairs_aa']} pairs, {result['warmups']} warmup runs.",
        f"Compiler observed on boundaries: {'YES (not isolated)' if result['any_compiler_boundary_interference'] else 'no at observed boundaries (not proof of full isolation)'}",
        "",
        "| Metric | Baseline median | Candidate median | AB median change [95% block bootstrap] | A/A median change [95% bootstrap] | AB blocks with compiler |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    aa=result["aa"]["metrics"];ab=result["ab"]["metrics"]
    for name,unit,_ in METRICS:
        b=ab.get(name);a=aa.get(name)
        if not b:
            lines.append(f"| {name} | - | - | insufficient samples | - | - |")
            continue
        def fmt(obj):
            if obj is None: return "n/a"
            boot=obj["paired_95ci"]
            return pct(obj["paired_change_pct_median"])+(" ["+", ".join(pct(x) for x in boot)+"]" if boot else "")
        lines.append(f"| {name} | {b['median_baseline']:.3f} {unit} | {b['median_candidate']:.3f} {unit} | {fmt(b)} | {fmt(a)} | {b['compiler_interfered_pairs_at_boundary']}/{b['blocks']} |")
    lines.extend([
       "","CPU means *entire proxy process CPU* divided by verified application GB, not only the stack. "
       "Original per-case raw files remain in the owned host evidence directory; aggregate uses full A/B and A/A pairs, never filters by observed outcome.",
       "","No netem/WG, and Python endpoints can limit link throughput. P99 is per-run of pooled loaded RR samples, "
       "not a statistically independent packet-level CI; bootstrap only over per-pair sample summaries."
    ])
    return "\n".join(lines)+"\n"

def main():
    p=argparse.ArgumentParser()
    p.add_argument("root",type=Path)
    args=p.parse_args()
    root=args.root.resolve()
    data=analyze(root)
    (root/"analysis.json").write_text(json.dumps(data,indent=2,sort_keys=True)+"\n")
    (root/"analysis.md").write_text(report(data))
    print(report(data))

if __name__=="__main__": main()
