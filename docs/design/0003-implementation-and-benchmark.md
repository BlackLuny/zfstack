# zfstack 实现说明与真机对比 v0.2

> 对应：[`0001-architecture.md`](0001-architecture.md) v0.3、[`0002-s0-benchmark-and-falsification.md`](0002-s0-benchmark-and-falsification.md) v0.2。
> 本文记录**已经实现了什么、与设计的差异、测试覆盖、以及在真机（本仓库的 zfbench 台架）上和 smoltcp（BlackLuny fork）及内核 TCP 的对比结果**。

## 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-09-24 | 首版：核心栈 + 仿真器 + zfbench 台架 + 第一轮真机矩阵 |
| v0.2 | 2026-09-24 | 修复 §6 的三项：低速 pacing CPU、伪造 ACK/时间戳失步、tokio orphan 与接收内存钉住；回归矩阵见 §5.7 |
| v0.3 | 2026-09-24 | zfbench 增加 WG 链路模式（两机真实网络 + WireGuard，不依赖 zfc），为 0002 WAN A/B 做准备；单机冒烟见 §5.8 |
| v0.4 | 2026-09-24 | 第一轮 WAN A/B（95 ↔ 218，内核 WG 两端）结果见 §5.9；据此修复单段尾部丢包要等 200 ms 的问题（TLP 用实测的对端延迟 ACK 代替固定 200 ms） |

---

## 1. 结论先行

第一轮完整真机矩阵：`bench/results/2026-09-24-full2/`（555 次运行 = 37 格 × 5 个栈 × 3 次重复，zfstack 为 `927cd26`，**0 次崩溃 / 超时 / 字节校验失败**）。

- **正确性与互通**：对端是 Linux 内核 TCP，所有下行/上行/mixed/connect 运行的逐字节校验全部通过；zfstack 两种 CC 在所有格都没有出现「零吞吐秒」（smoltcp-bbr 在 12 ms 各格有 2–10 s 零吞吐）。
- **CUBIC 对 CUBIC**（zfstack-cubic / smoltcp-cubic，下行）：16 格里 13 格在 0.90–1.12 之间（持平），另外 3 个浅队列格大幅领先：12 ms/0.25 BDP 单流 178 vs 109 Mbps（1.63×），80 ms/0.25 BDP 单流 182 vs 10.4 Mbps（17×），80 ms/0.25 BDP 8 流 191 vs 148。三次重复的离散度也小得多（smoltcp 同格 10–83 / 95–156）。没有 zfstack 明显落后的格（最差 0.90，出现在 80 ms/1% 丢包、12–14 Mbps 的格，落在三次重复的范围重叠内）。与内核 CUBIC 基本同档，浅队列格略优于内核。
- **BBR 对 BBR**：zfstack-bbr（BBRv3）在所有格都稳定：12 ms 无丢包 187–193 Mbps，1% 随机丢包 12 ms 下 129–193 Mbps、80 ms 下 30–122 Mbps；smoltcp-bbr 在 12 ms 格塌缩到 1–24 Mbps 并伴随大量瓶颈丢包（最多 15 万包），只在 80 ms/1%/深队列单流上领先（98.6 vs 77.7 Mbps）。
- **建连**：2000 次建连（并发 64）zfstack 全部成功，P99 25–26 ms（≈ 2 RTT）；smoltcp 只成功 835–983 次，其余被拒（listener 数有限），P99 145–282 ms。
- **延迟**：mixed 场景 zfstack-bbr 在 2 BDP 深队列下 RR P99 18.3 ms（12 ms RTT）/ 114 ms（80 ms RTT），是所有栈里最低；**zfstack-cubic 在深队列下会把队列填满**（P50 30.7 / 202 ms，高于 smoltcp-cubic 的 17.2 / 173 ms，也高于内核），这是 CUBIC 的预期行为且换来了满速，但它是 zfstack-cubic 唯一明确劣于 smoltcp 的指标。
- **CPU（被测栈线程 s/GB）**：无丢包下行 zfstack-cubic 4.1–7.7，smoltcp-cubic 5.3–9.6，除 80 ms/2 BDP/8 流持平（7.67 vs 7.57）外 zfstack 都更低；上行（zfstack 当接收方）9.3–9.6 对 8.5–9.8，基本持平；有丢包的高 RTT 低速率格 zfstack-cubic 12.8–17.4 对 8.8–9.9，**更贵**（pacing 在 1–12 Mbps 下的唤醒开销，见 §6）。zfstack-bbr 在无丢包格 7.5–11 s/GB，高于 cubic（pacing 更细 + 模型更新）。

