# What the code can and cannot establish

This argument concerns the production source used by frozen V5, not a proposed
replacement or an inference from a small benchmark. Actual CPU and useful
throughput still require the independent physical measurements in PROTOCOL.md.

## Uncontended bypass is independent of RTT and congestion algorithm

Let A be `IfacePacing.active`, R its configured rate, and C the optional cold
clock. Initially A=false and C=None. With `competing=false`, the first condition
in `IfacePacing::allowance` is true for both R=0 and R>0:

```
R == 0 || (!competing && !A)
```

It returns `(usize::MAX, false)` before allocating or accessing C. It does not
change A or C. Induction over any number of such calls preserves that state.
Changing a nonzero configured rate or MTU while C=None also preserves it.
`deadline()` returns None without following C. The shard uses the returned
false to select `serve::<false>` and does not call `charge`. The compile-time
false removes aggregate wire accounting and the aggregate TSO-size clamp from
that service specialization. Existing TCP congestion control and its ordinary
per-connection pacing still execute.

Consequently configuring an arbitrarily low interface rate cannot impose that
rate on a never-contended peer, even at an arbitrarily high RTT or achieved
single-peer bandwidth. Multiple connections belonging to one peer have the
same property while `ready_peers.len() <= 1`. The pacer is shared per iface;
this does not mean one connection always bypasses when another peer is ready.

The initial-state assumption matters: after real competition the one-second
hold deliberately retains aggregate pacing between sparse interactive bursts.
The proof does not promise immediate bypass on the departure of the last
competitor, nor does it assert identical instructions or CPU cost. The actual
outer scheduling loop still executes configured/inactive checks.

`bench/capacity/pacing_contract.rs` includes the actual production `time.rs`
and `iface_pacing.rs`; it does not copy a Python model of them. It checks 1,197
bypass states/rate transitions including zero/max rates, MTUs 68–65535,
near-saturated times and changed inactive configurations. This finite check
supports the argument; induction, rather than enumerating those values, is
what establishes independence from the untested numeric values. The existing
simulator separately checks exact packet-content/time/step digests off/on for
CUBIC/BBR at 1/500 ms, and includes two connections of the same peer.

## Accepted-byte envelope during an active pacing epoch

Use nanoseconds as time credit. At fixed rate R>0 define:

- B=max(2,000,000, ceil(MTU*1e9/R)), nominal batch credit;
- M=max(10,000,000, B), maximum retained catch-up credit;
- c, signed credit/debt;
- w_i, accepted IP bytes in one charged service quantum;
- q_i=ceil(w_i*1e9/R), its charged nanoseconds.

For a nondecreasing caller clock, refill is
`c := min(M, c + elapsed)` and charging is `c := c - q_i`.
Refill clipping only discards credit; q_i >= w_i*1e9/R. Telescoping over a
continuously active epoch without reinitialization therefore gives:

```
sum(w_i) <= R/1e9 * (elapsed + c_start - c_end).
```

A charged service quantum starts only with a positive allowance and hence
positive credit. If H is a finite upper bound on the charged time of one
service quantum, then c_end >= -H and c_start <= M, giving the conservative
burst/debt envelope:

```
sum(w_i) <= R/1e9 * (elapsed + M + H).
```

H includes packet overshoot and every repeated GSO IP/TCP header. It must not
be silently replaced with one MTU for arbitrary GSO/quantum settings. Native
packet/quantum bounds and admitted finite configurations make H finite. A full
sink is not accepted and does not consume this credit. No second packet queue
is needed to retain debt: the existing connection queues retain unsent data.

When c<=0, `park()` sets a deadline at `at + (B-c)`; input at an earlier time
cannot reset that future deadline. At the deadline, elapsed credit is enough
to repay debt and restore a batch. Very low rates can make this interval
longer than the one-second hold; the explicit future-deadline guard prevents
an expired hold from minting another full batch in that case.

The displayed envelope is for charged traffic within the specified epoch.
Fresh contention epochs, dynamic rate/MTU changes, stateless responses and
TIME_WAIT control output require their own accounting. In the ordinary tested
rate range B is 2 ms and the hold is 1 s; after a genuinely expired hold with
no pending deadline, elapsed time suffices to refill a nominal batch. This
does not justify extending the simple formula to every possible low-rate,
partially consumed batch or reconfiguration history.

## Conditional scheduling fairness

In the existing peer DRR queue, serving a peer removes it from the head and,
if it still has ready connections, places it at the tail. Pacing pauses the
iface without resetting this order. With finitely many ready peers and finite
stale entries, a continuously ready peer receives a dequeue opportunity after
finitely many opportunities for the peers ahead of it. New peers join the
tail. A finite service quantum prevents one ready peer from consuming all
future opportunities. This is the scheduling reason a peer with many bulk
connections cannot permanently hold the head ahead of interactive peers.

This argument assumes advancing time, an eventually writable sink, eventual
driver execution, admitted ready data and finite peers. A dequeue opportunity
does not guarantee application bytes if TCP's own window, congestion state,
remote endpoint or application blocks progress. No finite Internet/user
latency bound follows without bounds on all those dependencies. The measured
arrival/progress ledgers, rather than an unconditional starvation theorem,
establish which tested users and bulk flows actually progressed.

## CPU is deliberately left to measurement

The bypass removes aggregate allocation, charge and deadline work; it retains
outer-loop checks. Their cost scales with service quanta/iface visits, and
actual cost also depends on cache layout, branch prediction and the compiler.
Neither the induction nor a short instruction sequence bounds that percentage
against a different baseline executable. The exact V5 disassembly is used to
verify the compiled branch/outlined-loop structure, not to claim identical
cycles. Environment-controlled repeated CPU/GB and CPU/s observations provide
the performance evidence.
