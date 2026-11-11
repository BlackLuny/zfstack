# Final policy selection, before independent RTT validation

Freeze **peer byte DRR + pushout; queue ceiling max(250,000 bytes, 2 BDP)**,
using configured base RTT and known bottleneck rate. Both directions use the
same rule. Keep original scheduling, prefetch, 16-MiB send/receive windows and
512/64/32-MiB global/port/peer budgets. Do **not** add an active-flow queue floor.
At the tested 100 Mbps, capacities for 1/10/50/100/250/500 ms are
250,000 / 250,000 / 1,250,000 / 2,500,000 / 6,250,000 / 12,500,000 bytes.

This is the best cross-condition candidate to validate from the bounded search,
not a production recommendation before validation and not a global optimum.
FIFO/250 KB/original core parameters is the control. Every arm uses final
binary `74a5d0f81bea350c2a0dfd2f217dc4f373a4c0fd52979c0aba7ea0e4a7bfb518`
with the benchmark writer recovery explicitly enabled.

## Why the additional flow floor is not selected

All 24 floor attempts passed identity/configuration audit; 23 passed measurement
checks, with zero cgroup throttling. One 4-packet BBR/1-ms attempt had a generator
lag of 32.07 ms and one missed arrival. This is not evidence of mechanism-induced
user starvation, and is not silently replaced. Its pairing is invalid.

Four packets per connection improved CUBIC/1-ms combined CPU/GB by 8.68% with
only 0.33% goodput loss, but at 20 ms the same rule increased stack CPU/GB by
21.98% and combined CPU/GB by 12.48%. BBR goodput fell about 3.83% at 1 ms
(invalid request pairing) and 5.49% at 20 ms (valid pairing); the latter also
increased combined CPU/GB by 6.06%. Eight packets worsened BBR throughput by
about 10–11% at both RTTs. Larger floors reduced inactive bulk-flow observations,
so they are a conditional fairness tradeoff, not universally useless. They fail
the frozen cross-RTT CPU/throughput gates and are not promoted into this policy.

## Independent validation still required

Run **96** fresh attempts: 1/10/50/100/250/500 ms × CUBIC/BBR × one/200 bulk
connections × two adjacent balanced pairs. Warmup max(8 s, 20 RTT), measurement
20 s, fixed light arrivals in 200-flow cells. Then **24** fresh confirmation
attempts at CUBIC/20 ms/200 flows (12 balanced pairs), **16 A/A** noise-control
attempts at the endpoints, and **12 host-peak** guards for CUBIC one/two and
BBR one connection. Retain every failed attempt. No exploratory sample is
included in confirmation intervals, and no V1–V4 host data is pooled.

The capacity rule is a configured known-rate/base-RTT experiment. It does not
implement online estimation, heterogeneous per-user RTT adaptation, upload or
mixed-traffic validation, or a fix for provider ingress queues. Finite per-flow
progress observations do not establish starvation freedom, and interactive
traffic inside the busy peer still shares that peer's FIFO. These remain
material boundaries regardless of the final latency result.
