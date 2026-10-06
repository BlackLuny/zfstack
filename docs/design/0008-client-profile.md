# 代理客户端场景：client profile 与 TUN 快路径

状态：已实现，单机台架验收完成（2026-10-06）。基线 `e4119e2`（main）。实现分支 `claude/client-profile`。

本文把 zfstack 的使用范围从「zf-worker 的 WG 入站」扩展到「普通代理客户端的 TUN 入站」（0001 §0 原本明确排除了 zf-client-core）。目标：在客户端场景下吞吐、CPU、内存都优于 sing-box 1.15（当前最新的 `go` 栈），同时**服务器路径不回退**。客户端专属的行为全部由配置或新增 API 开启，默认值与原有接口语义不变。

## 1. 场景差异

| | 服务器（WG 入站，0001） | 代理客户端（TUN 入站） |
|---|---|---|
| 对端 | 远端用户的内核 TCP，经过 WAN | **本机内核 TCP**（同一台设备上的 App） |
| 链路 | 有丢包、有排队、RTT 10–200 ms | 无丢包、RTT 几十 µs；瓶颈在上游代理连接，不在这一跳 |
| MTU | 1420（WG 内层） | Linux 桌面 65535（sing-box 默认），Android 9000，iOS NE 4064，路由器常见 1500 |
| 设备卸载 | 无（包来自解密） | Linux TUN 可用 `IFF_VNET_HDR`：校验和卸载、TSO/GRO |
| 连接 | 很多用户、很多连接，公平与配额是核心 | 一个用户；浏览器大量短连接 + 少量大流 |
| 资源 | 服务器内存相对充裕，按用户限额 | 手机/路由器内存紧（iOS NE 进程 50 MB 上限），省电 |
| CPU | 多核、WG 加解密占大头 | 每字节 CPU 主要是拷贝、校验和、唤醒 |

因此客户端要优化的是**每字节的 CPU 与常驻内存**，拥塞控制、pacing、公平在这一跳上几乎不起作用。

## 2. 对手：sing-box 1.15（sing-tun `go` 栈）

sing-box 1.15.0-alpha.10（sing-tun `v0.9.7-0.20261002083955`）新增并默认使用自研的 `go` 用户态 TCP 栈（约 2.1 万行），读源码确认的要点：

- **TUN 卸载**：Linux 上 MTU < 49152 时开启 `IFF_VNET_HDR` + TSO/USO（GSO 双向）；默认 MTU 65535 时**不开**，此时 TX 校验和由 AVX2 汇编计算，RX 不校验（直接信任 TUN）。
- **splice**：直连出站的上游 socket 由引擎线程自己 epoll，上游数据直接读进发送缓冲，接收数据直接写给 socket，没有中继 goroutine 与中间缓冲。
- **单引擎线程**（可选多队列 TUN，每队列一个引擎）。
- 缓冲上限：接收 64 KiB 起步、最大 4 MiB；发送最大 2 MiB（iOS 档更小）。

同一台架上的 sing-box 基线（MTU 65535，单流）：下行 9.9 Gbit/s、0.77 CPU·s/GB；上行 10.5 Gbit/s、0.68 s/GB；常驻 37 MB，峰值 45–67 MB。gvisor 栈约慢 2–3 倍，`system` 栈（内核 TCP + NAT）介于两者之间。

## 3. 基线与瓶颈

在 zfbench 新增 client 模式（`bench/README.md`「Client mode」）：内核 TCP 客户端 → TUN → 代理进程 → 127.0.0.1 上的内核服务端；代理作为独立进程，按 `/proc/<pid>` 统计 CPU 与 RSS，zfstack 与 sing-box 口径相同，所有字节逐一校验。

zfstack 改动前（原 tokio 适配器 + 中继任务 + 默认配置）：

- MTU 65535 直接 **panic**：`clamp(2·MSS, max_quantum)` 在 MSS 65495 > 64 KiB 时 min > max。
- 修复后：下行 8.5 Gbit/s、1.45 s/GB；上行 6.7 Gbit/s、1.76 s/GB，上行峰值内存 84–103 MB。

剖析（perf，cpu-clock）给出的开销，按用户态拷贝路径列出：

