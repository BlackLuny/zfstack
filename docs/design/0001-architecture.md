# zfstack 总体设计 v0.1（草案）

> 状态：草案，未评审，未实现。日期 2026-09-24。
> 使用方：zfc `zfw-wireguard`（WG 入站 userspace 后端）、`zf-client-core`（客户端 TUN netstack）。
> 本文先定边界、架构与验收口径；各子系统（缓冲、pacing、恢复）到对应阶段再出细化稿（0002+）。

---

## 0. 一句话定位

**被动打开、IP 包进 / 字节流出的代理专用 TCP 终结栈。** 不是通用嵌入式栈：不做主动建连、以太网/ARP、
IP 分片重组、URG，也不做 UDP（UDP 由使用方自行 `parse_l4` 分流，现状已健康）。
在此范围内，TCP 语义（重传、拥塞控制、乱序、SACK、窗口管理、半关闭）必须是完整、成熟的。

## 1. 为什么做：现状证据

数据来自 zfc `docs/issues/2026-09-23-wg-inbound-netstack-downlink-stall/report.md` 与 #637/#639 真机 A/B。

| 场景 | smoltcp | 内核 TCP（kernel-TUN 后端） | 备注 |
|---|---|---|---|
| 下行单流（#637 修复前） | 0–18 Mbps，整秒 stall | 453 Mbps（= 原生内核 WG） | 根因：fork 不按 cwnd 限在途 + BBR 关 pacing |
| 80 ms 浅瓶颈（#637 修复后） | 59 Mbps | 178 Mbps | #639 遗留 |
| 12 ms 0% 丢包（修复后） | 约 −10% | 基准 | #639 遗留 |
| 上行单流 | 591–602 Mbps | 861–873 Mbps | 接收侧单流窗口/调度 |
| 并发 64 建连 | 1602/2000 成功 | 2000/2000 | 待命 listener 模型 |
| 开 BBR pacing | 80 ms 吞吐减半 | — | 1 vCPU 唤醒间隔 4–7 ms，pacing 追赶失效 |

smoltcp 的**结构性**限制（fork 内难以根治）：

1. `Interface::poll` 每轮遍历整个 `SocketSet`，成本 O(连接数)；`poll_egress` 每 socket 每次只发 1 段，调用方需循环。
2. 收发缓冲是连续 `RingBuffer<u8>`：必须预付连续内存，扩缩容要搬数据，无法持有上游 buffer 做零拷贝。
3. 拥塞控制/pacing 是后加的，恢复不含 RACK-TLP/PRR；定时只有一个 `poll_at`，无高低精度分层。
4. listen 模型是「每个目的地址先建待命 listener socket」，在并发 SYN 下失败（上表 20%）。

zfc 已有的 smoltcp 侧补丁（BDP 自适配、autotune、listener 池、背压通道、Brutal、WGDIAG）是本栈的需求来源，
也是迁移时的对照清单（§12）。

## 2. 目标与非目标

### 2.1 目标（按优先级）

1. **正确性不打折**：RFC 9293 状态机，RFC 7323（WS/TS/PAWS），RFC 2018/6675（SACK），RFC 8985（RACK-TLP），
   RFC 6937（PRR），RFC 6298（RTO），RFC 5961（挑战 ACK），RFC 6528（ISN）。
2. **吞吐/延迟**：在 §10 的基准矩阵里，每一格 ≥ smoltcp（修复后），目标 ≥ 内核 TCP 的 90%。
3. **低开销**：空闲连接每条 ≤ 512 B 元数据、0 数据缓冲；活跃连接缓冲按需增长；有全局内存预算。
4. **可观测**：每连接 `tcp_info` 级统计 + 限速原因（cwnd/rwnd/sndbuf/pacing/app 受限）计时。
5. **可嵌入**：核心 sans-IO、运行时无关；worker（Linux）与客户端（Linux/macOS/Windows/iOS/Android）同一份代码。

### 2.2 非目标

- 主动建连（connect）、listen 任意端口的通用 socket API。
- 以太网 / ARP / ND / IP 分片重组（使用方用 MSS 钳制避开；收到分片直接丢并计数）。
- UDP、ICMP 应答（ICMP 错误报文可选读取用于 PMTU，后期）。
- TCP Fast Open、MPTCP、TCP-AO/MD5、URG（URG 指针忽略，数据按普通数据交付）。
- 默认开启 busy-spin（见 §6.4）。

