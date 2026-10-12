# L0 multi-tenant matrix

Re-run after the PR #25 harness fixes, seed 1, release build, `matrix --full`. All 20 rows passed. Remote task: `t-01M4RCQB85K71CA7DQCVE7T1RQ`. Wall times include the harness driver and are not a performance threshold.

Layer **L0** (sans-IO, virtual clock, no TUN, no WireGuard). These numbers are not wall-clock P99 and are not comparable to L1 or L2.

| scenario | ok | ports | topology | steps | wall_s | reserved peak | note |
|---|---|---:|---|---:|---:|---:|---|
| tuple-isolation | true | 2 | shared-shard | 10 | 0.001 | 140624 |  |
| peer-reuse | true | 2 | shard-per-port | 10 | 0.001 | 140624 |  |
| same-shard-peer-alias | true | 2 | shared-shard | 14 | 0.001 | 140624 | Budget::peers is keyed only by PeerId inside a shard, not by iface. measured peer.bytes peak covers both ifaces, so this PeerId shared one ceiling (peak=16384). |
| noisy-same-iface | true | 1 | shared-shard | 89 | 0.002 | 102376 | heavy and lights share one iface and therefore one link; peers differ |
| noisy-cross-iface | true | 4 | shared-shard | 63 | 0.003 | 102376 | heavy is iface 0, lights are other ifaces of the same shard; each iface has its own link |
| noisy-cross-shard | true | 4 | shard-per-port | 63 | 0.007 | 102952 | one shard per port, one shared GlobalBudget; each port has its own link |
| noisy-shared-uplink | true | 4 | shared-shard | 89 | 0.003 | 102376 | cross-iface on one shard, but every port shares one bottleneck queue |
| heavy-fanout | true | 1 | shared-shard | 106 | 0.001 | 300304 |  |
| port-density | true | 16 | shared-shard | 7 | 0.003 | 109184 | Port count and connection count both change together here; do not read the result as a per-port fixed cost. |
| quota | true | 1 | shared-shard | 2415 | 0.037 | 1736544 |  |
| zero-window | true | 1 | shared-shard | 181 | 0.001 | 210120 |  |
| sink-full | true | 1 | shared-shard | 341 | 0.002 | 284488 |  |
| sink-full-iface | true | 2 | shared-shard | 350 | 0.002 | 284872 |  |
| idle-reclaim | true | 1 | shared-shard | 18 | 0.000 | 140624 |  |
| lossy | true | 1 | shared-shard | 28 | 0.000 | 71464 |  |
| iface-rebuild | true | 2 | shared-shard | 340 | 0.003 | 354800 |  |
| wall-bulk | true | 1 | shared-shard | 77 | 0.001 | 73768 | Headline figures are wall_sec and process_cpu_sec for this fixed byte count. finished_at_us is virtual and is not a latency result. |
| crowd | true | 16 | shared-shard | 47 | 0.004 | 181120 |  |
| port-density | true | 64 | shared-shard | 7 | 0.012 | 436736 | Port count and connection count both change together here; do not read the result as a per-port fixed cost. |
| port-density | true | 128 | shared-shard | 7 | 0.024 | 873472 | Port count and connection count both change together here; do not read the result as a per-port fixed cost. |

L0 virtual time is not wall-clock latency, PPS, RSS, or a WireGuard path. L1 (Linux TUN + kernel TCP, no WG) and L2 (ZFC+WG) are different layers and are not numerically comparable to these numbers. Cross-iface equal bandwidth is not a zfstack guarantee (docs/design/0001: no user-level QoS).
