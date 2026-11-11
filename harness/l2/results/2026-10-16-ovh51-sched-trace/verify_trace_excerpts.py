"""Independently reparse retained event excerpts; compare registered late windows.
Excerpts retain 50 ms context around every >=5 ms sample. Full traces remain on
OVH with hashes, as prescribed by PLAN.md. No new measurement is performed.
"""
import json
import shutil
import tempfile
from pathlib import Path
import analyze_trace
from retained_evidence import evidence_root

HERE = Path(__file__).resolve().parent
E = evidence_root()
results = {}
for name in ("trace-2", "trace-3"):
    source = E / name
    with tempfile.TemporaryDirectory() as directory:
        dest = Path(directory)
        for file in ("host-before.json", "processes.json", "trace-stats.json"):
            shutil.copyfile(source / file, dest / file)
        shutil.copyfile(source / "late-windows.txt.gz", dest / "trace.txt.gz")
        for file in source.rglob("udp-*.json"):
            if file.stem.split("-")[1].isdigit():
                shutil.copyfile(file, dest / file.name)
        analyzed = analyze_trace.analyze(dest)
    full = json.loads((source / "analysis.json").read_text())
    assert analyzed["valid"], analyzed["invalid_reasons"]
    assert analyzed["late_samples"] == full["late_samples"], name
    results[name] = {"valid": analyzed["valid"], "late_samples_match_full_trace_analysis": True,
                     "late_samples": analyzed["late_samples"],
                     "scope": "local reparse of raw event excerpts; whole-trace drop counters checked separately",
                     "excerpt": json.loads((source / "late-windows-index.json").read_text())}
(HERE / "trace-excerpts-recomputed.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps({name: {"valid": r["valid"], "match": r["late_samples_match_full_trace_analysis"]} for name, r in results.items()}))