总体：S0 设计要回答的「自研栈能否在正确性、吞吐、延迟、建连上全面不劣于 smoltcp fork」——在这台架上答案为**是**，唯二例外是 zfstack-cubic 在深队列下的排队延迟（CC 算法本性，BBR 解决）和低速率有丢包时的每 GB CPU。


## 2. 实现范围（对照 0001）

代码约 7.5k 行（`src/`，含测试），另有台架约 3.2k 行（`bench/`）。

| 设计章节 | 状态 | 实现位置 / 说明 |
|---|---|---|
| §3.2 sans-IO 核心 | ✅ | 核心不读时钟、不做 I/O；`now` 由调用方传入（`time.rs`） |
| §3.1 Shard / iface / PeerId，连接键 (IfaceId, 4-tuple) | ✅ | `shard.rs`；表用 std `RandomState`（带进程随机密钥的 SipHash-1-3，§10.1） |
| §4.1 核心 API | ✅ | `ingress / run / egress_released / next_deadline / poll_event / read / read_chunk / write / shutdown_write / close / abort / info / set_iface_mtu / remove_iface`；`write_chunk` 未做（chunk 快路径本期不做，§5） |
| §4.2 OutPacket = header + payload 切片 | ✅ | `OutPacket { header, payload: [&[u8]; 2] }`，TX 环形块保证一个段最多跨两块 |
| §4.3 出口合同 | ✅ | `plan → build → commit` 三段：sink 返回 `Full` 时不生成发送记录、不推进 `snd_nxt`、不动 RTO；测试 `egress_full_does_not_count_as_sent` |
| §4.4 tokio 适配 | ✅ | feature `tokio`：`tokio_adapter.rs`，按字节有界队列 + 低水位部分写入 + 检查→注册→再检查；端到端测试 `echo_roundtrip_through_async_handles` |
| §5 拷贝次数 | ✅（AsyncRead/Write 路径） | 上行：入包 `Bytes` 切片直接挂进 RX 队列（0 拷贝入栈），`read` 1 次拷贝；下行：`write` 拷进 TX 块 1 次，出包拼接 1 次 |
| §6.1 乱序队列按需分配 | ✅ | `Option<Box<OooQueue>>` |
| §6.2 物理字节记账 | ⚠️ 简化 | 按 payload 字节记账（每段一次），未做 truesize 与 collapse |
| §6.3 三级额度、分配前预留 | ✅ | `budget.rs`：全局原子计数 + shard 以 64 KiB 批量预留；per-peer 字节与连接数；per-conn 由 sndbuf/rcvbuf 约束 |
| §6.4 超售 + 进展保留 | ✅ | 预留失败丢未确认数据；按序段可用 2×MSS 进展保留 |
| §6.5 TX 拆分 | ✅ | 在途上限 `max_snd_inflight`（默认 16 MiB）+ 预读 `max(64 KiB, pacing_rate × 20 ms)` |
| §6.6 接收自调 | ✅ | Linux `tcp_rcv_space_adjust` 式；接收 RTT 用 TSecr（毫秒粒度） |
| §6.7 压力档 | ⚠️ 部分 | Pressure 档不收新乱序段、右边沿不再右移；reneging 最后手段未做 |
| §7.1/7.2 EDT pacing、信用上限跟随实测唤醒间隔 | ✅ | 信用 = clamp(唤醒间隔 P99, 2 ms, 10 ms)；pacing 粒度 = 一个量子 clamp(rate×1ms, 2MSS, 64KiB)（与 Linux TSO autosizing 一致），积累满一个量子才唤醒 |
| §7.4 两级定时器、无惰性条目 | ✅（实现方式不同） | 定时器与 pacing 都用**带位置索引的 4 叉堆**（原地改 key），每连接只占一个槽；未用 timer wheel |
| §7.5 驱动 A / B | A ✅ / B ❌ | tokio 适配即驱动 A；驱动 B（同线程加密 + sendmmsg）属 S5 zfc 接入，未做 |
| §8.1 调度状态与事件激活 | ✅ | ready / pacing 堆 / iface 冻结 / 定时器；只有 `wants_tx()` 为真才入 ready |
| §8.2 两级 DRR | ✅ | **每个 iface 一套** peer→conn DRR；测试 G-fair（1 条 vs 16 条连接，份额比 0.7–1.3） |
| §8.3 出口隔离 | ✅ | sink `Full` 时该 iface 的 DRR 原地冻结（即阻塞集），`egress_released` 后从断点继续；无忙循环（测试断言） |
| §8.4 轮次预算 | ✅ | `round_bytes_cap` 256 KiB，超出置 `RunOutcome.more` |
| §9.1 CC 框架 + delivery rate 采样 | ✅ | `cc/mod.rs`；采样按 draft-cheng，含 `tx_in_flight`/`lost`（BBRv3 需要） |
| §9.2 BBRv3 / CUBIC / Brutal | ✅ | `cc/bbr.rs` 按 draft-ietf-ccwg-bbr（Startup/Drain/ProbeBW 四相/ProbeRTT、inflight_longterm/shortterm、bw_shortterm）；**尚无参考实现对照向量**；`cc/cubic.rs` RFC 9438 + HyStart++；`cc/brutal.rs` |
| §9.3 发送记录 | ✅ | `scoreboard.rs`：按序号有序的紧凑记录（VecDeque），pipe = out − sacked − lost + retrans_out 增量维护 |
| §9.3 RACK 时间序链表 | ⚠️ 简化 | 按序号扫描到 RACK.end 为止（新数据按序发送，时间序≈序号序）；丢包多时扫描成本随窗口增长，已加 `rack_scanned` 计数 |
| §9.4 按发送决策分状态约束 | ✅ | 新数据：pipe+len ≤ cwnd 且不超对端窗口；恢复期：PRR（RFC 6937 + SSRB）；TLP 每 PTO 一段；RTO 后不 go-back-N |
| §9.5 RTO / F-RTO / D-SACK | ✅/⚠️ | RFC 6298（min 200 ms、退避、上限 60 s）；伪 RTO 用时间戳 Eifel 检测并撤销（替代 F-RTO）；D-SACK 用于 RACK reo_wnd 自适应，快速恢复的撤销未做；**重复 RTO 时清除 SACK 记分板（RFC 2018 §8）** |
| §10.1 半连接 | ✅ | SYN → admission → 半连接（仅元数据）→ SYN-ACK 重传 3 次 |
| §10.2 拒绝与 SYN cookie | ✅ | 策略拒绝回 RST；半连接满用 cookie（MSS 8 档、WS/SACK 编码在 TSval 低 6 位、60 s 轮换保留上一代）；cookie 最终 ACK 重新检查准入与资源 |
| §10.3 关闭语义 | ✅ | FIN 按序紧跟最后数据；close 有未读→RST；孤儿超时；keepalive 25 s×3；user timeout 120 s；TIME_WAIT 60 s、同时关闭、表满回收最老 |
| §10.4 MTU 变化 | ✅ | `set_iface_mtu`：MSS 只降；重传按新 MSS 重新切分记录（payload 在 TX 块里任意切片） |
| RFC 7323 WS/TS/PAWS、RFC 5961 | ✅ | 挑战 ACK 限速；TS 时钟 1 ms/tick，每连接随机偏移 |
| RFC 6528 ISN | ✅ | 4 µs 时钟 + 带密钥哈希 |
| §12 可观测性 | ⚠️ 部分 | `ConnInfo`（srtt/cwnd/pipe/pacing/各队列/恢复计数/受限计时 rwnd·cwnd·pacing·egress/CC 内部状态）；shard 计数；未做全链路时间线 |
| §14.1 确定性仿真 | ✅ | `sim.rs`：带宽/时延/drop-tail/随机与突发丢包/乱序/重复/执行间隔 G；同种子可复现 |
| §14.2 一致性脚本 DSL | ❌ | 用仿真场景测试替代（见 §3） |
| §14.3 Linux 互通 | ✅ | zfbench 真机台架：对端就是 Linux 内核 TCP（见 §4） |
| §14.4 不变量 | ✅（部分） | 每步检查：记分板计数、窗口右边沿不回退、ready 去重、容器大小 ≤ 槽位；结束检查预算归零 |
| §14.5 fuzz | ⚠️ 轻量 | 随机/变异（含重算校验和）报文注入测试；未接 cargo-fuzz，未跑 72 h |

