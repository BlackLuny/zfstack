# Memory extension, predeclared during screening and before extension runs

The reduced-prefetch cells show higher CPU cost in the initial screening
observations. The request explicitly permits memory tradeoffs, so test a
separate **wider prefetch + budget profile** before selecting the WAN candidate.
This is an exploratory hypothesis informed by screening, not confirmation.

Same frozen binary, same 20 ms RTT, 100 Mbps, 200-heavy/three-light traffic,
four-second warmup and now 16-second measured cohorts. S remains default:
screening has not established a reproducible advantage for smaller batches.

W0: production prefetch minimum 64 KiB, time 20 ms, maximum unbounded;
isolate ample ceilings 512 MiB global / 64 MiB port / 32 MiB heavy peer.

W1: minimum 256 KiB, time 20 ms, maximum 2 MiB;
`--budget-mb 128`, giving 128 MiB global / 64 MiB port / 64 MiB peer.
Global limit is lower but remains ample for this one-heavy-peer fixture;
do not apply these topology-specific absolute limits as production defaults.
The lower global ceiling is recorded as part of this bundled profile and
cannot be attributed independently. Inflight remains 16 MiB, not 256 KiB.

Cross W0/W1 with FIFO/peer queue and CUBIC/BBR, two adjacent balanced pairs
per queue/CC stratum: 16 runs. `rtt_memory.py` saves all cells/order before
running. Compare W1 against W0 **within the same queue and CC**, never against
a temporal control from the initial screening. Use the same validity, CPU,
throughput, latency and few-flow gates in PROTOCOL.md. Select only after all
extension runs finish; reserve WAN and 12-pair confirmation data independently.