## 3. 总体架构

```
            ┌──────────────────────── Shard（单线程独占，无锁） ────────────────────────┐
 IP 包 ───▶ │ parse/validate ─▶ demux(5-tuple→ConnId) ─▶ Conn 状态机 ─┬─▶ RX 队列 ──▶ │──▶ app read_chunk()
 (入, 所有权) │        │ SYN                                             │               │
            │        └─▶ SYN 处理（无待命 listener；压力下 SYN cookie） │               │
            │                                                          ▼               │
 app write ─▶│──▶ TX 队列(chunk) ─▶ 发送调度（cwnd ∧ rwnd ∧ pacing ∧ 出口背压）─▶ 分段 │──▶ IP 包（出, iovec）
            │                        ▲                    ▲                             │
            │   定时：粗轮(RTO/keepalive/TIME_WAIT/delack)  细堆(pacing EDT)             │
            │   内存：per-conn 上限 + 全局预算（压力分级）                              │
            └────────────────────────────────────────────────────────────────────────────┘
                     ▲ driver：唤醒源 = min(下一 deadline, 入包, app 事件)
```

- **Shard** 是唯一的状态所有者：连接表、定时器、pacing 堆、缓冲统计都在 shard 内，不共享、不加锁。
- **核心 sans-IO**：不做 I/O、不读系统时钟，`now` 由调用方传入；便于确定性测试（虚拟时钟 + 模拟链路）。
- **Driver** 负责唤醒与 I/O 搬运：`tokio` feature 提供基于 Tokio 的 driver；Linux 另提供专用线程 driver（§6.3）。
- **多核**：入口按 5-tuple hash 选 shard。初期（zfc worker 单 runtime）只跑 1 个 shard，但代码从第一天按
  「shard 私有状态 + 跨 shard 只走 SPSC 队列」写，后续扩核不改核心。

## 4. 对外 API（草图，S1 定稿）

### 4.1 核心（sans-IO）

```rust
pub struct Shard<B: PacketBuf> { /* … */ }

impl<B: PacketBuf> Shard<B> {
    pub fn new(cfg: Config, budget: Arc<MemBudget>) -> Self;

    /// 入包。所有权转移给栈：payload 可被 RX 队列直接持有（零拷贝），头部解析后丢弃。
    pub fn ingress(&mut self, now: Instant, pkt: B) -> IngressOutcome;

    /// 出包：栈把待发报文以（头部, payload 切片...）形式交给 sink，sink 负责拷贝进加密缓冲。
    /// 返回值告诉 driver 是否因出口背压而停发（sink 返回 Full 时栈保留数据，不丢包）。
    pub fn egress(&mut self, now: Instant, sink: &mut impl EgressSink) -> EgressOutcome;

    /// 下一次需要被唤醒的时刻（细堆与粗轮的最小值）。
    pub fn next_deadline(&self) -> Option<Instant>;

    /// 新连接事件（已完成三次握手）：返回 ConnId + 四元组（透明代理的 target = 原始目的地址）。
    pub fn poll_accept(&mut self) -> Option<Accepted>;

    // 应用侧（按 ConnId）：
    pub fn read_chunk(&mut self, id: ConnId, max: usize) -> ReadResult<Chunk>;   // 零拷贝读
    pub fn write_chunk(&mut self, id: ConnId, data: Chunk) -> WriteResult;        // 零拷贝写（chunk 所有权）
    pub fn shutdown_write(&mut self, id: ConnId);                                 // 半关闭（FIN）
    pub fn abort(&mut self, id: ConnId);                                          // RST
    pub fn info(&self, id: ConnId) -> Option<ConnInfo>;                           // tcp_info 等价
}

/// 使用方的包缓冲（zfc 的 PooledBuf 实现它）：拥有字节，可按区间切出共享只读视图。
pub trait PacketBuf: AsRef<[u8]> + Send + 'static {
    fn into_chunk(self, range: Range<usize>) -> Chunk;  // 至少一种实现为 Bytes 包装
}
```

### 4.2 async 适配（feature `tokio`）