## 3. 测试

`cargo test --release --features tokio`：39 个测试全部通过（另有 2 个 `#[ignore]` 的诊断用例）。

| 类别 | 用例 |
|---|---|
| 单元 | 报文编解码与校验和（含奇数切片）、序号回绕、TX 环、乱序合并与 SACK 块、索引堆、记分板计数、CUBIC、BBRv3 Startup 退出 |
| 仿真场景 | 干净下载（20 MiB@100 Mbps < 2.6 s）；0.1–5% 随机丢包；突发丢包+乱序+重复；200 Mbps/80 ms/0.25 BDP 浅队列；上传；双向；零窗口 + persist；sink Full 不计发送且无忙循环；SYN cookie；传输中 MTU 下降；未读关闭发 RST；200 连接全部释放且预算归零；G-fair；执行间隔 5 ms 下 pacing 保持速率；确定性回放；BBR/Brutal；BBR 浅队列与 1% 随机丢包 |
| 鲁棒性 | 变异/随机报文注入（含 RST 与不含 RST 两组，共 7 个种子）：无 panic、无数据损坏；被伪造 ACK/时间戳失步的连接必须被拆除并释放全部资源。定向用例：伪造 ACK、伪造 TSval 均在 1.4 s 内以 `Desync` 重置；突发丢包 + 1.5 s ACK 乱序 + 重复（8 个种子、337 次 RTO）无误判 |
| 内存 | 1–7 字节小段与大段交错写入接收队列：顺序正确，不再每段保留一个钉住报文的切片 |
| 异步适配 | tokio 句柄端到端回显 2 MiB；drop 句柄时对端零窗口，连接在 orphan 超时后被中止 |

