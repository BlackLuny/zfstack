# Active-flow queue floor amendment, before any floor/policy/confirmation run

Corrected screening still observes some bulk flows without progress, especially
BBR at 200 connections. These all belong to one peer; light-peer latency and
intra-peer flow progress are different outcomes. BBR's core minimum cwnd is four
MSS (`src/cc/bbr.rs`). At short RTT, 200 such windows exceed the pipe plus a small
BDP-sized queue. This is a hypothesis for testing, not an established attribution
of every stall or a reason to enlarge uncontrolled FIFO queues.

The ongoing 500-ms depth experiment is unchanged. Its already completed CUBIC
single-flow repeats give approximately 80/80 Mbps with 1 BDP and 96/96 Mbps with
2 BDP. Finish all four depth cells before selecting the multiplier.

After that selection, independently compare **0 / 4 / 8 MTU-sized packets per
bulk connection** as a queue-capacity floor, at RTT 1/20 ms, CUBIC/BBR, 200 bulk
connections plus three fixed-arrival light users. Capacity is
`max(250000, selected_k * 12500 * RTT_ms, floor_packets * 1420 * 200)`.
All arms use peer DRR, original core parameters, the same final binary, warmup
8 s and measurement 16 s. Two randomized/reversed three-floor blocks in each
cell: **24 additional attempts**, separate from depth and final validation.

Prefer floor 0 unless both congestion controls show a consistent CPU reduction
(at least 5% combined CPU/GB, no clear >3% stack CPU regression), stable useful
throughput, valid light outcomes and no increase in inactive bulk flows. Prefer
4 over 8 if throughput/CPU are within 3% and progress is comparable. If only a
conditional result qualifies, label that boundary rather than relaxing gates.

If a floor qualifies, freeze it before the not-yet-run 96-run policy matrix and
24-run independent confirmation. The candidate becomes
`max(250000, k * B * base_RTT / 8, f * MTU * eligible_bulk_connections)`;
control stays FIFO/250 KB. At one/two connections the new floor is below 250 KB,
so it cannot reduce the selected few-flow capacity. The production implementation
would need bounded memory and an actual active-set/identity contract; this rig
has known rate, RTT and offered flow count. No deployed estimator is claimed.

Larger peer queues may increase that busy peer's own latency and do not protect
interactive traffic mixed into the same peer FIFO. They do not fix an upstream
provider queue. Preserve these limitations and all unsuccessful trials.
