# RTT and factorial follow-up (frozen before measurements)

Request: improve user fairness and latency under contention, keep useful-load CPU
flat or lower, permit memory tradeoffs and modest saturated throughput loss,
and protect peak throughput with very few connections over 1–500 ms RTT.

This is a bounded candidate search, not proof of a global optimum. Local TUN
measurements cannot establish a fix for the provider ingress congestion seen
in issue #69. No production defaults are changed by this experiment.

## Factors and invariants

- Q: FIFO service control / peer byte DRR with pushout at the **emulated
  bottleneck**. Same serializer and 250,000-byte queue, both directions, 100 Mbps.
  This intervention needs control of the real queue; it is not an application
  userspace queue in front of an unknown provider bottleneck.
- S: default 64 KiB quantum / 256 KiB round cap, or 16 KiB / 64 KiB.
- P: default minimum prefetch 64 KiB / 20 ms / unbounded maximum, or
  16 KiB / 4 ms / 512 KiB maximum. S and P are deliberately bundled profiles;
  this does not identify the separate effect of each member of a profile.
- CUBIC and BBR are separate strata. Full 2×2×2 combinations in each stratum.
- Retain 16 MiB core receive/inflight limits, the production default, in every
  arm. Previous 4 MiB fixture would cap 100 Mbps × 500 ms (6.25 MB BDP).
  Kernel tcp_rmem autotuning max is recorded and must exceed this BDP.
- 32 concurrent handshake slots, identical in all arms; 20 RTT warmup with a
  four-second floor. This is setup only, not a production parallelism change.
- Heavy peer has 200 saturated download connections; three other peers each
  offer a 256-byte request at absolute 25 Hz. Two seconds idle baseline,
  12/16/20 seconds loaded measurement for screening/WAN/confirmation. Keep all
  planned requests, including missed and unfinished ones; 20 ms generator
  lag and three-second deadline gates. Tail uses planned-to-completed latency.
- Peak guard: one saturated connection, zero light clients, same link.
- Per-heavy-flow read counters at 250 ms resolution; report zero-work flows,
  maximum observed no-read duration, and Jain index within the heavy peer.
  These are observations, not a proof of bounded starvation. They cannot be
  confused with fairness between independently authenticated users.

## Sequence and selection

`rtt_matrix.py` saves the complete seeded order, source hashes and binary hash
before each phase. Every attempt is retained; no retries replacing bad samples.
No builds or other owned workloads during measurement. All runs serial and
pinned to CPUs 0–3, within a four-core cgroup quota. Record quota throttling,
host load and link scheduler lateness; shared-host noise remains a limitation.

1. Four pilots: 1/500 ms peak CUBIC, 500 ms loaded CUBIC and BBR. Pilot evidence
   never enters screening or confirmation estimates. Check actual idle RTT,
   receiver window/autotune, useful throughput, generator and setup validity.
2. Screening: two independently randomized full factorial blocks at 20 ms,
   32 runs total. All interactions are observable; two blocks are exploratory.
   A candidate needs no clear >3% throughput loss, no extra failed requests or
   zero-work users, at least 20% mean / 30% worst-user P99 reduction, and no
   clear >3% stack+link CPU/GB increase in both CC strata. Prefer the simpler
   profile when benefits cannot be distinguished. If none meets all gates,
   retain the most informative frontier candidate and explicitly label it
   unqualified rather than silently relaxing a requirement.
3. Freeze the selected candidate before WAN validation: RTT 1, 10, 50, 100,
   250, 500 ms × CUBIC/BBR × one/200 heavy flows × two balanced adjacent pairs
   = 96 runs. Each setting gets A/B and B/A; two-pair cells remain descriptive.
4. Independent 12-pair confirmation at 20 ms / CUBIC / 200 flows. Report the
   median paired change and run-pair bootstrap 95% interval (10,000 resamples);
   no screening data enters it. This cannot stand in for confidence intervals
   at every RTT. AA noise controls at 1/500 ms, both CCs and flow counts: 16 runs.
5. If a pilot or selected candidate fails a premise, stop its dependent phase,
   retain evidence and publish a protocol amendment before new experiments.

## Decision and CPU scope

Peak: no repeatable >3% loss in few-flow cells. Loaded: no >3% goodput loss,
mean and tail gains must exceed AA noise and independently confirm. No added
missing requests or completely starved peers. Report heavy-connection gaps
even when user-level latency improves. CPU/GB and CPU/s are separate: compare
stack, stack+both link threads, and whole process (including clients/observer).
Rate-capped bulk and fixed RR arrivals keep useful work close across arms;
this is not proof of CPU equivalence at every possible offered load. Memory
is whole-process HWM, not a live core-only allocation figure. An apparent CPU
win from lower delivered work is not accepted. A deployment recommendation
requires all relevant guards; a tested conditional frontier is acceptable
when no universal setting qualifies.
