# FIFO 本机迟到与取消丢包：诊断、修复和复验

2026-10-16（Asia/Ho_Chi_Minh）。接续 [本机失败记录](../2026-10-16-fifo-local/REPORT.md) 与 [PR #75](https://github.com/BlackLuny/zfstack/pull/75)。修复源码冻结为 **`1b19023df422fc3472d1f52125efece15706219a`**；诊断前源码为 `909e0cda43d34d20f79301e9f932694bcfa10c8d`。

**结论：已证明一段超限迟到发生在 VM 获得宿主 CPU 的环节；已修复 cancel 信号越过客户端所有权、过早拆除 clsact 的缺陷。修复后 cancel/overflow 负例通过，正常 full 仍因迟到 FAIL。20 ms 上限未改，worker 未构建，正式 held-out C16 未运行，known-BLOCKED 保留。**

## 迟到：同时核对 guest 和 macOS 的 CPU 记录

环境继续使用本机 M1 Max、原生 ARM64 Docker Desktop Linux VM（`7.0.12-linuxkit`），独立容器请求 6 CPU / 6 GiB。没有改变宿主调度策略、进程优先级、亲和性或业务负载。此环境没有物理资源独占证明。

先在原注入器上加诊断包装：记录 epoll、admit、emit、send 的 wall/thread CPU 时间与上下文切换；使用独立 tracefs instance 记录注入器的 sched_switch/sched_wakeup 和丢包事件。它们是诊断运行，不能作为未插桩的资格运行。前两轮 full 和一轮 cancel 的次数事先固定；额外 full-3 是在发现 guest CPU 计时不足以区分 vCPU 停顿后，增加 macOS 原生线程计数的观测。见 [计划](diagnostic-plan.json)、[追加观测说明](additional-plan.json)。追加说明文件写于运行之后，不能当作预注册文件。

关键证据来自 `before-full-3` 的原始帧 **23025**，其应发时间 `476564478173 ns`，实际发出 `476605436215 ns`，迟到 **40.958042 ms**：

| 观测 | 原始值及含义 |
|---|---|
| epoll 请求等待 | **1.742624 ms** |
| epoll 实际 wall 时间 | **42.535875 ms**；线程 CPU 仅 **0.047500 ms** |
| guest sched_wakeup | `476.565013 s`，target CPU 0，任务已可运行 |
| guest sched_switch 到注入器 | `476.605237 s`；唤醒后约 **40.224 ms** 才运行 |
| macOS vCPU 线程 CPU 增量 | 包住整个等待区间的采样窗内，CPU 0 线程仅 **0.871 ms**；**全部 10 个 vCPU 合计也只有 1.870 ms** |
| host/guest 时钟映射 | 运行前后通过持久管道往返约束 offset，交集宽 **0.317605 ms**；CPU 计数取覆盖该不确定性两端的外侧样本，因此包含额外 CPU 工作，作为保守上界使用 |

因此，这段约 40 ms 的停顿不能解释为注入器执行 Python、计算释放时间或拷贝帧用了 40 ms：guest 已经把它唤醒，但 VM 线程在宿主上几乎没有执行。证据定位到了**宿主/虚拟化层的 vCPU 可用性**，没有进一步证明是哪个 macOS 应用或某个虚拟化内部机制造成。不能把这个结论扩展为每个历史迟到帧的唯一归因。

另一个窗口的 emit wall/thread CPU 都约 **18.267 ms**，但外侧采样窗中全部宿主 vCPU CPU 合计仅 **2.375 ms**。这说明此 VM 中的 guest thread CPU 也可能包含 vCPU 未实际执行的时间；仅凭 wall≈guest CPU 就判成 Python 计算瓶颈会误判。Docker cgroup 原始记录没有 quota throttling 或 OOM，且 LinuxKit 未启用 CONFIG_SCHEDSTATS，不能把 `/proc/*/schedstat` 中的零等待当作没有调度等待。

完整证据和可复算入口：

- [关键窗口](causal-witness.json)、[guest trace](before-full-3.ftrace.gz)、[注入器诊断事件](before-full-3/diagnostic-events.json.gz)。
- [macOS 原始 vCPU 计数](host-vcpu-full-3.json.gz)、[前映射](clock-mapping-before-stream.json)、[后映射](clock-mapping-after-stream.json)。采样器使用公开的 `proc_pidinfo` 线程结构，见 [Apple XNU 定义](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info.h)。
- [宿主采样器](host_vcpu_probe.py)、[时钟映射工具](clock_mapping.py)、[注入器诊断包装](injector_probe.py)、[关联脚本](analyze_host.py)、[关联结果](host-correlation.json)。其中单个 guest CPU 的映射只是候选，尤其不能跨 trace 缺失区间采信；上面的结论使用全部宿主 vCPU 的 CPU 增量上界。

trace 的部分每 CPU ring buffer 有 overrun，原始 trace 与 `*.trace-stats` 均保留。这里引用的关键调度窗口在 trace 中完整可见；不声称整个运行的内核事件都完整保存。也没有用这些插桩结果替代固定 20 ms oracle。

早期 trace 的 NOT_SPECIFIED 事件流量过大，后续改用 `reason != 2` 过滤；full-3 使用过滤后的配置。诊断 shim 调用了原 main，但没有透传其返回码，因此 before 组的子进程退出码不能作为资格证据；保存的原始账本、oracle 和调度观测仍可独立重算。after 组三模式使用原入口，未使用 shim。

## cancel：提前撤掉路径，给 clsact graft 留出了丢包窗口

原实现收到 SIGTERM 后立即从 serve 返回，进入 teardown，随后删除 redirect 和 clsact。父进程则还在等待发送客户端跑完。这使原本应持续拥有流量的路径在客户端仍发包时消失。

历史 cancel 丢失的 UDP 为 seq **10541**。相邻两包的发送时间把其发送限定在 `159056950153–159063026070 ns`；redirect 已在 `158983531445 ns` 删除，注入器于 `159059312236 ns` 完成关闭。缺包位于已撤掉 redirect、且跨越 clsact 收尾的区间，见 [历史边界复算](historical-cancel-boundary.json)。原 FIFO 账本里的帧全部交付，worker tx_dropped 增量为 1。

为了验证机制，独立执行固定的四个对照：持续 UDP、不装拆；30 次 clsact 装拆；不装拆；再 30 次装拆。负载和拓扑与旧 clsact 探针相同，仅省去 ping 依赖，以 UDP 预热解析 ARP。

| 对照 | 发送 / 接收 | 缺包 | tx_dropped 增量 |
|---|---:|---:|---:|
| 不装拆 | 7348 / 7348 | 0 | 0 |
| 30 次装拆 | 7820 / 7793 | 27 | 27 |
| 不装拆 | 6537 / 6537 | 0 | 0 |
| 30 次装拆 | 7854 / 7830 | 24 | 24 |

内核 trace 恰有 **51 次 noop_enqueue、51 次 QDISC_DROP**，与 51 个缺包及计数增量闭合。50 次落在 dev_deactivate_many 与 dev_activate 的函数入口之间；另 1 次在 dev_activate 入口后 1 µs。function tracer 只记录入口，不能把 dev_activate 入口误当作激活已完成。原始 [探针输出](clsact-probe.log)、[trace](clsact-probe.ftrace.gz)、[计数](clsact-probe-counts.json) 与 [关键证据](causal-witness.json) 保留。

这与内核实现一致：[qdisc_graft](https://github.com/torvalds/linux/blob/v7.0/net/sched/sch_api.c) 会在替换/删除 clsact 时调用 dev_deactivate；[noop_enqueue](https://github.com/torvalds/linux/blob/v7.0/net/sched/sch_generic.c) 丢弃进入临时 noop 队列的 skb，并增加 tx_dropped。当前内核上不能再套用旧 6.8 实验中“该丢包计数不可见”的观察。

这些证据证明了可复现的内核机制和取消生命周期缺陷。但历史运行没有逐包内核 trace 或每个 teardown 步骤的原始 eth0 计数，**不能声称已追溯出历史 seq10541 对应的确切 skb/调用时间**；其边界与该机制一致。这一证据缺口保留，不补造。

## 修复与冻结后的验证

`1b19023` 将取消请求和路径所有权释放分开：

- 信号记录 cancelled 和时间戳，保持原 FIFO、500 ms 调度及后续透明切换，继续转发现有负载。
- 父进程回收客户端后发送最终控制消息；正常所有权交接还必须等到 measurement_end，才可拆 redirect/clsact。`load.py` 原来就按此顺序回收客户端，不需改变业务输入。
- 父进程管道 EOF 触发明确失败的异常清理；活着的父进程不释放时有 60 秒上界，重复信号不能延长它。此上界是清理保护，不是 3 秒请求 deadline 或 20 ms 释放迟到豁免。
- selftest 保存原始 link 前后计数和生命周期时刻，并收紧 cancel：UDP 必须全到、有序，路径必须晚于客户端回收与 measurement_end 才移除。原“删除后允许异常”的检查已移除。

新增 5 项回归覆盖信号后等待所有者、完整窗口、父进程死亡、有界超时及重复信号。Linux L2 **101 项零 skip**、QA **208 项 / 1 个原有 skip**通过，见 [L2](linux-l2-tests-fixed.log)、[QA](linux-qa-tests-fixed.log)；macOS 为 101 项 / 2 平台 skip、208 项 / 5 平台 skip，通过。清单审计纯新增通过，见 [Linux audit](linux-coverage-fixed.json)。初次测试 mock 的 Darwin epoll 适配错误等开发失败单独记录在 [development-failures.json](development-failures.json)，未冒充执行通过。

先写入 [验证计划](validation-plan.json)，再从冻结提交的干净 clone 顺序各执行一次 full、cancel、overflow。诊断包装、host 采样、trace instance、并行工具测试全部关闭；未改变负载或阈值，也未继续重复择优。

| 冻结后模式 | 真实判定 | 关键事实 |
|---|---|---|
| full | **FAIL** | 其余 12 项通过；pre/active/drain/transparent 最大迟到分别 **21.901917 / 126.362792 / 1.465552 / 71.531750 ms**，仍超过 20 ms。该轮未插桩，不能逐帧追加归因。 |
| cancel | **负例自检 PASS，oracle FAIL** | 18475/18475 UDP 恰好一次、有序，TCP 一致，0 EAGAIN、零链路 drop，44028 帧全部交付；redirect 晚于客户端回收 **4.112375 ms**，且晚于 measurement_end；qdisc 恢复、cleanup PASS。oracle 仍记录取消及真实迟到失败，不提供时序资格。 |
| overflow | **负例自检 PASS，oracle FAIL** | UDP 18475 发 / 13369 收，缺 5106，恰等于账本中的 UDP 溢出数；总溢出 5123 帧。零链路 drop、qdisc 恢复、cleanup PASS。该负例也不能当作正常时序资格。 |

原始数据在 `after-full/`、`after-cancel/`、`after-overflow/`。新保存的 `link-counters.json` 可直接复算 link drop，`lifecycle.json` 可复算拆除边界。完整七轮账本（含三轮 before full、一轮 before cancel）均可重新判定：

```sh
python3 harness/l2/results/2026-10-16-fifo-diagnosis/recompute.py
python3 harness/l2/results/2026-10-16-fifo-diagnosis/analyze_host.py
```

输出分别对应 [recomputed.json](recomputed.json)、[host-correlation.json](host-correlation.json)。全部文件的哈希见 [artifact-index.json](artifact-index.json)，索引不含自身。压缩原始终端日志保留原字节，解压前的大小/哈希见 [compressed-logs.json](compressed-logs.json)。原 TCG 失败、本机前轮失败、本次 before/after 失败均未覆盖。

**正式 held-out C16 继续 NOT_RUN，known-BLOCKED 未移除。** 需要能满足冻结时序上限的隔离环境，才可继续 worker 冻结构建、公共 Lease 与正式双臂运行；不能用 cancel 修复或一轮偶然通过替代这些要求。本次只定位到宿主/虚拟化调度层，尚没有使本机 full 达标的环境修复。

独立 trace instance 已禁用并删除，独立容器已停止并删除，未修改宿主调度策略。见 [cleanup.json](cleanup.json) 与 [清理前原始状态](final-container-state.log)。
