# 修复后 FIFO：宿主与 guest 同步观测及 CPU 调度对照

2026-10-16（Asia/Ho_Chi_Minh），接续 [取消修复及 126.36 ms 失败](../2026-10-16-fifo-diagnosis/REPORT.md)。注入器源码仍冻结为 **`1b19023df422fc3472d1f52125efece15706219a`**。

**本机仍未达标，正式 C16 为 NOT_RUN，known-BLOCKED 保留。** 在修复后的代码上捕获了更长的 **154.757459 ms** 迟到；同步数据将该窗口的主要停顿定位到宿主/虚拟化层的 vCPU 执行可用性。guest CPU 绑定、SCHED_FIFO 和缩小 VM 均没有消除超限；最后一次无热路径插桩 full 仍迟到 **24.045208 ms**。没有放宽 20 ms，也没有追加择优重跑。

## 旧 126.36 ms 的证据边界

旧 `after-full` 最大迟到是 ACTIVE 帧 **29273**：应发 `1157551234939 ns`，发出 `1157677597731 ns`，差 **126362792 ns**，见 [原始行复算](historical-126ms-row.json)。该轮没有宿主采样或 guest trace，无法事后确定这一个历史窗口的具体停顿点。

本次在相同修复源码上复现并同步观测，能够证明新捕获窗口的来源；它支持宿主调度可用性是这类故障的重要来源，**不能替代旧帧缺失的观测，也不能把全部历史迟到都归于同一个原因**。

## 事先固定的对照与全部结果

先保存 [实验计划](experiment-plan.json)，再依 A、B、C 顺序各跑两轮诊断 full，之后在 C 条件下各跑一次无热路径插桩 full / cancel / overflow。全部使用 `qa/isolate-linux.sh`，默认业务输入、500 ms、100000 包、8/16/8 秒、250 ms guard、3 秒 deadline、250 ms 合同与 **20000000 ns** 上限均未改。overflow 保持原来的 100 包负例配置。

本机为 M1 Max（8 个性能核、2 个能效核）、64 GiB；Docker Desktop 原生 ARM64 Apple Virtualization VM，Linux `7.0.12-linuxkit`，约 8 GiB。独立容器限额 6 GiB，测试时没有其他运行中的 Docker 容器。见 [环境](environment.json)。本任务没有并发启动构建或单元测试；**宿主其他任务仍在工作**，前后 ps 快照可见 rustc/cargo、rust-analyzer、WindowServer 等竞争 CPU。例如 [B2 前快照](b-full-2/before-host.txt.gz) 有两个 rustc 分别约 85% CPU。这些快照不能将 154 ms 的具体窗口归因到某个进程，环境也不构成物理资源隔离。

| 条件 | VM / 容器 CPU | 注入器 | 客户端及控制进程 | 宿主调整 |
|---|---|---|---|---|
| A | 10 vCPU / quota 6 | 原 SCHED_OTHER、未绑定 | 未绑定 | 原条件 |
| B | 10 vCPU / quota 6 | guest CPU 2，SCHED_FIFO 10 | guest CPU 3–7 | `caffeinate -i` |
| C | 4 vCPU / quota 4 | guest CPU 1，SCHED_FIFO 10 | guest CPU 2–3 | 同上，VM 10→4，给宿主留调度余量 |