| 方向 | 改动前 | sing-box go |
|---|---|---|
| 下行（上游 → App） | 内核 → 中继缓冲 → 适配器 TX 队列（**先 memset 64 KiB 再拷贝**）→ TX 块 → 软件校验和（标量，12 GB/s）→ TUN | 内核 → 发送缓冲 → AVX2 校验和 → TUN |
| 上行（App → 上游） | TUN → 内核算一次校验和 → **我们再验一次** → 拷贝进 8 KiB 块（每块 memset + 一次 lease）→ 适配器队列 → 中继缓冲 → 内核 | TUN → 接收链（1 次拷贝）→ 内核，不验校验和 |

此外：中继任务与驱动跨线程交接（mutex、唤醒）；sub-ms RTT 下 `prefetch = pacing_rate × 20 ms` 实际无上界（默认 `prefetch_max = u32::MAX`），splice 之后单连接 TX 峰值可达数百 MB。

## 4. 改动

每项注明是否影响服务器路径。

### 4.1 巨型 MSS（64 KiB TUN MTU）

`quantum_for`：量子上界取 `max(max_quantum, MSS)`，下界 `min(2·MSS, 上界)`；BBR `send_quantum` 同理。普通 MSS 下结果不变。

### 4.2 校验和

- **向量化求和**（`wire::sum_words`）：按本机字节序把 32 位字累加到 8 条 64 位通道（编译器向量化），最后折叠并交换字节序（RFC 1071 §2(B)）；x86_64 运行时检测 AVX2。基线 x86-64 从 12.3 提到 24.3 GB/s，AVX2 下 49 GB/s。与逐字参考实现的等价性有测试。**服务器也受益**。
- **RX**：`RxChecksum { Verify, Trusted }`，新增 `ingress_with` / `ingress_borrowed_with` / `ingress_buf`。`Trusted` 只跳过 TCP 校验和（IP 头仍校验），用于本机 TUN：vnet 头带 `NEEDS_CSUM`（校验和根本没算，只有伪首部和）或 `DATA_VALID`，或宿主像 sing-box 一样信任无卸载的本机 TUN。原 `ingress` / `ingress_borrowed` 语义不变。
- **TX**：`Shard::set_iface_tx_checksum_offload`：数据段以部分校验和发出（字段为伪首部折叠和，与 Linux `CHECKSUM_PARTIAL` 一致），`OutPacket::csum_partial` 告知宿主；控制段和无状态回复仍是完整校验和。
- **TSO**：`Shard::set_iface_tso(max)`：新数据按 MSS 整数倍组成最多约 64 KiB 的超级段，`OutPacket::gso_size = MSS`，由设备切分；cwnd 不够整段时缩到 cwnd 允许的 MSS 整数倍；重传、TLP 仍按单个 MSS。发送记录按超级段一条，记下段大小（`Rec::seg`）。设备切分后若中间某段丢失，对端的 SACK 边界会落在超级段内部：SACK 处理在处理前为每个块预留两条记录的余量，然后**只在段网格上**切开这条记录（每段至多一条记录，与逐段发送时一样，对端无法借此把记分板切得更碎），只重传缺的那一段。回归测试 `lost_segment_inside_a_tso_super_segment_is_recovered_by_sack` 让设备切分后分别丢首段、中段、尾段：修复前首段丢失要 TLP + 重传 2 段，中段丢失重传超级段剩余的 5 段；修复后三种都只做一次快速恢复、重传 1 段，没有 TLP/RTO。余量申请失败时退回原行为（块对这条记录不生效）。开启 TSO 隐含 TX 校验和卸载。
- `offload::VirtioNetHdr`：10 字节 `virtio_net_hdr` 的编解码，`rx_checksum()` 与 `for_packet()`（含 GSO 类型/大小/头长）。

开了 `TUN_F_CSUM` 之后，本机内核在写入 TUN 时不再计算校验和、从 TUN 收包时也不再校验，我们也不算——这一跳上**两端都不再扫描负载**。sing-box 在默认 MTU 65535 下不开 vnet 头，这是我们相对它的结构性优势。

`OutPacket` 新增两个字段（`csum_partial`、`gso_size`）。只有用结构体字面量构造 `OutPacket` 的调用方需要补字段（库内部构造；宿主只读）。

### 4.3 去掉零填充分配

