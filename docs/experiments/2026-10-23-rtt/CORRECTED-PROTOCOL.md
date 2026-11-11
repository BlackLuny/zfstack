# Final corrected-host protocol, frozen before screening

The missing bare-Shard budget wakeup invalidates use of old high-load CPU and
flow-liveness observations to select production parameters. Retain those
exploratory cohorts and all three diagnostic versions, but repeat selection
and validation using a new frozen binary with writer recovery enabled in every
arm. No production library default changes. See HOST-RETRY.md.

## Repeated joint-factor screening

CUBIC/BBR, RTT 20 ms, 100 Mbps, 200 bulk connections in one peer and three
separate light peers, each 25 fixed requests/s. Eight original Q/S/P combinations
(FIFO versus peer DRR; 64/256 versus 16/64 KiB scheduling; default versus small
prefetch), plus wide prefetch with original and expanded peer budgets, both
under peer service/default scheduling. Two independently randomized complete
blocks: **40 attempts**. Warmup 8 s, measurement 12 s. Original 16-MiB send and
receive windows, 250-KB bottleneck queue. All ten profiles use the same binary.

Wide profiles are exploratory memory-for-CPU options, not presumed unsafe based
on the old fixture. Select original scheduling/prefetch unless a change gives
consistent CPU improvement in both congestion controls without more missing
light requests or persistently inactive bulk flows. Small sample estimates are
descriptive, not significance claims. Do not select a low CPU number produced
by doing less useful work. Guards remain necessary for any selected profile.

## Queue and final validation

Use QUEUE-CAPACITY.md for the independent 24-run depth experiment and 96-run
six-RTT policy matrix. All now use the corrected binary. Do not reuse V1 low-RTT
cells. Freeze the chosen scheduling/prefetch/depth before this validation.
Then run 12 independent adjacent balanced pairs at CUBIC/20 ms/200 flows
(24 runs); bootstrap whole paired runs, 10,000 resamples, 95% interval.

Noise controls: **16 A/A runs** at 1/500 ms, both CCs, one/200 bulk connections;
both sides are identical FIFO/default configuration, warmup max(8 s, 20 RTT),
measurement 20 s. These quantify local drift; they cannot remove all shared
host noise or certify other hardware.

Few-connection peak guards: **12 runs**, CUBIC one/two connections and BBR one,
two balanced pairs per case, warmup 4 s/measurement 12 s, base RTT zero and a
10-Gbps serializer above the host's observed peak. Candidate queue still runs;
it is not bypassed by a zero-rate setting. No peak increase is claimed from
noisy two-pair results. A repeatable >3% loss rejects a universal default.

All plans, order, binary/source identities, request ledgers, per-flow progress,
CPU scopes, memory and cgroup deltas are retained, including invalid attempts.
Report observations separately from finite-window starvation guarantees.