测试过程中发现并修复的实际问题（均有回归测试）：
1. 应用每次 read/write 都把连接放进 ready，导致空转（加 `wants_tx()` 门控）。
2. sink `Full` 时先前的阻塞集实现会让「刚把 sink 填满的 peer」总是先被服务 → 另一个 peer 饿死（改为每 iface 的 DRR 原地冻结）。
3. user timeout 从不触发（每次 RTO 都重置了计时基准）。
4. 重复 RTO 后清除 SACK 时，被「再次 SACK」的旧记录产生数秒的伪 RTT 样本，pacing 速率塌缩。
5. BBRv3 字节制 cwnd 比 inflight_longterm 小几个字节，导致 ProbeBW_UP 永远不增长（加 1 MSS 容差）。
6. 独立代码审查发现并修复：SYN-ACK 超时不释放 half-open 计数（栈永久进入 SYN cookie 模式）；有序关闭时丢弃未读数据；对端可强迫 SACK 记录任意切分及乱序队列 O(n²)；TLP 后紧接 RTO；零窗口下的 FIN；重叠字节预算泄漏；TS.Recent 更新顺序；CUBIC 重复 RTO 重复降窗；TIME_WAIT 计数泄漏；pacing 延迟纯 ACK。
7. 审查修复引入的回归：带 FIN 的记录永远不能被 SACK 覆盖 → TLP 死循环（改为按数据末端判断 + 接收方把乱序 FIN 放进 SACK 块 + 每个 snd_una 只发一次 TLP）；乱序段上限 2048 误丢合法段（改为随 rcv_target 缩放）。

## 4. 真机测试方法

台架是 `bench/`（zfbench），在一台 4 vCPU 的 Linux 6.18 VM 上运行，**对端是真实的 Linux 内核 TCP**：

```
内核 TCP 客户端 ──TUN zfbA (MTU 1420)── 用户态链路模拟器（上/下行各一线程）── 被测栈线程（+ 同线程的应用服务端）
```

- 被测栈：`zfstack-cubic`、`zfstack-bbr`（本仓库 HEAD）、`smoltcp-cubic`、`smoltcp-bbr`（BlackLuny/smoltcp `8014f8b`，即生产 fork；8 个待命 listener、4 MiB 缓冲、nagle 关、BBR 保持 fork 的 pacing 默认值）、`kernel-cubic`（第二个 TUN + netns 里的内核 TCP 服务端，走同一个模拟器，作为「内核基线」）。
- 链路模拟：按 0002 §2.1 修正后的语义——传播时延与瓶颈队列分离，只有瓶颈 drop-tail 队列（`Lq = k × R × RTT`）会因排队丢包，随机丢包单独施加在数据方向。**本 VM 内核没有 netem**，所以用用户态模拟器；投递时刻误差均值约 20–35 µs。
- 应用：下行（服务端持续写固定模式流，客户端校验每个字节）、上行（客户端写，服务端校验并回报字节数）、mixed（一条下行大流 + 每 100 ms 一次 1 KiB 往返，测 P50/P99）、connect（2000 次建连，并发 64）。
- CPU：只统计被测栈线程的 CPU 秒 / 有效 GB（有效 = 应用收到的字节）。内核基线没有可比的线程 CPU。
- 每格 3 次重复、每次 15 s（丢弃前 3 s），报告中位数 [最小–最大]。

与 0002 的差异（诚实说明）：没有 WireGuard 加解密与 UDP 层；单机 4 vCPU 上客户端、模拟器、被测栈共享 CPU；瓶颈速率 200 Mbps；只跑了 RTT 12/80 ms。结论适用于「协议栈本身」的比较，不能直接替代 0002 的 WAN 真机 A/B。

## 5. 结果

完整报告（含每格 min–max、瓶颈丢包、零吞吐秒、仿真器健康度）：[`bench/results/2026-09-24-full2/report.md`](../../bench/results/2026-09-24-full2/report.md)，原始 JSON/日志在同目录。下面只摘录关键表（中位数，Mbps / ms）。

### 5.1 下行吞吐（被测栈发送）

