# Source-level properties and limits of adaptive pacing

The frozen A1 binary is built from 54 Rust/Cargo inputs recorded in the build
metadata. This document concerns its semantics, not a claim of equal CPU cycles.

## No supplied-capacity channel

The controller module imports only the core clock and Duration. Its observation
inputs are newly ACKed/SACKed delivery, each bulk connection's smoothed/minimum
RTT, queued-sender demand and MTU. It cannot access the benchmark crate's link
serializer, capacity schedule or source-pause clock. Both experimental arms
pass fixed iface rate zero; the benchmark rejects adaptive plus a nonzero fixed
rate. A learned target is not proof of the physical capacity: application/window
limits, ACK timing, cross traffic and heterogeneous paths remain ambiguous.

Delivery is accumulated per interface over wall time, not by adding each flow's
historical BBR maximum. Translation from ACKed payload to IP bytes uses its
headers/MSS and is approximate, especially for partial segments/retransmissions.
Accepted output is still charged using the existing exact IP/GSO accounting.

## Single-peer induction

Assume no previous contention, active=false and clock=None. With adaptive=true,
rate=0 and competing=false, `IfacePacing::allowance` returns unlimited/false at
its initial guard. It modifies neither active nor clock. Induction preserves
this for any number of calls and every RTT/CC/time trace. `tracks_delivery()`
is false, so the lazy `bool::then` does not even read the delivery counter, and
the estimator/queued-demand/RTT path is skipped. `serve::<false>` is selected;
no aggregate charge, cold allocation or interface pacing deadline is created.

This also covers multiple connections of the same ready peer. It does not
cover another ready peer or the one-second hold after genuine competition.
The deterministic BBR simulator independently compares packet contents, virtual
times and step count at 1/500 ms with two connections of one peer. A late-peer
test checks that updates/charged bytes cease after contention expires while
bulk connections continue progressing. This is simulator evidence, not physical
cycle invariance. Compiled branches and real single-flow guards are separate.

## Avoiding a purely self-shrinking estimate

Passive `next_rate=0.99*current_delivery` has the valid saturated feedback trace
delivery=current_rate. Then rate_n=rate_0*0.99^n tends to zero even if the path
has spare capacity. A1 deliberately supplies upward excitation. Under qualified
demand and constant irreducible queue inflation, delivery=target, and no severe
capacity-fall event, its three 98% reductions and fourth 108% probe give
`0.98^3*1.08 = 1.01648736 > 1` per cycle (apart from integer rounding).
Thus that specific trace does not suffer the passive exponential collapse.

This is not a general stability proof. Sparse demand, loss, delayed feedback,
integer minima and changing RTT populations can produce other trajectories.
A1's extra queued-demand signal and elastic low-queue increase repair the A0
counterexamples described in A1-CORRECTION.md, but do not establish native
capacity-change recovery. That recovery is measured independently.

## State and work bounds

The iface hot wrapper remains 24 bytes. A never-contended peer allocates no
adaptive state. Active state is constant per iface; there is no new packet
queue, periodic connection scan or production thread. Each ACK contributes
constant work; rate-control decisions occur at most once per observation
window. Delivery/header conversion and timestamp accumulation still occur per ACK. The window is at least
100 ms and two largest sampled bulk minimum RTTs. Application pauses or gaps
do not automatically replace capacity with zero.

The existing pacer preserves signed credit/debt when changing rates and keeps
DRR order across pauses. The fixed-rate continuous-epoch accepted-byte envelope
in the parent study does not become a universal envelope at the unknown true
capacity: the target itself changes and can temporarily exceed that capacity.
The RTT signal also contains delayed ACK, scheduling and route effects; it is
not an isolated measurement of the common FIFO.

## Conditional fairness, not an unconditional latency theorem

Finite ready peers enter the existing DRR tail; a served ready peer returns to
the tail. A pacing pause preserves this order. With advancing time, finite
quanta, eventual driver execution and an eventually writable sink, a ready
peer gets a service opportunity after finitely many opportunities ahead of it.
TCP windows/congestion, application silence or external queues may still
prevent useful data. Native planned-arrival and per-flow progress ledgers,
including every missing request, determine what actually happened in this study.

Additional inactive checks, ingress iface lookup, register/cache layout and
branch prediction can change CPU. Neither this induction nor assembly size
proves zero overhead. Same-binary off/on data and independent V5-to-new-disabled
guards intentionally measure different costs; both must be reported.

## Completed source and compiled checks

After all 312 measurements, the standalone executable directly including the
production modules checks 1197 fixed inactive states and 70 adaptive inactive
states. The separately compiled frozen V5 source checks its original 1197
states. Both hot wrappers are 24 bytes; V5's cold clock is 96 bytes and A1's
is 112 bytes, plus a 96-byte adaptive controller when allocated. The two A1
cold allocations total 208 bytes, excluding allocator metadata. These checks
do not measure packet CPU time.

Disassembly of the retained release executables finds four A1
`adaptive_allowance` call sites. Each has a preceding ready-peer/active guard;
for example `Driver::service` near `0x1524c9` compares the ready count with two,
combines active state, and jumps to the unlimited result before the call at
`0x152516` when neither applies. Addresses identify this exact executable only.
`ingress_owned`/`ingress_slice` symbol sizes match at 1372/832 bytes, while
`input_existing` grows from 4170 to 7151 bytes. This corroborates conditional
bypass and also shows why identical wrappers cannot prove identical cycles.
Exact symbols, binary hashes and excerpts are in the archived preparation
`assembly-analysis.json` and `assembly-*.txt` files.

Even the positive `0.98^3*1.08` cycle above is not a prompt-recovery guarantee.
In that specific constant-inflation, delivery=target trace, increasing fivefold
requires 99 complete cycles, about 39.6 seconds with 100-ms windows or 396
seconds with one-second windows. This is an admissible controller-input
counterexample, not a claim that native TCP follows that exact trajectory.
The independent native step measurements decide recovery; their original
five/ten-second bounds remain unchanged.
