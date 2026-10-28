# Corrected-host screening decision, before queue-depth measurements

Final binary SHA-256:
`74a5d0f81bea350c2a0dfd2f217dc4f373a4c0fd52979c0aba7ea0e4a7bfb518`.
Forty frozen attempts completed. All passed source/configuration/executable
identity audit; 39 passed request/measurement checks. No cgroup throttling.
Each cell has only two randomized block comparisons; estimates are descriptive.

Retain **q1s0p0** for queue-depth exploration: peer DRR/pushout at the controlled
bottleneck, original scheduling, original prefetch and 512/64/32-MiB budgets.
This is an informative frontier candidate, **not a qualified production default**.

Against FIFO/default at 20 ms, CUBIC paired median changes were goodput -0.01%,
stack CPU/GB -0.33%, stack+link CPU/GB -0.03%, equal-user mean -57.51%, worst-user
P99 -86.16%. BBR: +0.06%, -3.96%, -1.29%, -71.33%, -92.15%, respectively.
The independent confirmation and peak/RTT guards have not yet run.

Smaller prefetch under peer service reduces stack CPU/GB, but increases combined
stack+link CPU/GB by 6.94% CUBIC and 4.74% BBR relative to FIFO/default, without
material additional light-user latency improvement. Smaller scheduling batches
do not consistently improve both CPU strata. Keep original batch sizes.

Wide prefetch with original budgets, compared to peer/default prefetch, increases
stack CPU/GB by 5.79% CUBIC / 3.97% BBR, combined CPU by 2.46% / 1.10%, and RSS
by 59.87% / 25.46%. It improves some bulk-flow progress observations, but is not
the previously apparent CPU saving. Wide prefetch plus expanded peer budget
has one BBR run with 600 missing planned requests across two light users; keep
that failure. Its port and peer budgets are both 64 MiB, unlike the baseline
64/32 MiB headroom. Do not select this profile on its CUBIC CPU/RSS tradeoff.

Remaining limitation: all bulk connections belong to one peer. In the two BBR
q1s0p0 windows, 18/12 bulk flows had no observed reads, versus 5/5 for FIFO.
CUBIC counts were 4/6 versus 11/6. These observations prevent a blanket claim
of per-connection starvation freedom; no light-peer requests were missing in
q1s0p0. Larger queues must be evaluated with these counters retained. User-level
service at a bottleneck is distinct from fairness between a user's own flows.

Next: 250 KB / 1 BDP / 2 BDP at 100 Mbps × 500 ms, both CCs, one/200 bulk flows,
two balanced reversed three-depth blocks per cell. Same selected profile and
same final binary in all arms. Queue depth is selected only after that cohort.