| RTT | 丢包 | 队列 k | 流 | kernel | smoltcp-cubic | smoltcp-bbr | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|---|---|
| 12 | 0 | 2 | 1 | 193 | 171 | 23.9 | **193** | 187 |
| 12 | 0 | 0.25 | 1 | 156 | 109 | 1.18 | 178 | **181** |
| 12 | 1% | 2 | 1 | 9.38 | 9.31 | 2.82 | 9.87 | **137** |
| 12 | 1% | 2 | 8 | 75.5 | 82.7 | 18.8 | 80.8 | **193** |
| 80 | 0 | 2 | 1 | 193 | **194** | 193 | 192 | 186 |
| 80 | 0 | 2 | 8 | 193 | 183 | 184 | 189 | 191 |
| 80 | 0 | 0.25 | 1 | 159 | 10.4 | 52.7 | **182** | 175 |
| 80 | 0 | 0.25 | 8 | 192 | 148 | 125 | 191 | 175 |
| 80 | 1% | 2 | 1 | 1.80 | 1.73 | **98.6** | 1.82 | 77.7 |
| 80 | 1% | 2 | 8 | 12.5 | 12.8 | **129** | 12.3 | 118 |
| 80 | 1% | 0.25 | 1 | 1.83 | 1.68 | 5.61 | 1.83 | **30.4** |
| 80 | 1% | 0.25 | 8 | 12.4 | 13.9 | 35.4 | 12.5 | **122** |

### 5.2 上行吞吐（被测栈接收）

所有栈基本相同（0.95–1.02×），因为发送方都是内核 CUBIC；唯一差异是 80 ms/0.25 BDP 单流（161 / 164 / 143 / 189 Mbps，三次重复离散度大：zfstack-cubic 143–184），属于内核发送方在浅队列下的行为。上行 CPU zfstack 在无丢包单流比 smoltcp 贵约 9%（9.4 vs 8.6 s/GB），多流/有丢包时更省（80 ms/1%/8 流 14.6 vs 18.2）。

### 5.3 mixed：大流下的 1 KiB 往返延迟

| RTT | 队列 k | 指标 | kernel | smoltcp-cubic | smoltcp-bbr | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|---|
| 12 | 2 | P50 / P99 | 30.9 / 35.7 | 17.2 / 33.2 | 12.7 / 33.7 | 30.7 / 36.0 | **12.9 / 18.3** |
| 12 | 0.25 | P50 / P99 | 12.8 / 14.9 | 12.4 / 14.9 | 12.6 / 13.0 | 12.4 / 15.0 | 12.7 / 15.1 |
| 80 | 2 | P50 / P99 | 131 / 132 | 173 / 173 | 163 / 170 | 202 / 203 | **80.9 / 114** |
| 80 | 0.25 | P50 / P99 | 80.7 / 82.9 | 80.5 / 81.5 | 80.5 / 1081 | 80.4 / 83.5 | 80.9 / 100 |
| — | — | 同时大流 Mbps（12/2, 12/.25, 80/2, 80/.25） | 193, 163, 193, 158 | 171, 122, 194, 10.5 | 86, 0.8, 193, 53 | 193, 167, 192, 182 | 186, 181, 186, 175 |

空闲时（无大流）所有栈的 RR P50 都在 RTT + 0.1 ms 以内。

### 5.4 建连（2000 次，并发 64，RTT 12 ms）

| | kernel | smoltcp-cubic | smoltcp-bbr | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 成功 | 2000 | 861 | 969 | **2000** | **2000** |
| 被拒 | 0 | 1139 | 1031 | 0 | 0 |
| P50 / P99 ms | 25.0 / 30.2 | 47.5 / 282 | 45.9 / 145 | 24.7 / 25.4 | 24.8 / 26.2 |

### 5.5 CPU（被测栈线程 s / 有效 GB，下行）

| 场景 | smoltcp-cubic | zfstack-cubic | smoltcp-bbr | zfstack-bbr |
|---|---|---|---|---|
| 12 ms/0/2 BDP/1 流 | 5.80 | **5.24** | 6.43 | 8.07 |
| 80 ms/0/2 BDP/1 流 | 5.28 | **4.10** | 5.18 | 8.29 |
| 80 ms/0/0.25 BDP/8 流 | 7.60 | **6.05** | 8.58 | 10.9 |
| 12 ms/1%/2 BDP/1 流 | 7.92 | 8.62 | 11.5 | **4.73** |
| 80 ms/1%/2 BDP/1 流（≈1.8 Mbps） | **9.91** | 17.4 | 3.52 | 4.48 |

### 5.6 台架健康度

下行链路投递时刻误差均值 30.6 µs，最坏单次 48 ms（VM 调度抖动，出现次数极少，不影响中位数）；模拟器之外（TUN qdisc / softnet）丢包 0。


### 5.7 v0.2 回归（§6 第 1、4、5 项修复后）

