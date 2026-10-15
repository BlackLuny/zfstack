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

`quota` 的四个 peer 各发送 512 KiB，必须观察到短写、`share_blocked` 或物理预算分层失败计数，再恢复推进至完成。EventOnly 在短写时停止，不为增加失败计数额外重试。三个 `world::tests::c04_*` 测试另外逐一撞 global / port / peer 的物理上限，核对唯一对应的失败计数、事务回滚和最后 owner 释放，再用真实连接完成传输。

`port-density` 的 128 端口每口只有 1 KiB，只说明大量 iface 能握手。`crowd` 才是一个重端口加许多轻端口；`--ports 128` 可以加大，不放进每次 CI。

L0 的 `wall_sec` / `process_cpu_sec` 是 **这个进程把虚拟时钟跑完花的 CPU**，不是 PPS，也不是 RSS。`wall-bulk` 把这两列当成标题，虚拟完成时间不当延迟。

默认 `--app-drive eager` 保留原来的应用泵（L0-C）。`--app-drive event-only` 使用 L0-E：Accepted/Connected 启动应用，Readable 只激活读，Writable 只激活写；RR 消费请求/响应后才产生新写任务。读至 WouldBlock、写至短写/阻塞，沿用 core 的兴趣重新登记。应用到达、读恢复和显式外部 owner 释放分别是记录在脚本中的外部唤醒源。`RunOutcome.more` 只驱动 core，不重试所有 writer。

ready 集按含 generation 的完整 ConnId 去重；关闭/移除 iface 会清理旧项。每 endpoint 每轮最多 1024 个应用任务，每步最多 64 轮，还有总 step cap 和同一虚拟时刻的 watchdog。服务端执行间隙不会令虚拟时间倒退。这里仍是测试器调度，并非生产 adapter 的线程或 waker 测试。

```sh
cargo +1.94.1 run -p zfharness --release -- list --format json
cargo +1.94.1 run -p zfharness --release -- l0 --scenario matrix --full --app-drive event-only --json /tmp/l0-e.json
cargo +1.94.1 run -p zfharness --release -- l0 --scenario capacity-recovery --app-drive event-only
cargo +1.94.1 run -p zfharness --release -- replay --script harness/regressions/sink-recovery-v1.json
# 负对照，预期 exit 1；不能把它作为普通成功场景提交给门禁。
cargo +1.94.1 run -p zfharness --release -- replay --script harness/regressions/missing-writable-v1.json --failure-dir /tmp/missing-writable
cargo +1.94.1 run -p zfharness --release -- reduce --script harness/regressions/missing-writable-v1.json --assertion liveness.flow_complete --output /tmp/minimized.json
```

JSON 保留兼容的 `ok` 和 `errors`，增加 `app_drive`、`contract_ids`、逐流 `assertions`、调度计数、事件执行证据和资源快照。`ok` 只是功能结论，不是性能放行。机器目录给出场景名字、关联契约和驱动模式。退出码：0 功能通过；1 功能或故障前提失败；2 无效输入；3 输出文件写入失败。

`capacity-recovery` 默认使用 64 条小流突发，保留一条停读连接，再归零和重复突发；`resource_samples` 要实际观察峰值→1→0→峰值→0，并检查第二次撤载的物理预算不高于第一次。采样测量连接表、物理账本和 ready 项，不是 RSS，也不等于内部容器 retained capacity。`--heavy-conns` 可改变该场景的突发规模；4096 连接及本机 RSS 属于独立重规模验收，默认矩阵不宣称这些已覆盖。

### 有界事件重放

`harness/regressions/` 保存版本化脚本和每个反例的来源/triage。脚本只引用固定 catalogue 的逻辑 flow / port，禁止直接传进 ConnId。最多 1 MiB、4096 个事件、1 小时虚拟时间；unknown field、重复 ID、缺条件截止、越界目标都会拒绝。触发时间是从 origin 开始的 `at_ms`；同刻按 `priority`、脚本顺序执行。`when` 支持 accepted、write-blocked、memory-blocked、received；条件事件必须带 `deadline_ms`。截止没有达到条件，或重复释放、未建连关闭，结果为 `PRECONDITION_NOT_REACHED`。

支持 StartFlow、AppWrite、PauseRead、ResumeRead、CloseWrite、Abort、RemoveIface、RebuildIface、LinkBlackhole、LossBurst、SinkFull、SinkRelease、HoldMemory、ReleaseMemory、ExecutionGap 和 SetMtu。StartFlow 指定的流由事件启动；AppWrite 限制该方向只能发送脚本已经到达的字节，全部到达量不得超过 catalogue 的目标。CloseWrite 要求所有目标字节已接受。RebuildIface 先要求 RemoveIface。SinkRelease 要实际观察过 Full，黑洞恢复要实际丢过包，LossBurst 要消耗完规定丢包数；只配置故障但没有触发不会通过。

```json
{
  "schema_version": 1,
  "scenario": "wall-bulk",
  "seed": 7,
  "app_drive": "event-only",
  "until_ms": 1000,
  "events": [{
    "id": "first-arrival",
    "at_ms": 0,
    "deadline_ms": 100,
    "when": {"kind": "accepted", "flow": 0},
    "action": {"kind": "AppWrite", "flow": 0, "side": "server", "bytes": 65536}
  }]
}
```

流 payload 按流/方向 salt 和绝对 offset 生成，超过 251 条流不再出现旧周期 pattern 的同内容碰撞。每条链路的上下行使用独立 PRNG；同源码/依赖/seed/调度下可重放。随机数按该链路发包消耗，因此跨版本包数变化时**不保证**丢掉相同 TCP 序号。

端点、应用进度和真实 packet 内容/丢弃/交付摘要进入版本化 JSON 轨迹，各 ring 最多 128 项。通过只保留摘要，失败附最近轨迹；FNV-1a-64 是稳定诊断摘要，不是安全身份哈希，源码/依赖/二进制的 SHA-256 由统一 QA manifest 负责。重放失败会导出 `case.json`、`result.json`、`identity.json` 到 `--failure-dir`，未指定则使用临时目录。缩减器执行确定性事件删除，保留同一逐流失败证据、原条件事件和抑制通知次数；不支持自动缩减流/字节，不声称穷尽并发或覆盖引导 fuzz。

永久缺 Writable 反例只丢弃 server flow 0 第一次阻塞后的 Writable。EventOnly 必须留下未完成写入、零在途队列，且已接受字节全部送达；没有未来 ACK 能救场。Eager 在相同抑制策略下完成；它可能在到达通知水位前已经轮询写入，所以 Eager 的实际抑制计数可能是 0，不能伪称两种调度有同一发包/事件历史。

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
