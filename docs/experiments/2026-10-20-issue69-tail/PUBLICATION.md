# PR evidence packaging

The measurements were made against `5830d2b5afd2e25df55bbafd950a2796eb7de04f`
with the recorded test-side patches. This PR integrates those benchmark controls
on `f467635a032e0c5ace36536d348263d8f47ee1d0`. Historical performance results
are not measurements of this newer integration base. `pr-validation/` records
the separate integration checks; no production performance qualification is implied.

`publication-manifest.json` maps every original evidence file to its published
location, original SHA256/size and stored SHA256. Large JSON/JSONL files use
deterministic gzip. The original `artifact-index.json` and its hash remain
unchanged. The original report is preserved in `original/README.md`; the main
README adds publication information and compressed-file links.

Three frozen executables are retained locally and explicitly omitted from Git.
Their hashes remain in the manifest and run identities. Rebuild the benchmark
for new runs and record the resulting identity; do not assume bit-for-bit
reproduction or relabel old data with the rebuilt executable's hash.

Verify all published original bytes and the mapping to the original index:

```sh
python3 bench/tail69_verify.py
python3 bench/tail69_summary.py
```

For packet attribution, decompress a case's `trace.jsonl.gz` into a fresh case
directory together with its `result.json.gz`, then run `bench/tail69_analyze.py` on
that directory. The analyzer can read both trace and result gzip directly. Keep newly generated
`attribution.json` separate from the frozen archive. The JSON/JSONL files,
including failed and contaminated cases, remain available for independent review.

To make new measurements, build the Linux benchmark and prepare a disposable
container named `zfstack-issue69-tail`, with the repository mounted at `/work`,
Linux TUN, iproute2 and Python3 available, and the desired CPU affinity. The
runner rejects existing case directories. Each case's `identity.json` contains
the exact historical command and environment. Run the frozen plans in order;
do not silently discard failed arrival or correctness checks.