- `Stack` 句柄 + `TcpStream`（`AsyncRead`/`AsyncWrite`，另有 `read_chunk().await` / `write_chunk(Bytes).await` 快路径）。
- `TcpStream` 与 shard 之间：每连接一对有界 SPSC 队列 + `AtomicWaker`，队列深度按字节计（不是按帧数）。
- 半关闭语义与 zfc 现状一致：对端 FIN 且 RX 排空 → `poll_read` 返回 EOF；app `shutdown` → 发 FIN。

### 4.3 与 zfc 现有接口的对应

| zfc 现状（`zfw-wireguard/src/inbound/netstack.rs`） | zfstack |
|---|---|
| `NetstackHandles.to_stack_tx: Sender<PooledBuf>` | driver 入口：`ingress(now, PooledBuf)` |
| `from_stack_rx: Receiver<PooledBuf>` | `EgressSink` 实现：拷入加密缓冲后投递给 boringtun |
| `accept_rx: Receiver<AcceptedConn>` | `poll_accept` → 包装成 `AnyStream` |
| `WgVirtualTcpStream` | `zfstack::tokio::TcpStream`（实现 `ClientStream` 的薄 newtype 留在 zfc） |
| `WgCcChoice::{Bbr,Cubic,Brutal}` | `Config.cc`，`CongestionControl` trait 的三个实现 |
| `ZFW_WG_SOCK_*`、`ZFW_WG_BDP_RTT_MS` 等 env | 由 autotuning + 预算取代；迁移期映射为 `Config` 上限 |

zf-client-core 的 `PacketIo` trait 同样可以直接接入 driver。

## 5. 缓冲与内存

### 5.1 chunk 化队列

- **RX**：按序到达的段直接以 `Chunk`（对入包 buffer 的共享切片）入队，不拷贝。
  乱序段放进按序号排序的小区间结构（`SmallVec` 起步，超阈值换成 BTreeMap），填洞后整体移入按序队列。
- **TX**：app 交来的 `Chunk` 入发送队列；分段/重传时对 chunk 切片；ACK 后释放。
- **truesize 记账**：按底层 buffer 实际大小记账，不按有效载荷（2 KiB buffer 只装 100 B 就按 2 KiB 计）。
  truesize/payload 比值过高时（小包攒成大量碎 buffer）触发 collapse：拷贝进紧凑 chunk。与 Linux `tcp_collapse` 思路一致，
  避免「窗口还没满、内存先爆」。

### 5.2 自调（autotuning）

- **接收侧**（参考 Linux DRS）：每个 RTT 统计 app 实际读走的字节 `copied`，`rcv_target = 2 × copied`（慢启动期再 ×2 余量），
  单调增长到 per-conn 上限；app 读取速度跟不上则不增长，窗口自然收敛，不预付。
- **发送侧**：`snd_target = max(2 × cwnd_bytes, pacing_rate × srtt × 2)`，app 写入受它背压。
  这取代 zfc 现在的「按端口限速估 BDP + 150ms 保守 RTT」静态上限。
- **缩容**：空闲超过 N 个 RTO，或者全局进入内存压力时，把队列中已消费的部分归还；空闲连接回到 0 数据缓冲。

### 5.3 全局预算（参考 `tcp_mem` 三档）

`MemBudget`（跨 shard 共享，只在记账时做原子加减，按 64 KiB 粒度批量申请，避免热路径频繁争用）：

| 档位 | 行为 |
|---|---|
| < low | 自由增长 |
| low–pressure | 停止增长；新连接从最小初始窗口起步 |
| pressure–high | 优先丢弃乱序队列（可由 SACK 恢复）；通告窗口不再右移（**不收缩右边沿**，RFC 9293 §3.8.6） |
| > high | 拒绝新 SYN（RST 或静默丢弃，可配置），活跃连接维持最小窗口 |

per-conn 上限 = 配置上限（默认 16 MiB，足够 1 Gbps × 120 ms）与「使用方给的限速 × RTT × 2」取小。

## 6. 定时器与调度

### 6.1 两级定时

| 级别 | 用途 | 结构 | 精度 |
|---|---|---|---|
| 粗 | RTO、TLP、keepalive、TIME_WAIT、零窗口探测、delayed ACK、空闲回收 | 分层 timer wheel（1 ms tick，4 层） | ≤ 1 tick |
| 细 | pacing 下一次可发时刻（EDT） | 只含「有数据且被 pacing 挡住」的连接的 4 叉 min-heap | 取决于唤醒源 |