`bench/results/2026-09-24-followup1-down/`、`-up/`：smoltcp-cubic / zfstack-cubic / zfstack-bbr，下行 16 格 + 上行 2 格，每格 3 次，162 次运行全部通过，无零吞吐秒。

| 下行 CPU s/GB | smoltcp-cubic | zfstack-cubic v0.1（full2） | zfstack-cubic v0.2 |
|---|---|---|---|
| 80 ms/1%/2 BDP/1 流 | 11.8（full2: 9.91） | 17.4 | **13.0** |
| 80 ms/1%/2 BDP/8 流 | 9.46（full2: 8.78） | 13.2 | **9.84** |
| 80 ms/1%/0.25 BDP/1 流 | 10.9（full2: 9.79） | 17.1 | **13.4** |
| 12 ms/1%/2 BDP/8 流 | 8.09（full2: 8.06） | 8.81 | **7.26** |

- 低速有丢包时 zfstack-cubic / smoltcp-cubic 的 CPU 比从 1.3–1.8× 降到 0.84–1.23×；吞吐各格与 v0.1 持平（浅队列 12 ms 单流 173 vs 113、80 ms 单流 181 vs 10.4 Mbps）。
- 上行（zfstack 当接收方，受接收队列改动影响）：193 Mbps，CPU 比 1.10（v0.1 为 1.09），满 MSS 段不拷贝，无回退。
- zfstack-bbr 80 ms/1%/单流 54.7 [17–107] Mbps（v0.1 77.7 [16.5–79.5]）：BBR 不受本轮改动影响，两轮的三次重复区间都很宽，属于该格的离散度。

### 5.8 WG 链路模式（v0.3）

**为什么放在 zfbench 而不是 zfc**：zfstack 只做 IP 包进、字节流出，出口交给 `EgressSinks`，本身不依赖 WireGuard；加解密、UDP、peer 管理归 zfc。WAN A/B 要回答的「真实路径 + 加解密占 CPU 时，三个栈谁更好」并不需要 zfc 的业务代码，所以做成台架的一种链路模式：迭代快，三个栈同一条路径。zfc 的接线（PeerId、`EMSGSIZE`→MTU、AdmissionPolicy、`ZFW_WG_USTACK`）以及上线验收仍然要在 zfc 里做。

**结构**（详见 `bench/README.md`「WG link mode」）：

- 服务端 `zfbench --serve-wg`：用户态栈由**一个线程**完成 recvmmsg → boringtun 解密 → `ingress` → `poll` → 加密 → 每轮一次 sendmmsg；内核基线是 WG 接口（内核 WG 或 boringtun TUN 桥）加内核 TcpListener。
- 客户端 `zfbench --wg-server HOST`：内核 TCP 客户端走 `zfbwg`（内核 WG 或 boringtun 用户态），测试用例与模拟器模式完全相同（down/up/mixed/connect，逐字节校验）。
- 控制通道：隧道外的 TCP + JSON 行，带 token 与 `--allow`；负责交换 WG 公钥、下发栈参数、取服务端 CPU 与上传计数（`up` 测试每秒一次）。
- `run_matrix.py --wg-server HOST`：只按测试 × 流数展开，报告换成「栈+WG 线程 CPU s/GB」和「服务端整机 CPU s/GB」（后者是内核基线唯一公平的口径）。

**单机冒烟**（`bench/results/2026-09-24-wg-smoke-veth/`）：服务端在 netns 里、经 veth 连接，两侧都是 boringtun 用户态（这台 VM 没有内核 WG），各栈 1 次 6 s。只用来证明**功能正确**：24 次运行全部通过逐字节校验；客户端 kill -9 后服务端能清理并接受下一个会话；错误 token 被拒。**数字不能当对比结论**：

- 无瓶颈、RTT 0.3 ms、同一台 4 vCPU，吞吐被客户端用户态解密卡在约 1.3 Gbps，下行各栈都在 1.0–1.4 Gbps；
- 内核基线在服务端也走 boringtun TUN 桥（多一次 TUN 往返），所以它的上行吞吐偏低，不代表内核 WG；
- mixed 的 zfstack-cubic RR P99 135 ms（其余 18–36 ms）来自客户端 UDP 接收缓冲（8 MiB）成了排队点：CUBIC 会填满瓶颈前的任何缓冲，这正是 §6 第 2 项讲的深队列问题；BBR 为 18 ms；
- smoltcp 在建连测试中 300 次里有 199 次被拒，与 §5.4 的待命 listener 现象一致。

用户态栈的「栈+WG 线程」CPU 约 2–3.6 s/GB（上行低、下行高），其中 boringtun 约占 30–40%（`link.server_wg.peer.{decap,encap}_sec`）。

