# Representative WAN confirmation, frozen before observations

The user now accepts looser requirements for particularly extreme cases and
asks for sufficient repeated tests, environmental controls, assembly inspection
and/or a mathematical argument for most ordinary cases. This is an independent
confirmation of frozen V5, not a new candidate, parameter search, or extension
of a historical cohort. Keep every earlier verdict and sealed artifact intact.

## Fixed scope and execution

- Same V5 executable `96695f880dd600172e3dfa5b32304da0fa296579357785b5f83dbb87ea1a26c5`.
  No Rust/benchmark rebuild or production edit. Pre-change executable
  `74a5d0f81bea350c2a0dfd2f217dc4f373a4c0fd52979c0aba7ea0e4a7bfb518`
  is used only for the separately reported local peak.
- A new owned, network-isolated container uses the existing image
  `534d24a02c010c3a09192250287558c3be3b66738fb485454832df2ba03ff613`,
  CPUs 0–4, unchanged parent quota four CPUs, unchanged TCP/sysctl ceilings.
  No host governor, priority, service, IRQ or quota changes. No exclusive-core,
  physical-hardware, full-softirq-CPU or historical-causation claim.
- **Eight preflights**: off/on for CUBIC/1-ms/single, BBR/500-ms/single,
  CUBIC/10-ms/8-bulk+3-interactive and BBR/250-ms/8-bulk+3-interactive.
  Preflight validates identities, placement, arrivals, payload/teardown and
  counters, not treatment effects. Retain failures; amend/freeze before any
  formal runs if the fixture fails. No silent replacement.
- **576 ordinary attempts**: both CCs × RTT 1/10/50/100/250/500 ms × either
  one bulk connection without interactive traffic or eight bulk connections
  belonging to one peer plus three independent interactive peers. Each of the
  24 exact cells has 12 independent off/on pairs. Pair order is balanced six
  AB/six BA, with a frozen seed. All 24 cells run once per round in shuffled
  order, for 12 rounds, preventing a CC/RTT/workload from occupying one time
  segment. Seed 2026102501. No pooling of old samples.
- Same actual native Tokio/splice relay, MTU 1420, 16-MiB stack ceilings,
  100-Mbps bidirectional FIFO, 250,000-byte queue, no loss, seed 23. Pacing off
  is rate zero; on is 12,375,000 IP bytes/s (99% known capacity). Warmup
  max(8 s, 40 RTT); measurement 20 s, unchanged from the WAN study.
- Native driver CPU 0, link up 1, link down 2, all other ordinary threads 3.
  Interactive sender/readers explicitly request CPU 4; the external observer
  on CPU 4 preserves those self-assigned masks. Audit actual masks and arrival
  ledgers. Role assignment must finish within 2 s, before warmup ends. Native
  driver migrations after assignment must be zero. This placement controls
  migration and benchmark/stack contention; it does not represent all placements.
- **16 A/A attempts**, independently retained: CUBIC/50-ms/single and
  BBR/500-ms/single, both original CPUs 0–3 and the fixed native placement,
  two off/off pairs per cell. These run after preflight and before ordinary
  confirmation. Pure noise diagnostic; no CI from two pairs and no selecting
  a placement based on favorable A/A results. Formal placement is fixed above.
- **48 extreme local-peak attempts**, after the ordinary cohort: one/two
  actual local TUN downloads, old/V5, 12 balanced pairs per cell, two proxy
  workers, dedicated driver, MTU 65535, original process mask CPUs 0–3,
  8-s warmup/30-s useful-work window. V5 configured capacity is deliberately
  12,375,000 bytes/s; a single ready peer must bypass it even at many Gb/s.
  The external observer on CPU 4 only observes, without assigning proxy roles.
- Runs are serial, bounded, and exclusively create fresh directories. No
  build, test suite, plot, compression, PMU sampling or CPU-heavy analysis
  overlaps measurements. Inspect only progress/validity, not interim effect
  estimates. No outlier deletion, unplanned rerun, sample/window extension or
  adaptive stopping to obtain a passing result. Identity/timeout failures stop
  the plan; invalid controls and candidates remain visible.

## Statistics and predeclared decision