- 每连接每类定时器只有一个内嵌槽位（intrusive），不做每定时器堆分配；取消采用惰性删除（代次号不匹配就丢弃）。
- 粗轮成本 O(到期数)；细堆规模 O(同时被 pacing 挡住的连接数)，远小于总连接数。

### 6.2 Pacing（EDT + 有界突发）

- 每连接维护 `next_send_time`，发完一段后推进 `len / pacing_rate`。
- `pacing_rate`：BBR 取模型值；CUBIC/Reno 取 `cwnd / srtt × gain`（慢启动 2.0，拥塞避免 1.2，同 Linux）；Brutal 取配置速率。
- **突发量子**：每次唤醒可发 `quantum = clamp(rate × 1 ms, 2×MSS, 64 KiB)`（同 Linux `tcp_tso_autosize` 思路）。
- **迟到补偿有界**：唤醒迟到时，可补发的量 = 迟到时间 × 速率，但上限为 `max_backlog`（默认 = 2 个量子）；超出部分直接放弃，不累积信用。
  #637 里 pacing 失败的原因是追赶预算只有 2 ms，唤醒间隔一旦是 4–7 ms，就会重新锚定、每次只发 1 段。
  这里把「平滑」与「均速」拆开处理：迟到时按比例补发，保住均速，突发量又有上限。
- **出口背压（类 TSQ）**：每连接在出口队列里的未发字节 ≤ `max(2 × quantum, 1 ms × rate)`；
  出口（WG 加密/发送）满时停发，不丢包、不重传；出口腾出后按 EDT 顺序恢复。

### 6.3 唤醒源（driver）

- **Linux 专用线程 driver**：`epoll_pwait2`（纳秒超时），或 `timerfd` + `TFD_TIMER_ABSTIME`；入包/app 事件用 eventfd 唤醒。
  shard 跑在这个线程上，Tokio 只负责上层 relay。
- **Tokio driver**（通用，客户端默认）：`sleep_until(next_deadline)` 唤醒，精度 1 ms。靠 §6.2 的有界突发保住均速，只是平滑度较差。
- 两种 driver 的核心逻辑完全相同，差别只在唤醒精度；S0 实验负责量化两者差距（§11）。

### 6.4 关于 spin

**默认不 spin。** 目标部署有大量 1 vCPU VPS，而且 zfc worker 默认 `force_single_rt=true`：spin 会和 relay 抢 CPU，
hypervisor steal 带来的抖动也不是 spin 能消除的。保留 `spin_threshold_ns` 配置（默认 0），只在多核专用机上、经 A/B 证明有收益后才开启。

## 7. 窗口与 ACK

- **Window scale**：握手时按 per-conn 上限计算 shift，不按当前缓冲大小；这样之后扩容也能通告大窗口（zfc 现在的 smoltcp 补丁也是这样做的）。
- **通告窗口** = min(rcv_target − 已排队, 预算允许量)；满足 SWS 规避（窗口更新至少为 min(MSS, rcv_buf/2)）。
- **窗口更新**：app 读走数据使可通告窗口增长 ≥ 2 × MSS 或 ≥ rcv_target/2 时，立即发窗口更新，不等下一个数据包。
- **ACK 策略**：每 2 个满段回一个 ACK；连接起始阶段、乱序或填洞时 quickack；delayed ACK 上限 40 ms（粗轮）。
  同一轮 ingress 批次内对同一连接的多个 ACK 合并，只发最后一个。
- **SACK / D-SACK / 时间戳**：默认全开（对端支持时）；时间戳用于 RTT 采样和 PAWS。

## 8. 拥塞控制与丢包恢复

- `CongestionControl` trait：`on_ack(rate_sample)`、`on_loss(event)`、`cwnd()`、`pacing_rate()`；输入统一为 **delivery rate sample**
  （BBR 需要，CUBIC 也能用），框架提供 RFC 草案 `draft-cheng-iccrg-delivery-rate-estimation` 的采样器。
- 实现：**CUBIC**（RFC 9438，含 HyStart++ RFC 9406），**BBR**（先做 v1，v3 另行评估），**Brutal**（产品需要，固定速率，对丢包不退让）。
- **丢包检测**：主路径 RACK-TLP（基于时间，对乱序鲁棒）；SACK 记分板（RFC 6675）只作为数据结构提供依据。
  不采用 go-back-N：RTO 之后只重传记分板上未被 SACK 的段。
