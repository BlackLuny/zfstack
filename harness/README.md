# 多租户 harness（issue #24）

测的是 **ZFStack 自己**在多端口、多 peer、混合负载下的行为。主路径不启动 WireGuard。可选 L2 单独测 ZFC 的生产接线，其加密、UDP 和握手开销不进入 core 的性能分母。

```
L0  zfharness     虚拟时钟，sans-IO，无 root / TUN / WG
L1  zfbench       Linux 内核 TCP + 一条 TUN + 链路模拟器，无 WG
L2  l2/run-rig.sh ZFC + 真实 WireGuard + 生产 Tokio adapter，独立集成结果
```

L0 的延迟是虚拟时间。L1 的延迟是墙钟。两列数字不能相减，也不能和 L2 比。

服务端配置是 `StackConfig::default()`（生产服务器 profile）。不用 `StackConfig::client()`：那是本机 TUN、几乎零丢包、亚毫秒 RTT 的客户端 profile。

## 复用了什么

| 已有 | 这里怎么用 |
|---|---|
| `zfstack::sim` 的链路模型（带宽、传播时延、丢尾队列、丢包、乱序） | L0 自己带了一份带端口标签的 pipe，语义与 sim / zfbench 相同：传播时延和瓶颈队列分开 |
| `Shard` 的 `IfaceId` / `PeerId` / 共享 `GlobalBudget` | L0 直接驱动；L1 在 zfbench 的栈线程里驱动 |
| zfbench 的 TUN、链路线程、应用协议（`D` 下载、`E` 回显、pattern 校验） | L1 `--test isolate` |
| 0001 的部署 | 默认 **一个 shard、多个 iface**。另有 **每端口一个 shard、共享 GlobalBudget**，仍由一条线程轮流 `run`，不是多核 |

没有复用生产 Tokio adapter。L0/L1 都是 core + 测试应用。adapter 的额外开销要单独测，不能和这里的数混在一起。

## L0

```sh
cargo run -p zfharness --release -- list
cargo run -p zfharness --release -- l0 --scenario matrix --format md
cargo run -p zfharness --release -- l0 --scenario noisy-cross-iface --lights 16 --seed 1
cargo run -p zfharness --release -- l0 --scenario port-density --ports 128
cargo run -p zfharness --release -- l0 --scenario matrix --full --format md
```

不需要 root。同一 seed 可重放（`tuple-isolation` 单测锁了 steps 和 reserved peak）。

场景覆盖：

- 相同四元组打在两个 iface 上，字节按 salt 对上，不串流
- 两个 shard 复用同一个 `PeerId`，各自的 `peer.bytes` 峰值不超过自己的流量
- **同一个 shard 的两个 iface 复用 `PeerId`：预算键是 shard 内的 `PeerId`，不是 `(iface, peer)`**。8 KiB + 8 KiB 同时在途时，测到的 peak 是 16384。要分开计费，调用方必须在一个 shard 里分配不同的 `PeerId`。这是当前 `Budget::peers` 的合同，不是跨 iface 等权
- 1 个重用户 + 多个轻用户：同 iface、跨 iface（各有一条链路）、跨 shard、以及跨 iface 但共享瓶颈
- 一个 peer 开多条连接
- 1/16/128 个逻辑端口（`--full` 再加 64）
- 紧预算、暂停读取的邻居（发送方有发不出去的数据，恢复读取后积压下降）、出口 `Full` 打在已经有未发送数据的连接上（全局，以及只堵一个 iface）、关闭后重建 iface、1% 丢包 + 乱序
- 传完进入空闲，`reserved` 不涨，连接表回到 0（`time_wait` 配成 1s 的那一格）

轻用户的 P99 要 ≥100 个虚拟样本才输出，P99.9 要 ≥1000。不够就显式省略。请求延迟从「应用准备发送」算起，另外单列等 `write` 接受第一个字节的时间。跨 iface 带宽平均 **不是**产品保证（0001：不做用户级 QoS），所以只报告退化，不设“必须一样快”的门槛。

出口 `Full` 从握手之后才开始，并且要求故障期间发送缓冲里还有数据、`sink_full` 计数上升、放开之后接收方继续收到字节。只堵一个 iface 时，另一个 iface 必须在故障窗口内完成。

重建 iface 时，旧连接与另一 iface 的长流必须已经在服务端建立。通过条件是 `remove_iface` 之后旧 iface 上没有连接、`info` 看不到旧 slot、`conns_freed` 至少增加了这些连接、客户端 `abort` 之后栈报告 `Closed`，以及 `reserved` 不增加。另一 iface 的原连接须保留并跨过重建时刻完成。客户端关闭用实际状态验证。

`quota` 的四个 peer 各发送 512 KiB，必须观察到 `share_blocked` 或物理预算分层失败计数，再恢复推进至完成。只配置小额度而未撞到额度不算通过。

