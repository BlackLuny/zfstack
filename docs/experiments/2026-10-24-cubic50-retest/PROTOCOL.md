# CUBIC / 50 ms / one connection: independent CPU retest

Freeze before any preflight or confirmatory attempt. The user specifically asks
to retest V5's reported +13.915% driver/relay CPU per useful GB and distinguish
environmental effects. The original two pairs (+2.568%, +25.263%) remain in the
sealed V5 study and are never pooled into this confirmation.

## Unchanged executable and workload

Use the exact frozen V5 executable, SHA256
`96695f880dd600172e3dfa5b32304da0fa296579357785b5f83dbb87ea1a26c5`.
Do not rebuild or change Rust, CC, queue, socket, rate, batching or budget policy.
Native Tokio plus real kernel upstream TCP and production `TcpStream::splice`;
CUBIC, base RTT 50 ms, one heavy download, no light peers, 100 Mbps each way,
the identical 250,000-byte FIFO each direction, MTU 1420, 16-MiB zfstack window
ceilings and unchanged Linux receive autotuning maximum 32 MiB. Seed 23.
Warmup **8 s**, measured window **20 s**, matching the original case.
Only the interface aggregate-rate setting differs: off=0, on=12,375,000 IP B/s.
Single-peer pacing counters must remain zero in every valid attempt.

## Environmental control and observation

Use a fresh owned network-isolated container, the identical retained image and
sysctls, cpuset 0–4, four-CPU parent quota and 16-GiB memory ceiling. No other
benchmark/build/suite/plot/compression/heavy analysis runs while measuring.

Both placements have the same external observer on CPU 4 and start the frozen
process with affinity 0–3. The observer uses PPID-based descendant discovery,
without relying on the unsupported proc children file or reading unrelated
process command lines. It records requested and actual per-thread masks:

- **original**: leave every benchmark thread at CPU 0–3, as in the original.
- **fixed**: production `zfb-stack` driver/relay on CPU 0, `zfb-link-up` on 1,
  `zfb-link-down` on 2, main/source/client/bridge and remaining threads on 3.
  Assignment must finish within 2 s of process launch, before 8-s warmup ends;
  late role discovery or a changed mask thereafter invalidates the attempt.

Observe each thread's proc stat CPU/ticks, schedstat runtime/runqueue wait,
available migration/context-switch counters, sampled CPU locations, cgroup
throttling and memory events, host load and window system steal/softirq data.
Read the same sources without setting affinity in original placement. Sampling
is 20 ms during the first 2 s, then 500 ms. Observer CPU is separately recorded.
Thread metrics cover their **observed lifetime**, not exact measured-window
counters. Packet counts, drops, shard runs and link lateness exposed by the
frozen executable are also lifetime diagnostics, labelled accordingly.
Native bare-host wakeup/poll counters are unsupported and are not CPU evidence.

Preflight probing established that schedstat advances on this kernel and proc
sched exists; sysfs CPU-frequency paths are absent. Do not invent frequency
data or claim exclusive cores on a shared host. Affinity removes controllable
migration/competition in the fixed placement; residual host interference is
measured, not assumed absent. No governor, priority, IRQ or host-service changes.

## Fixed sample plan

1. **Two preflights**, original/off then fixed/on, identical workload above.
   Check all raw results, required thread roles, masks, observations, payload
   errors, shutdown accounting and binary/configuration identity. These do not
   enter confirmation. Retain a failed preflight and fix/freeze instrumentation
   before the main cohort; never overwrite an attempt.
2. **128 independent confirmation attempts**: 16 rounds; each round contains
   one four-run original-placement block and one four-run fixed-placement
   block. Alternate which placement comes first, balanced eight each, with a
   fixed seed before execution. In each placement, balance eight **ABBA** and
   eight **BAAB** orders (A=off, B=on), fixed before execution. Every block has
   two repeats of each setting; the symmetric positions cancel linear time
   drift. All runs execute serially and each has a fresh stack/link/socket set.

Do not inspect intermediate effect estimates to choose sample count, window,
CPU roles or order. No outlier removal, environmental-threshold exclusions,
selective reruns, or follow-up sample/window extensions to obtain a pass.
Keep high-steal/throttled/missing observations and their verdicts.

## Estimands and decision rules

Primary metric: exact frozen-executable window **driver + relay CPU seconds per
useful GB**, not process wall time and not CPU savings from less delivered work.
For each four-run block and setting, sum the two measured driver CPU times and
divide by their summed useful GB. Compare on/off as a percent change. Report
the median of 16 block effects in each placement, plus mean and all effects.
Bootstrap the 16 **whole rounds** 10,000 times with a fixed seed for pointwise
95% intervals. The same round resamples retain the correspondence of placements
for an interaction diagnostic; do not treat runs/packets as independent rounds.

Report both same-setting A/A deltas within every block (off/off and on/on),
their absolute changes, raw CPU/second, throughput, combined link/process CPU
and observed scheduling/work diagnostics. A/A quantifies baseline variability;
it does not by itself prove that an A/B change is environmental.

All 128 observations and identities must be valid for an unqualified main
confirmation. Report both placements separately. If the CPU-effect lower bound
is above +3%, that placement demonstrates a repeatable material increase. If
the upper bound is <= +3%, useful-throughput lower bound is >= −3%, and all
observations are valid, it supports non-inferiority in that placement/window.
Bounds crossing the limit remain inconclusive. Bounds excluding +13.915% can
rule out that sustained magnitude under the retest conditions without proving
strictly zero overhead. No all-environment or familywise-confidence claim.

A clean fixed-placement result cannot replace an inconclusive original-placement
result. Do not claim that the original +25% observation was entirely caused by
the environment without causal evidence. Preserve unresolved requirements in
PR #94; these CPU data cannot resolve its separate local peak-throughput bounds.
