# zfstack 总体设计 v0.3

> 状态：设计定稿待 S0，未实现。v0.2 已经过一轮外部评审（GPT-6 Pro），本版回灌了评审意见，处理结果见附录 A；待拍板项已全部决定（§18）。
> 使用方：**仅 zf-worker 的 WG 入站**（`zfw-wireguard`）。zf-client-core 不在范围内。（2026-10-06：客户端 TUN 入站场景见 [0008](0008-client-profile.md)，以配置与新增 API 开启，不改变本文的服务器路径。）
> 子文档：[`0002-s0-benchmark-and-falsification.md`](0002-s0-benchmark-and-falsification.md)（S0 基准与证伪实验）。

## 修订记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v0.1 | 2026-09-24 | 初稿 |
| v0.2 | 2026-09-24 | 只做 worker；默认 BBR（BBRv3）；SYN 超限静默丢弃、策略拒绝回 RST；多端口共享 shard；关闭语义；per-peer 配额；测试体系 |
| v0.3 | 2026-09-24 | 回灌 GPT-6 Pro 评审。**重写**：pacing 参数语义（§7）、发送调度与公平（§8）、内存口径与预算（§6）、恢复不变量（§9.4）、出口合同（§4.3）。**新增**：数据所有权与拷贝次数表（§5）、驱动 B 必须覆盖加密发包（§7.5）、PeerId 由 zfc 传入、接收窗口超售策略、TX 拆分、发送记录结构、定时器无惰性条目、MTU 变化、BBRv3 版本锁定、G-lat/G-fair/G-progress。**调整顺序**：S1 就建立完整数据面骨架；CUBIC 先跑通闭环，BBRv3 放在 S3（上线默认仍为 BBRv3）。§18 待拍板项全部决定：暂不做用户级 QoS，其余按建议 |

**设计的核心**是把四件事定义完整：**谁持有数据、谁有资格发送、阻塞后由谁唤醒、内存何时真正释放**。下文各节都围绕这四件事展开。

---

## 0. 定位与范围

**被动打开、IP 包进 / 字节流出的代理专用 TCP 终结栈**，只服务 zf-worker 的 WG 入站。

- **做**：RFC 9293 状态机与完整 TCP 语义（重传、拥塞控制、乱序、SACK、窗口管理、半关闭）。
- **不做**：主动建连（只在 `test-peer` 测试 feature 中提供，用作仿真对端）、以太网/ARP/ND、IP 分片重组（收到直接丢并计数）、UDP（仍由 zfc `parse_l4` 分流）、URG（忽略指针、按普通数据交付）、TFO、MPTCP、TCP-AO/MD5、ECN（后期可选）、默认 busy-spin。
- **平台**：生产只跑 Linux（x86_64/aarch64 musl）；macOS 只要求能编译、能跑单测。

## 1. 为什么做：现状证据

来源：zfc `docs/issues/2026-09-23-wg-inbound-netstack-downlink-stall/report.md`、`tasks/wg-netstack-perf/`、#637/#639。

| 场景 | smoltcp | 内核 TCP（kernel-TUN） | 备注 |
|---|---|---|---|
| 下行单流（#637 修复前） | 0–18 Mbps，整秒 stall | 453 Mbps | fork 不按 cwnd 限在途 + BBR 关了 pacing |
| 80 ms 浅瓶颈（修复后） | 59 Mbps | 178 Mbps | #639 遗留 |
| 12 ms 0% 丢包（修复后） | 约 −10% | 基准 | #639 遗留 |
| 上行单流 | 591–602 Mbps | 861–873 Mbps | 接收侧 |
| 并发 64 建连 | 1602/2000 | 2000/2000 | 待命 listener 模型 |
| 开 BBR pacing | 80 ms 吞吐减半 | — | 1 vCPU 唤醒间隔 4–7 ms |

注意：#639 里的 59/178 这组数来自旧台架，浅队列的实现方式在 0002 §2.1 指出的问题下可能有偏差，S0 会在修正后的台架上重测。

smoltcp 的结构性限制：O(连接数) 的 `SocketSet` poll；连续 `RingBuffer<u8>` 无法零拷贝、扩缩容要搬数据；拥塞控制和 pacing 是后加的，没有 RACK/PRR；待命 listener 模型。S0 负责确认剩余差距究竟落在哪一条上。

## 2. 目标与验收口径

### 2.1 目标（按优先级）

1. **正确性不打折**：RFC 9293、7323、2018/6675、8985（RACK-TLP）、6937（PRR）、6298、5682（F-RTO）、5961、6528。
2. **多用户下的吞吐、延迟、公平**：不仅单流快，混合负载下小请求的尾延迟也不恶化；开更多连接不等于拿到更多用户级份额。
3. **低开销**：空闲连接成本低且与活跃连接数无关；内存有硬上限（分配前检查）。
4. **可解释**：每条连接都能说出「当前被什么限住了」。

### 2.2 验收门（详见 §15）

G-correct、G-perf、G-lat、G-fair、G-progress、G-mem、G-cpu。对比对象除了原版 smoltcp，还包括 **S0 回灌 E3/E4 之后的改良版 smoltcp**：真正有意义的结论是「超过认真优化过的旧实现」。

## 3. 总体架构

```
            ┌──────────────────────────── Shard（单线程独占，无锁） ─────────────────────────────┐
 入包批 ──▶ │ parse/validate ─▶ demux((IfaceId,4-tuple)) ─▶ Conn 状态机 ─▶ RX 队列 ─────────────▶ │──▶ app 读
 (+PeerId)  │      └─SYN─▶ AdmissionPolicy ─▶ 半连接表 / SYN cookie                                 │
            │                                                                                      │
 app 写 ──▶ │──▶ TX（未发 prefetch）──▶ ┌─────────── 发送调度（§8） ────────────┐                 │
            │                            │ ready: peer 轮转 → 连接轮转（DRR）    │─▶ 分段+发送记录 ─▶│──▶ EgressSink(iface)
            │   事件激活 ───────────────▶│ pacing 堆 / sink 阻塞集 / 等窗口 / 等额度 │                 │     （驱动 B：同线程加密+sendmmsg）
            │                            └───────────────────────────────────────┘                 │
            │   粗轮：RTO/TLP/RACK 重排序/persist/keepalive/delack/TIME_WAIT（原位更新，无惰性条目）│
            │   内存：物理字节记账（随 allocation 生命周期）+ 全局/peer/conn 三级额度（分配前预留）  │
            └──────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Shard、iface、peer

- 进程级 shard 池，所有 WG 端口共用（初期 1 个 shard）。端口通过 `add_iface` 注册，拿到 `IfaceId`。
- **连接键 = (IfaceId, 4-tuple)**。不同端口的 overlay 地址可能重合，只用 4-tuple 会串连接，这条写成不变量测试。
- **PeerId 由 zfc 传入**：zfc 在解密时就知道包来自哪个 WG peer（按公钥路由），入包批次附带 `PeerId`。
  连接在 SYN 时记下 PeerId。公平调度和配额都以它为单位，不依赖 overlay IP 与用户的一一对应。
- 多核扩展：入口按 `hash(IfaceId, 4-tuple)` 选 shard，跨 shard 只走 SPSC 队列。代码从一开始按这个结构写。

### 3.2 核心 sans-IO

核心不读时钟、不做 I/O，`now` 由调用方传入，因此可以跑确定性仿真（§14.1）。

## 4. 对外接口

### 4.1 核心 API（草图，S1 定稿）

```rust
impl<B: PacketBuf> Shard<B> {
    pub fn add_iface(&mut self, cfg: IfaceConfig) -> IfaceId;
    pub fn set_iface_mtu(&mut self, id: IfaceId, mtu: u16);          // §10.4
    pub fn remove_iface(&mut self, id: IfaceId, mode: CloseMode);

