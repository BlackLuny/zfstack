# Production iface pacing: protocol frozen before measurements

Implement in the production core and Tokio adapter. Existing peer/connection
DRR remains the scheduler; no second packet queue. The caller supplies the
known downstream capacity in IP bytes/second. A single ready peer bypasses
aggregate pacing, multiple ready peers engage it, and sparse competitors remain
represented for one second. Nominal service batches are 2 ms; scheduler credit
can catch up by at most 10 ms (or one MTU at very low capacities). Physical and
logical budgets and TCP receive/inflight windows retain their contracts.

The external link is **FIFO in every arm**. Neither peer DRR in the emulator nor
candidate-only queue capacity changes are permitted. This tests business code,
not the queue prototype from 2026-10-23. It controls a downstream bottleneck and
does not claim to fix an earlier provider-ingress queue or infer unknown capacity.

## Bounded initial screen

One frozen executable and identical 100-Mbps FIFO link, RTT 20 ms, CUBIC/BBR,
200 heavy connections belonging to one peer plus three interactive peers.
The interactive arrivals are 25 Hz/user, 256 bytes, absolute planned times, 3-s
deadline and 20-ms generator lag gate. Retain every planned/missed/cancelled
request and 250-ms bulk-connection progress observations.

Profiles: off, aggregate rate 100%, 99%, 97% of the known link capacity. Two
independently randomized complete blocks, 16 attempts, 8-s warmup and 12-s
measurement. This initial choice screen is descriptive. Prefer the highest
rate with clear mean/P99 benefit, <=3% goodput loss, no clear >3% stack/combined
CPU-per-GB increase and no additional missing requests or persistently inactive
users. Report individual bulk-flow stalls even if cross-user latency improves.
No apparent CPU saving from doing less work is accepted.

## Guards and independent validation

Before promoting a profile, independently compare both CCs at RTT
1/10/50/100/250/500 ms and one/200 heavy connections, preserving identical
FIFO capacities within each pair. Low-RTT queue floor is 250 KB; a separate
matched 2-BDP fixture may diagnose high-RTT buffer dependence, never silently
replace a failed small-FIFO pair. Keep the few-connection bypass active in the
candidate. Add host-peak CUBIC 1/2 and BBR 1/2 guards, RTT zero and 10-Gbps link,
including a comparison to the previous production binary as well as same-binary
off/on controls. Protect peak goodput against a repeatable >3% regression.

Repeat selected loaded cells with at least 12 adjacent balanced pairs. Bootstrap
whole pairs, 10,000 resamples, 95% interval; screening data do not enter it. A/A
at the endpoints and host peak quantify local interference. All required core,
Tokio, bench and source-inventory checks must pass. Test production Tokio
execution in addition to the bare-Shard fixture, including control updates,
bidirectional traffic, payload integrity, teardown and budget bounds.

No build, test suite, plot or archive compression during throughput measurements.
Runs execute serially in an owned network-isolated container, CPUs 0–3. Record
source/binary identity, command, counters, queue discipline/capacity, useful work,
stack/stack+link/process CPU per second and per GB, RSS and generator validity.
Keep all failed attempts and prior versions. Amend the protocol before a changed
dependent phase rather than selecting only successful repeats. Passing finite
windows is not a proof of universal starvation freedom or unknown-link support.
