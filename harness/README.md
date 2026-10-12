# 多租户 harness（issue #24）

测的是 **ZFStack 自己**在多端口、多 peer、混合负载下的行为。主路径不启动 WireGuard。WireGuard 的加密、UDP 和握手会把 CPU / 尾延迟算到别人头上，所以它只可能是以后的 L2，不是这里的分母。

```
L0  zfharness     虚拟时钟，sans-IO，无 root / TUN / WG
L1  zfbench       Linux 内核 TCP + 一条 TUN + 链路模拟器，无 WG
L2  （不做）      ZFC + WireGuard。不能拿来给 core 下结论
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
- 紧预算、零窗口邻居、出口 `Full` 之后放开、关闭后重建 iface、1% 丢包 + 乱序
- 传完进入空闲，`reserved` 不涨，连接表回到 0（`time_wait` 配成 1s 的那一格）

轻用户的 P99 要 ≥100 个虚拟样本才输出，P99.9 要 ≥1000。不够就显式省略。跨 iface 带宽平均 **不是**产品保证（0001：不做用户级 QoS），所以只报告退化，不设“必须一样快”的门槛。

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

`--topology per-port-shard` 是多个 shard 共用一个 `GlobalBudget`，仍由 **一条**栈线程轮流跑。这不是多核部署。

P99 少于 100 个样本、P99.9 少于 1000 个样本时写成 null。把 `--secs` 加长、`--rr-interval-ms` 减小才能谈尾延迟。kernel / smoltcp 不进这个模式：它们的 adapter 是单口的，拓扑不同，不能在这里说“比内核快”。

这次在 debug 构建上做过功能烟测（12 ms、200 Mbit/s、每格一次，不是回归门槛）：

- 同 iface，1 条重下载 + 2 个轻用户：重用户窗口约 195 Mbit/s；轻用户空载 P50 约 13 ms，加载后约 28.5 ms（各 15 个样本）
- 4 端口跨 iface：重流量在 iface 0，每个轻 iface 只有自己的小包；P50 仍上升约 14 ms，因为链路是共享的
- 4 shard：重 shard 上是重连接，每个轻 shard 各 2 条连接（空载一轮 + 加载一轮），`rx_dropped_peer_mismatch` 为 0

## 不在这里

- 不跑 100 个真 WireGuard
- 不给 #22 / #23 下性能结论。`idle-reclaim` 和预算计数可以当它们的混合负载入口，微基准仍以原来的为准
- 不设数值回归阈值。L0 全绿只说明不变量成立；L1 烟测是 debug、单次

最小矩阵的一次输出在 [`examples/l0-matrix.md`](examples/l0-matrix.md)。
