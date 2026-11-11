# Current-main TX capacity and prefetch screening

Frozen source baseline: `9d6fb7684e3842da16e673825d3c9ace724aa433`.
This is a bounded screening experiment, not production qualification. The only
Rust additions are benchmark controls and their input/identity checks; `src/`
is unchanged. All arms use one release binary (Rust 1.94.1, fat LTO, one codegen
unit, panic abort, locked dependencies), so build-layout differences cannot be
confounded with configuration. SHA-256 identities are recorded before runs.

## Questions and arms

- A: existing configuration, no override.
- B: `max_snd_buf = 262144` bytes, no prefetch override.
- C: `prefetch_max = 65536` bytes, no total TX override.
- D: both B and C. This is an explicit interaction control.

These controls do not change `max_snd_inflight`, congestion control, physical
budgets, queue limits, ingress policy, or driver scheduling. The total TX cap
includes unacknowledged data; prefetch-only preserves the existing flight cap.
Server-mode benchmark defaults use a 4 MiB flight cap. Client mode uses the
existing client profile, splice, dedicated driver, MTU 65535, no offload.

## Frozen screening matrix and order

1. Client TUN, eight downloads, 12 seconds / 2-second warmup.
2. Client TUN, eight uploads, same duration (negative/direction control).
3. L1 isolate: CUBIC, 200 heavy connections on one Peer, three light Peers,
   100 Mbps, 10 ms RTT, 250000-byte FIFO in each direction.
4. L1 isolate: CUBIC, one heavy connection, three light Peers, 100 Mbps,
   80 ms RTT, 2000000-byte FIFO in each direction (BDP regression guard).

Isolate runs use 12 seconds / 2-second warmup, two-second idle baseline,
256-byte requests at an absolute 25 Hz per light user, independent sender and
reader, three-second deadline. Existing generator maximum lateness remains
20 ms. Every loaded user should have 250 scheduled requests; verify the actual
saved plan. Do not replace open-loop failures with closed-loop samples.

For every case, execute an A/A pair first, then three independent ABBA blocks:
A/B/B/A, A/C/C/A, A/D/D/A. Case order and candidate-block order are balanced
using the frozen seed 20261023. This yields 56 measured attempts, two baseline
calibration runs and two paired comparisons per candidate per case. A separate
setup smoke runs before these attempts; it never enters the comparison.

This small matrix detects large regressions and generates hypotheses; it cannot
certify 1–3% changes. All attempts, errors, timeouts and outliers are retained.
No additional attempt substitutes for a failed one. A broken environment can
stop the remaining plan, which must then be reported incomplete.

## Metrics and decision boundary

- Delivered bulk goodput, separately by direction. Ratios below 0.97 reject a
  universal default in the tested cell; small changes remain inconclusive.
- Client proxy CPU seconds/GB and CPU seconds/second; server stack-thread CPU
  separately from link/whole-harness CPU. Saturated cases do not establish CPU
  non-regression at a fixed useful workload. Whole-machine counters on a shared
  host are diagnostic only. Save cgroup throttling counters before/after.
- Per-user request mean, P50/P95/P99 (nearest rank), worst-user P99, maximum,
  counts and arrival validity. Average user means with equal user weights;
  missing requests make unconditional means/percentiles unknown and are reported
  explicitly. No P99.9 claim from these sample sizes.
- Payload errors, zero-progress seconds where available, link drops, retransmits,
  process peak RSS (whole lifecycle), and connection cleanup. Isolate does not
  currently have per-heavy-flow progress telemetry: do not claim starvation
  freedom from an aggregate heavy counter.
- CPU reductions and user-latency reductions are observations, not automatic
  adoption. Promotion requires a separately frozen longer confirmation: at
  least 12 pairs, fixed useful load, BBR, MTU1500, bidirectional traffic, multiple
  users/ports, late arrival, tight memory, idle/reburst and high RTT/loss.

## Scope and environment

Linux TUN runs in a dedicated privileged Docker container with its own network;
no host networking or external service modifications. The shared cloud executor
has a four-CPU cgroup quota and five allowed CPUs; tests use CPUs 0–3. No physical
CPU isolation, frequency control or PMU availability is claimed. Build and test
work finishes before sequential measurements. Kernel/link/loopback work remains
part of this local topology; these are not real VPS/WG results.

Evidence is written to a fresh `qa/results/capacity-20261023/` directory. The
runner refuses overwrites, saves command/configuration/raw output and source,
binary and plan hashes, and checks binary identity throughout. A small summary
and final report are retained beside this protocol. Do not relabel the older
OVH capacity results or the failed ZFC #895 experiments as this build's results.
