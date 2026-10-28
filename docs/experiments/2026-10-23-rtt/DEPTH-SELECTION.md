# Depth selection, before low-RTT floor or final validation runs

All 24 depth attempts passed payload/request checks and independent source,
configuration and executable audit. No cgroup throttling. Same final binary
and q1s0p0 in every arm. These two-pair cells are exploratory estimates.

Select **k = 2 BDP** for subsequent independent tests. CUBIC single-flow
throughput at 500 ms was 80.29/80.22 Mbps with 1 BDP and 96.34/96.34 Mbps with
2 BDP. Small queue: 83.90/77.49 Mbps. The larger depth has a repeatable peak
benefit, so the predeclared preference for smaller comparable depths does not
apply. BBR single-flow was about 95 Mbps at either larger depth and 65.80/67.98
Mbps with the small queue. Do not interpret noisy two-pair CPU estimates as
equivalence; BBR single-flow stack CPU/GB in particular remains uncertain.

At 200 flows, both larger depths delivered about 96.15 Mbps with both CCs.
Small-queue controls: CUBIC 72.47/78.17 Mbps, BBR 72.64/72.54 Mbps. Relative to
small peer queues, 2 BDP paired combined CPU/GB changes were -7.79% CUBIC and
-19.73% BBR; stack CPU/GB -6.81% and -13.44%. All bulk flows progressed in these
20-second windows, with stronger within-peer Jain fairness at larger depths.
Light-user means remained close to base RTT; this phase compares peer queues
to peer queues and cannot estimate the additional FIFO-to-peer latency benefit.

The logical queue ceiling at 100 Mbps × 500 ms becomes 12,500,000 bytes per
direction, excluding propagation. Whole-process peak RSS is a separate metric:
single-flow CUBIC approximately doubles versus the small queue; loaded RSS is
not monotonic with the ceiling. Larger allowed capacity is not preallocated RSS.

Next freeze the separate 24-attempt flow-floor screen using k=2 and floor
0/4/8 packets per bulk connection. The 96-run policy matrix and 24-run independent
confirmation have not started. Their final policy will be frozen after that
screen. No depth sample is reused in final estimates.
