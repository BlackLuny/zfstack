## 目标与边界

追踪 zfstack 多用户代理下**实际字节服务公平、连接数量抗抢占、跨 Iface/Shard 共享瓶颈争用**的可测量边界，并在证据支持时寻求帕累托修复。**这是调查/验收 Issue，不把尚未证明的差异定性为 bug，也不预先决定重写调度器。**

与现有事项分工：
- #24：无 WG 的多用户多端口 L0/L1/L2 综合 harness（复用，不另造）
- #50：客户端 mixed-bi reactor / `relay_pump` 有界调度归因（归它负责）
- #44：已量化的同端口轻用户 p50 回归（作为重要回归保护）
- #43：share-blocked relay 混合负载与等待集合
- #53：DRR 队列及预算查找成本优化（避免与本项功能契约混为一谈）
- PR #46：[2026-10-15 client benchmark](https://github.com/BlackLuny/zfstack/blob/cursor/client-stack-compare-95bb/bench/results/2026-10-15-client-latest/REPORT.md)：mixed-bi 吞吐与小 RR p99 tradeoff 的客户端基线；**不是**服务端 Peer 公平性证明

## 源码已核实的机制与待验证的结论

1. [`Drr`](https://github.com/BlackLuny/zfstack/blob/main/src/shard.rs#L232-L251) 各 Iface 维护 `ready_peers: VecDeque<PeerId>` 和 `peer_q: HashMap<PeerId, VecDeque<Conn>>`。[`Shard::run`](https://github.com/BlackLuny/zfstack/blob/main/src/shard.rs#L1839-L1888) 轮转 Peer，Peer 内轮转 Conn；[量子](https://github.com/BlackLuny/zfstack/blob/main/src/shard.rs#L289-L296) 随估计速率/MSS 变化。当前结构未看到经典按字节 DRR 的显式 deficit 累积，**不能仅凭轮转推断异构 MSS/速率条件下的严格字节公平**。
2. `iface_rr` 改变遍历起点，但当前 `run` 对某一 Iface 的循环可消耗剩余 `round_bytes_cap`，**跨 Iface 不存在已验证的严格等带宽合同**；共享物理瓶颈还受宿主 egress 队列/调度约束。不同 Shard 和多个实际 WG 端口须分别测。
3. [`Budget::send_share`](https://github.com/BlackLuny/zfstack/blob/main/src/budget.rs#L1271-L1293) 是根据发送者数调整每连接 TX 可用份额的**资源上限**，不是保底带宽。Global/Port/Peer 逻辑与物理额度要作为进度和隔离的独立维度。
4. `Sink::Full` 会冻结对应 Iface，`egress_released` 恢复，避免无意义 busy-loop；需确保恢复时队列顺序/已接受字节记账不被破坏。
5. 已有 [`sim::tests::per_peer_fairness`](https://github.com/BlackLuny/zfstack/blob/main/src/sim/tests.rs#L562-L587)：同 Iface，一个 Peer 1 条流、另一个 16 条流，100 Mbps sink 瓶颈，要求吞吐比 0.7–1.3、总吞吐 >85 Mbps；留档的一次比值约 1.00。**该测试不能外推到跨 Iface、异构 RTT/MSS/速率、拥塞控制、真实 TUN 或尾延迟。**

## 先观测、再决策（建议工作项）

- [ ] **定义公平合同**：区分同 Iface 内「可发送 Peer 的服务机会」、长期有效字节份额、单 Peer 内 Conn 份额、跨 Iface/Shard、资源配额，以及小流尾延迟；Peer 权重默认等权，但 app-limited / cwnd-limited / rwnd-limited / pacing-limited / egress-full 必须从“应得服务份额”的分母中妥善剔除或单独呈报。
- [ ] **低开销归因**：按 Peer/Conn/Iface 汇总 accepted wire bytes、有效接收字节、可发送时间、服务次数、实际量子大小、饥饿最长间隔、cwnd/rwnd/pacing/budget/sink-full 限制时间、每轮 Shard 预算使用和各 Iface 占用。测试可用编译期 instrumentation，生产默认无高频逐包追踪/大映射；不把 wire bytes 与 goodput 混淆。
- [ ] **确定性 L0 反例**（复用 #24）：1 vs 16/64/200 Conn；同样/不同 MSS、RTT、CC（CUBIC/BBR）、应用负载（饱和/短流/限速）、量子大小、出口 Full/恢复、内存压力/小预算、长尾 ACK 与小 RR。要求精确字节正确性、最终资源清零、无永久停滞。
- [ ] **跨 Iface/Shard 矩阵**：1/16/64/128 逻辑端口；独立出口瓶颈 vs 共享总瓶颈，重用户/轻用户不同端口、相同 PeerId 在不同 Iface 的记账语义；显式区分栈调度公平和共享网络队列效应。L1 使用 Linux TUN + kernel TCP；WG/ZFC 只放可选 L2 集成。
- [ ] **双向 mixed & small RR 联动**：对照 #46 以及 #44，记录 bulk goodput、每 Peer 吞吐、轻用户 RR p50/p95/p99/p99.9、连接成功/长时间无进展、CPU/有效 GiB、RSS/alloc、出口排队；`relay_pump` 的 reactor 归因与优化直接复用 #50，禁止单独重复实现。
- [ ] **消融 / A-B 验证后决定是否修改**：只有确证不公平来自调度量子或队列组织，才比较现状与按字节 deficit 记账、跨 Iface 有界轮转/预算等最小候选。不得为“看起来更公平”而固定为小量子、对每包加锁/分配、引入忙轮询或降低流量峰值。
- [ ] **验收门**：现有 G-fair 1:16 不回退；针对新增场景先冻结合理的公平比值/尾延迟阈值，避免事后选门槛。采用同机绑核、ABBA/BAAB、固定 work、分离宿主噪声，保存源提交、环境、原始数据、CI 正确性与统计区间。必须保持正确性、无泄漏/额外死流、不新增 RTT/网络探测；吞吐、CPU/GB、RSS、轻流 P99/P99.9 均设非回退门，任何取舍必须显式报告，不用单流提升掩盖多用户回退。

## 交付定义

1. 可复现的 fairness 矩阵和原始证据（含每个场景“共享瓶颈在哪里”），解释现状哪些公平目标成立、哪些不保证。
2. 如果无实测缺口，保留明确的合同与回归测试即可关闭，不做没有收益的重构。
3. 如果存在缺口，最小修复与独立 PR；对照本 Issue 的吞吐/CPU/内存/尾延迟/可靠性验收后再考虑 active，避免把性能 tradeoff 隐藏进“公平性优化”。

## 2026-10-20 本地调查进展

2026-10-19 本机完成 L0 公平性 40 格、Linux TUN 墙钟负载 22 格，以及生产 Tokio adapter 两种共享预算各三轮。完整数据、方法和限制见 [本次测试记录](https://github.com/BlackLuny/zfstack/issues/69#issuecomment-6280371601)。

- **同 MSS 的连接数量抗抢占得到局部支持**：1 对 16/64/200 连接，CUBIC Peer goodput 比值约 1.00，BBR 约 0.923，总 goodput 约 96.34 Mbps。
- **异构 MSS 的严格字节公平不成立于本次测试条件**：协商 MSS 524 对 1368，CUBIC 比值约 0.766，BBR 约 0.645，更换 seed/接入顺序后仍存在；尚未完成 CC/pacing/量子归因消融。
- **共享队列长尾退化可重复**：同 iface 从 1 增到 200 条重连接，CUBIC 最差用户 P99 从约 30 ms 升到 90–101 ms；BBR 从约 15 ms 升到 95–290 ms，单次最慢 830 ms，重流 goodput 仍约 96 Mbps。跨 iface/shard 也观察到慢请求；不能据此把全部退化归因于调度器。
- **资源恢复与测试侧缺口分别处理**：生产 adapter 的 1/4 MiB 共享预算实验通过完整字节校验、释放后恢复及最终资源归零。L0-E 压力格存在 physical waiter 覆盖不足，失败格与人工中止对照保留，未计作产品死流或成功性能证据。

本次没有修改生产调度器，也没有运行完整 ZFC worker/WG；closed-loop 结果不能替代 open-loop、双向 mixed、异构 RTT、RSS/CPU/吞吐非回退及 ABBA 验收。原有工作项保持未完成，Issue 保持 OPEN。本地案卷 `docs/experiments/2026-10-19-issue69-local/` 尚未提交/推送。

## 2026-10-20 长尾根因补充

继续调查已得到逐包证据：本次 Linux TUN 场景中，共享 FIFO 满载丢轻请求响应，TLP/RTO 将等待放大到数百毫秒；采样请求的栈内首次发送等待远小于这一量级。小量子未治愈，4 MB 深 FIFO 增加排队且吞吐降至约86.56 Mbps。

测试瓶颈队列的 Peer DRR+拥塞腾位原型，在13轮无跟踪 AA/ABBA/BAAB 中将最差用户 P99 从约93–290ms降到11–13ms；bulk goodput下降0.398%–0.510%，通过预先冻结的1%门。closed-loop轻请求完成约增至三倍，进程CPU/GB上升5.72%–8.29%，部分RSS上升，因此未证明严格无代价修复。固定到达率完整四轮有两轮空载基线发生器漏发，整组验收明确失败；全部原始失败保留，不追认为通过。

仅新增测试侧追踪、队列与负载原型，生产TCP/调度器/ZFC未改，完整WG未重放。优先验证实际瓶颈处的Peer队列隔离，保持OPEN；原宽范围工作项不据此勾选。详细逐包时间线、成本和失败项见[本次补充评论](https://github.com/BlackLuny/zfstack/issues/69#issuecomment-6283112819)。本地案卷：`docs/experiments/2026-10-20-issue69-tail/`，尚未提交/推送。
