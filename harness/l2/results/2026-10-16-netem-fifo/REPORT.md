# 候选 `netem-fifo`：仿真三模式自检，full 因 veth 下游乱序 FAIL

接续 [正式故障臂离线分析](../2026-10-16-fifo-formal-analysis/REPORT.md)。那里的根因证据是：用户态 FIFO 容量约 70k fps，而突发约 129k fps；读取前的等待在账本之外。据此实现了候选注入器 `netem-fifo`（schema 5）：

- [`netem_fifo.py`](../../netem_fifo.py) 在 worker eth0 上只用一个持久 root netem `44:`，始终带 rate，相位经 netlink 原地 change。
- 证据来自两份内核时间戳捕获：队列前用 clsact mirror 到私有 dummy，队列后取 eth0 outgoing。
- [`netem_fifo_ledger.py`](../../netem_fifo_ledger.py) 自行配对两份原始捕获，并复算释放上下界、顺序、相位、生命周期和计数闭合。

**结论：在 SHA `96a3fcc` 上，cancel、overflow 负例和一次信息性的较重负载 full 均符合预期。预注册的 full 自检 FAIL：注入器出口 FIFO 精确无误，但接收端 UDP 顺序被 veth 下游跨 CPU 投递打乱。** 因此没有把 `netem-fifo` 写入 case：C16 仍为 case v2 `fifo-delay-line`，known-BLOCKED 保留，没有冻结 worker，也没有正式运行。所有运行只做一次并全部保留，没有择优重跑，20 ms 及全部冻结常量未改。

## 环境与计划

- virtme-ng TCG 仿真（`--disable-kvm`，4 vCPU、4 GiB），内核 6.8.0-147-generic，在 `qa/isolate-linux.sh` 内执行。**不是 6.1 资格宿主，也不是真实 CPU。** 本机没有远端 SSH 凭据，`root@23.136.204.72:23826` 上的工作本次没有执行。
- 运行前提交了 [自检计划](selftest-plan.json)（`db4dbeb`，代码 `96a3fcc`，工作树干净），顺序为 full → cancel → overflow → full-heavy。[执行脚本](selftests/run.sh)、[运行记录](selftests/runs.log)。
- full-heavy（`--rate 4000 --tcp-rate 20000000`）只作信息性参考，不是正式业务负载。

## 结果

[复算](recomputed.json) 由 [recompute.py](recompute.py) 从保留的原始账本重新运行 oracle 和顺序分析生成，结论与自检自报一致。

| 运行 | 帧数 | 自检 | oracle | 说明 |
|---|---:|---|---|---|
| full | 42380 | **FAIL**（`udp_exactly_once_in_order`） | PASS | 99 个 UDP 被后发帧超越，全部发送于 drain/guard 期 |
| cancel | 39622 | 负例 PASS | 拒绝（取消、相位不完整） | UDP 全到且严格有序 |
| overflow | 35275 | 负例 PASS | 拒绝（5037 帧丢失、qdisc drop） | UDP 缺口等于账本丢失 |
| full-heavy | 154840 | PASS | PASS | 信息性；UDP 严格有序 |

来自原始账本：

- **full**：active 驻留 500.018–504.781 ms，recovery 驻留 max 3.62 ms。
- **full-heavy**：active 驻留 500.011–505.165 ms，recovery 驻留 max 1.24 ms。接收端看到的 UDP 端到端最大 534.75 ms，说明约 30 ms 发生在队列之外（仿真下的发送端、接收端或 veth），不在 FIFO 内。
- 两次 full 的相位 change 窗口为 0.4–1.0 ms（上限 5 ms），透明切换由 `qlen == 0` 触发，早于 recovery 边界约 229–236 ms。

## full 的乱序定位

[analyze_order.py](analyze_order.py) 按 UDP 帧逐一比较两个顺序：账本中的 eth0 出口顺序和接收 socket 顺序。

- full：**出口顺序与发送顺序完全一致**，oracle 的逐流顺序检查因此通过。超越者比被超越帧晚出口 p50 2.22 ms、max 4.81 ms。

随后按 [诊断计划](order-diagnostic-plan.json)（运行前提交，`461b9fb`）执行了一次带 tracefs 的 full，脚本为 [veth_order_trace.sh](veth_order_trace.sh)。它记录每个 UDP 帧在接收 veth 上的 `netif_rx`（由从 netem 出队的 CPU 交付）和 `netif_receive_skb`。原始 trace 见 [diagnostic/](diagnostic/)，所有 CPU 都是 0 overrun、0 dropped。

