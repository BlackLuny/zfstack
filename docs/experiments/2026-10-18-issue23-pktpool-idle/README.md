# Issue #23 — PacketPool idle-retirement suffix-shift optimization (2026-10-18)

## Scope and safety invariant

This is one **maintenance-path** optimization toward #23; it does not enable
automatic idle maintenance or close #23. `PacketPool::trim_idle` previously
retired up to 32 cold buffers per lock/unlock iteration using
`Vec::drain(..count)`, which moves the entire surviving suffix during each
batch. For a large cache this repeats the memmove many times while holding the
shared free-cache mutex.

For a cache **larger than 128** buffers, the implementation now retires from
the *end of the untouched prefix*, using `swap_remove` on each candidate.
Each removal is O(1) and leaves the recently borrowed tail intact. For 128
buffers or fewer, the original `drain` path is kept to avoid unnecessary
rearrangement overhead. The window age, warm floor, batch size of 32,
concurrency lock boundaries, and accounting remain unchanged. Actual buffer
drops and budget callbacks still happen **after releasing the mutex**.
No TCP state machine, normal `PacketPool::get` or `PacketBuf::drop`
operation, packet format, RTT, new timer, or public API is changed.

Correctness tests cover 513 cached packets / 510 cold candidates (over 15
retirement batches), three distinct surviving hot allocations, quota release,
in-use lifetime, concurrent get/drop/trim, and reentrant budget wakeups.

## Reproducible microbenchmark

- Exact source baseline: `f467635a032e0c5ace36536d348263d8f47ee1d0`.
- Candidate: same baseline plus this PR's production change.
- Same `examples/issue23_trim_scaling.rs` source built in two separate
  worktrees: `cargo build --release --example issue23_trim_scaling`.
- Baseline example executable SHA256:
  `4df497891f695dce875e764c334bbdb0b46e2c375a17dedbe6e15b0eef014108`.
- Candidate example executable SHA256:
  `98b69bd1e45030088089d50673e358a643e6dd9490935974d709ddcfd85c9f52`.
- CPU: Intel Xeon E5-1630 v4 @ 3.70 GHz, CPU 3 affinity
  (`taskset -c 3`); Linux 6.1.0-41-amd64, rustc 1.95.0.
- 64-byte buffers; 32 hot tail elements retained; 128 / 4096 / 16384
  cached buffers; 3 warmups plus 11 timed trials within each subprocess.
  Timing isolates only a single `trim_idle` operation; allocation, pool
  construction, initial sampling, verification and destructor of retained
  buffers are not timed.
- 12 blocks per size, each ABBA/BAAB with alternating first arm, so 24
  subprocesses per arm per size. Each raw row records the median/min/max
  of 11 trials; block-relative percent = mean(candidate subprocess medians)
  / mean(baseline subprocess medians) - 1. Report the **median of the 12
  block-relative percentages**. Paired bootstrap samples full blocks with
  replacement 10,000 times, using fixed per-size seeds, and calculates the
  median of 12 sampled block effects. Baseline A/A is the second vs first
  baseline subprocess within a block.

| Cached | Base median | Candidate median | Median paired change (95% paired bootstrap CI) | Baseline A/A median |
|---:|---:|---:|---:|---:|
| 128 | 7,023 ns | 6,989 ns | +1.42% [-6.95%, +7.95%] | +0.63% |
| 4,096 | 807,640 ns | 243,332 ns | -69.78% [-71.02%, -69.59%] | -0.36% |
| 16,384 | 12,707,338 ns | 987,696 ns | -92.22% [-92.27%, -92.12%] | +0.15% |

These figures are **local microbenchmark** observations for the maintenance
operation only. This does not prove lower end-to-end CPU, throughput or tail
latency improvements, nor physical RSS reclaim under the host allocator.
128 is indistinguishable from noise. Larger pools are uncommon with the
current default PacketPool cache sizes; those results quantify scaling, not
necessarily production gains. The full #23 burst/idle/TUN network and physical
memory qualification remains open. CI configurations and hardware may differ.

Rebuild this table using `python3
docs/experiments/2026-10-18-issue23-pktpool-idle/summarize.py` from the
unaltered `paired-micro.csv` observations. Run the example from both
baseline and candidate worktrees with the same toolchain and
`taskset -c 3 .../issue23_trim_scaling <count> 11`.

## Correctness checks

On this development host:
- `cargo test --lib --features tokio -- --test-threads=4`: 316 passed,
  6 ignored before the small-pool compatibility branch (the branch does
  not touch other paths).
- `cargo test --lib pktpool::idle_trim_tests`: all 11 passed after the final
  change.
- `cargo test --test packet_pool_release`: 1 passed after the final change.
- `cargo fmt --all -- --check`, `git diff --check`: passed.
- `cargo clippy --lib --features tokio`: passed with existing warnings.
  Strict `-D warnings` cannot pass at baseline because unrelated modules
  have pre-existing Clippy diagnostics; none reported in `src/pktpool.rs`.

This change intentionally does not close #23; the full adaptive policy,
field validation and performance non-inferiority criteria remain.
