# Issue #23 PacketPool diagnostic protocol

This is a diagnostic of the public production `PacketPool::get` and
`PacketBuf::drop` APIs. The candidate repairs the lifetime ordering of discarded
backing allocation and its budget lease, and may avoid the free-cache mutex on
the zero-cache return path. Correctness of allocation-before-lease destruction
must be established by separate regression tests; timing cannot establish it.

The probe has no allocator instrumentation, synthetic mutex, simulated pool,
Tokio runtime, network traffic, budget waiter, or contention thread. It does not
qualify end-to-end throughput, packet latency, fairness, RSS, a network operating
point, or a Pareto improvement. The host is shared, not a calibrated dedicated
performance host. No benefit is assumed in advance.

## Source and build identity

- Baseline `A1` and `A2`: separate exports and separate Cargo target directories
  of `ddc2edfff7b49134c247485af0dd72dc44cfea6b`.
- Candidate `B`: the exact production commit selected and recorded by the main
  runner before building. Its authoritative identity is the build manifest's
  `B` entry, embedded again in `frozen-plan.json` before measurement.
- `probe.rs.txt` is generator input. Copy its bytes unchanged to
  `examples/issue23_packetpool_probe.rs` in each temporary source export. It is
  an ordinary Cargo example using public APIs; no production module needs an
  include, feature, instrumentation, or source edit. These temporary exports are
  measurement inputs, not a new production build path. The input and evidence
  may be published with repository documentation.
- Use Rust `1.94.1` for all three builds, locked dependencies, release mode,
  fat LTO, one codegen unit, `panic=abort`, and incremental compilation disabled.
  A normal example supports that panic policy; `cargo test` would not establish
  the same build contract.

The main runner owns transfer, source export, build, environment capture, and
execution. A representative build inside each export is:

```sh
CARGO_INCREMENTAL=0 CARGO_PROFILE_RELEASE_LTO=fat CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 CARGO_PROFILE_RELEASE_PANIC=abort CARGO_TARGET_DIR=/absolute/unique/target cargo +1.94.1 build --locked --release --example issue23_packetpool_probe
```

Record source commit/tree, probe and exported production-file hashes, Cargo.lock
hash, the complete command, a whitelist of build-related environment variables,
`rustc +1.94.1 -Vv`, Cargo version, build log/exit status, and binary SHA-256.
Use identical explicit flags across arms; record `RUSTFLAGS`,
`CARGO_ENCODED_RUSTFLAGS`, build target, target CPU flags, linker flags, and
profile overrides if present. Do not infer these from the requested settings.
Do not compile an arm during measurement. Verify the frozen binary hashes before
and after the measurement window.

## Workload

CLI: `issue23_packetpool_probe CASE BUF_SIZE ITERATIONS`.

| Case | Cache capacity | One measured iteration | Formal iterations | Formal packets |
| --- | ---: | --- | ---: | ---: |
| `cache0` | 0 | Get, write/check 64 bytes, drop one packet | 1,000,000 | 1,000,000 |
| `cache32` | 32 | Get, write/check 64 bytes, return one cached packet | 1,000,000 | 1,000,000 |
| `overflow33` | 32 | Borrow/hold 33 packets, write/check each, then return all 33 | 30,304 | 1,000,032 |

Run every case at buffer capacities 64, 1,500, and 65,536 bytes: nine cells.
The probe writes and verifies only `min(buf_size, 64)` initialized bytes per
packet, using the fixed seed-23 pattern `(23 + 37 * byte_index) mod 256`.
All selected sizes therefore touch 64 bytes per packet. A 65,536-byte buffer
does not represent a fully written 65,536-byte payload or a measured RSS delta.
`capacity_bytes` is capacity multiplied by packet operations, not live memory,
payload bytes, allocation traffic, or resident bytes.

The byte comparison and checksum read are inside the timed loop. A compiler
`black_box` is applied to the actual returned byte slice; this is an optimization
barrier, not a lock or allocator instrument. Each successful `get` also verifies
that the returned capacity equals the configured size. These costs are part of
the workload and can dilute a small `drop` improvement.

