# Bare-Shard host validity amendment (before diagnostic runs)

Code inspection while auditing zero-progress flows found that the L1 app host
handles `WouldBlock`, `QuotaBlocked`, and `MemoryBlocked` identically: it stops
pumping and waits for a connection event. The core distinguishes these reasons.
In particular, a writer that has not sent data cannot rely on its own ACK to
wake after another connection releases physical/aggregate quota. The production
Tokio adapter explicitly registers budget waiters; this L1 adapter does not.
The L1 adapter can also submit a 256-KiB atomic write when a smaller TX block
could make progress. Its pending application vector is not the production
Tokio adapter's accounted TX queue.

This is a fixture hypothesis requiring a direct control. Previous zero-progress
observations and CPU gains are **not evidence of a production root cause**.
All V1 raw data, binary identity, and exact sources remain retained. V1 results
with these stalls cannot certify production CPU/fairness non-regression.

## Same-binary diagnostic

New binary V2 adds opt-in `ZFBENCH_RETRY_BUDGET_WRITERS=1` to this host only:
track memory/aggregate-quota-blocked writers, retry after a real release epoch,
and try one 64-KiB partial write when a larger atomic write cannot fit. Share-
or TCP-window-blocked writers retain the existing core `Writable` path. Teardown
removes pending retries. This host owns all managed allocations on one stack
thread; this is not an asynchronous production notification implementation.

Use the same V2 binary for off/on/on/off, fixed q1s0p3, CUBIC, RTT 20 ms,
200 heavy connections, three 25-Hz light users, warmup 4 s, measure 12 s.
Keep raw per-flow progress and retry counters. The diagnostic succeeds only
if the stalls disappear/reduce substantially, payload/request checks pass,
and the retry bookkeeping does not spin without a release. No performance
gain across V1/V2 is claimed.

If confirmed, enable the correction in **every arm** of future trials. Recheck
wide prefetch against default under the corrected host before rejecting it on
production grounds. Do not pool V1 and V2 CPU or starvation measurements.
Queue-capacity experiments and final candidate confirmation use only the
corrected host; their binary hash and source manifest must say so explicitly.

## Diagnostic result and bounded scheduling correction

V2 off/on/on/off reproduced 100/0/0/101 completely inactive bulk connections.
All requests/payloads passed. However, forcing an immediate app deadline on
any changed budget epoch caused millions of startup retries: an unsuccessful
multi-block allocation can itself roll back/cache/reclaim memory and change
that epoch. This prototype is not accepted as the measurement host.

V3 retains the writer recovery but **does not create a new timer deadline**
from budget epochs. Existing input/core-timer processing owns the releases
and performs app retries inside its bounded poll passes. A failed allocation
alone must not cause a self-wakeup loop. Repeat the same frozen off/on/on/off
diagnostic with a new binary, retain V2, and inspect retry counters as well as
progress. No old attempt is overwritten or replaced in its cohort.

V3 removed the host's explicit immediate deadline, but core deadlines still
allowed rollback/reclaim epochs to trigger excessive retries during startup.
V4 additionally permits at most one retry batch for each real progress token:
core ingress packets, freed connections, or successful application reads/close.
A release epoch without one of those events cannot recursively fuel another
app retry. Add a regression check for this sequence, then repeat the same
diagnostic before accepting the repaired host. V2 and V3 remain in evidence.

V4 off/on/on/off gave 100/2/0/100 inactive bulk flows, valid request cohorts,
and about 208k/378k retries with the host enabled, instead of millions of
self-triggered retries. The no-self-progress retry regression test passes.
The remaining finite-window zero-progress observations are still reported,
not relabeled as guaranteed starvation freedom.

The final host enables this repair by default; explicit `0` retains the legacy
diagnostic control. A new final binary is frozen before further measurements,
and all final comparison arms explicitly set the switch to `1`. The production
library, its defaults, and its Tokio adapter remain unchanged. V1–V4 fixture
diagnostics are not pooled into the final CPU/fairness estimates.
