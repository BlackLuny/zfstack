# L0 multi-tenant matrix

Layer **L0** (sans-IO, virtual clock, no TUN, no WireGuard). These numbers are not wall-clock P99 and are not comparable to L1 or L2.

| scenario | ok | ports | topology | steps | wall_s | reserved peak | note |
|---|---|---:|---|---:|---:|---:|---|
| tuple-isolation | true | 2 | shared-shard | 10 | 0.001 | 157040 |  |
| peer-reuse | true | 2 | shard-per-port | 10 | 0.001 | 157040 |  |
| same-shard-peer-alias | true | 2 | shared-shard | 14 | 0.000 | 157040 | Budget::peers is keyed only by PeerId inside a shard, not by iface. measured peer.bytes peak covers both ifaces, so this PeerId shared one ceiling (peak=16384). |
| noisy-same-iface | true | 1 | shared-shard | 89 | 0.001 | 143416 | heavy and lights share one iface and therefore one link; peers differ |
| noisy-cross-iface | true | 4 | shared-shard | 63 | 0.002 | 143416 | heavy is iface 0, lights are other ifaces of the same shard; each iface has its own link |
| noisy-cross-shard | true | 4 | shard-per-port | 63 | 0.003 | 143992 | one shard per port, one shared GlobalBudget; each port has its own link |
| noisy-shared-uplink | true | 4 | shared-shard | 89 | 0.002 | 143416 | cross-iface on one shard, but every port shares one bottleneck queue |
| heavy-fanout | true | 1 | shared-shard | 106 | 0.001 | 349552 |  |
| port-density | true | 16 | shared-shard | 7 | 0.002 | 240512 | Port count and connection count both change together here; do not read the result as a per-port fixed cost. |
| quota | true | 1 | shared-shard | 2415 | 0.027 | 1769376 |  |
| zero-window | true | 1 | shared-shard | 181 | 0.001 | 226032 |  |
| sink-full | true | 1 | shared-shard | 341 | 0.002 | 292696 |  |
| sink-full-iface | true | 2 | shared-shard | 350 | 0.002 | 293080 |  |
| idle-reclaim | true | 1 | shared-shard | 18 | 0.000 | 157040 |  |
| lossy | true | 1 | shared-shard | 28 | 0.000 | 79672 |  |
| iface-rebuild | true | 2 | shared-shard | 340 | 0.002 | 371216 |  |
| wall-bulk | true | 1 | shared-shard | 77 | 0.000 | 81976 | Headline figures are wall_sec and process_cpu_sec for this fixed byte count. finished_at_us is virtual and is not a latency result. |
| crowd | true | 16 | shared-shard | 47 | 0.003 | 312448 |  |
| port-density | true | 64 | shared-shard | 7 | 0.009 | 962048 | Port count and connection count both change together here; do not read the result as a per-port fixed cost. |
| port-density | true | 128 | shared-shard | 7 | 0.017 | 1924096 | Port count and connection count both change together here; do not read the result as a per-port fixed cost. |

L0 virtual time is not wall-clock latency, PPS, RSS, or a WireGuard path. L1 (Linux TUN + kernel TCP, no WG) and L2 (ZFC+WG) are different layers and are not numerically comparable to these numbers. Cross-iface equal bandwidth is not a zfstack guarantee (docs/design/0001: no user-level QoS).