For `cache32` and `overflow33`, each process first borrows 32 packets together,
then validates and returns them to prime the cache. That 32-packet setup is
outside `loop_wall_ns` and is reported separately as `priming_packets` and
`priming_checksum`. `cache0` has no priming packets. The 33-element holding Vec
is allocated once before timing and reused without growing. In `overflow33`,
each batch begins with 32 cached buffers, holds all 33 simultaneously, and ends
with 32 cached returns plus one cache-full discard. The measurement is for the
complete 33-packet batch; it does not isolate the time of that one discard.

The probe keeps a 64 MiB global budget and the default peer share. The largest
simultaneous backing charge in this workload is 33 × 65,536 = 2,162,688 bytes,
so no budget-pressure or wakeup workload is intentionally introduced.

## Warmup and fixed measurement sequence

For each arm/cell, run a separate warmup process before its formal measurement
window. Retain its raw result with `warmup=true`, but exclude it from paired
statistics. Warmup uses 10,000 iterations for `cache0` and `cache32`; it uses
304 iterations, or 10,032 packets, for `overflow33`. Warmup has the same setup,
verification, and cleanup as the corresponding formal process. The probe itself
adds no hidden warmup loop.

Precompute and persist all 315 slots in `frozen-plan.json` before running any
sample. Use Python `random.Random(23)`. For each phase and cell, shuffle a
balanced list of forward/reverse pair directions: two of each for A/A, six of
each for A/B. Shuffle the nine-cell order separately for each pair index, then
run each cell's two arms consecutively in its precomputed direction.

First run 18 A/A warmup processes (`A1` and `A2` for all nine cells), then all
four formal A/A pairs for every cell. Next run nine B warmup processes, then
all twelve formal A/B pairs. A1 is not warmed a second time for A/B. Warmups
carry their enclosing `phase=AA` or `phase=AB`, `warmup=true`, and `pair=-1`.
Formal rows carry `warmup=false` and zero-based `pair` indices 0–3 or 0–11.
Every pair uses identical case, size, iteration count, and probe. There are
288 formal processes: 9 cells × (4 + 12) pairs × 2 arms, plus 27 warmups.

Hold the project QA lease using the actual `socket.gethostname()` domain and
apply the same CPU affinity to all arms. Record affinity, CPU model/topology,
kernel, relevant governor/frequency configuration, load and CPU counters, and
compiler/build-process observations. A lease coordinates participating tasks;
it does not prove that the host or pinned CPU is otherwise idle.

The fixed admission policy is a diagnostic on a shared host. All builds owned
by this task finish before the runner starts. Foreign compilers are recorded
without excluding samples or delaying/retrying individual slots. Capture
process/load/CPU-counter observations before and after the complete window;
these endpoint observations cannot establish which individual samples were
affected by other activity between them. Compiler observations use a process-name
list and are not exhaustive. CPU information and topology/governor/frequency
settings are recorded before the window only; those settings are context, not
evidence of stable frequency or calibrated conditions throughout the window.

Complete the fixed sequence regardless of observed ratios. Do not terminate,
retry, replace, or select samples because they appear slow, noisy, or unfavorable.
The fixed per-process timeout is 30 seconds. A timeout or necessary execution
failure may leave an incomplete window; retain that slot's partial output,
failure details, CPU/time observations, all earlier samples, and the final
environment/binary-identity observations. Never label that window complete or
stop another task's compiler.

## Measurements and checks

Each successful process emits one JSON object, ending in a newline. Failure
aborts with a nonzero exit and no successful summary. Retain stdout, stderr,
exit status, timing, and environment observations for every invocation.

- `packets` is the exact number of get/drop packet operations in the loop.
  `bytes` is the exact number of initialized bytes written and checked there.
  `checksum` must equal the independently calculated `checksum_expected`.
- `loop_wall_ns` is Rust `Instant` elapsed time around the loop only. It excludes
  argument parsing, pool construction, cache priming, end-state validation,
  trimming, and JSON output.