    pub fn ingress(&mut self, now: Instant, iface: IfaceId, pkts: impl Iterator<Item = (PeerId, B)>);
    pub fn run(&mut self, now: Instant, sinks: &mut impl EgressSinks) -> RunOutcome; // 一轮调度（§8.4 轮次预算）
    pub fn egress_released(&mut self, iface: IfaceId, bytes: usize);  // §4.3 出口额度归还
    pub fn next_deadline(&self) -> Option<Instant>;
    pub fn poll_event(&mut self) -> Option<Event>;

    pub fn read(&mut self, id: ConnId, dst: &mut [u8]) -> ReadResult;    // AsyncRead 路径
    pub fn read_chunk(&mut self, id: ConnId, max: usize) -> ReadResult<Chunk>;
    pub fn write(&mut self, id: ConnId, src: &[u8]) -> WriteResult;      // 可部分接受（§4.4）
    pub fn write_chunk(&mut self, id: ConnId, data: Chunk) -> WriteResult;
    pub fn shutdown_write(&mut self, id: ConnId);
    pub fn close(&mut self, id: ConnId);
    pub fn abort(&mut self, id: ConnId);
    pub fn info(&self, id: ConnId) -> Option<ConnInfo>;
}

pub trait AdmissionPolicy {  // 同步判断，不许 await
    fn on_syn(&mut self, iface: IfaceId, peer: PeerId, src: SocketAddr, dst: SocketAddr) -> Admission;
}
```

### 4.2 EgressSink 的形态

栈交给 sink 的是 `OutPacket { header: &[u8], payload: &[&[u8]] }`。boringtun 的 `encapsulate(src, dst)` 要求 `src` 连续，
所以 sink 必须先把 header 和 payload 拼成一个连续的 IP 包（这次拷贝无法避免，§5 已计入），然后再加密。

### 4.3 出口合同：什么叫「已经发送」

| 状态 | 含义 | 栈的动作 |
|---|---|---|
| ① 未被 sink 接收（`Full`） | 包没有离开栈 | 不算发送：不生成发送记录、不推进 `snd_nxt`、不启动或重置 RTO；连接挂到该 sink 的阻塞集 |
| ② sink 接收，进入本地有界发送队列 | 已提交 | 生成发送记录（记录发送时间与采样状态），推进 `snd_nxt`，占用该连接和该 iface 的出口额度 |
| ③ 交给 sendmmsg 完成 | 离开进程 | sink 调 `egress_released(iface, bytes)` 归还出口额度，唤醒阻塞集中的连接 |
| ④ 被 TCP ACK | 对端收到 | 释放重传数据与发送记录 |

要点：
- `Full` **不是**网络丢失，不触发任何丢包响应；但此前已经真正发出的数据，其 RTO/RACK 定时器照常运行。
- 发送记录的时间戳取 ② 的时刻。②→③ 的本地排队时长计入 `ConnInfo.egress_queue_delay`；如果它显著，说明瓶颈在加密/发包，不在 TCP。
- **BBR 的 app-limited 判定**：只有当「应用未发队列 + 本地出口队列 + 可发未发的数据」都为空时，才标记 app-limited。本地出口堵塞不算应用没有数据。
- 所有停止推进的状态都有明确的恢复事件，见 §8.1 的状态表。

### 4.4 async 适配（feature `tokio`）与唤醒

- `TcpStream` 实现 `AsyncRead`/`AsyncWrite`，内部是每连接一对按字节计的有界 SPSC 队列 + `AtomicWaker`。
- **防丢唤醒**：固定采用「检查 → 注册 waker → 再检查」的顺序（zfc 现有 `poll_write` 已是这个写法）。
- **部分写入**：写方向的可用空间 ≥ 低水位（默认 16 KiB）时，`poll_write` 接受 `min(len, 可用空间)` 字节并返回实际写入数；
  低于低水位时注册 waker 并返回 `Pending`，栈消费到可用空间 ≥ 低水位后再唤醒。这样不会出现「剩一点空间但放不下当前 chunk」导致的死等。
- **唤醒合并**：只在跨越水位时唤醒对方，不按 chunk 唤醒。

## 5. 数据所有权与拷贝次数

> 2026-09-25 接入修订：以下为最初的 sans-IO/目标快路径模型。zfc 生产适配的借用 RX、实际拷贝次数、allocation ledger、端口份额和 TIME_WAIT 生命周期以 [0004](0004-zfc-production-adapter.md) 为准；接入实现仍在进行，以 0004 的逐项状态为准。

以下是按字节路径计算的**用户态拷贝次数**（内核 socket 收发与 WG 加密/解密本身不计入）。

| 方向 | 现状（smoltcp，按 `netstack.rs` 核实） | zfstack，AsyncRead/Write 路径 | zfstack，chunk 快路径 |
|---|---|---|---|
| 下行（上游 → 客户端） | relay buf → PooledBuf（`poll_write`）→ smoltcp 发送环（`send_slice`）→ 发包缓冲（TxToken），**3 次**，然后加密 | relay buf → TX chunk（`write`）→ 拼成连续 IP 包（§4.2），**2 次**，然后加密 | TX chunk 直接来自 relay → 拼包，**1 次** |
| 上行（客户端 → 上游） | 解密缓冲 → smoltcp 接收环 → PooledBuf（`sock.recv`）→ relay buf（`poll_read`），**3 次** | 解密缓冲直接挂进 RX 队列 → relay buf（`read`），**1 次** | **0 次** |

结论与取舍：
- **第一期只做 AsyncRead/Write 路径**：下行减 1 次、上行减 2 次，不需要改 zfc relay 和 private_tun 的 `ClientStream`/`AnyStream`。
- chunk 快路径要穿透 `AnyStream` 和 relay 的拷贝循环，这是跨仓改动（private_tun），**不在本期**。S4 之后用 CPU 剖析确认拷贝确实是瓶颈再立项。
- 不为了「绝对零拷贝」引入跨线程共享的引用计数 buffer；数据一经交给某一方，就只由那一方持有。

## 6. 内存：口径、记账与预算

### 6.1 三个独立指标

| 指标 | 含义 | 目标 |
|---|---|---|
| 核心 `Conn` 大小 | 结构体本身，决定 cache 布局 | ≤ 512 B（优化目标，不为了压到这个数引入额外的指针追逐） |
| 空闲连接完整增量 | 含连接表槽位、slab 空槽摊销、async 通道与 waker、zfc 侧 handle、分配器开销 | S1 实测后定目标，初定 ≤ 1.5 KiB；**1 万空闲连接 ≤ 15 MiB** |
| 活跃连接额外成本 | payload、发送记录、乱序/SACK 描述符、出口排队 | 全部纳入 §6.3 的额度 |

乱序队列不用 `SmallVec<[_; 8]>` 内联（空队列也会占对象空间），改为 `Option<Box<OooQueue>>`，只在出现乱序时分配。

### 6.2 物理字节记账：跟着 allocation 走，而不是跟着队列位置走

- 入包 buffer 在进入栈时包装成一个**记账 owner**（一次 allocation 只记一次 truesize）。它的所有切片（RX 队列里的、`read_chunk` 交给应用的）共享这个 owner。
- **只有最后一个引用释放时才归还额度**。`read_chunk` 出队时不归还：应用还拿着这块内存，就还算占用。
- 一个 allocation 被切成多段时不重复计算。
- 分四类统计：排队 payload 字节、物理持有字节、描述符字节（发送记录、乱序区间）、池保留字节（zfc buffer 池的常驻部分，固定且小，单列）。
- truesize/payload 比值超过阈值（默认 4）时 collapse：把碎块拷贝成紧凑 chunk，释放原 allocation。

### 6.3 三级额度：分配前预留

- **全局**：`MemBudget` 跨 shard 共享。shard 以 64 KiB 为单位**先向全局预留**，再在本地消费；归还时同样按批。先预留、后使用，不存在「先分配、再补账」的窗口，所以 high 是硬上限。
- **per-peer**：默认全局 high 的 25% + 4096 连接。
- **per-conn**：见 §6.5。
- **默认总额**：取 `min(cgroup memory.max, 物理内存)` 的 3%/5%/8% 作为 low/pressure/high；读不到 cgroup 时退回物理内存。可以配置绝对值。
- **入包的预留**：入包 buffer 在 zfc 解密时已经分配好了，栈的硬上限控制的是**保留**，而不是分配：预留失败的入包直接丢弃（不确认，对端重传），buffer 立刻还给 zfc 的池。

### 6.4 已通告接收窗口：采用超售 + 丢未确认数据

已通告窗口是一种承诺，但给每条连接都按窗口预留实际内存，1 万条连接会立刻吃光预算。所以选择**超售**：

- 通告窗口不做物理预留；实际到达的数据在入栈时才预留额度（§6.3）。
- 预留失败时，丢弃**尚未确认**的数据（不会造成 SACK reneging，对端只是按正常丢包重传）。
- **进展保留**：每条连接保留一小块不受压力档影响的额度（2 × MSS），专门用于接收 `rcv_nxt` 处的按序段和填洞段。这样即使预算满了，缺失的前序段仍能进来，从而释放后面的乱序数据，不会卡死。
- 代价是明确的：严重超售时会出现额外重传和吞吐下降。压力档行为与计数见 §6.7。

### 6.5 TX 拆分

TX 内存拆成三部分，各自有各自的约束：

| 部分 | 约束 |
|---|---|
| 已发送未确认（重传所需） | 受 cwnd 和对端窗口约束；per-conn 上限默认 **16 MiB**，约等于 1 Gbps × 130 ms 的 BDP。**单流目标限定为 1 Gbps、RTT ≤ 120 ms**（§18 决策 7），超出这个范围需要提高配置上限 |
| 未发送的应用预读 | 按时间约束：`prefetch ≤ max(64 KiB, pacing_rate × prefetch_ms)`，`prefetch_ms` 默认 20 ms，只需覆盖 relay 从上游读数据的延迟，不随 BDP 放大 |
| 发送记录与出口排队 | 记在描述符字节和出口额度里（§4.3、§9.3） |

这样修正了 v0.2 的问题：原公式 `snd_target = max(2 × cwnd, …)` 在 1 Gbps × 120 ms 下可能达到约 57 MiB，和 16 MiB 上限矛盾，也会把应用预读一起放大成隐藏队列。

### 6.6 接收侧自调

- `rcv_target = 2 × 每个接收 RTT 内应用读走的字节`（慢启动期再 ×2），单调增长到 per-conn 上限。
- **接收侧 RTT 的测量**：对端带时间戳时，用 TSecr 回显计算；没有时间戳时，用「收到一个窗口的数据所需时间」估计（与 Linux `tcp_rcv_rtt_measure` 相同）。纯上传连接因此也有可靠的测量周期。
- **起步**：新连接初始通告 64 KiB。
- **应用暂停**：`rcv_target` 不立即下调；应用空闲超过 1 s 后，按每秒减半衰减。
- **应用持有 chunk**：计算可通告窗口时，把应用还持有未释放的字节也减掉。窗口通过「右边沿停止右移」逐步收窄，不回退已通告的右边沿。

### 6.7 压力档行为

| 档位 | 行为 |
|---|---|
| < low | 自由增长 |
| low – pressure | 停止增长；新连接从 64 KiB 起步 |
| pressure – high | 不再接收**新的**乱序段（进展保留除外）；限制乱序区间数量；通告窗口右边沿停止右移 |
| ≥ high | 预留失败即丢弃未确认入包（进展保留除外）；新 SYN 按 §10.2 处理 |
| 最后手段 | 只有进展保留也无法推进时，才丢弃已 SACK 的乱序数据（reneging），并单独计数告警 |

与 v0.2 的区别：进入压力档后**不再**首先丢弃乱序队列。丢掉已 SACK 的数据会造成 reneging，对端可能要等到 RTO 才会重发，对吞吐和尾延迟的伤害很大。

## 7. Pacing 与时间

### 7.1 三个参数分开

| 参数 | 控制什么 | 默认 |
|---|---|---|
| **调度量子** `quantum` | 一条连接在一次调度中最多被服务多少字节。只影响公平性和 CPU 摊销，不代表平滑尺度 | `clamp(rate × 1 ms, 2 × MSS, 64 KiB)` |
| **pacing 信用上限** `credit_cap` | 唤醒迟到后，允许追赶多少。控制「均速与突发」的取舍 | 见 7.2 |
| **出口排队时延上限** `egress_delay_cap` | 允许提前排进本地出口队列多少数据。控制线上的真实延迟 | 2 ms × iface 速率估计 |

### 7.2 信用上限的选取

EDT 语义：每条连接有 `next_send_time`，每发出 `len` 字节就推进 `len / rate`。
信用 = `max(0, now − next_send_time) × rate`，上限 `credit_cap = rate × T_credit`。

**必须承认的边界**：一个每 `G` 秒才能获得一次 CPU、又没有下层定时发送的发送器，想维持速率 `rate`，每次就必须发出 `rate × G` 字节。
所以只有 `T_credit ≥ G` 时才能保住均速；`T_credit < G` 时吞吐上限是 `rate × T_credit / G`。
v0.2 的写法（信用上限 = 2 个量子 ≈ 2 ms）在 G = 5 ms、200 Mbps 下只能跑到约 80 Mbps，这个评审指出的算术是对的。

因此：
- `T_credit = clamp(实测唤醒间隔 P99, 2 ms, 10 ms)`。唤醒间隔由 shard 自己持续测量（滑动窗口直方图）。
- 在 1 vCPU、G ≈ 5 ms、200 Mbps 的机器上，这意味着每次唤醒会发出约 125 KB 的突发。这是在「CPU 给不了更及时的执行」前提下的最小代价，而不是设计失误。
- `G > 10 ms` 时主动放弃均速，并计入 `pacing_credit_dropped`。持续出现说明这台机器需要驱动 B，或者 CPU 不够。
- **一次唤醒内**，同一条连接可以被多次调度（每次最多一个量子），但累计发出量不超过它的信用；同时受 shard 轮次预算（§8.4）约束。

### 7.3 pacing_rate 的来源

- BBRv3：取模型值。
- CUBIC：`cwnd / srtt × gain`，慢启动 2.0，拥塞避免 1.2（与 Linux 相同）。
- Brutal：配置速率。

### 7.4 两级定时器（无惰性条目）

| 级别 | 用途 | 结构 |
|---|---|---|
| 粗 | 发送侧槽（RTO / TLP / RACK 重排序 / persist，按 RFC 8985 共用一个槽，任一时刻只有一个生效）、连接槽（keepalive / delayed ACK / TIME_WAIT / 空闲回收） | 分层 timer wheel，1 ms tick；槽位是嵌在 `Conn` 里的双向链表节点，**重设时原地摘链再挂链**，O(1)，不追加新条目 |
| 细 | pacing 的 `next_send_time` | 带位置索引的 4 叉堆（位置存在 `Conn` 里），支持原地调整 key 和删除 |

两种结构的容量都等于「当前挂着的连接数」，不会因为反复重设而增长。验收时同时检查容器容量（§14.4）。

### 7.5 驱动线程模型

| | A：Tokio 任务 | B：专用线程 |
|---|---|---|
| shard 运行位置 | 与 relay 同一 current-thread runtime | 独立 OS 线程 |
| 唤醒 | `sleep_until`，1 ms 粒度 + 任务调度延迟 | `epoll_pwait2` 超时 + eventfd |
| **出包** | 交给现有 WG 任务加密 + sendmmsg | **shard 线程自己做加密 + sendmmsg**：sink 持有 UDP socket 的 fd 克隆，对 peer 的 `Mutex<Tunn>` 加锁调用 `encapsulate`（锁按 peer 划分、持锁时间很短，与解密侧争用有限） |
| 风险 | relay 长轮次推迟 pacing 唤醒 | 与 relay 争用 CPU；跨线程交接开销 |

- **驱动 B 必须覆盖加密和发包**。否则 TCP 线程唤醒得再准，包也要交回 Tokio 任务，等待几毫秒才被加密发出，网络上看到的 pacing 并没有改善。
- `epoll_pwait2` 的纳秒超时参数只说明「超时可以这么精确地指定」，不代表线程能在这个时刻拿到 CPU。实际调度延迟由 S0 的全链路时间线测量。
- **全链路时间线**（两种驱动都采样记录，默认 1/1000 抽样）：计划发送时间 → shard 实际运行时间 → sink 接收时间 → 加密完成 / sendmmsg 提交时间。先找出迟到发生在哪一段，再决定线程边界。
- **驱动 B 的验收**：线上出包节奏确实改善（0002 E3 的突发与排队时延指标），**且**整机 CPU / 有效 GB 不退化。计时探针变准不算。
- 默认不 spin；保留 `spin_threshold_ns`（默认 0）。

## 8. 发送调度

### 8.1 连接的调度状态

每条连接在任一时刻处于下列状态之一（同一类集合里只出现一次，入集时去重）：

| 状态 | 所在集合 | 由什么事件激活 |
|---|---|---|
| 可发送 | ready（按 peer 分组） | — |
| pacing 阻塞 | 细堆 | 到达 `next_send_time` |
| 出口阻塞 | 对应 sink 的阻塞集 | 该 sink 的 `egress_released` |
| 等待应用数据 | 不在任何集合 | app `write` |
| 等待窗口/ACK | 不在任何集合 | 收到推进 `snd_una` 或窗口的 ACK |
| 等待内存额度 | 额度等待队列（按 peer） | 该 peer 或全局额度归还 |
| 等待定时器 | 粗轮 | 定时器到期 |

每一轮只处理发生了事件的连接，成本与活跃连接数成正比，与总连接数无关。
两种退化要显式防止：
1. 出口持续 `Full` 时，连接留在阻塞集里，**不**回到 ready 空转。
2. 额度归还时，直接唤醒额度等待队列里的连接，**不**依赖周期扫描。

### 8.2 用户级公平：两级 DRR

- 第一级在 peer 之间轮转，第二级在同一 peer 的连接之间轮转，每次服务一个量子。
- EDT 只决定「这条连接是否已经到了可以发的时间」，**不**决定谁先被服务。
- 效果：一个 peer 开 200 条下载连接，拿到的服务机会和只开 1 条的 peer 大致相同（在两者都有数据可发时）。
- 各 peer 等权（§18 决策 6：暂不做用户级 QoS，不实现权重）。

### 8.3 出口隔离

- 每个 iface（WG 端口）有自己的 sink 和出口额度。某个 sink `Full` 只挂起该 iface 的连接，其他 iface 照常调度。
- 被挂起的 iface 在 `egress_released` 之前不会被重试，避免忙循环。

### 8.4 shard 轮次预算

每轮 `run` 最多发出 `min(Σ 各 iface 的 egress_delay_cap 余量, round_bytes_cap)` 字节，`round_bytes_cap` 默认 256 KiB，用完就结束本轮，交还控制权。
原因：每条连接单独有界，不等于整个 shard 的突发有界。比如 100 条连接各发一个 64 KiB 量子，一轮就是 6.25 MiB，1 Gbps 出口需要约 52 ms 才能发完，短请求会被压出很高的尾延迟。

## 9. 拥塞控制与丢包恢复

### 9.1 框架

`CongestionControl` trait：`on_ack(&RateSample)`、`on_loss(&LossEvent)`、`on_rto()`、`cwnd()`、`pacing_rate()`。
框架统一提供 delivery rate 采样（`draft-cheng-iccrg-delivery-rate-estimation`），样本状态保存在发送记录里（§9.3）。

### 9.2 算法与版本锁定

| 算法 | 规范 | 说明 |
|---|---|---|
| **BBRv3**（上线默认） | **锁定 `draft-ietf-ccwg-bbr-06`（2026-07-06）**，变量命名以该版为准（`inflight_longterm/shortterm` 等），参考实现为 Google Linux BBRv3 树的某个具体 commit（S3 开工时锁定并写进本文） | 端口配置为 BBR 时使用。选 v3 的原因：v1 在浅队列下对丢包不响应。不同版本草案的伪代码不混用；用参考实现生成对照向量，做逐步比对 |
| CUBIC | RFC 9438 + HyStart++（RFC 9406） | **开发顺序上先做**，用于跑通和验证完整代理闭环 |
| Brutal | `cwnd = rate × srtt × 2`，对丢包不退让，pacing = 配置速率 | 产品需要 |

### 9.3 发送记录与 RACK 数据结构

- **发送记录**：每个已发送但未确认的段一条紧凑记录（约 40 B：起止序号、发送时间、是否重传、是否已 SACK、delivery rate 样本状态）。payload 仍由 TX chunk 持有，记录里不存数据。
- **存储**：每连接一个按序号排列的环形数组（从 shard 级 slab 按块分配，不做每段一次堆分配），按序号二分查找。
- **按发送时间的次序**：用记录上的侵入式链表维护，重传的记录移到链尾。RACK 从链头开始检查，遇到第一条「还不能判为丢失」的记录就停止，每次 ACK 的成本与「新判定丢失的段数」成正比（RFC 8985 讨论的避免扫描整个记分板的做法）。
- **规模**：16 MiB 在途约 1.16 万段，按 40 B 计约 460 KiB，计入描述符字节额度。

### 9.4 恢复路径与发送约束（修正 v0.2 的错误不变量）

v0.2 写的「任何时刻 in_flight ≤ cwnd」和「序号不超过 `snd_una + min(cwnd, snd_wnd)`」都会把正确的 TCP 行为判成错误：
cwnd 缩小之后，已经发出去的数据无法收回，在途量暂时超过新 cwnd 是正常的；TLP 允许在途量超过 cwnd 一个段；SACK 恢复期按 `cwnd − pipe` 计算，而不是按序号右边界。

改为**按发送决策分状态约束**：

| 发送类型 | 约束（在决定发送的那一刻检查） |
|---|---|
| 正常新数据 | `pipe + 段长 ≤ cwnd`，并且段尾 ≤ `snd_una + snd_wnd`（对端窗口右边沿） |
| 恢复期（RFC 6675 + PRR） | 发送量 ≤ PRR 给出的额度；新数据仍受对端窗口右边沿约束 |
| TLP | 每个 PTO 最多一个段，允许在途量超过 cwnd 一个段 |
| persist 探测 | 对端零窗口时，每次 persist 到期最多 1 字节（或 1 个段） |
| RTO 重传 | RTO 后 cwnd 复位，重传只补记分板上未被 SACK 的段，**不做 go-back-N** |

#637 的根因（fork 发送新数据时没有减去在途量）在这张表里对应「正常新数据」一行，作为发送决策点上的断言。

### 9.5 其他

- RTO：RFC 6298，min 200 ms，指数退避，上限 60 s；F-RTO 识别伪超时。
- D-SACK 和时间戳用于识别伪重传，并撤销 cwnd 的缩减。

## 10. 建连与关闭

### 10.1 半连接与 accept

- 连接表：`HashMap<(IfaceId, FourTuple), ConnId>` + slab。**哈希用带进程随机密钥的 SipHash-1-3**（std `RandomState`）：连接键由租户控制（源端口可任意选），租户虽经 WG 认证但不可信；foldhash 的抗 DoS 能力有限，不作为安全边界。S1 做基准，如果哈希成为热点再评估。
- SYN → `AdmissionPolicy::on_syn` → Accept 时建立只有元数据的半连接，回 SYN-ACK。握手完成后进入 accept 队列，并分配初始缓冲。
- 半连接超时：SYN-ACK 重传 3 次（1 s 起指数退避）后回收。

### 10.2 超限与拒绝

| 情况 | 行为 |
|---|---|
| 策略拒绝（防火墙、目的地址禁止） | 回 RST |
| 半连接表满 | SYN cookie |
| accept 队列满、全局 ≥ high、per-peer 配额满 | 静默丢弃 SYN |

**SYN cookie 只节省半连接状态**，不产生建立连接所需的内存，也不给 accept 队列腾位置。所以：
- 带 cookie 的最终 ACK 到达时，**重新检查准入和资源**。检查失败就丢弃这个 ACK，不建连接。
- 客户端此时认为连接已建立，会继续发数据或重传。这些段携带同一个 ack 号，cookie 在有效期内（当前代 + 上一代密钥，共约 120 s）可以再次验证，因此资源恢复后连接能被延迟建立。
- cookie 编码 MSS 档位（8 档）、WS shift、SACK 许可；有时间戳时放在 TSval 低位。密钥每 60 s 轮换，保留上一代。
- 计数分开：cookie 发出、cookie 验证成功、验证成功但因资源不足被丢弃。

### 10.3 关闭语义

| 情况 | 行为 |
|---|---|
| `shutdown_write` | FIN **紧跟在最后一个数据字节之后按序发出**，不需要等之前的数据被确认 |
| `close`，只有未发数据 | 进入孤儿状态：继续发完、发 FIN。受孤儿超时（默认 60 s）和孤儿总内存（计入全局预算）约束 |
| `close`，有未读数据（无论是否还有未发数据） | **回 RST，丢弃未发数据**。未读数据优先：应用不再读取，说明数据确实丢了，要让对端知道（与 Linux 相同） |
| `abort` | 立即 RST |
| 对端 FIN | 接收队列排空后 `read` 返回 EOF，写方向不受影响 |
| 对端 RST | 读写报错，立即释放 |
| handle drop | 等价于 `close` |

- keepalive：空闲 25 s 开始探测，每 25 s 一次，连续 3 次无响应断开（与 zfc 现状一致）。
- user timeout（RFC 5482）：已发数据 120 s 未被确认就断开。
- **TIME_WAIT**：主动关闭（FIN_WAIT_2 → TIME_WAIT）和**同时关闭**（CLOSING → TIME_WAIT）都会进入。紧凑条目约 48 B，时长 60 s（2 × MSL，MSL = 30 s）。
  表满时回收最老的条目。这是一个取舍：提前结束 TIME_WAIT 存在 RFC 1337 所述的风险，时间戳和 PAWS 只能降低、不能消除这一风险。回收次数单独计数。
- 端口下线：默认 RST 全部连接；可选优雅关闭（发 FIN，最多等 N 秒）。

### 10.4 MTU 变化

不做 IP 分片，但要处理路径 MTU 变小的情况：
- zfc 在 sendmmsg 返回 `EMSGSIZE`，或配置的出口 MTU 发生变化时，调用 `set_iface_mtu`。
- 该 iface 上的连接把有效 MSS 降到新值，之后的新段按新 MSS 分段；**重传也按新 MSS 重新切分**（payload 由 chunk 持有，可以任意切片，不需要额外拷贝）。
- MSS 只降不升（除非 iface 显式恢复）；变化次数计数。

## 11. 与 zfc 限速、计费的交互

- 端口限速继续在 relay 层用令牌桶做，不移进栈。对栈来说是 app-limited，BBRv3 按 app-limited 处理这类样本。
- per-conn 的「已发送未确认」上限由 zfc 按限速收紧：`min(16 MiB, 限速 × 200 ms × 2)`。
- 计费继续在 relay 层按字节统计。
- 把限速下沉为栈内 pacing 上限：本期不做，以后另行立项。

## 12. 可观测性

- `ConnInfo`：srtt/rttvar/min_rtt、cwnd/pipe、pacing_rate、delivery_rate、retrans/lost/reordering/spurious、TX 三部分与 RX 的占用、
  **受限计时**（`rwnd / sndbuf / cwnd / pacing / egress / quota / app`）、`egress_queue_delay`。
- Shard：各状态连接数、ready/阻塞集/等待队列长度、预算四类占用与档位、SYN 与 cookie 各项计数、唤醒间隔直方图、`pacing_credit_dropped`、全链路时间线抽样、reneging 与 TIME_WAIT 回收计数。

## 13. zfc 接入约定

- 依赖：git + rev 锁定；本地联调用 `[patch]` 指向 `../zfstack`，提交前改回。zfc 加闸：`Cargo.toml` 不许出现指向 zfstack 的 path 依赖。私有 repo 需要在 S5 前给 remote-compile 和发版机配好凭据。
- 不做镜像型 feature；批大小等数字由 zfstack 导出常量。API 只增不改。
- 切换：`ZFW_WG_USTACK=smoltcp|zfstack`，进程启动时读取，默认 smoltcp；端口级 `netstack` 字段不变。
- zfc 侧需要的配合：入包批次带 `PeerId`；`EMSGSIZE` 时调用 `set_iface_mtu`；驱动 B 模式下向 sink 提供 UDP fd 克隆和 peer 的 `Tunn` 句柄；`AdmissionPolicy` 接防火墙。
- smoltcp 侧现有能力的迁移对照：

| 现有能力 | zfstack 对应 |
|---|---|
| BDP 自适配 | per-conn 上限按限速收紧 + 自调 |
| autotune | §6.6 |
| listener 池 / 待命 TTL | 半连接表，废弃 |
| 有界背压通道 | 按字节 SPSC + 低水位（§4.4） |
| Brutal + pacing backlog | Brutal + §7.2 信用上限 |
| keepalive 25 s / idle 120 s | §10.3 |
| 出口 MTU 钳制 | `IfaceConfig.mtu` + `set_iface_mtu` |
| WGDIAG | §12 |
| FIN 后及时 EOF | §10.3 |

## 14. 测试体系

### 14.1 确定性仿真器

虚拟时钟 + 模拟链路（带宽、时延、队列、随机/突发丢包、乱序、重复、抖动），**外加可注入的「执行间隔」**：模拟 shard 每隔 G 才获得一次 CPU。
同一种子结果完全可复现；CI 用固定种子，每晚跑随机种子扫描。

### 14.2 一致性脚本

packetdrill 风格的 Rust DSL。覆盖握手各分支、SYN cookie（含最终 ACK 被资源拒绝后的延迟建立）、WS、SACK、零窗口/persist、
半关闭四种组合、同时关闭、各状态的 RST、挑战 ACK、PAWS、序号回绕、TIME_WAIT、MTU 下降后重传重新切分。

### 14.3 Linux 内核互通

netns + TUN，对端为 Linux 内核 TCP，配合 netem。GitHub CI 可以跑。

### 14.4 不变量（仿真每一步检查）

1. **发送决策约束**：每次发送都满足 §9.4 表中对应行的约束。
2. 已通告窗口的右边沿不回退。
3. 物理记账平衡：所有连接关闭、应用释放全部 chunk 之后，全局、peer、conn 三级记账都回到 0。
4. 额度不透支：任何时刻已预留总量 ≤ high。
5. 连接键隔离：两个 iface 上相同 4-tuple 的连接互不影响。
6. 字节流正确：应用读到的字节流与对端写入的逐字节一致。
7. 定时器与堆：连接释放后没有它的条目；**容器容量不随重设次数增长**。
8. 调度：任一连接在同一类集合中最多出现一次；出口 `Full` 期间，该 iface 的连接不进入 ready。

### 14.5 Fuzz 与变异验证

- Fuzz 目标：包解析、对已建立连接注入随机包序列、仿真中随机网络参数与随机应用读写节奏。S2 结束时连续 72 h 无崩溃。
- 关键防线都做变异验证：故意改坏实现，确认有测试会变红。重点是 §9.4 的发送约束、§6.3 的预留、§8.1 的去重。

## 15. 验收基准

矩阵定义见 0002 §2。在稳态吞吐矩阵之外，增加下列场景：

| 场景 | 回答的问题 |
|---|---|
| 大流下载 + 小请求/响应 | 满载时小请求 P99/P99.9 是否明显恶化 |
| 多 peer，其中一个开大量连接 | 多开连接是否挤压其他用户 |
| 多 iface，其中一个 sink 长期阻塞 | 是否跨端口拖慢，或者出现忙循环 |
| 1 万空闲 + 活跃数递增 | 成本是否随活跃数增长，而不是随总连接数 |
| 慢读、停读、应用持有 chunk 不释放 | 物理内存、窗口、额度归还是否正确 |
| 小 MSS、小包、碎片化写入、密集乱序 | 描述符与逐包 CPU 是否失控 |
| 双向同时满载 | ACK 与收发队列是否互相压制 |
| 限速突变、短时 CPU 停顿、带宽变化 | 是否积累信用、产生巨大突发、恢复缓慢 |

**验收门**：

| 门 | 内容 |
|---|---|
| G-correct | 一致性脚本全绿；fuzz 72 h 无崩溃；Linux 互通全绿 |
| G-perf | 每格 ≥ 改良版 smoltcp（按统计口径）；80 ms 浅瓶颈与上行单流 ≥ 内核的 80%（最终 90%） |
| G-lat | 大流满载时，小请求 P99 增量 ≤ 内核同场景增量的 1.5 倍 |
| G-fair | 两个 peer 分别开 1 条与 64 条连接，下行份额比在 0.7–1.3 之间 |
| G-progress | 任一阻塞场景（sink 阻塞、预算满、零窗口）解除后 1 s 内恢复推进；全程无忙循环（空转轮次计数为 0） |
| G-mem | 1 万空闲连接增量 ≤ 15 MiB；超预算压测不 OOM，已预留总量不超过 high |
| G-cpu | 同吞吐下整机 CPU / **有效 GB**（接收端应用收到的字节，不含重传）≤ 改良版 smoltcp |

**比较与统计口径**：
- 两组对照：**相同 CUBIC 配置**（隔离架构本身的收益）；**各实现的最优配置**（比较最终产品表现）。
- 每格至少 5 次重复，报告中位数与 bootstrap 95% 置信区间。只有置信区间的上界低于基线的 95% 时才判为回归；落在噪声范围内的差异不算收益，也不算退化。
- CPU 分别统计 worker、客户端、iperf 服务端和 netem，并单独报告 worker 进程。整机口径只用于和内核后端比较。
- 性能数据只在自有机器与真机台架上采集；GitHub CI 只跑正确性。

## 16. 分阶段计划

| 阶段 | 内容 | 继续条件 |
|---|---|---|
| **S0** | 见 0002：台架校准、基准矩阵、证伪实验、改良版 smoltcp | 按 0002 §5 决策表 |
| **S1** 数据面骨架 + 核心状态机 | **一开始就包含**：chunk 所有权与物理记账、发送记录结构、ready 集与各类阻塞集、按字节有界队列、预算接口（先用固定额度）、出口提交/归还事件；以及状态机、握手、RTO、WS/TS/PAWS；仿真器、一致性脚本、Linux 互通框架 | 零丢包下 Linux 互通全绿；不变量 1–8 在仿真中全程成立 |
| **S2** 可靠性 + CUBIC 闭环 | SACK 记分板、RACK-TLP、PRR、F-RTO、D-SACK；CUBIC + delivery rate 采样；两级 DRR；fuzz 上线；**在仿真与 netem 上跑完整代理闭环**，验证正确性、G-lat、G-fair、G-mem | 丢包各格 ≥ 改良版 smoltcp（CUBIC 对照组）；G-lat/G-fair/G-progress 通过；fuzz 72 h |
| **S3** pacing、BBRv3、Brutal、驱动 | 信用与量子分离的 EDT pacing、轮次预算；BBRv3（锁定版本 + 对照向量）；Brutal；驱动 A 与 B（B 含加密发包） | 80 ms 浅瓶颈 ≥ 内核 80%；驱动 B 以线上节奏和 CPU/有效 GB 验收 |
| **S4** 自调与规模 | 接收/发送自调、三级预算的动态部分、超售与进展保留、SYN cookie、TIME_WAIT 表、collapse | G-mem、G-cpu 通过；§15 全部场景通过 |
| **S5** zfc 接入 | PeerId、MTU、AdmissionPolicy、驱动 B 的 fd/Tunn 句柄接线；`ZFW_WG_USTACK` 切换；zfc-rig 加用例 | zfc-rig 全绿；真机 A/B 通过 G-perf |
| **S6** 灰度与替换 | 部分节点开启，soak ≥ 2 周 | 何时改默认由用户决定；smoltcp 路径保留一个版本后删除 |

每个阶段结束先交外部评审，再进入下一阶段。

**后续候选（不在本期）**：chunk 快路径穿透 `AnyStream`（跨仓）；UDP GSO/GRO（需要与 WG 报文边界和加密方式匹配）；限速下沉为 pacing 上限。

## 17. 风险

| 风险 | 缓解 |
|---|---|
| 可靠性长尾 | 测试体系先于性能优化；每个 bug 先补脚本再修 |
| 新增的状态（发送记录、BBRv3、跨线程通道、预算）成本抵消收益 | S1 起用 G-cpu 跟踪；CUBIC 对照组隔离架构收益 |
| 唤醒抖动在任何驱动下都存在 | 信用上限跟随实测间隔（§7.2），明确接受突发代价；驱动 B 备选 |
| 超售在极端情况下造成大量重传 | 进展保留 + 压力档；计数告警 |
| BBRv3 草案仍在演进 | 锁定版本 + 参考实现对照向量；CUBIC 保底 |
| 投入大（3–5 人月） | 分阶段交付，S5 之前对现网零影响 |

## 18. 已拍板决策

| # | 决策 | 日期 |
|---|---|---|
| 1 | 只做 zf-worker 的 WG 入站；zf-client-core 不在范围内 | 2026-09-24 |
| 2 | 上线默认拥塞控制为 BBR（实现为 BBRv3）；开发顺序上 CUBIC 先跑通闭环 | 2026-09-24 |
| 3 | 超限策略（§10.2）：策略拒绝回 RST；半连接满用 SYN cookie；其他资源超限静默丢弃 | 2026-09-24 |
| 4 | per-peer 配额默认值：全局 high 的 25% + 4096 连接 | 2026-09-24 |
| 5 | 孤儿超时 60 s，TIME_WAIT 60 s | 2026-09-24 |
| 6 | **暂不做用户级 QoS**：各 peer 等权，份额与连接数无关（§8.2）；不实现权重 | 2026-09-24 |
| 7 | 单流目标范围：1 Gbps、RTT ≤ 120 ms（§6.5）；更高 BDP 靠提高配置上限，不作为验收目标 | 2026-09-24 |

---

## 附录 A：v0.2 评审（GPT-6 Pro）处理表

所有条目先对照代码或规范核实，再处理。

| # | 评审意见 | 核实 | 处理 |
|---|---|---|---|
| 1 | pacing 的 `max_backlog = 2 quantum` 在 5 ms 唤醒间隔下把 200 Mbps 限到约 80 Mbps | 算术成立 | 采纳。三参数分离，信用上限跟随实测唤醒间隔（§7.1–7.2），并写明边界 |
| 2 | 驱动 B 只移走状态机，出包仍回 Tokio 任务加密 | 属实：`wg_instance.rs:509` 在 Tokio 任务里 `from_stack_rx.recv()` 后加密 + sendmmsg | 采纳。驱动 B 在 shard 线程做加密 + 发包；`Tunn` 在按 peer 的 std Mutex 里，可跨线程调用（§7.5） |
| 3 | `epoll_pwait2` 参数精度 ≠ 实际调度延迟；E2 探针不足以单独决策 | 成立 | 采纳。增加全链路时间线；驱动 B 以线上节奏 + CPU 验收（§7.5，0002 E2） |
| 4 | 缺就绪调度；EDT ≠ 公平；per-peer 内存上限不能阻止抢占调度 | 成立 | 采纳。调度状态表、两级 DRR、事件激活（§8.1–8.2） |
| 5 | peer 身份应由 zfc 传入 | 成立：zfc 解密时已知 peer | 采纳。入包带 `PeerId`（§3.1） |
| 6 | 每连接有界 ≠ shard 突发有界 | 算术成立 | 采纳。轮次预算（§8.4） |
| 7 | 一个 sink 阻塞不能阻塞其他端口 | 成立 | 采纳（§8.3） |
| 8 | 544 B/连接与 1 万连接 5 MiB 矛盾；`SmallVec` 内联占空间 | 成立 | 采纳。三指标拆分，目标改为 1 万连接 ≤ 15 MiB（S1 实测后再定）；乱序队列改 `Option<Box>`（§6.1） |
| 9 | 缺发送记录的结构与预算 | 成立 | 采纳（§9.3） |
| 10 | truesize 要跟随 allocation 生命周期 | 成立 | 采纳（§6.2） |
| 11 | 已通告窗口是资源承诺，需要明确选择 | 成立 | 采纳。选择超售 + 丢未确认数据 + 进展保留（§6.4） |
| 12 | high 必须分配前检查；默认值要考虑 cgroup | 成立 | 采纳。先预留后使用；默认值取 `min(cgroup memory.max, 物理内存)`（§6.3）。补充：入包 buffer 由 zfc 先分配，所以栈控制的是保留而不是分配 |
| 13 | `snd_target` 公式与 16 MiB 上限矛盾 | 算术成立 | 采纳。TX 拆三部分，预读按时间约束，单流目标写明（§6.5） |
| 14 | 接收自调的测量周期未定义 | 成立 | 采纳（§6.6） |
| 15 | 不变量 `in_flight ≤ cwnd` 与序号界限会把正确行为判错 | 成立（RFC 8985 TLP、RFC 6675 pipe） | 采纳。改为按发送决策分状态约束（§9.4） |
| 16 | RACK 数据结构未定义 | 成立 | 采纳（§9.3） |
| 17 | EgressSink 缺「已发送」合同；app-limited 判定要考虑出口 | 成立 | 采纳（§4.3） |
| 18 | AtomicWaker 丢唤醒、部分空间写不进 | 成立 | 采纳。检查-注册-再检查 + 低水位部分写入（§4.4） |
| 19 | netem `limit` 包含处于传播时延中的包 | 成立（netem 文档：limit 是 qdisc 能持有的包数） | 采纳。时延与瓶颈队列分离、关闭 offload、台架先校准（0002 §2.1、E0） |
| 20 | 零拷贝要穿透 `AnyStream` | 成立：现状 `poll_write` 拷贝进 PooledBuf | 部分采纳。给出拷贝次数表；第一期只做 AsyncRead/Write 路径（仍减少 1–2 次），chunk 快路径穿透 `AnyStream` 是跨仓改动，列为后续（§5） |
| 21 | 惰性删除的定时器条目会无界增长 | 成立 | 采纳。原位摘链 + 带索引的堆，无惰性条目（§7.4） |
| 22 | 压力档先丢乱序队列会造成 reneging | 成立（RFC 2018） | 采纳。reneging 改为最后手段（§6.7） |
| 23 | SYN cookie 不解决 accept 满与预算满 | 成立 | 采纳。最终 ACK 重新检查准入，失败则丢弃 ACK，之后可延迟建立（§10.2） |
| 24 | TIME_WAIT 漏了同时关闭；「时间戳足够安全」的说法过强 | 成立（RFC 9293、RFC 1337） | 采纳（§10.3） |
| 25 | `shutdown_write` / `close` 的语义与优先级不清 | 成立 | 采纳（§10.3） |
| 26 | MSS 钳制不能替代路径 MTU 变化处理 | 成立 | 采纳。`set_iface_mtu` + 重传按新 MSS 重新切分（§10.4） |
| 27 | BBRv3 要锁定版本；命名与草案不一致 | 属实：draft-06（2026-07-06）使用 `inflight_longterm/shortterm` | 采纳（§9.2） |
| 28 | foldhash 抗 DoS 能力有限 | 成立：键由租户控制 | 采纳。改为带密钥的 SipHash-1-3（§10.1） |
| 29 | 验收矩阵不足以证明多用户、低 CPU、低延迟 | 成立 | 采纳。新增场景与 G-lat/G-fair/G-progress（§15） |
| 30 | CPU 分母应为有效 payload；分开统计各组件 CPU；与改良版 smoltcp 比较；统计口径 | 成立 | 采纳（§15，0002） |
| 31 | S1 就建立数据面骨架；CUBIC 先跑通闭环，BBRv3 后做 | 合理，与「上线默认 BBR」不冲突 | 采纳（§16）。上线默认仍是 BBRv3 |
| 32 | UDP GSO/GRO 作为后续候选 | — | 记入后续候选（§16） |
