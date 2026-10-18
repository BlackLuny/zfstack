# Issue #51 (`src/wire.rs` Checksum & Header Emission) Raw Evidence Package

This directory contains the raw JSON/JSONL benchmark outputs, steady-state GDB step counts, symbol size tables, and harness scripts referenced in [`../2026-10-17-issue51-wire-checksum.md`](../2026-10-17-issue51-wire-checksum.md).

## Files

### 1. Steady-State GDB Step Counts (`P1a`)
- `instruction-counts-warm-named.json`: Steady-state GDB single-step counts per call across 25 `wire` functions/payloads under default `x86_64-unknown-linux-gnu` (`-C opt-level=3`), recorded **after** warming up `is_x86_feature_detected!("avx2")` (`std_detect::detect::cache::detect_and_initialize` costs a one-time 315 retired instructions on the first `>= 64B` call).
- `instruction-counts-native-named.json`: Same steady-state GDB step counts under `-C opt-level=3 -C target-cpu=native`.
- `count-ir-warm-script.txt`: GDB batch script used to record exact post-warmup instruction counts per call boundary.

### 2. Multi-Architecture & Multi-Flag Microbenchmarks (`P1b`, `P2a`, `P2b`)
All microbenchmarks use 12 interleaved blocks (`ABBA/BAAB`) + `AA` baseline-vs-baseline controls, direct function calls (eliminating indirect-call predictor aliasing across `base`/`cand`/`aa`), and `-C llvm-args=-align-all-functions=6`:
- `x86_64-default-summary.json` & `rows-pcore-clean.jsonl`: `x86_64-unknown-linux-gnu` (`rustc 1.94.1`, `-C opt-level=3`, pinned P-core) summary and 324 raw block rows across 27 cases.
- `x86_64-native-summary.json` & `rows-pcore-native.jsonl`: `x86_64-unknown-linux-gnu` (`rustc 1.94.1`, `-C opt-level=3 -C target-cpu=native`) summary and 324 raw block rows across 27 cases.
- `aarch64-summary.json` & `aarch64-rows.jsonl`: `aarch64-apple-darwin` (`Apple M1 Max`, `rustc 1.94.1`, `-C opt-level=3`) summary and 324 raw block rows across 27 cases.
- `wire-microbench-harness.txt` & `aarch64-microbench-harness.txt`: Exact standalone microbenchmark harness sources used on `x86_64` and `aarch64`.

### 3. Symbol & Code Size Comparison (`P3b`)
- `symbol-sizes.json`: ELF `.text` symbol sizes (`nm -S --size-sort`) for `base` vs `cand` under `-C opt-level=3`.

### 4. Dual-Stack Sans-IO Packet-Pump Benchmarks (`pareto_probe`)
- `pump-x86_64-summary.json` & `pump-x86_64-rows.jsonl`: 4 scenarios × 6 paired blocks + AA on `x86_64-unknown-linux-gnu`.
- `pump-aarch64-summary.json` & `pump-aarch64-rows.jsonl`: 4 scenarios × 6 paired blocks + AA on `aarch64-apple-darwin` (`Apple M1 Max`).

### 5. Controlled Linux TUN End-to-End & Tail-Latency Suite (`P1b`, `P2d`)
- `tun-clean-summary.json` & `tun-clean-rows.jsonl`: 6 scenarios × 6 blocks (AB/BA + AA = 108 runs) on `root@38.207.178.131` with `--client-proxy zfstack` (full RX+TX software checksum verification when `--vnet-hdr` is off) pinned to fast vCPUs `0,1,2`, including full `rr_loaded` tail latency percentiles (`P50`, `P90`, `P95`, `P99`, `P99.9`, `Max`).
- `run-tun-clean-script.txt`: Runner script used to execute and summarize the controlled TUN benchmark suite.

## Reproduce derived statistics

Run `python3 docs/experiments/2026-10-17-issue51-wire-checksum/summarize.py` from the repository root, or add `--check` to verify generated files. `tables.md` and the five microbench/packet-pump summaries come from the same raw rows. Microbench percent changes use paired arithmetic means; times use conventional sample medians. Packet-pump changes use paired medians. Bootstrap intervals resample whole blocks with stable seeds. TUN retains its original runner and summary; its actual order is AB/BA + AA, with AA always last. Intervals are descriptive, not non-inferiority evidence. Raw observations are unchanged.