### 5.9 WAN A/B 第一轮（v0.4）

数据：`bench/results/2026-09-24-wan-20260924-2018/`（`bench/wan_ab.sh`，72 次运行 = 6 格 × 4 栈 × 3 次，30 s/次；zfstack 为 PR #5 合并后的 main，本轮 JSON 未记录提交号，v0.4 起由 `wan_ab.sh` 写入）。

- 路径：客户端 95（8 核）↔ 服务端 218（4 核，另有生产负载），两端**内核 WireGuard**，隧道 RTT 12.2 ms（12.1–13.7）。下行（服务端发）约 456 Mbps 封顶，上行约 900 Mbps 封顶；下行的封顶很像一个限速器/浅缓冲（见下文重传率）。
- 所有运行逐字节校验通过，无崩溃/超时。

| | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|
| 下行单流 Mbps | 447 | **190** | 452 | 456 |
| 下行 8 流 Mbps | 457 | 443 | 456 | 456 |
| 下行 栈+WG 线程 s/GB（1 / 8 流） | – | 9.5 / 9.6 | 7.9 / 8.6 | 7.3 / 8.8 |
| 下行单流重传率 | – | – | 0.6–1.0% | **3.2–4.6%** |
| 上行 Mbps（1 / 8 流） | 848 / 881 | 904 / 909 | 853 / 901 | 899 / 896 |
| 上行 栈+WG 线程 s/GB | – | 4.9 | 4.9–5.3 | 4.8–5.0 |
| mixed RR P99 ms | 14.5 | 18.9 | 15.6 | **212** |
| 建连成功 / 2000 | 2000 | **864**（1136 被拒） | 2000 | 2000 |

结论：

- **吞吐与内核持平，单流下行是 smoltcp 的 2.4 倍**；模拟器上看到的 smoltcp 单流问题在真实链路上同样存在。
- **CPU**：下行（发送）栈+WG 线程比 smoltcp 省 10–23%；上行（接收）持平。服务端整机 CPU 受 218 上的生产负载影响，只在吞吐相同的格里有参考价值。
- **建连**：smoltcp 的待命 listener 问题在真实链路上 57% 被拒；zfstack 与内核一致（P50 24 ms = 2 RTT）。
- **zfstack-bbr 的 RR P99 = 212 ms（三次都是）**，原因是两件事叠加：
  1. **BBR 在这条路径上丢得多**：单流重传 3.2–4.6%，30 s 内 75–90 次快速恢复（远多于 ProbeBW 每 2–3 s 一次的上探），CUBIC 只有 0.6–1.0%。RR 回应与大流共用瓶颈，约 3% 的回应被丢。模拟器 450 Mbps/0.1 BDP 能复现同样的形态（BBR 1.09%、15 s 117 次恢复；CUBIC 0.24%）。BBRv3 设计上容忍每轮 ≤ 2% 的丢包，但 3–4% 超出了设计目标；需要同一路径上的内核 BBR 作参照（`wan_ab.sh` 默认栈已加入 `kernel-bbr`）。
  2. **单段尾部丢包要等 200 ms**：在途只有一个段时，RFC 8985 §7.2 的 PTO 为 `1.5·SRTT + WCDelAckT(200 ms)`，比 200 ms 的 RTO 下限还晚，于是由被 RTO 截断的 TLP 在 200 ms 修复，RR 往返变成 12 + 200 ms。服务端统计里 RR 连接 TLP 9–11 次、RTO 0 次，吻合。Linux 用同样的公式，内核基线只是丢得少。

**修复（第 2 项）**：RFC 8985 允许在确知对端不延迟 ACK 时去掉 WCDelAckT。zfstack 现在**实测对端对单个段的 ACK 延迟**（在途仅一段且被整体确认时，RTT 样本 − min RTT，衰减最大值），PTO 取 `max(2·SRTT, SRTT + 1.25·实测延迟 + 2 ms)`，上限仍是 200 ms（未测到时仍用 RFC 公式，不会比原来更激进）。Karn 规则会屏蔽被探测过的段的 RTT 样本：如果对端从 quickack 转为延迟 ACK，估计值会一直偏小、每次都误探测，所以在确认到达距探测发出不足 min RTT 时（只可能是原段的确认）用原段的发送时间补一个样本。

- 回归测试 `lost_lone_segment_is_probed_before_rto`（仿真，对端 40 ms 延迟 ACK，RTT 20 ms）：修复前 211 ms，修复后 80 ms。
- 模拟器 mixed（12 ms，内核 Linux 客户端）：有丢包时丢一个回应的代价从 212 ms 降到约 37 ms；无丢包时误探测 1–3 次/30 s（学习阶段）。