- The runner obtains process CPU from child `rusage` user + system time. This
  includes process startup, priming, all verification, cleanup, and output; it
  is not an isolated-loop CPU measurement. If shown as CPU ns per packet, state
  that the full process CPU is divided by the formal loop packet count, with
  these additional costs included. Child rusage needs to be attributed to the
  specific invocation, not accidentally accumulated across previous children.
  The actual runner launches children serially and records a
  `RUSAGE_CHILDREN` before/after delta for each invocation. Raw `cpu_sec`,
  `cpu_user_sec`, and `cpu_system_sec` are in seconds; the summary converts
  `cpu_sec` to nanoseconds by multiplying by 1,000,000,000. `process_wall_ns`
  covers the runner's spawn/wait interval and is distinct from the probe's
  `loop_wall_ns`.
- After the loop, `cached` is 0 for `cache0`, otherwise 32; `in_use` is always
  zero. `reserved`, `peer_reserved`, and `port_reserved` each equal
  `cached * buf_size`. These are budget ledgers, not allocator or RSS readings.
- Cleanup trims the cache to zero and checks zero cached/in-use counts. After
  dropping the pool, all three final reserved counters must be zero. The probe
  holds no borrowed buffers at cleanup.

The timing binary contains no allocator counter, mutex counter, waker callback,
or other code added to the library. Any separate allocation-order regression or
lock-count evidence must use a separate build and be reported separately.

## Paired reporting and scope

For each of the nine cells, retain and report all four A/A ratios and all twelve
A/B ratios. Use `A2/A1` for A/A and `B/A1` for A/B. Compare paired process CPU
and paired loop wall time separately. Describe the median paired ratio and
percentage change `(ratio - 1) * 100`; negative values mean lower measured cost.
If descriptive bootstrap intervals are used, freeze seed 23, 10,000 resamples
of pairs, and the interpolated 2.5th/97.5th percentiles of resampled medians.
Apply the percentage conversion to both endpoints as well as the median.

A/A measurements show repeatability of separate baseline builds and executions
on this one host window. Four A/A pairs provide little distributional evidence;
twelve A/B pairs and a bootstrap interval do not establish independent host or
workload replication. Do not pool buffer sizes or cases into one improvement
number, hide incomplete pairs, filter noise, or make a formal performance PASS
claim from this diagnostic. Report significant host interference and material
A/A variation alongside any candidate ratios.

The zero-cache source path can support the specific structural observation that
one `drop` free-cache lock is eliminated; `get` still has its existing lock.
Measured `cache0` CPU also includes allocation, memory-budget accounting,
buffer validation, and destruction. `cache32` is a retained-cache control.
`overflow33` exercises discard ordering once per 33 operations and has a large
cache-hit component. Their measurements must not be presented as the isolated
cost of the changed instructions or as network throughput gains.

## Summary input contract

`summarize.py RAW.jsonl` reads the actual `run_probes.py` schema: `phase`
(`AA` or `AB`), boolean `warmup`, `case`, `size`, `arm` (`A1`, `A2`, or `B`),
`pair`, `iterations`, sequential `index`, integer `returncode`, `cpu_sec`,
`cpu_user_sec`, `cpu_system_sec`, `process_wall_ns`, and `probe` (parsed probe
JSON). A passing invocation requires `returncode=0`; the probe object separately
contains `status=PASS`. There is no top-level PASS field. Raw stdout/stderr,
the command, `timed_out`, failure/validation errors, and other metadata remain
in the runner evidence. Recorded timeout or error flags invalidate that row.

The summary independently checks packet/byte/checksum and ledger expectations,
lists malformed or invalid rows, and detects duplicate, missing, or wrong-arm
pairs. It also checks all 27 required warmups, the 0–314 execution indices, and
the CPU user/system sum. It reports complete and incomplete cells without
replacing samples.
Statistics over an incomplete cell are descriptive of only the available valid
pairs and are explicitly marked incomplete. `complete_planned_window` means the
288 formal records and 27 warmups matched the declared workload and indices;
it is not performance PASS, host calibration, or a substitute for auditing the
runner's fixed order, environment, raw evidence, and binary identity. The summary
does not exclude a sample because a foreign compiler was present.
