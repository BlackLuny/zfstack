# Bottleneck-memory amendment, declared before depth measurements

The frozen fixed-250-KB WAN matrix remains unchanged and will finish. Its
high-RTT single-flow observations do not establish peak protection: e.g. BBR
500 ms varies around 31–44 Mbps, and individual CUBIC 100/250 ms pairs have
large changes in both directions. The 250,000-byte queue is only 0.04 BDP at
100 Mbps × 500 ms, even though the core/receiver windows are now adequate.

Test the memory tradeoff at the actual fair bottleneck rather than increasing
uncontrolled application prefetch. This is a new exploratory factor, not a
retroactive substitution into the existing validation matrix.

## Depth experiment

Same final corrected-host binary in every arm; scheduling/prefetch selected
prospectively by CORRECTED-PROTOCOL.md screening. At 500 ms / 100 Mbps, keep peer DRR + pushout
and compare capacities **250,000 / 6,250,000 / 12,500,000 bytes** (small / one /
two BDP) in both directions. CC CUBIC/BBR × one/200 heavy connections. Two
balanced reversed blocks of all three depths per cell, 24 attempts. Warmup
20 seconds, measured window 20 seconds, identical within every comparison.
Three fixed-arrival light users only in 200-flow cells. No prior matrix sample
enters these estimates. `rtt_queue.py` freezes order, identities and capacities.

This keeps inflight limits at 16 MiB and selected budgets fixed. If those
ceilings bind, retain that fact; do not silently increase them. A larger queue
can increase the heavy peer's own RTT and memory. Per-peer service is not a
guarantee for interactive traffic sharing that same peer or for an uncontrolled
provider ingress queue.

Select the smaller depth if it provides comparable peak throughput and CPU
(within 3%) with valid light outcomes. A larger depth needs a clear additional
benefit. Reject CPU gains accompanied by more completely inactive bulk
connections, extra missing light requests, or a repeatable >3% peak loss.
If neither depth qualifies, stop this branch and report the fixed-queue boundary.

## Independent policy validation, only after depth selection

Candidate capacity = max(250,000, k × B × R / 8), k fixed to one or two after
the depth experiment; B is **known bottleneck bit/s**, R the configured base
RTT in seconds. This is a known-capacity/known-RTT experiment, not a deployed
estimator or controller. Byte ceilings exclude the separate propagation line.

Compare original FIFO/250 KB with peer service/adaptive capacity at 1, 10, 50,
100, 250, 500 ms × both CCs × one/200 heavy flows × two balanced adjacent pairs:
96 attempts. Warmup max(8 s, 20 RTT), 20-second measured window. No old low-RTT
samples are reused after the host correction. This amendment precedes any
depth or policy run; all comparisons use the same final binary.

If a depth is selected, replace the **not-yet-run** fixed-queue confirmation
with 12 independent pairs of this frozen policy at 20 ms / CUBIC / 200 flows,
24 attempts. This is an explicit prospective amendment; no confirmation
samples are reused. If no depth is selected, keep the original confirmation.
The planned AA matrix remains. Large apparent host-peak gains will not be
claimed without suitable noise controls. No broad success claim if any
relevant outcome/CPU/throughput gate remains unresolved.
