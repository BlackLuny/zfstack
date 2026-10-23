# Independent confirmation of the prefetch signal

Frozen after all 56 screening attempts passed, before confirmation starts.
The screening CUBIC/200-heavy cell showed paired median changes of -11.36% in
equal-user mean, -33.34% in worst-user P99, -0.01% in goodput and +0.31% in
stack CPU/GB. Its A/A worst-user P99 varied +24.55%. This is a selected hypothesis,
not evidence of a general improvement. Screening samples never enter the
confirmation estimate.

Use the same release binary and owned isolated container. Compare A (existing
defaults) and C (`prefetch_max=65536`) in the same CUBIC/200-heavy/three-light,
100 Mbps, 10 ms RTT, 250000-byte FIFO cell. Increase each run to 20 seconds with
four-second warmup; each light user has 400 loaded and 50 baseline scheduled
requests at 25 Hz. Retain the original generator timing/deadline checks.

Run 12 fresh pairs, exactly six AB and six BA, shuffled before execution with
seed 20261024. No replacement, optional extension or stopping after a favorable
subset. Use fresh output `qa/results/capacity-20261023-confirm/`; save every
attempt, command, frozen sources and binary identities. A timeout stops and
marks the plan incomplete.

Primary latency endpoint: equally weighted mean of each user's latency.
Report median within-pair changes with deterministic pair bootstrap percentile
95% intervals (10000 resamples, seed 20261025). Worst-user P99 is secondary;
also report goodput, stack-thread CPU/GB and CPU/s, RSS, request completion,
per-run metrics and all pair directions. Do not pool thousands of requests as
independent experiment replicates. Bootstrap intervals describe this one local
cell and its sampling assumptions, not production or shared-host systematics.

For this confirmation, a substantial mean improvement means at least 20% at the
point estimate and an interval excluding no change. The goodput interval must
stay above -3%; a CPU interval permitting regression does not establish CPU
non-regression. A tail-only signal does not substitute for the primary endpoint.
Failure to meet these rules leaves the default unchanged. Invalid arrival runs
are retained and prevent a clean confirmation claim; do not silently filter them.

Even a successful result still needs the broader protocol's BBR, MTU1500,
bidirectional/multi-user/high-BDP/low-memory cases and fixed useful-load CPU
qualification. This confirmation measures no ZFC ingress control.