- 这次乱序**再次出现**：29 个 UDP 在 socket 被超越，全部发送于 drain/guard 期，自检同样 FAIL、oracle PASS。
- 18475/18475 帧都在交付它的那个 CPU 上处理，没有 RPS；交付 CPU 在 4 个 CPU 之间切换了 57 次。
- 按处理时间排序有 32 帧被超越，socket 层的 29 帧是它们的子集（另外 3 帧在 trace 的 µs 时间分辨率以内）。**32/32 的超越者都由另一个 CPU 交付**，被超越帧在本 CPU 积压中等待 p50 1.16 ms、max 1.23 ms；超越者晚出口 p50 0.65 ms、max 1.09 ms。

机制：netem 在执行 qdisc 的 CPU 上出队，可能是发送者 CPU，也可能是 watchdog 定时器所在 CPU。veth 把帧放进该 CPU 的接收积压，而各 CPU 的积压独立处理。drain 期积压以 2 Gbit/s 密集释放，在约 1 ms 内跨 CPU 交付，后出队的帧就可能先被处理。旧 `fifo-delay-line` 由单线程发送，所以没有这一现象。普通 netem 同样由多个 CPU 出队，但下节的对照中它没有出现乱序。正式 C16 的 UDP 探针只检查重复和未计划的数据报，不检查顺序。

这个失败不能通过修改自检判据或 oracle 来消除。当时列出的两个可选方向如下（路线 1 已按下节对照的结果否决）：

1. 认定注入器的顺序义务止于 eth0 出口（与原 netem 注入一致），然后**重新预注册**自检判据。这必须作为新的冻结标准，不能追认本次 full。
2. 找到一种让 FIFO 出队后的交付固定在单一路径上的实现，并且不改变发送端 CPU 和负载，不新增队列，不改动对端设备（例如在对端开启 RPS、GRO 或 NAPI 都会改变被测接收路径）。

## 对照：普通 netem（历史 v3 时间线）不出现乱序，路线 1 不采用

维护者同意按路线 1（顺序义务止于 eth0 出口）推进，前提是先确认乱序是 netem on veth 本身的属性。为此运行前提交了 [对照计划](order-control-plan.json)（`2833374`）及其决策规则。[netem_order_control.py](netem_order_control.py) 与 full 使用同一 veth 对、同一发送端/接收端和同一轻负载，worker eth0 上不挂注入器，只按历史 v3 时间线操作：

- loaded 开始时加 root `netem delay 500ms`；
- drain 边界原地 change 为 `delay 0 rate 2Gbit`；
- 读到空后删除。

对照共运行 3 次，原始输出见 [control/](control/)。

| 运行 | 发送 / 接收（唯一） | 被超越 | 删除完成相对 recovery 边界 |
|---|---:|---:|---:|
| 1 | 18475 / 18473（18473） | 0 | 晚 115.0 ms |
| 2 | 18475 / 18475（18475） | 0 | 晚 254.5 ms |
| 3 | 18475 / 18475（18475） | 0 | 晚 90.7 ms |

- 3 次都没有任何 UDP 被超越，drain 期的 2 Gbit/s 密集释放同样存在。
- 3 次 `tc` 轮询都没有在 recovery 前读到空队列，因此都在边界后删除。这正是历史 v3 的非原子删除竞态，运行 1 缺的 2 个数据报与此一致。
- 这个对照不是任何注入器的资格证据。

按预注册规则，**乱序没有被证明是普通 netem 也会有的属性，因此不采用路线 1，不修改自检判据**。`netem-fifo` 独有的成分包括 clsact mirror、两个 TPACKET 捕获环、`qlen` 轮询，以及注入器进程在仿真 CPU 上的负载，哪一个使跨 CPU 投递的差异显现，尚未定位。真实 CPU 上是否复现须在真机自检中确定，步骤见 PR 中的真机运行说明。

## 开发阶段运行（不归属任何 SHA）

[development/](development/) 保留了提交前在未提交源码上运行的一组三模式（三个都符合预期，其中 full 当时 UDP 有序）和原型探针 `netem_probe.py` 的结果，只作为开发记录：

- 原型探针用 `tc` 子进程做 change，窗口约 75 ms；后改为 netlink，0.3–0.7 ms。
- 原型中 dummy 自己发出的 IPv6 RS 被计为“缺帧”；后关闭 dummy 的 IPv6/ARP。
- 早期 ring 以 `ETH_P_ALL` 创建，在 bind 前就接收所有设备的帧；后改为 protocol 0 创建、bind 时再指定。
- 早期 qlen 的 netlink GET 不带 `NLM_F_ECHO`，得不到单播回复，导致注入器挂起；后加 `NLM_F_ECHO | NLM_F_ACK` 和 1 s 超时。
- 时钟样本在慢速安装时出现超过 1 s 的空隙；后增加强制采样。

开发阶段 full 有序、而提交后两次 full 都乱序，说明这一现象是间歇性的；不能据此认为开发版本没有这个问题。