The unit is a complete independent run pair, never individual requests or
packets. For each metric use log(on/off) and its arithmetic mean (a geometric
mean ratio). Report all 12 paired effects, arithmetic/descriptive medians,
pointwise 95% whole-round bootstrap intervals (20,000 draws, seed 2026102502),
and a simultaneous bootstrap band across the 48 primary ordinary cell/endpoints
(24 throughput, 24 driver CPU/GB). The band uses the 95th percentile of the
maximum absolute standardized centered log-effect deviation, scaled by each
original pair-series standard error (SD/sqrt(12)); zero-variance series have
zero width. Resample entire rounds,
preserving cross-cell environmental correlation. This is an approximate
finite-sample bootstrap band, not a distribution-free mathematical guarantee.

Equal weight each exact cell when aggregating, then bootstrap the 12 complete
rounds. Report all ordinary, single-only, contended-only and each CC separately;
do not weight by request counts or hide an isolated cell behind a pooled result.
Report CPU per useful GB AND CPU/s, driver/relay, driver+emulated-link and full
process separately. Record observer CPU separately. RSS excludes kernel buffers.
Scheduler counters cover observed lifetimes, not the exact CPU work window.

Ordinary practical protection remains throughput lower >= -3%, driver CPU/GB
upper <= +3%. Also count strictly non-increasing CPU upper <= 0% separately;
passing +3% does not establish literally zero overhead. A representative-scope
recommendation needs all of:

1. Every planned ordinary sample fully observed, binary/source/config/placement
   valid, no throttling, no missing scheduled arrivals, and every candidate
   meeting the existing 3-s interactive SLO and every candidate bulk flow
   progressing during its measured window. Fully completed late controls may
   retain actual latency under the prior observation rule, but strict SLO
   failures stay flagged. Generator error never becomes a latency win.
2. At least 18/24 ordinary cells pass both primary pointwise protection bounds;
   at least 13/24 also pass both simultaneous bounds. These are proportions of
   this enumerated grid, not estimates of the fraction of users or all networks.
3. Every one of the 12 single-connection cells passes the pointwise -3%
   throughput bound. No ordinary cell shows a clear >3% CPU increase or >3%
   throughput loss (its entire pointwise interval beyond the margin).
4. The all/single/contended aggregates and both CC aggregates each pass the
   primary protection bounds. Driver+link and process CPU/GB aggregate upper
   bounds are <= +3% separately for single and contended workloads.
5. Contended aggregate user-equal mean and worst-user P99 upper bounds <= -5%;
   at least 9/12 contended cells have both latency upper bounds <= +5%, with
   no cell showing a clear >5% latency regression.

The uncapped local 10+-Gb/s, MTU-65535 test is an extreme case, separately
allowed throughput lower >= -10% and service CPU/GB upper <= +10%. Continue to
report the original +/-3% result too; do not relabel historical failures. A
clear worse-than-10% result blocks recommending this candidate. An inconclusive
extreme bound is disclosed rather than treated as a failure of ordinary WAN
performance. Every extreme payload/teardown/measurement still must be valid.

This study adds repetition, not independence from this host/kernel or coverage
of every bandwidth, MTU, loss/asymmetry, shared iface or unknown capacity.
100-Mbps results do not prove 10-Mbps/1-Gbps behavior. Even a passing result can
support automatic contention gating only after a caller supplies trustworthy
per-iface IP capacity. It cannot invent a universal default rate or estimate
provider-side queues. No automatic merge or unrequested deployment.

## Code argument and assembly

Inspect the exact frozen executable with objdump; preserve tool version,
symbol addresses, relevant disassembly and binary hashes. Explain which
configured/inactive checks remain and whether the paced loop is outlined.
Never infer cycle/CPU invariance from source line counts or assembly size.

Give an induction over `IfacePacing::allowance`: with no competing ready peer
and initially inactive state, any nonzero rate returns unlimited/false, never
allocates/charges/parks, and selects the unpaced service specialization.
This proof is independent of CC, RTT, capacity and time sequence, but requires
no previous contention (the one-second hold is explicit). It proves scheduling
semantics, not identical instruction counts, packet traces after arbitrary
history, wall time or CPU cycles. Check the actual Rust source in a standalone
contract harness before measurement; do not edit the production module.

For continuously active contention at fixed rate, derive the signed
time-credit/debt envelope, finite packet overshoot, bounded catch-up credit,
and conditional DRR service opportunity. State finite peers, advancing time,
writable sink, admitted ready data and unchanged rate assumptions. This is not
an unconditional TCP/application latency or Internet starvation theorem.

Seal raw attempts, plans, source snapshots, executable/build identities,
independent replay, figures and checksums; update the existing Draft PR.
