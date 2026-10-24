# 竞争时的接口聚合发送节奏

每条 TCP 的拥塞控制和 pacing 只约束该连接。大量连接合计仍能填满下游 FIFO；
核心已有的 peer/connection DRR 在数据进入外部队列之前只能决定先后，无法约束合计速率。
本策略在核心的既有 DRR 出队处控制聚合发送，数据保留在原来的 TX 缓冲中，背压继续沿
Tokio 写队列和 `TcpStream::splice` 传到上游。

宿主知道物理出口容量时可配置：

```rust
// iface 来自 add_iface()，handle 来自 Tokio spawn 系列函数。
let capacity_bytes_per_second = 100_000_000_u64 / 8; // 100 Mbps，按 IP 字节计
handle.set_iface_egress_rate(iface, capacity_bytes_per_second * 99 / 100);

// sans-IO 宿主使用同名核心入口，并履行 next_deadline() 的唤醒约定。
// shard.set_iface_egress_rate(iface, capacity_bytes_per_second * 99 / 100);
```

99% 是本次实验选择的竞争期目标，需要按真实链路验证。它不是套餐限速：一个 ready peer
不进入聚合 pacing，保留原单用户路径。两个以上 peer 同时 ready 时进入策略，并记住竞争
一秒，避免稀疏交互用户的两次请求之间恢复灌满 FIFO。空闲份额由现有调度借用；
窗口、预取、物理预算与逐连接拥塞控制继续生效。策略不需要配置 RTT。

聚合器只保留每接口常数状态，没有第二份包队列。名义批量为 2 ms；按真实经过的时间
补足最多 10 ms 的调度 credit，避免 Tokio 定时器粒度和偶发晚唤醒导致持续吞吐不足。
极低速率至少允许一个 MTU；最后一个输出包可超出剩余额度，超出的字节及 TSO 重复头部
全部记入债务，并带入下一次唤醒。
TSO 按分段后的 IP/TCP 头部计费，活跃策略也限制一个超段的大小。

额度耗尽时保留 DRR 队列顺序，用接口 deadline 恢复，不逐连接扫描，不靠持续 yield 重试。
接口 sink `Full` 仍等待外部 `egress_released`，两种等待相互独立；其他接口可继续服务。
接收处理、重传定时器、无状态 RST 和 TIME_WAIT 控制处理仍运行。普通连接的控制报文
可经历一个有限的聚合批次等待，因此该策略不提供控制报文的独立优先级或准入保证。

配置为零会关闭策略并释放接口定时等待；Tokio 入口同时通知 driver。重复相同配置不会
重置 credit。更新容量会保留竞争状态并重新计价未结债务，不能借重复配置制造额外突发。
接口删除后不再保留其 deadline；没有 ready 工作的接口不因策略持续唤醒。

`ShardStats::egress_paced_bytes` 和 `egress_pacing_pauses` 记录策略是否实际介入。
前者为计费的 IP 字节，包含 TSO 重复头部；与核心原来的超段字节统计口径不同。

这个入口应放在真正瓶颈之前；宿主必须提供容量，库不盲猜运营商带宽。若多个接口共享
一个物理瓶颈，分别配置各接口并不形成共享总额度，需要宿主配置相应层级。
它不能消除数据到达 worker 之前已经发生的排队；上游运营商入口拥塞仍需在真实源站
连接处控制聚合读取和内核接收窗口。有限压测也不保证无限拥塞下所有已准入连接都获得
固定最小带宽，应结合实际身份、预算和准入控制。

验证协议、结果与未确认的峰值保护位于 [生产路径实验](../experiments/2026-10-24-production-pacing/README.md)。
