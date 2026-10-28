# Few-connection peak-throughput guard

Frozen after the user's additional requirement: preserve peak single-connection
throughput as much as possible when connection count is low. The earlier
80 ms server result already rejects a universal 256 KiB total TX cap. The local
client candidate had only eight-flow measurements, which cannot establish this
new condition.

Compare A (client defaults) against B (only total core TX = 262144 bytes) with
one and two concurrent local TUN downloads. Keep the same Rust binary, splice,
dedicated driver, two proxy workers, MTU65535 and offload disabled. Each attempt
lasts 20 seconds, warmup four seconds; no bandwidth shaper. This is a peak-capacity
screen, not fixed useful-load CPU qualification or multi-user fairness testing.

For each case run A/A followed by A/B/B/A. Freeze case order using seed 20261026:
12 total attempts, two pairs per candidate/case. Keep all errors, no replacement.
Record the new isolated container/image identity: the prior measurement
container was already removed; this phase uses the verified final runtime TUN
entrypoint. Do not pool absolute values across the two container phases.

Goodput is primary. A paired median loss over 3% conservatively rejects adoption
in that case. Smaller observed differences remain unqualified with two pairs;
do not claim no regression from a wide or unmeasured uncertainty interval.
CPU/GB and RSS gains cannot compensate for a material single-flow goodput loss.
Any future contention-only policy must explicitly test entry/exit transitions,
borrowed-window reclamation and idle-to-single-flow return to the original fast
path; merely counting connections is not evidence of resource competition.
