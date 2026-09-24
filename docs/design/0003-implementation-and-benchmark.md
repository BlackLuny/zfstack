# zfstack 实现说明与真机对比 v0.1

> 对应：[`0001-architecture.md`](0001-architecture.md) v0.3、[`0002-s0-benchmark-and-falsification.md`](0002-s0-benchmark-and-falsification.md) v0.2。
> 本文记录**已经实现了什么、与设计的差异、测试覆盖、以及在真机（本仓库的 zfbench 台架）上和 smoltcp（BlackLuny fork）及内核 TCP 的对比结果**。

## 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-09-24 | 首版：核心栈 + 仿真器 + zfbench 台架 + 第一轮真机矩阵 |

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

`cargo test --release --features tokio`：33 个测试全部通过（另有 2 个 `#[ignore]` 的诊断用例）。

| 类别 | 用例 |
|---|---|
| 单元 | 报文编解码与校验和（含奇数切片）、序号回绕、TX 环、乱序合并与 SACK 块、索引堆、记分板计数、CUBIC、BBRv3 Startup 退出 |
| 仿真场景 | 干净下载（20 MiB@100 Mbps < 2.6 s）；0.1–5% 随机丢包；突发丢包+乱序+重复；200 Mbps/80 ms/0.25 BDP 浅队列；上传；双向；零窗口 + persist；sink Full 不计发送且无忙循环；SYN cookie；传输中 MTU 下降；未读关闭发 RST；200 连接全部释放且预算归零；G-fair；执行间隔 5 ms 下 pacing 保持速率；确定性回放；BBR/Brutal；BBR 浅队列与 1% 随机丢包 |
| 鲁棒性 | 变异/随机报文注入（含 RST 与不含 RST 两组，共 7 个种子）：无 panic、无数据损坏；被伪造 ACK 卡死的连接必须由 user timeout 拆除并释放全部资源 |
| 异步适配 | tokio 句柄端到端回显 2 MiB |

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


## 6. 已知问题与后续

按优先级：

1. **低速率 pacing 的 CPU 成本**：1–12 Mbps 且有丢包时 zfstack-cubic 每 GB CPU 是 smoltcp 的 1.3–1.8×。原因是 pacing 量子 `clamp(rate×1ms, 2 MSS, 64 KiB)` 在低速时退化为 2 MSS，每个量子一次唤醒。可选：低速时放宽到 ≥ 1 ms 的时间量子而非字节量子，或 CUBIC 在 cwnd 远小于 BDP 时关闭 pacing。绝对值很小（1.8 Mbps × 17 s/GB ≈ 0.4% 单核），优先级中。
2. **zfstack-cubic 深队列排队延迟**：CUBIC 本性，默认 CC 若对交互延迟敏感应切到 BBR（本轮数据支持 BBRv3 作为默认候选：延迟最低、丢包下吞吐高一个数量级、12 ms 无丢包仍有 187–193 Mbps）。在默认切换前需要 0002 的 WAN A/B。
3. **zfstack-bbr 无丢包单流约 186 Mbps（上限 193）**：ProbeBW 的 DOWN/CRUISE 阶段和 ProbeRTT 带来约 3.5% 的损失，符合 BBRv3 预期；BBRv3 没有可对照的参考向量（未做逐 ACK 对拍 Linux 的 tcp_bbr v3），目前只靠行为测试。
4. **伪造 ACK 卡死**：接受范围内但伪造的 ACK 能让连接停滞，目前只能由 120 s user timeout 拆除（有回归测试保证资源释放）；可考虑 RFC 5961 更严格的 ACK 限速或 per-peer 伪造计数。
5. **tokio 适配层**：应用 drop 句柄后若对端窗口为零，连接作为 orphan 需等到 user timeout；缓冲按 payload 计而非 truesize（`Bytes` 分配开销未计入预算）。
6. **尚未实现 / 未完成**：0001 的 Driver B（io_uring/AF_XDP 批量驱动）、一致性 DSL、cargo-fuzz 接入与 72 h fuzz、WireGuard 集成与 0002 §3 的 WAN 真机 A/B（本台架是单机 + 用户态链路模拟器，200 Mbps 上限）。
7. **台架局限**：4 vCPU 共享于客户端/模拟器/被测栈；无 netem；只测了 RTT 12/80 ms、200 Mbps。更高带宽（≥1 Gbps）下的 CPU 结论需要在物理机上复测。