这只是 guest 内亲和性分离，**没有物理核独占或 macOS 硬亲和性证明**。C 相对 B 同时改变 VM CPU 数和必要的 guest CPU 映射，不能独立估计两者的效果。原生 VM 进程不在 PRIO_DARWIN_BG；通过 task-name port 读 QoS override 失败，未猜测并修改无法准确恢复的策略，见 [能力记录](host-policy-capability.json)。CPU 限额和绑定的含义与 [Docker 资源约束文档](https://docs.docker.com/engine/containers/resource_constraints/) 一致，quota 并不保留物理核。

下表全部迟到由原始账本重新计算，单位 ms；不是报告 summary 的 residence time。

| 运行 | pre | active | drain | transparent | 超 20 ms 帧数 | 判定 |
|---|---:|---:|---:|---:|---:|---|
| a-full-1 | 17.078500 | 37.472334 | 1.119286 | 21.841709 | 22 | FAIL |
| a-full-2 | 16.257916 | 34.802417 | 3.712248 | 26.467666 | 10 | FAIL |
| b-full-1 | 25.057833 | 48.043709 | 5.363333 | 16.714750 | 46 | FAIL |
| b-full-2 | 19.418541 | 47.844583 | 2.677927 | **154.757459** | 61 | FAIL；recovery 延迟检查也失败 |
| c-full-1 | 87.222875 | 91.967542 | 2.130946 | 61.002458 | 255 | FAIL |
| c-full-2 | 35.627458 | 70.551042 | 1.434780 | 23.231791 | 43 | FAIL |
| c-plain-full | 9.684292 | **24.045208** | 3.468805 | 14.441750 | 6 | FAIL |
| c-plain-cancel | 11.617708 | 48.099750 | 1.197501 | 10.074625 | 55 | 负例自检 PASS，oracle FAIL |
| c-plain-overflow | 14.225583 | 22.901126 | 0.822537 | 8.927583 | 5 | 负例自检 PASS，oracle FAIL |

七轮 full 的已发送 UDP 均全到且有序、内容正确，TCP 字节一致，0 EAGAIN，零链路 drop，全部 FIFO 帧交付，注入期延迟不少于 500 ms，清理与 qdisc 恢复通过。六轮只有 oracle 时序失败，其他 12 项通过；**B2 还触发 recovery_transparent 检查失败**，recovery UDP 最大端到端延迟 **377.264917 ms**，超过自检原定的 125 ms（500 ms / 4），该轮其他 11 项通过。账本迟到与 UDP 端到端延迟测量区间不同，两种失败都保留。

实际输入数量也原样保留：`a-full-1` 发/收 **18464/18464**，`c-plain-cancel` 为 **18472/18472**；其他无溢出轮为 **18475/18475**。默认速率和窗口没有更改，但这两轮发送循环未在截止前发足通常的 18475 包。这是另一项本机调度不稳定的观测，不能把少发解释成链路零丢包已经证明完整业务输入达标。

## 154.76 ms：guest 看似在计算，宿主 vCPU 实际几乎没有执行

诊断启动器 [injector_probe.py](injector_probe.py) 调用字节不变的原实现，记录超过 1 ms 的 epoll / admit / emit / read_batch / send 时间段、guest CPU ID、thread CPU 和上下文切换；运行中只在内存记账。退出码透传原 main。guest 使用独立 tracefs instance，只开注入器相关的 sched_switch / sched_wakeup，每 CPU 16 MiB，六轮 **overrun / dropped events 均为 0**。macOS [host_probe.py](host_probe.py) 每请求 1 ms 通过只读 proc_pidinfo 采样全部真实 VM XPC vCPU 线程；实际间隔及空隙全部保留。

每轮前后分别通过持久 docker exec 管道做 80 次时钟往返，取所有 offset 区间的交集。B2 交集宽 **0.287208 ms**；分析取覆盖时钟不确定性两端的外侧宿主样本，不把采样间隙当作零 CPU。

关键证据见 [causal-witness.json](causal-witness.json)、[B2 完整关联](b-full-2/correlation.json)、[guest trace](b-full-2/guest.ftrace.gz)、[前后内核事件](b-full-2/worst-frame-kernel.txt)、[宿主计数](b-full-2/host-vcpu.json.gz)、[诊断事件](b-full-2/b-full-2/diagnostic-events.json.gz)：

| 观测 | 原始结果 |
|---|---|
| 帧 | seq **42402**，TRANSPARENT，1074 字节，非 GSO |
| 应发 / 发出 | `387040330633` / `387195088092 ns`，迟到 **154.757459 ms** |
| `admit()` wall / guest thread CPU | **154.699500 / 154.701541 ms** |
| guest 上下文切换 | 该 admit 内 voluntary / involuntary 均 **0**，起止均 CPU 2；对应内核 trace 无切换事件 |
| 普通外侧样本 CPU 增量 | 全部 10 vCPU 合计 **3.728 ms**；这里只是暂定计数，不直接作为上界 |
| 等计数结清后的保守上界 | 每个 vCPU 延长到连续两次 WAITING 且 user/system 计数相同；合计 **4.330 ms**，包含额外执行和每线程 1 µs 量化余量 |
| 额外窗口 | 最晚的稳定端点再晚 **2.539916 ms**，已计入上述 4.330 ms |
| 推论 | 帧的迟到窗口内，至少 **150.427459 ms** 无法解释为任何 vCPU 执行代码 |

因此这段严重迟到的主体不是 Python 给一个小帧计算 due、hash 或拷贝用了 154 ms；VM 没有得到相应的宿主执行时间。guest 没有切换线程，并不等于其 vCPU 始终在物理 CPU 上执行；guest thread CPU 在这里同样把停顿算进去了。不能用 wall≈guest CPU 将其误判为注入器计算瓶颈。

为防止宿主计数尚未结清造成误判，[分析器](analyze.py) 不再把任意 RUNNING 样本直接称为 CPU 上界。XNU 的 [thread_read_times](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/thread.c) 读取累计 recount 时间，[recount_thread_times](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/recount.c) 与当前线程即时计时有区别；[fill_taskthreadinfo](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/bsd_kern.c) 将其转换为纳秒字段，精度为微秒。本次逐线程等待稳定端点，并保留全部端点和时间戳。A1/A2/B1 的保守结清窗过宽，所得合计大于迟到本身，**不能用它们独立证明宿主停顿**；报告没有从未结清的低计数强推结论。

**对前次报告的修订：** 旧 `before-full-3` 把未经结清的 1.870 ms 合计称作宿主 CPU 上界，论证不足。本次用 [复核脚本](previous_counter_closure.py) 对旧原始样本延长端点后，得到 **376.634 ms** 的保守合计，窗口太宽，无法用它排除 guest 内其他任务竞争；见 [复核结果](previous-witness-counter-closure.json)。旧 guest 证据仍确认 epoll wall 42.536 ms、线程 CPU 0.0475 ms、唤醒后等待 40.224 ms，但单凭那组宿主累计计数不能进一步证明宿主层归因。旧报告和原始文件作为当时记录保持原字节，其相关归因结论以本修订为准。本次 B2 结清后的 4.330 ms 才是足以支持宿主/虚拟化层定位的新证据。

还事先写入 [计数校验计划](accounting-calibration-plan.json)，在全部 FIFO 运行结束后只执行一次固定 sleep / 500 万次循环 / sleep / 同样循环 / sleep。两段忙循环 guest wall 为 **547.177 / 451.738 ms**，宿主对应 vCPU 的外延计数约 **497.449 / 448.529 ms**，证明这些计数确实响应 guest 计算，见 [原始阶段](accounting-calibration/guest-phases.json)、[校验复算](calibration-recomputed.json)。这不是额外 FIFO 重跑或资格结果。

缩到 4 vCPU 后仍有同类证据：C1 最大迟到 **91.967542 ms**、结清后的全部宿主 vCPU 上界 **22.055 ms**；C2 为 **70.551042 / 29.007 ms**。这两项上界分别还包含约 300 / 157 ms 的后续窗口，仍小于迟到。三轮证据都不依赖 guest CPU 到宿主线程的一对一映射。cgroup 原始记录均无 quota throttling 或 OOM。

证据定位到宿主/虚拟化层的 CPU 执行可用性，尚不能确定是哪个 macOS 进程、内核调度机制或虚拟化内部机制触发；也没有证明每个不足 20 ms 的剩余开销都在宿主。六轮插桩与一轮无插桩不能用于估计稳定故障率或声称某个环境调整改善了尾延迟。无插桩 full 的 24.045208 ms 同样没有逐帧同步归因能力。

## cancel / overflow 与正式运行边界

- cancel：**18472/18472** UDP 恰好一次且有序、TCP 一致、0 EAGAIN、零链路 drop，**44579** 帧全部交付。redirect 移除晚于 sender 回收 **58.140250 ms**，且晚于 measurement_end **27.256228 ms**，见 [生命周期](c-plain-cancel/c-plain-cancel/lifecycle.json) 与 [原始链路计数](c-plain-cancel/c-plain-cancel/link-counters.json)。oracle 保留取消与 48.099750 ms 迟到失败，没有给负例时序豁免。
- overflow：**18475 发 / 13556 收**，缺 **4919** UDP，恰等于相同格式 UDP 的账本溢出数；全部帧溢出 **4928**。链路零 drop，TCP 字节一致，清理/qdisc 恢复通过；oracle 仍 FAIL，5 个已交付帧迟到超限也保留。
- 每轮使用新目录，返回码、命令、实际 CPU 策略、时钟映射、全部账本、内核计数、receiver/sender 原始数据均保留。唯一 setup 失败是缩 VM 后容器旧 6 CPU quota 导致启动被拒，尚未产生测试运行；已记录 [失败及修复](setup-failures.json)，调整已停止的自有容器 quota 后才开始 C。

没有变更注入器生产实现、验证器、case 或 known-failures。没有重做已通过的无关单元测试；本次验证为九轮真实内核运行、原始数据复算及脚本语法检查。测试用源码及 runtime 哈希见 [计划](experiment-plan.json)、[容器末态](final-container-state.log.gz)。

**资格前提未满足，worker 冻结构建与 held-out 正式 C16 均未开始。** 继续保留 known-BLOCKED，PR 草稿及 issue 状态不变。该 Mac 的 guest 绑定和 VM 缩容目前不能提供 20 ms 保证；后续需要可验证的宿主 CPU 隔离条件，不能把重复中偶然通过的一轮当作资格。

实验已清理，VM CPU 恢复 **10**、`Cpus` 设置恢复原本缺省，其他原有设置逐项不变；自有容器、trace instance、caffeinate 均已移除/终止，见 [恢复](vm-cpu-restoration.json) 与 [cleanup](cleanup.json)。原来的所有失败目录未覆盖。

复算入口（读取现有证据，不启动 VM 或新测试）：

```sh
python3 harness/l2/results/2026-10-16-fifo-scheduling/analyze.py
python3 harness/l2/results/2026-10-16-fifo-scheduling/previous_counter_closure.py
```

输出见 [recomputed.json](recomputed.json)。所有本次文件的大小和 SHA-256 见 [artifact-index.json](artifact-index.json)，索引不含自身；压缩终端日志保留原始字节，解压哈希见 [compressed-logs.json](compressed-logs.json)。