| 模拟器 12 ms | 修复前 P99 | 修复后 P99 |
|---|---|---|
| zfstack-bbr 0.2% 丢包 | 212.6 | 17.9 |
| zfstack-bbr 1% 丢包 | 212.3 | 37.6 |
| zfstack-cubic 1% 丢包 | 212.4 | 37.7 |

## 6. 已知问题与后续

按优先级：

1. ✅（v0.2）**低速率 pacing 的 CPU 成本**：实测根因不是 pacing 定时器本身，而是小窗口被 pacing 打散后 ACK 也被打散，唤醒次数多约 40%。窗口型 CC（CUBIC）在 cwnd < `pacing_min_cwnd_segs`（默认 32 段）时不做 pacing，由 ACK 时钟定速（Linux 的 CUBIC 在无 fq 时完全不 pacing）；BBR/Brutal 始终 pacing。结果见 §5.7。
2. **默认 CC 未定**：模拟器上 BBRv3 延迟最低、随机丢包下吞吐高一个数量级；但 WAN 第一轮（§5.9）在疑似限速的路径上 BBR 重传 3–4%、吞吐并无优势。随机丢包路径（移动/跨境）偏向 BBR，干净的限速路径偏向 CUBIC。需要：同路径的 `kernel-bbr` 参照、一条有真实随机丢包的长 RTT 路径，再定。
3. **zfstack-bbr 无丢包单流约 186 Mbps（上限 193）**：ProbeBW 的 DOWN/CRUISE 阶段和 ProbeRTT 带来约 3.5% 的损失，符合 BBRv3 预期；BBRv3 没有可对照的参考向量（未做逐 ACK 对拍 Linux 的 tcp_bbr v3），目前只靠行为测试。
4. ✅（v0.2）**伪造 ACK / 时间戳失步**：失步检测只认同步对端不可能发出的报文——ACK 越出 [SND.UNA, SND.MAX] 且 TSval > TS.Recent，或 PAWS 失败却确认了 SND.UNA 之后的数据；每个无进展周期（RTO / 零窗口探测 / keepalive）最多计一次且 TSval 必须递增，连续 3 个周期即发 RST、`CloseReason::Desync`。乱序/重复的旧报文两个条件都不满足。未协商时间戳的连接不检测，仍由 user timeout 兜底。
5. ✅（v0.2）**tokio orphan 与接收内存钉住**：drop 句柄时若适配层 tx 队列因零窗口排不空，以前永远不会调用 `close`、orphan 超时也不启动（对端响应探测，user timeout 同样不触发）→ 永久泄漏；现在驱动自己按 `orphan_timeout` 到期中止。接收队列里 < 1 KiB 的段拷贝进共享 8 KiB 缓冲，不再各自钉住整个入站报文（以前应用不读时 64 KiB 窗口的 1 字节小段可钉住约 6.4 万个报文缓冲）。剩余误差：≥ 1 KiB 的段仍零拷贝，钉住量 ≈ 报文大小/载荷（按 MTU 分配时 < 1.5×）；乱序队列按段数上限（平均 ≥ 512 B/段）约束在 ~3× 以内。若 WG 侧把一批报文切片自同一个大缓冲，应在 `ingress` 前拷贝或接受更大的钉住量。
8. **仿真器不完全确定**：fuzz 用例同一种子在不同运行间结局不同（很可能是 sim 里 `HashMap` 的随机迭代顺序），削弱了「确定性回放」；待改为有序容器。
6. **尚未实现 / 未完成**：0001 的 Driver B（io_uring/AF_XDP 批量驱动）、一致性 DSL、cargo-fuzz 接入与 72 h fuzz、zfc 侧接线（S5）。0002 的 WAN 真机 A/B 已有工具（§5.8 的 WG 链路模式），待在真实节点（如 95 ↔ 218/96）上跑；用数据决定默认 CC。Driver B 的必要性看 WAN 数据与 zfc 里的 E2 时间线：WG 模式目前按 Driver B 的形态（同线程加密发包）运行，若要量化 zfc 现状（交回 Tokio 任务加密）的代价，可以再加一个「加密线程交接」选项做 A/B。
7. **台架局限**：模拟器模式下 4 vCPU 共享于客户端/模拟器/被测栈；无 netem；只测了 RTT 12/80 ms、200 Mbps。更高带宽（≥1 Gbps）下的 CPU 结论需要在物理机上用 WG 链路模式复测。
9. **zfstack-bbr 在限速/浅缓冲路径上重传偏高**（§5.9）：超过 BBRv3 每轮 2% 的设计目标，恢复事件远比上探周期频繁，说明 cruise 阶段也在丢。先用 `kernel-bbr` 同路径对照判断是 BBRv3 本身还是实现问题；BBRv3 没有参考向量（第 3 项）让这件事更难判断。