- 适配器 TX 队列块原来是 `vec![0; 64 KiB]` 再拷贝，改为 `Vec::with_capacity` + `extend_from_slice`；RX 计费块同理。容量（计费依据）不变。
- RX 计费块对大于 8 KiB 的负载按负载大小分配一块（上限 64 KiB），不再切成 8 个 8 KiB 块和 8 个 lease。小负载（服务器 MTU 1420 的常态）走原路径，不变。

**服务器也受益**（适配器 TX 路径少一次 memset）。

### 4.4 零拷贝写：`write_with`

`Shard::write_with(id, max, |[a, b]| -> Result<usize, E>)`：调用方直接往发送缓冲尾部写，最多两段（最后一块的剩余部分 + 一块新块），按 `readv` 顺序填充。空间、份额、预算规则与 `write` 完全相同（抽出 `Conn::admit_write` 共用）；没写进去的新块立即还给池；`fill` 出错时什么都不保留。中继可以把上游 socket 直接 `readv` 进 TX 块；加密出站也可以直接解密进 TX 块。

### 4.5 零拷贝收：`PacketPool` + `ingress_buf`

0004 §2 规定：owned 入包若不能核验 backing 容量，必须保守复制。新增 `pktpool::PacketPool`：宿主从池里拿 `PacketBuf`（**先按 `buf_size` 向预算申请 lease 再分配**；分配器实际给出的容量若超过 lease 则拒绝，与其他计费容器规则一致；缓存中的块也一直计费），把 TUN 包直接读进去，交给 `Shard::ingress_buf(buf, start, csum)`：

- 有序负载 ≥ 缓冲容量一半：以缓冲切片直接挂进 RX 队列（`IngressPayload::Leased` → `RxQueue::push_leased`，只预留描述符槽），不拷贝；最后一个切片释放时缓冲回池。
- 其余（ACK、小包、乱序段）：照旧复制进计费块，缓冲立即回池。

物理计费覆盖整块：被钉住的缓冲按整块容量计费，最坏放大 2 倍（负载恰好半块）。池有上限（`max_cached`），`in_use()` 可用于诊断被应用未读数据钉住的缓冲数。

### 4.6 驱动内中继：`TcpStream::splice`

`TcpStream::splice(upstream) -> Splice`：把流和上游 socket 一起交给驱动，驱动自己搬字节：

- 上游 → 栈：`write_with` + `try_read_vectored`，直接读进 TX 块。
- 栈 → 上游：`read_chunk_for_adapter` 取零拷贝块，`try_write_vectored` 批量写；只有 socket 真正接收的字节才 `consume_adapter`（窗口才重新打开），所以中继侧的积压受接收窗口约束。
- 两边各自的 FIN 作为半关闭转发；任一侧 RST/错误则重置另一侧；两个方向都结束后走 `shard.close`（剩余数据与 FIN 照常发出）。
- 核心连接**有序关闭**（`Closed(Normal)`，例如对端先 FIN、上游后 FIN 走到 LAST_ACK）时，已收到但上游尚未接收的字节仍然可读：中继继续把它们写给上游，写完再关闭上游写端，不提前结束。回归测试 `splice_delivers_upload_tail_after_the_connection_closes` 用 4 KiB 的上游 socket 缓冲构造该时序：修复前上游只收到 10 KiB/200 KiB。
- 交接前 App 已经写入句柄的字节先发，句柄里已收到的块先写给上游，顺序不变。
- 上游 socket 在交接时**重新注册到驱动所在 runtime 的 reactor**：宿主若把驱动放在独立的 current-thread runtime，所有中继 I/O 都在这一个线程上完成，没有跨线程唤醒。

这是 sing-box splice 的对应物，但适用于任何 tokio `TcpStream` 出站（直连）。普通流（AsyncRead/AsyncWrite）不受影响。

### 4.7 驱动调度

- **入口优先**：`service()` 每一轮（第一轮除外）先非阻塞地收已排队的入包（宿主 source 与 `StackHandle::ingress`，每次最多 16 项），再继续生产出口。原来一次 `service()` 最多 16 轮出口，CPU 打满时 ACK 和其他连接的请求积压在设备队列里：大流下 1 KiB 往返 P99 从 30–100 ms 降到约 7 ms（与 sing-box 相当）。**服务器路径同样适用**：MTU 1420 的复制中继路径上 RR P99 8.4 → 5.6 ms，吞吐不变（§7）。
- 尝试过的反例：给 splice 每次 pump 加字节预算并重新入队，延迟反而更差（驱动永远有活，入包更难被处理），已撤回。

