# Loaded-regime amendment, before any alternative-policy run

The 96-run 2-BDP matrix remains intact and continues to map its limits. Partial
independent evidence already prevents a universal low-CPU/default claim:

- CUBIC/50 ms/200 flows: both pairs pass measurement checks, but combined
  CPU/GB rises about 11.54%, stack CPU/GB about 14.70%, despite strong latency
  gains and only 2.07% goodput loss.
- BBR/500 ms/200 flows: candidate attempt 026 has eight heavy connection setup
  timeouts (20-second deadline), so the cell is not validly confirmed. All light
  requests complete. Do not count the eight absent connections as serviced or
  equate this failure with generator noise. Long queues within a busy peer are
  a possible contributor, not a proven complete causal attribution.

Depth screening had already shown that **1 BDP sufficed for 200-flow goodput at
500 ms**, whereas CUBIC single-flow required 2 BDP. Test that distinction instead
of treating one queue depth as universally optimal.

## Bounded alternative

Keep the same final binary, peer service and original core parameters. For the
known 200-flow saturated regime use `max(250000, B * base_RTT / 8)`; for the
one-flow regime retain 2 BDP. This is a comparison of two configured operating
regimes, not an implemented online concurrency threshold or transition policy.

Run **32 new attempts**: CUBIC/BBR × RTT 50/100/250/500 ms × 200 flows × two
adjacent balanced pairs, direct FIFO/250-KB control against peer/1-BDP candidate.
Warmup max(8 s, 20 RTT), measure 20 s, all original outcome/CPU guards retained.
Order is frozen by the new `loaded` phase of rtt_queue.py. Retain all failures.

At 1/10 ms both formulas produce exactly 250 KB, and one-flow configuration is
unchanged at all six RTTs. Those unchanged cells retain their existing final-
binary independent observations, with source-cohort provenance shown explicitly.
Do not relabel the new 32 runs as a second complete 96-run experiment or silently
overwrite any result from the original 2-BDP policy, including attempt 026.

The not-yet-run **12-pair CUBIC/20-ms/200-flow confirmation** now tests the loaded
1-BDP rule (250 KB) versus FIFO/250 KB. This isolates peer service at that cell;
it cannot independently confirm every RTT or the few-flow regime. A/A and
host-peak guards remain unchanged. The complete 2-BDP matrix is reported as a
separate alternative, not pooled with changed cells.

After this bounded continuation, report the measured conditional frontier and
any remaining CPU, setup, or throughput failures. If one regime rule does not
pass all relevant cells, do not describe it as a universal optimum or a proven
starvation-free production adaptation. Intermediate flow counts, heterogeneous
RTTs, transitions and uncontrolled provider ingress remain untested.