- **恢复期发送量**：PRR（RFC 6937）。
- **RTO**：RFC 6298，min RTO 200 ms（可配），指数退避，上限 60 s；支持 F-RTO（RFC 5682）识别伪超时。
- **按 cwnd 限在途**：`bytes_in_flight` 是一等状态，按 RFC 6675 pipe 计算。#637 的根因正是 fork 没减去在途量，这一点要作为测试不变量钉死。

## 9. 连接规模与建连

- 连接表：`HashMap<FourTuple, ConnId>`（foldhash）+ slab 存 `Conn`；`Conn` 热字段放在前 2 个 cache line。
- **无待命 listener**：SYN 直接建一个轻量的半连接（只有元数据，不分配缓冲）；三次握手完成后才进入 accept 队列并分配初始缓冲。
  半连接数有上限，超限后启用 SYN cookie（需编码 MSS/WS/SACK 协商结果）。这修复的是 §1 里 20% 的并发建连失败。
- **accept 回调/过滤**：使用方可在 SYN 阶段拒绝（例如防火墙、目的地址不允许），直接回 RST，不占用资源。
- **TIME_WAIT**：只有栈这一侧先关时才进入；以紧凑条目（四元组 + 到期时间，约 48 B）放在单独的表里，由粗轮回收，有数量上限。
- 目标：10 万空闲连接元数据 ≤ 50 MiB；在超出预算的场景下，内存只随活跃字节增长，不随连接数线性增长。

## 10. 验收基准（S0 建立，贯穿全程）

**矩阵**（netns + netem，zfc-rig 同一套基础设施；另有真机 WAN 台架 95↔218/96）：

- RTT：1 / 12 / 80 / 200 ms
- 丢包：0 / 0.1 / 1 / 2 %（随机）+ 突发丢包一格
- 瓶颈：深队列（≥ 2×BDP）/ 浅队列（0.25×BDP）
- 流数：1 / 8 / 256；另加 1 万空闲 + 100 活跃的混合场景
- 方向：上行 / 下行

**对照三方**：smoltcp（当前 fork）、kernel-TUN 后端、zfstack（两种 driver）。

**指标**：吞吐（均值 + 每秒序列，用来暴露 stall）、重传率、RTT 膨胀（队列时延）、整机 CPU/GB、RSS/连接、建连成功率/延迟。

**通过线**：
- G-perf：每格 ≥ smoltcp；80 ms 浅瓶颈和上行单流这两格 ≥ 内核的 80%（最终目标 90%）。
- G-mem：1 万空闲连接 RSS 增量 ≤ 5 MiB（不含预分配池）；压力测试超过全局预算时不 OOM。
- G-correct：一致性脚本全绿；fuzz 72 h 无崩溃；和 Linux 内核 TCP 双向互通（上传/下载/半关闭/RST/零窗口）。

性能数据只在自有机器或真机上采集；GitHub CI 只跑正确性（netns 可用 sudo），不跑性能闸。

## 11. 分阶段计划（每阶段有继续/止损节点）

| 阶段 | 内容 | 交付 / 继续条件 |
|---|---|---|
| **S0** 基准与证伪（1–2 周） | ① §10 矩阵脚本，跑出三方基线；② 在 smoltcp fork 上做 EDT + 有界突发 + 专用线程唤醒实验；③ profile 1000 连接下的 poll 成本 | 如果②已让 80 ms 浅瓶颈接近内核 → 先把修复回灌 fork（与 #639 合并），zfstack 降低优先级；如果差距落在结构性问题上 → 进入 S1 |
| **S1** 核心状态机 | sans-IO 骨架、解析/校验、握手/数据/FIN/RST、RTO、window scale、时间戳、固定缓冲；虚拟时钟 + 模拟链路测试框架；一致性脚本框架 | 零丢包下与 Linux 互通；一致性脚本覆盖状态机 |
| **S2** 可靠性 | SACK 记分板、RACK-TLP、PRR、F-RTO、D-SACK；CUBIC + delivery rate 采样器 | 1–2% 丢包格 ≥ smoltcp；fuzz 目标上线 |
| **S3** pacing 与 BBR/Brutal | EDT 细堆、有界突发、类 TSQ 出口背压；BBR v1、Brutal；Linux 专用线程 driver | 80 ms 浅瓶颈 ≥ 内核 80% |
| **S4** 缓冲与规模 | chunk 队列、truesize/collapse、autotuning、全局预算、SYN cookie、TIME_WAIT 表 | G-mem 通过；1 万连接混合场景通过 |
| **S5** zfc 接入 | `zfw-wireguard` 抽出 `UserspaceStack` 抽象，`ZFW_WG_USTACK=smoltcp\|zfstack`（默认 smoltcp）；zf-client-core 同样接入 | zfc-rig 全绿；真机 A/B 通过 G-perf |
| **S6** 灰度与替换 | 真机 soak（≥ 2 周）、指标对比；达标后改默认；smoltcp 路径保留一个版本后删除 | 由用户决定何时改默认 |

