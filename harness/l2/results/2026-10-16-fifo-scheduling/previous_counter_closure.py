"""Recheck the previous report's CPU witness without changing historical files."""
import bisect
import gzip
import json
from pathlib import Path

P = Path(__file__).resolve().parent
old = P.parent / "2026-10-16-fifo-diagnosis"
witness = json.loads((old / "causal-witness.json").read_text())["lateness_witness"]
begin, end = witness["guest_begin_ns"], witness["guest_end_ns"]
host = json.loads(gzip.decompress((old / "host-vcpu-full-3.json.gz").read_bytes()))["rows"]
maps = sum([json.loads((old / name).read_text()) for name in
            ("clock-mapping-before-stream.json", "clock-mapping-after-stream.json")], [])
low, high = max(r["offset_low"] for r in maps), min(r["offset_high"] for r in maps)
assert low <= high
starts, ends = [r[0] for r in host], [r[1] for r in host]
i, j = bisect.bisect_left(ends, begin+low)-1, bisect.bisect_right(starts, end+high)
before = {r[0]: r[1]+r[2] for r in host[i][2]}
closed = {}
for cpu in before:
    for k in range(j+1, len(host)):
        a = next(r for r in host[k-1][2] if r[0] == cpu)
        b = next(r for r in host[k][2] if r[0] == cpu)
        if a[3] == b[3] == 3 and a[1:3] == b[1:3]:
            closed[cpu] = {"cpu_delta_ns": b[1]+b[2]-before[cpu]+1000,
                           "sample_index": k, "sample_window_ns": host[k][:2]}
            break
result = {"source": "../2026-10-16-fifo-diagnosis/host-vcpu-full-3.json.gz",
    "guest_begin_ns": begin, "guest_end_ns": end, "clock_offset_width_ns": high-low,
    "settled_endpoints": closed,
    "all_vcpu_cpu_upper_bound_ns": sum(r["cpu_delta_ns"] for r in closed.values()) if len(closed) == len(before) else None,
    "begin_index": i, "ordinary_end_index": j,
    "ordinary_host_sample_window_ns": [host[i][0], host[j][1]],
    "interpretation": "Retrospective counter closure, not new execution. Do not use earlier arbitrary running counters alone as CPU upper bounds."}
(P / "previous-witness-counter-closure.json").write_text(json.dumps(result, indent=2)+"\n")
print("previous witness settled bound ns", result["all_vcpu_cpu_upper_bound_ns"])