### 4.8 `StackConfig::client()`

| 参数 | 默认 | client | 理由 |
|---|---|---|---|
| `pacing` | true | false | 本机一跳没有瓶颈队列；sub-ms RTT 下 pacing 只增加定时器 |
| `max_snd_inflight` | 16 MiB | 2 MiB | 这一跳的 BDP 只有几十 KB；缓冲只需覆盖宿主调度抖动（10 Gbit/s × 几 ms） |
| `prefetch_max` | 无上限 | 512 KiB | RTT 50 µs 时 `rate × 20 ms` 实际无上界（§3） |
| `max_rcv_buf` / `init_rcv_wnd` | 16 MiB / 64 KiB | 4 MiB / 256 KiB | 与 sing-box 同档；64 KiB 窗口在 64 KiB MSS 下只有一个段 |
| `delayed_ack` | 40 ms | 5 ms | 本机 App 开着 Nagle 时会等我们的 ACK |
| `time_wait` | 60 s | 15 s | 对端是本机内核，会换新的临时端口；墓碑只占内存 |
| backlog | 4096 | 1024 | 单用户 |

## 5. 宿主接入建议

1. **Linux**：MTU 65535；TUN 打开 `IFF_VNET_HDR` 并 `TUNSETOFFLOAD(TUN_F_CSUM)`；读到的包用 `VirtioNetHdr::rx_checksum()` 作为 `RxChecksum`；打开 `set_iface_tx_checksum_offload`；写包前加 `VirtioNetHdr::for_packet(pkt)`。MTU 必须较小时（如路由器 1500）再加 `TUN_F_TSO4|TUN_F_TSO6` 与 `set_iface_tso(65000)`，读缓冲按 64 KiB 分配。
2. **Android / iOS**（VpnService fd / NEPacketTunnelFlow，无 vnet 头）：`RxChecksum::Trusted`（与 sing-box 一致），TX 仍算完整校验和（已向量化）；`PacketPool` 缓冲按 MTU 分配。
3. 从 `PacketPool` 读包、`ingress_buf` 入栈；`max_cached` 约 64–128（64 KiB 缓冲时 4–8 MiB 计费上限）。
4. **驱动放在独立的 current-thread runtime**（专用线程），应用其余部分可以继续用多线程 runtime；直连出站用 `splice`。实测共享 4 worker runtime 时，上游 socket 的就绪事件会唤醒停在 `epoll_wait` 的其他 worker，8 流下行每 GB 多 15% CPU。
5. 设备队列不要设得过深（台架用 txqueuelen 500，与 sing-box 一致）：驱动 CPU 打满时，深队列只会把入包延迟放大。

## 6. 结果

台架：单机 4 vCPU VM（Linux 6.18），客户端、服务端、代理同机；每格 3 次交替运行取中位数，每次 10 s（丢弃前 3 s），所有字节逐一校验，0 失败。原始 JSON 与汇总：`bench/results/2026-10-06-client/`，复现脚本 `bench/client_matrix.py`。sing-box 为 1.15.0-alpha.10（`with_gvisor`），各栈用其默认设置。

zfstack 配置（§5 推荐接法）：`StackConfig::client()` + `splice` + vnet 头校验和卸载 + 驱动独立 current-thread runtime；`zfstack-mt` 为驱动与应用共用 4 worker runtime。「代理 CPU」按代理进程计；「整机」为全机 CPU（含客户端、服务端、内核）。注意 sing-box 是完整产品进程（Go runtime + 全部协议），**空闲常驻 37 MB 不可直接比**，峰值减空闲的增量与每连接内存更可比（zfstack 空闲 5.3 MB）。

### 6.1 MTU 65535（Linux 桌面默认）

