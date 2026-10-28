# V5 fast-path continuation: frozen before measurements

The user explicitly requested another implementation attempt after the sealed
502-attempt production study. Keep that study and all its unresolved guards.
This is a new candidate and new independent data, not additional samples pooled
into the preceding V4 intervals. No rate/window/queue/CC parameter search.

## Candidate and controls

V5 keeps the 99% known-IP-capacity policy, 2-ms batches, 10-ms catch-up credit,
one-second contention hold, DRR, budgets, TCP windows and FIFO unchanged.
Move the 96-byte pacing clock out of the iface's hot inline state, allocate it
only on first competing ready peers, cache the inactive bit, and use the
allowance's active verdict rather than reloading it before every quantum.
Keep the paced service loop out of the ordinary scheduling round with a
non-inlined wrapper. No new production queue, worker or packet copy.

The V4 executable has no standalone `serve` symbol after LTO. Do not claim a
measured calling-convention penalty from its extra returned counter. The two
changes target code/data working-set size; physical measurements decide benefit.
Only one combined V5 candidate is planned in this continuation. A regression
does not authorize selecting/repeating only favorable observations.

Controls are the retained pre-change executable (SHA256 `74a5d0f8…`), V4
(`550f9732…`) and newly frozen V5. Preserve exact source snapshots, binary and
Cargo identities, command, CPU scopes, payload/teardown failures and raw ledgers.
Run core/Tokio/bench, fmt, Clippy, Python and source-inventory checks first.
Build once with Rust 1.94.1, fat LTO, one codegen unit and panic abort.

## Placement diagnostic

Use the same actual local TUN/Tokio/splice client path: two proxy workers,
dedicated driver, MTU 65535, 4-s warmup and 12-s useful-work window. The primary
comparison retains the preceding CPU 0–3 process affinity and four-CPU quota.

A second, explicitly controlled placement reduces migration/contention between
the benchmark and proxy: harness/source/client on CPUs 2–3, proxy driver on 0,
TUN reader and remaining proxy threads on 1. A separate observer on CPU 4 pins
and audits OS threads externally, so even the frozen old binary is identical.
All proxy threads must first be assigned within 2 s of benchmark launch and
remain at the requested affinity during observations after that bound. Record
requested and actual masks and errors. Do not count a failed pin as valid.
The four-CPU total quota is unchanged. Neither this diagnostic nor an A/A result
replaces the primary unrestricted physical-peak guard.

## Fixed serial phases

1. **Native smoke: 2 attempts**, V5 off/on, CUBIC/20 ms/four heavy flows,
   2-s warmup/4-s window. Light reader/writer threads on CPU 4 as in V4.
2. **Placement preflight: 2 attempts**, old then V5, one local download in
   controlled placement. If instrumentation fails, retain it and amend/freeze
   the fixture before starting the following cohorts; no hidden in-place retry.
3. **A/A: 24 attempts**, frozen old binary twice per pair, one/two downloads,
   unrestricted/controlled placement, three adjacent balanced pairs per cell.
   This is descriptive noise context (no qualified CI with three pairs).
4. **Primary physical peak: 72 attempts**, one/two downloads, 12 complete
   blocks of old/V4/V5 in each cell. Balance all six three-arm permutations
   twice, with a fixed seed before execution. Bootstrap whole blocks, 10,000
   resamples; report old→V4, old→V5 and V4→V5 separately. Keep the original
   unrestricted affinity and 12-s window. To qualify V5, require every sample
   valid, useful-throughput 95% lower bound >= −3% against old, and service
   CPU/GB upper bound <= +3%. Never infer non-regression from the median alone.
5. **Independent V5 loaded confirmation: 48 attempts**, CUBIC/BBR each 12
   adjacent balanced off/on pairs, 20-ms base RTT, 100 Mbps, 250-KB FIFO both
   directions, one peer's 200 downloads plus three fixed-arrival interactive
   peers, 8-s warmup/20-s window, 16-MiB stack ceilings and unchanged Linux TCP
   receive autotuning maximum. RR reader/writer on CPU 4; actual masks audited.
   Require all 12 pairs fully observed, no missing arrivals, every candidate
   bulk flow progressing, candidate SLOs passing. Bounds: throughput lower
   >= −3%, user mean/P99 upper <= −20%, driver/combined/process CPU per useful
   GB upper <= +3%. Preserve the original 20-ms generator gate and 3-s SLO.
6. **V5 WAN matrix: 96 attempts**, if the loaded cohort passes and the primary
   peak does not show a clear regression (a throughput CI entirely below −3%).
   Both CCs × RTT 1/10/50/100/250/500 ms × one/200 downloads × two balanced
   pairs. Same V5 off/on, unchanged FIFO/250 KB and windows, warmup
   max(8 s, 40 RTT), 20-s window. Isolate RR reader/writer on CPU 4 for heavy
   cells. This is descriptive coverage, not 12-pair protection of every RTT.
7. **Controlled physical peak: 48 attempts**, only if the primary old→V5
   throughput bound is inconclusive (median >= −3%, CI includes zero but lower
   < −3%) and the A/A median absolute paired throughput change is lower with
   controlled placement in both one/two-flow cells. Use 12 old/V5 balanced
   pairs per cell and the independently audited controlled placement. Its own
   bounds cannot erase an unqualified primary result or imply every placement
   is protected. No further sample/window extension follows this fixed phase.

If native correctness, payload, budget or candidate service progress fails,
retain the evidence and investigate before advancing. If the candidate clearly
regresses, retain/revert its production delta instead of promoting it. Keep all
failed controls; incomplete observations invalidate the corresponding whole
cohort mean claims, not just the unfavorable pair. Pointwise bootstrap CIs do
not provide familywise error control over this continuation.

Use a fresh, owned network-isolated container with the previous image/sysctls,
cpuset 0–4 and parent quota four CPUs. Runs are serial. No build, suite, plot,
compression or CPU-heavy analysis during measurements. The CPU-4 observer only
does bounded affinity bookkeeping; preserve its own CPU cost in diagnostic
metadata rather than treating it as production cost. Final CPU metrics remain
driver/relay, combined user-space link, and full benchmark process separately;
none equals full-system softirq CPU. RSS excludes kernel socket memory.