S0 的②会动 smoltcp fork，和 #639 的工作面重叠（本地已有 `smoltcp-639*` 工作区），开工前要先对齐，避免两边冲突。

## 12. zfc 接入约定

- **依赖**：`zfstack = { git = "https://github.com/BlackLuny/zfstack.git", rev = … }`（私有 repo，构建机/CI 需配 token 或 deploy key）。
  本地联调用 `[patch."https://github.com/BlackLuny/zfstack.git"] zfstack = { path = "../zfstack" }`；
  提交前必须改回 git 依赖（建议 zfc 加一道闸：`Cargo.toml` 中不允许出现指向 zfstack 的 path 依赖）。
- **无镜像型 feature**：zfstack 不声明和 zfc 同名的 `mmsg`/`jemalloc`/`metric` feature；批大小、缓冲尺寸这类数字由 zfstack 导出常量，zfc 引用。
- **API 稳定**：新能力以新增方法或 `Config` 字段（带默认值）的方式加入，不改已有签名，避免出现「双仓须同版」。
- **切换方式**：环境变量只切换「userspace 后端用哪套实现」，不新增端口级产品字段（端口的 `netstack=smoltcp|kernel` 保持不变）。
  是否以及何时改为默认，由用户决定。
- **迁移对照**：smoltcp 侧现有能力逐条要有对应实现或明确废弃：BDP 自适配、autotune、listener 池（被 §9 取代）、
  背压通道、Brutal + pacing backlog、keepalive 25 s / idle 120 s、待命 listener TTL（被半连接上限取代）、WGDIAG 诊断。

## 13. 可观测性

- `ConnInfo`：srtt/rttvar/min_rtt、cwnd/ssthresh、pacing_rate、delivery_rate、bytes_in_flight、retrans/lost/reordering、
  rcv/snd 缓冲占用与上限、**受限计时**（`busy / rwnd_limited / sndbuf_limited / cwnd_limited / pacing_limited / app_limited`，对标 Linux tcp_info chrono）。
- Shard 级：连接数（按状态）、预算档位与占用、细堆规模、唤醒迟到分布（直方图）、每轮 ingress/egress 包数。
- 以上作为结构体导出，由使用方决定打日志还是上报；zfstack 本身只依赖 `log` 门面。

## 14. 风险

| 风险 | 缓解 |
|---|---|
| 可靠性长尾（半关闭、零窗口、RST 竞态、序号回绕） | 一致性脚本 + 模型对照属性测试 + fuzz + Linux 互通，这些测试先于性能优化 |
| 预期落空：瓶颈其实在唤醒抖动，新栈同样受限 | S0 先证伪，设止损节点 |
| 投入大（估计 3–5 人月到替换） | 分阶段交付，S3 前的每一步对 smoltcp 路径零影响 |
| 私有 repo 使构建机拉取依赖变复杂 | S5 前配好 remote-compile / 发版机的访问凭据 |
| 客户端平台差异（iOS 内存上限、Windows 无 timerfd） | 核心 sans-IO；平台差异只在 driver 层；客户端默认用 Tokio driver |

## 15. 待拍板

1. **范围**：首个里程碑是只做 worker（WG 入站），还是 zf-client-core 同步接入？（建议 worker 先行，客户端在 S5 后接入）
2. **拥塞控制默认值**：现在端口默认 BBR。新栈的默认值沿用 BBR，还是改为 CUBIC？（#639 已记录 CUBIC 在随机丢包下偏慢）
3. **SYN 超限策略**：超过预算时回 RST，还是静默丢弃？
4. **S0 与 #639 的关系**：EDT 实验由哪一方负责、落在哪个分支。