| | 吞吐 Gbit/s | 代理 CPU s/GB | 整机 s/GB | 峰值 RSS MB |
|---|---|---|---|---|
| 下行 1 流 zfstack | **15.4** | **0.54** | **1.08** | **8.0** |
| 下行 1 流 zfstack-mt | 12.8 | 0.68 | 1.29 | 13.0 |
| 下行 1 流 sing-box go | 9.3 | 0.78 | 1.64 | 46.8 |
| 下行 1 流 sing-box gvisor / system | 3.8 / 8.8 | 3.12 / 1.17 | 3.58 / 1.94 | 43.1 / 37.8 |
| 上行 1 流 zfstack | **14.8** | 0.70 | **1.34** | **9.7** |
| 上行 1 流 sing-box go | 11.6 | **0.68** | 1.43 | 44.2 |
| 上行 1 流 sing-box gvisor / system | 5.4 / 12.4 | 2.30 / 0.96 | 2.70 / 1.66 | 44.6 / 37.8 |
| 下行 8 流 zfstack | 9.0 | 0.94 | **1.42** | **26.2** |
| 下行 8 流 zfstack-mt | 8.2 | 1.08 | 1.58 | 52.2 |
| 下行 8 流 sing-box go | **9.3** | **0.85** | 1.53 | 67.5 |
| 下行 8 流 sing-box gvisor / system | 9.4 / 7.4 | 1.98 / 1.38 | 2.55 / 2.10 | 51.2 / 38.1 |
| 上行 8 流 zfstack | **15.9** | **0.63** | **1.20** | **15.9** |
| 上行 8 流 sing-box go | 9.1 | 0.88 | 1.53 | 43.3 |
| 上行 8 流 sing-box gvisor / system | 13.7 / 10.4 | 1.36 / 1.35 | 1.85 / 1.85 | 52.4 / 38.1 |

单 worker（zfstack 1 个 tokio worker / sing-box `GOMAXPROCS=1`）：下行 14.5 vs 10.3 Gbit/s（0.57 vs 0.77 s/GB），上行 14.1 vs 9.9（0.73 vs 0.85）。

### 6.2 较小 MTU（Linux 上 sing-box 在 MTU < 49152 时自动开 GSO）

| MTU | 方向 | zfstack TSO（vnet + TSO 双向） | zfstack 无卸载 | sing-box go（GSO） |
|---|---|---|---|---|
| 9000 | 下行 | **12.0** Gbit/s, **0.68** s/GB | 7.9, 1.09 | 9.5, 0.71 |
| 9000 | 上行 | **11.9**, 0.81 | 11.6, 0.93 | 11.4, **0.67** |
| 4064 | 下行 | **12.1**, **0.69** | 5.2, 1.62 | 8.9, 0.78 |
| 4064 | 上行 | **11.9**, 0.80 | 9.8, 1.18 | 11.4, **0.67** |
| 1500 | 下行 | **12.9**, **0.64** | 3.0, 2.87 | 9.2, 0.86 |
| 1500 | 上行 | **11.7**, 0.80 | 6.6, 2.02 | 11.5, **0.66** |

小 MTU 下行开 TSO 后吞吐和 CPU 都领先；上行吞吐持平，但每 GB CPU 高约 20%（未剖析，可能是按超级段收包时 ACK 节奏与 sing-box 不同，见 §8）。「无卸载」一列对应 Android/iOS（没有 vnet 头），按包开销明显，Linux 台架上没有同条件的 sing-box 对照（它在这些 MTU 下总是开 GSO），需在真机上比较。

### 6.3 建连、空闲连接、负载下延迟

| | zfstack | sing-box go | sing-box system |
|---|---|---|---|
| 建连 4000 次（并发 64）：成功 / 速率 | 4000 / **5086** 次/s | 4000 / 4810 | 4000 / 4325 |
| 建连总延迟 P50 / P99 ms | **10.9 / 34.0** | 11.8 / 37.3 | 12.4 / 58.2 |
| 建连期间代理 CPU s / 峰值 RSS MB | **0.54 / 8.8** | 0.73 / 69.5 | 1.04 / 45.6 |
| 空闲连接内存（4000 条） | **4.7 KB/条** | 9.3 KB/条 | 13.5 KB/条 |
| 4000 条空闲连接时总 RSS | **23.3 MB** | 72.7 MB | 88.7 MB |

大流下 1 KiB 往返（每 100 ms 一次）：

| 运行 | 同时大流 | RR P50 | RR P99 |
|---|---|---|---|
| zfstack（独立驱动线程，最终矩阵） | 15.5 Gbit/s | **1.76 ms** | 10.0 ms |
| sing-box go（同一矩阵） | 9.9 | 2.77 | **5.1** |
| zfstack-mt（§4.7 修复后单独一轮） | 13.2 | **0.97** | 6.8 |
| sing-box go（同一轮） | 9.9 | 2.72 | 6.3 |

