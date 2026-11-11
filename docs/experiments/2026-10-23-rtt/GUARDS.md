# Host-throughput guards (finalized before any guard measurements)

100 Mbps WAN cells alone cannot protect extreme single-flow host throughput.
The selected queue-only candidate keeps every core/client parameter unchanged.
Test CUBIC one/two flows and BBR one flow, zero light users, zero artificial
RTT, **10,000 Mbps serializer**. This activates the FIFO/peer implementation
while aiming to place its configured rate above the host's achieved throughput.
If the host reaches that rate, report the guard as rate-limited instead of
claiming an unrestricted host peak. Same 16 MiB inflight, original budgets.

Two balanced pairs per cell (12 attempts), four-second warmup, 12-second
measurement, same binary. Candidate and sequence are frozen in the manifest.
A repeatable >3% few-flow peak loss rejects a universal queue setting; two
pairs cannot establish tight confidence intervals. AA controls quantify noise.

The initial draft considered testing wider prefetch in client mode as well.
That profile was rejected for widespread zero-progress bulk connections before
any guard ran. Client configuration therefore remains exactly the prior default;
these guards target the only selected change, the active bottleneck queue.
No adaptive production fast path or multi-gigabit deployment is claimed.
