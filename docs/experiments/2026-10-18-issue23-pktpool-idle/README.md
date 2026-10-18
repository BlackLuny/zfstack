# Issue #23 — PacketPool idle-retirement optimization (2026-10-18)

## Correction after independent P1/P2 report — 2026-10-18

**Do not merge the original PR head `63322a465c`.** The first large-cache
implementation used `swap_remove(stale_end - 1 - i)` while the cache length
was >128 and reverted to prefix `drain` below 128. This preserved borrowed
hot buffers but **did not preserve the most recently returned floor buffers
or the exact LIFO tail order**. For example, a 200-buffer cache with no reuse
and floor 10 retained an older, incorrect slice. The prior benchmark and
tests measured the wrong contract; the original performance result alone
was never enough to justify a merge.

The corrected implementation stores the free list in a `VecDeque`:
`get`/returns use `pop_back`/`push_back`, while old idle buffers retire by
`pop_front` into 32-item local arrays. This removes the 128 threshold and
preserves exact most-recently-returned **identity and ordering**, including
across batches. Callback/destruction continues strictly **outside** the mutex;
`trim_cached_to` still discards the LIFO tail by `pop_back`, and no active
buffer can be retired.

**Reproduction before/after fix:** identical release benchmark source
(`examples/issue23_trim_scaling.rs`, enhanced to verify exact pointer
identity/order) succeeds on `main` and the fix but fails with the old PR
`63322a465c` for all of:
- `200 3 0 10`: completely idle, floor > 0.
- `200 3 1 2`: one hot buffer, extra warm-floor slot.
- `129 3 3 10`: odd hot count, removal crosses 128.
- `513 3 3 0`: odd hot count, zero floor, LIFO order.

The regression test was added first and **failed** on the buggy PR for the
200/0/10 case before the fix. It passes after replacing the free list with
`VecDeque`. The test now checks newest-to-oldest pointer sequence **without
sorting**, including a second observation window, 10 specified combinations
around 128, nonzero floors, and non-multiples of 32. Existing integration
ownership, concurrency, budget-reconciliation and synchronous wake/reentry
tests remain applicable.

### New A/B data (corrected implementation vs unmodified `main`)

Both arms are the same `f467635a032e0c5ace36536d348263d8f47ee1d0`
main except for the corrected free-list implementation. Each size uses
12 alternating ABBA/BAAB blocks, two subprocesses per arm per block, nine
timed runs per subprocess for maintenance and seven for hot-path testing.
Report the median **within-block paired** percentage, with 95% block-level
bootstrap intervals (10,000 fixed-seed resamples), not independent medians
as a percent difference. Independent A/A baseline arms are also retained
in the raw data. Each subprocess is pinned to CPU 3; the same Rust 1.95.0,
Linux 6.1.0-41-amd64, release profile and test source are used on both arms.

**Correct idle maintenance operation** (all now preserve exact pointer
identity/order, allocation and verification excluded from timed section):

| Free cached / recent / floor | Baseline median | Fixed median | Paired change [95% CI] |
|---|---:|---:|---:|
| 128 / 3 / 10 | 10.15 us | 9.94 us | -2.25% [-3.14%, -1.94%] |
| 129 / 3 / 10 | 8.70 us | 8.47 us | -2.79% [-2.99%, -2.60%] |
| 200 / 0 / 10 | 11.70 us | 11.31 us | -3.49% [-3.70%, -2.94%] |
| 4096 / 32 / 0 | 806.89 us | 235.70 us | -70.82% [-71.07%, -70.70%] |
| 16384 / 32 / 0 | 12.56 ms | 0.946 ms | -92.46% [-92.49%, -92.44%] |

**Normal get/drop hot-path cost** (150,000 borrow/return iterations per
timed run; no idle maintenance in the measured region):

| Cache cap | Baseline median | Fixed median | Paired change [95% CI] |
|---:|---:|---:|---:|
| 1 | 11.50 ms | 11.40 ms | +2.33% [-1.50%, +4.61%] |
| 16 | 11.38 ms | 11.46 ms | +1.08% [+0.02%, +2.18%] |
| 128 | 11.42 ms | 11.55 ms | +2.08% [+0.75%, +2.87%] |
| 1024 | 11.44 ms | 11.57 ms | +1.20% [-1.86%, +2.72%] |

**Important tradeoff:** the corrected deque can cost around **1–2% extra
in isolated get/drop loops** depending on size; the 128-buffer result's
interval is positive, though the baseline A/A variation is not negligible.
This is not evidence of a strict CPU Pareto improvement in every operation.
The large idle-maintenance speedup is real for these test sizes but cannot
be extrapolated to per-packet service latency, throughput, physical RSS, or
real-world workloads where these large free lists are atypical. Keep the
overall issue #23 open and require actual TUN/production CPU profiling before
claiming an end-to-end win. The value here is restoring correctness while
avoiding previous maintenance-time quadratic suffix shifting.

Raw data: `repair-paired-trim.csv` and `repair-paired-hotpath.csv`.
Reproduce tables using `python3
docs/experiments/2026-10-18-issue23-pktpool-idle/summarize-repair.py`.
Bench sources: `examples/issue23_trim_scaling.rs` and
`examples/issue23_pool_hotpath.rs`. The original `paired-micro.csv`
documents the flawed original PR and must not be treated as valid
correctness/retention evidence.

### Verification after correction

- `cargo test --lib --features tokio -- --test-threads=4`:
  **319 passed, 0 failed, 6 ignored**.
- `cargo test --test packet_pool_release --features tokio`:
  **1 passed**.
- `cargo test --lib pktpool::idle_trim_tests`: targeted deterministic
  retention, ordering and wakeup tests passed.
- `cargo fmt --all -- --check` and `git diff --check`: passed.
- GitHub CI for corrected head must be checked independently.

---

## Superseded original PR #92 documentation (retained for audit only)

The section below describes the **incorrect** `swap_remove` version and
its original benchmark. It is historical evidence, **not a description of
the corrected implementation or a merge recommendation**.

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
