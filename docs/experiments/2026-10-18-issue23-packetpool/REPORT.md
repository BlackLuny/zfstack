# PacketPool release order and zero-cache lock: Issue #23

## Scope and build identity

- **2026-10-18, shared x86_64 Linux development server**, pinned to logical CPU 2, sibling CPU 6, governor `schedutil`. This is **only a microbenchmark diagnostic**, not calibrated performance QA.
- **Baseline** `97c4b285d89ada5cf76ef9def43f9bbf663a5906` (latest `main` at the frozen run); **candidate source** `42675eacfc37a96c222e1559de919c82e75fe305`.
- The PR is published through the GitHub Git Data API because the SSH gateway's HTTPS git push lacked write permission. Its source/test file Git blob SHAs are identical to the locally built candidate. The code candidate source tree SHA before this evidence-only commit was `6ef8cf2f34876c5f12c09d8b797a97611ca9cb3c`.
- Three independent Cargo release example binaries (A1 and A2 of baseline, B of candidate), Rust 1.94.1, locked deps, LTO=fat, codegen-units=1, panic=abort, each with a separate target directory and SHA-256 identity. See `build-manifest.json` and `frozen-plan.json.gz`.
- 9 cells: `cache0`, `cache32`, `overflow33` × capacity 64, 1500, 65536 bytes. 4 A/A paired controls and 12 A/B paired formal comparisons per cell; balanced randomized pair direction, seed 23. 288 valid formal processes + 27 valid warmup processes = **315 / 315 successful**, 0 invalid, 0 missing or substituted.
- Separate correctness regression: **baseline fails** due to release wakeup preceding backing deallocation in 4 owner scenarios; patched PacketPool passes all 7. See `baseline-release-red.log` and `candidate-release-green.log`.
- Final rebased candidate: `cargo +1.94.1 test --locked --release -p zfstack --features tokio,test-peer` = **315 passed, 7 previously ignored, 0 failed** across six test suites. See `full-release-tests.log` and invocation metadata.

## Paired CPU results (percent change, lower is better)

Ratio is A2/A1 for A/A, B/A1 for A/B. CPU is **whole-child rusage CPU time**, including process setup, priming, correctness checks, cleanup and output, divided by the loop packet count where indicated; it is not isolated loop CPU. The separately recorded `loop_wall_ns` measures only the timed Rust loop. Intervals are descriptive percentile bootstraps of the paired median (10,000 pair resamples, seed 23). Intervals are not formal independence or host-calibration evidence.

| Case | Capacity | A/A median CPU % | B/A median CPU % | B/A bootstrap 95% interval (CPU %) | B/A median loop-wall % |
| --- | ---: | ---: | ---: | ---: | ---: |
| `cache0` | 64 | -0.82% | -11.20% | [-13.06%, -10.94%] | -11.24% |
| `cache0` | 1,500 | +0.31% | -10.29% | [-11.60%, -9.01%] | -10.22% |
| `cache0` | 65,536 | +0.65% | -11.20% | [-11.94%, -10.21%] | -11.18% |
| `cache32` | 64 | +1.38% | +0.31% | [-2.99%, +1.87%] | +0.74% |
| `cache32` | 1,500 | -2.59% | +0.89% | [-0.01%, +3.51%] | +1.33% |
| `cache32` | 65,536 | +0.86% | +1.15% | [-0.18%, +4.78%] | +1.26% |
| `overflow33` | 64 | +1.39% | -0.37% | [-1.29%, +2.45%] | -0.33% |
| `overflow33` | 1,500 | +0.02% | +0.50% | [-0.53%, +3.39%] | -0.03% |
| `overflow33` | 65,536 | +1.11% | +0.73% | [-2.26%, +5.55%] | +0.46% |

The zero-cache cells show ~10–11% lower measured CPU under this synthetic process workload and structurally avoid **both** free-cache mutex acquisitions (in get/drop). The 32-cache-hit and 33-return workloads show small mixed changes, within or near the observed noise; no general speed-up or no-regression proof is claimed for those cases. A/A controls are particularly noisy for some cache32/overflow33 cells, with broad bootstrap intervals.

## Correctness and limitations

- The regression uses a separate `GlobalAlloc` observer (not in the benchmark) and a synchronous budget `Wake` callback that re-enters `PacketPool::get`; it checks data deallocation happens before budget release and no deadlock occurs when the cache is full. Also exercises cloned `Bytes` slices, idle/explicit trim and last-owner drop. Source regression is `tests/packet_pool_release.rs`.
- The change neither adds a task/timer nor modifies active cache sizes, charging rules, borrowed buffer ownership, networking protocol, admission/fairness logic or waiting policy. The original Issue #23 (autonomous idle-cache decay, real TUN/WG multi-peer + memory/RSS acceptance) **remains open**.
- The probe touches only 64 initialized bytes per packet even when backing capacity is 65,536 bytes. It has no real network, no TCP, no concurrency/contending packet threads, no induced memory pressure, no RSS/allocator-rate metrics, no throughput or network latency, no queue depth/timer rate and no P99. There is no claim of reduced RSS or end-to-end throughput gain.
- The shared host is not isolated. CPU governor is dynamic and unrelated processes can run at any time. Endpoint load averages were [1.076171875, 2.78564453125, 3.29736328125] and [1.79931640625, 2.82568359375, 3.29443359375]. Compiler process snapshots at those two endpoints were 0 and 0 respectively. These observations cannot rule out in-window interference.
- Raw paired timing/CPU/probe output is preserved **without excluding outliers**. Any CPU percentage in this report is per-cell median of paired ratios, not a global combined number. The initial exploratory baseline `ddc2edf` was superseded by the exact latest-main baseline `97c4b28` used in this report.

## Reproduction and evidence

1. Read `PROTOCOL.md`. The code in `probe.rs.txt` is **only an external diagnostic Cargo example**, not a production source modification.
2. Run the immutable-source `build_probes.py` with baseline and candidate commits shown above. Exact commands, source/archive/input hashes, toolchain and binary hashes are in `build-manifest.json`.
3. Run `run_probes.py --build-manifest build-manifest.json --output NEW_DIR --cpu 2` only after completing all builds, ideally holding the project QA host lease. It writes `frozen-plan.json` first, then retains every invocation and environment snapshot.
4. Recompute with `python3 summarize.py NEW_DIR/samples.jsonl`. The output data from the frozen run is in `summary.json.gz`; raw `samples.jsonl.gz` and `frozen-plan.json.gz` can be decompressed using `gzip -d -c`.
5. All compressed files are deterministic gzip with mtime=0. `SHA256SUMS` lists shipped artifact bytes. The original host paths embedded in the manifest identify where the recorded run occurred; change only the source/binary paths for a new run, without claiming the new run is the frozen one.