P50 我们更低；P99 两轮结果一前一后，每次运行只有约 70 个负载下样本，P99 基本由 1–2 个样本决定，不能下「更好」的结论。大流本身快 35–55%。

## 7. 服务器路径不回退

两套台架，都是 main（`e4119e2`）与本分支交替运行。

**sans-IO 栈（zfbench 模拟器模式，服务端单线程，与 0003 相同口径）**，每格 3 次中位数：

| 格 | main Mbit/s, 栈线程 s/GB | 本分支 |
|---|---|---|
| CUBIC 下行 12 ms 200 Mbit/s | 192.7, 6.33 | 192.7, 6.34 |
| CUBIC 下行 12 ms 1% 丢包 8 流 | 83.4, 8.21 | 83.6, 8.17 |
| CUBIC 上行 12 ms 200 Mbit/s | 188.0, 11.54 | 187.9, 11.64 |
| BBR 下行 80 ms 0.25 BDP | 170.0, 8.70 | 173.4, 8.51 |
| CUBIC 下行不限速 2 ms（CPU 受限） | 2463, 1.48 | **2644, 1.43** |
| CUBIC 上行不限速 2 ms（CPU 受限） | 4340, 1.23 | **4434, 1.18** |

**tokio 适配器（zfc 的生产路径）**：同一台架的 `--legacy` 代理只用本次之前已有的 API（owned 入包、完整校验和、复制中继、`StackConfig::default()`），MTU 1420，分别链接 main 与本分支的库：

| 格 | main Gbit/s, 代理 s/GB | 本分支 |
|---|---|---|
| 下行 1 流 | 2.39, 3.99 | 2.50, 3.86 |
| 上行 1 流（8 次） | 3.09, 4.01 | 3.16, 3.92 |
| 下行 8 流 | 2.27, 4.40 | 2.42, 4.29 |
| 上行 8 流 | 3.25, 4.10 | 3.42, 4.06 |
| 大流下 RR P50 / P99 ms | 2.47 / 8.39 | 1.68 / 5.61 |

（上行 1 流第一轮 3 次中位数为 3.49 vs 3.18，区间重叠；加到 8 次后差异消失，如上表。）

结论：各格都在噪声范围内或更好；CPU 受限格因向量化校验和与去掉零填充而改善。默认配置、默认 API 的行为不变；新增字段只影响以结构体字面量构造 `OutPacket` 的代码。全部 151 个库测试、zfbench 单测、`--no-default-features` 构建、clippy（相对 main 无新增警告）、`cargo fmt --check` 通过。原始输出：`bench/results/2026-10-06-client/server-ab/`。

## 8. 已知限制与后续

- 小 MTU + TSO 时上行每 GB CPU 比 sing-box 高约 20%（§6.2），未剖析。
- 无卸载的小 MTU（移动端形态）按包开销大：每段一次 TUN 写系统调用、每包一次入栈；需要在真机上与 sing-box 对比，并考虑批量化。
- 多流下行时驱动线程是瓶颈（单线程引擎，与 sing-box 相同）；此时我们用户态只占驱动线程的约 11%，其余是内核（拷贝、唤醒、本机 TCP 软中断）。按进程计的 CPU/GB 仍比 sing-box 高约 10%，但整机 CPU/GB 更低（§6）。多队列 TUN + 多 shard 是下一步（0001 §3.1 的结构已经预留）。
- TUN 读线程与驱动之间仍有一次 channel 交接；驱动内直接读 TUN（AsyncFd）可以去掉读线程的唤醒（约占代理 CPU 的 3–5%）。
- UDP（QUIC/DNS）不在本栈范围，仍由宿主分流。
- 移动端只在 Linux 上按 MTU 9000/4064 模拟；真机（Android/iOS）的功耗与吞吐需要另行验证。
- `sim::tests::fuzz_without_rst` 在 tokio feature 构建下偶发长时间运行（伪造包导致的失步走 150 s 用户超时）：main 上同样出现（35 次中 3 次 vs 本分支 6 次，无统计差异），属 0003 §6 第 8 项已知的仿真不确定性，未在本次处理。