`port-density` 的 128 端口每口只有 1 KiB，只说明大量 iface 能握手。`crowd` 才是一个重端口加许多轻端口；`--ports 128` 可以加大，不放进每次 CI。

L0 的 `wall_sec` / `process_cpu_sec` 是 **这个进程把虚拟时钟跑完花的 CPU**，不是 PPS，也不是 RSS。`wall-bulk` 把这两列当成标题，虚拟完成时间不当延迟。

应用泵每步会看所有未结束的流。这是 harness 的成本，不是生产里那种只在事件上唤醒的 adapter。

## L1

```sh
cargo build --release -p zfbench
sudo ./bench/run-isolate.sh --ports 1 --placement same-iface --lights 4 --secs 10
sudo ./bench/run-isolate.sh --ports 16 --placement cross-iface --topology shared-shard --lights 16
sudo ./bench/run-isolate.sh --ports 16 --placement cross-shard --topology per-port-shard --lights 16
```

需要 root、`/dev/net/tun` 和 `ip`（iproute2）。退出时删除 `zfbA`，包括 Ctrl-C。

每个 peer 在 TUN 上有一个 `/32`（`10.210.<peer>.1`）。`10.211.0.0/16` 路由进 TUN，由 zfstack 按目的地址拆端口。内核 TCP 是对端。链路模拟器仍是 zfbench 原来的那一条，所以 **所有逻辑端口共享一个瓶颈**。重用户把队列打满时，别的 iface 上的轻用户墙钟 RTT 也会变——这是共享链路，不要写成“栈没有按端口隔离”。要看栈调度本身，用 L0 的 `noisy-cross-iface`（每端口一条链路）。

`--topology per-port-shard` 是多个 shard 共用一个 `GlobalBudget`，仍由 **一条**栈线程按固定顺序轮流跑，不是生产 Tokio adapter，也不是多核。统计里的 `scheduler_polls` 把空转和真正有进展的 slot 分开，避免把空轮询当成协议 CPU。

重用户吞吐的分母是连接都建立之后的实测墙钟（`window_wall_sec`），不是 `secs - warmup`。建连耗时在 `connect_phase_sec`。

测量窗口独立于轻用户线程生命周期；`--lights 0` 或轻用户提前失败都不能提前结束重用户窗口。

P99 少于 100 个样本、P99.9 少于 1000 个样本时写成 null。把 `--secs` 加长、`--rr-interval-ms` 减小才能谈尾延迟。kernel / smoltcp 不进这个模式：它们的 adapter 是单口的，拓扑不同，不能在这里说“比内核快”。

这次在 debug 构建上做过功能烟测（12 ms、200 Mbit/s、每格一次，不是回归门槛）：

- 同 iface，1 条重下载 + 2 个轻用户：重用户窗口约 195 Mbit/s；轻用户空载 P50 约 13 ms，加载后约 28.5 ms（各 15 个样本）
- 4 端口跨 iface：重流量在 iface 0，每个轻 iface 只有自己的小包；P50 仍上升约 14 ms，因为链路是共享的
- 4 shard：重 shard 上是重连接，每个轻 shard 各 2 条连接（空载一轮 + 加载一轮），`rx_dropped_peer_mismatch` 为 0

## L2（可选）

[`l2/run-rig.sh`](l2/run-rig.sh) 在指定 Linux `zfc-rig` 测试节点运行两个真实 WG 端口、四个 peer 的空载/重下载/恢复矩阵。复用生产 worker 与 Tokio adapter，校验数据、WG 路由和实际路径；导出墙钟请求样本、worker CPU/RSS 与预算诊断。运行与口径见 [`l2/README.md`](l2/README.md)。

[首轮结果](l2/results/2026-10-12/REPORT.md)：64 MiB / 4 条重下载通过；8 MiB / 16 条重下载下，同端口轻用户出现 3 秒读超时，跨端口继续推进；长截止时间诊断确认原连接可以恢复。失败格与诊断格分别保留。 [后续归因实验](l2/results/2026-10-13-cause/REPORT.md) 保存预算对照、同端口轻请求抓包和分析脚本。

[RX 物理信用修复对比](l2/results/2026-10-13-rx-credit/REPORT.md) 保持默认缓冲与 3 秒超时，按 8 / 64 MiB 和增加上行延迟三种配置做 12 组 ABBA，记录轻请求重传、吞吐、CPU / GiB 和 RSS；core 同时增加边界和负对照测试。

## 不在这里

- 不跑 100 个真 WireGuard
- 不给 #22 / #23 下性能结论。`idle-reclaim` 和预算计数可以当它们的混合负载入口，微基准仍以原来的为准
- 不设数值回归阈值。L0 全绿只说明不变量成立；L1 烟测是 debug、单次

最小矩阵的一次输出在 [`examples/l0-matrix.md`](examples/l0-matrix.md)。
