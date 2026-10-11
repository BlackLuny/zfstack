# zfstack 最新性能对照与帕累托实验（2026-10-10）

日期：2026-10-10 UTC。基线：`56aed4ffd92f628d58a8ad773c52baf685fab79e`（main，含 #16/#19）。
候选：`c739e889f0a90427c438a1293d7c61e4229b8c60`。[PR #20](https://github.com/BlackLuny/zfstack/pull/20)。

## 1. 结论

对照同类栈，**服务端** zfstack 在已测格上已对齐或超过 Linux 内核 TCP 与 smoltcp fork（浅队列与 WAN 单流是结构性赢面）；**客户端 TUN** 相对 sing-box 1.15 `go` 栈，在桌面 MTU 65535 单流吞吐、内存、建连上领先，剩余差距集中在 8 流代理 CPU、小 MTU 上行、无卸载移动形态。这些大差距多数不在本仓库一个局部 patch 里。

本轮在 #16/#19 之后继续做**真正帕累托**的热点：不增加常驻字段、不增加用户态拷贝、不改公共 API / 校验 / DRR / 拥塞控制。固定工作量 A/B（fat LTO、`codegen-units=1`、绑核 AB/BA、6 对）测到：

- 临时预算分配 **−8.83%** CPU ns/op（95% 描述区间 **[−11.43%, −8.49%]**）
- 无缓存 TX 块分配 **−9.84%** s/GB（**[−14.02%, −9.33%]**）
- 短连接泵 `short_64x4096` **−25.29%** s/GB（**[−29.01%, −18.39%]**），进程峰值 RSS **11.25 → 8.07 MiB**（计费峰值不变）
- owned 入站泵 `up_8_owned_control` **−6.40%**（**[−8.64%, −3.09%]**）
- 保留 handle 对照 `budget_handle_control` **+1.42%**，区间跨 0（#19 路径未改）

单流 paced 泵 `down_1_paced` 区间跨 0，**不宣称收益**。本 VM 不暴露硬件 PMU，没有退休指令数交叉验证；所有泵场景的包数 / 有效字节 / 校验和在成对记录里完全一致。

这不是全负载严格帕累托支配证明，也不是 WAN/TUN 实网吞吐。

## 2. 最新性能对照（仓库内已有台架，非本作业重跑）

### 2.1 服务端：内核 TCP / smoltcp fork

证据：`docs/design/0003-implementation-and-benchmark.md`，`bench/results/2026-09-24-full2/`，WAN A/B §5.9–5.10。

| 场景 | kernel TCP | smoltcp fork | zfstack |
|---|---:|---:|---:|
| 模拟器下行 12 ms / 0% / 2 BDP / 1 流 | 193 Mbps | cubic 171 | cubic **193** |
| 模拟器 80 ms / 0% / 0.25 BDP / 1 流 | 159 | cubic **10.4** | cubic **182**（约 1.14× 内核） |
| WAN 下行单流（~456 Mbps 封顶） | 434–447 | **190–192** | **452–457**（约 2.4× smoltcp） |
| WAN 建连 2000@64 | 2000 | ~838–864 成功 | **2000** |
| WAN mixed RR P99 | cubic ~15 ms | ~14–19 | cubic **15.4**；bbr **38.6**（同路径内核 bbr 240） |
| 栈线程 s/GB（WAN 下行 1/8 流） | — | 10.5 / 9.9 | cubic 9.0 / 8.4；bbr **7.0 / 8.5** |

验收门 G-perf 在已测浅瓶颈与上行格上成立。smoltcp-bbr 仍在个别高 RTT 深队列 1% 丢包格领先（80 ms/1%/2 BDP/1 流 98.6 vs zfstack-bbr 77.7，离散很大）。

### 2.2 客户端 TUN：sing-box 1.15 / gvisor / system

证据：`docs/design/0008-client-profile.md`，`bench/results/2026-10-06-client/`。

| 场景 | zfstack | sing-box go | gvisor | system（内核 NAT） |
|---|---:|---:|---:|---:|
| 下行 1 流 Gbit/s / 代理 s/GB / RSS MB | **15.4 / 0.54 / 8.0** | 9.3 / 0.78 / 46.8 | 3.8 / 3.12 / 43 | 8.8 / 1.17 / 38 |
| 上行 1 流 | **14.8 / 0.70 / 9.7** | 11.6 / **0.68** / 44 | 5.4 / 2.30 | 12.4 / 0.96 |
| 下行 8 流 | 9.0 / **0.94** / 26 | **9.3 / 0.85** / 68 | 9.4 / 1.98 | 7.4 / 1.38 |
| 建连 4000@64 cps / P99 / 空闲 B/连接 | **5086 / 34 ms / 4.7 KB** | 4810 / 37 / 9.3 KB | — | 4325 / 58 / 13.5 KB |

### 2.3 上一轮局部 CPU（#16/#19）

`bench/results/2026-10-08-pareto/`：预算临时所有权 + RX 首 chunk 内联 + 等 deadline 早退。确认试验短连接 **−3.11%**、单流 paced **−3.45%**；等键 heap 有收益，纯改期 heap **+2.56%**。DRR 单元素内联因未计费驻留被拒绝。

## 3. 差距与帕累托分类

| 差距 | 量级 | 能否在本仓库局部帕累托 |
|---|---|---|
| WAN 默认 CC：BBR 尾延迟 vs CUBIC | RR P99 38.6 vs 15.4 ms；重传 2.7–4.6% | 产品策略；算法对照向量是另一项 |
| 客户端 8 流代理 CPU | 约 +11% vs go | 多队列 TUN / 多 shard，结构已预留，不是这次热点 patch |
| 小 MTU + TSO 上行 s/GB | 约 +20% vs go | 需剖析 ACK 节奏，本轮未做 |
| 无卸载 1500 MTU | 下行 3.0 Gbit/s @ 2.87 s/GB | 批量化 / 真机；改变 syscall 形态 |
| TUN 读线程交接 | 约 3–5% 代理 CPU | 驱动内 AsyncFd，调度形态变化 |
| zfc Driver B（同线程加密发包） | 唤醒迟到 | **zfc**，不在本库 |
| chunk 快路径穿透 AnyStream | 再减一次拷贝 | 跨仓 |
| 预算 / TX memset / ingress Arc / heap pop | 每包 Arc 与冷分配 | **本轮** |

明确不做：DRR 内联、换 SipHash、关校验、改 offload 默认、扩大未计费 HashMap value。

## 4. 本轮改动

| 改动 | 机制 | 代价 |
|---|---|---|
| `Budget::try_allocate_kind` 就地预留 | 成功路径只 clone lease 需要的 3 个 Arc，失败不构造带 `stats` 的临时 `MemoryHandle` | `MemoryHandle::try_allocate_level` 仍走 #19 的 inline 拒绝路径 |
| owned/leased ingress 去掉整包 `Bytes::clone` | parse 用借用，再对同一 `Bytes` 切 payload | 公共 API 不变 |
| leased 入队按值交给 `push_leased` | 去掉 `v.clone()` | OOO 的 leased 路径仍按 `[u8]` 复制进 charged 存储（0004） |
| TX 块 `Box::new_uninit_slice` | 只读已写入区间；短写不再给 64 KiB 块的未用页做 memset/缺页 | `write_with` 的 fill 仍须写入提供的缓冲，与 spare `Vec` 相同 |
| `IndexedHeap::pop_due` 只 `down(0)` | 根弹出不必再 `remove` 的 `up` | `set` 等键路径未改；同模块布局可能使 `heap_equal` 出现约 2% 的代码布局差 |

## 5. 测量方法

- 机器：KVM，4 vCPU Intel Xeon，Linux 6.12.94+，15 GiB。无硬件 PMU（`perf` 的 `instructions`/`cycles` 均为 `<not supported>`）。
- Rust 1.94.1；`CARGO_PROFILE_RELEASE_LTO=fat`、`codegen-units=1`、`panic=abort`、`CARGO_INCREMENTAL=0`。probe 二进制 rustc 行已核对含 `-C lto=fat` / `-C linker-plugin-lto`。
- 两侧同一份 `pareto_probe.rs` 与 `cases.json`；库代码来自 main vs 本分支。绑核 CPU 2，warmup 1 对 + 正式 6 对，顺序 AB/BA。
- 主要指标：进程 CPU（`CLOCK_PROCESS_CPUTIME_ID`）/ 有效字节或 ns/op。变化先逐对再取中位数；20,000 次 paired bootstrap，seed 20261010。
- 分析器：252 条记录，warmup 36 忽略，拒绝 0，工作量字段成对相等。

基线二进制 SHA256：`ee13fcfe28d1695602880921e2caa70b55bd464188e4475e8f89a6d278e2f46a`。
候选：`653648d2719d44dcc885569a1176bc3397700882a160e8cd55014381e27f65e9`。

## 6. 结果（6 对）

CPU 负值更好。区间跨 0 的格不宣称收益。`heap_equal` 的工作循环不含 `pop_due`，其 −2.56% 视为同模块布局上界，不归因于本轮逻辑。

### 6.1 微基准

| 场景 | CPU 基线 → 候选 | 配对 CPU 变化 | 95% 描述区间 |
|---|---:|---:|---:|
| `budget_1400_p1` | 100.27 → 91.43 ns/op | **−8.83%** | [−11.43, −8.49] |
| `budget_handle_control_p1` | 84.37 → 85.21 ns/op | +1.42% | [−1.18, +2.04] |
| `rx_64_p1` | 2.621 → 2.482 s/GB | **−5.52%** | [−6.86, −4.48] |
| `rx_1400_p8` | 0.261 → 0.254 s/GB | −2.50% | [−5.98, +0.86] |
| `rx_65535_p1` | 0.161 → 0.160 s/GB | −0.44% | [−2.95, +2.83] |
| `rx_tail_1400` | 0.312 → 0.302 s/GB | **−3.11%** | [−7.87, −2.47] |
| `tx_alloc_64k` | 0.181 → 0.163 s/GB | **−9.84%** | [−14.02, −9.33] |
| `heap_equal_4096` | 2.991 → 2.918 ns/op | −2.56% | [−3.91, −1.21]（布局，见上） |
| `heap_mixed_4096` | 3.858 → 3.810 ns/op | −0.93% | [−2.11, +4.90] |
| `heap_pop_4096` | 235.1 → 217.8 µs/op | **−7.62%** | [−13.79, −5.64] |

### 6.2 两端 Shard 的 packet pump

| 场景 | CPU 基线 → 候选 | 配对 CPU 变化 | 95% 描述区间 | 配对 wall 处理速率 |
|---|---:|---:|---:|---:|
| `down_1_paced` | 0.969 → 0.964 s/GB | −0.15% | [−2.58, +1.58] | +0.15% |
| `up_1_paced` | 0.961 → 0.913 s/GB | −3.15% | [−5.88, +2.75] | +3.26% |
| `up_8_peers_paced` | 0.922 → 0.921 s/GB | −3.04% | [−6.58, +6.48] | +3.16% |
| `bidi_8_one_peer` | 0.981 → 0.928 s/GB | **−3.91%** | [−9.12, −0.18] | +4.07% |
| `up_64_peers_paced` | 1.116 → 1.088 s/GB | **−2.59%** | [−4.03, −1.36] | +2.63% |
| `up_8_owned_control` | 1.052 → 0.985 s/GB | **−6.40%** | [−8.64, −3.09] | +6.85% |
| `down_8_unpaced` | 1.104 → 1.043 s/GB | **−5.54%** | [−7.63, −2.81] | +5.88% |
| `short_64x4096` | 2.619 → 1.956 s/GB | **−25.29%** | [−29.01, −18.39] | +33.91% |

`short_64x4096`：计费峰值 8640 KiB 两侧相同；进程生命周期 VmHWM 中位数 11.25 → 8.07 MiB（配对约 −28%）。原因是 4 KiB 短写分配 64 KiB TX 块后不再把未写入页 memset/缺页；账本仍按整块计费。

`up_64_peers_paced` 的 −2.59% 与 `heap_equal` 布局量级接近，单独看不够作为强归因。

## 7. 功能回归

`cargo test -p zfstack --release --features tokio`（Rust 1.94.1）：167 passed、3 ignored（原有 BBR/soak 诊断项）。`cargo build -p zfstack --no-default-features` 通过。Clippy 相对 main 无新增正确性错误。新增测试：`new_full_tx_block_returns_written_bytes_without_zero_fill`、`pop_due_removes_root_without_up_and_keeps_heap_order`。

## 8. 证据

- [evidence.tar.gz](evidence.tar.gz)：原始 JSONL、metadata、环境、lockfile、补丁、分析器输出。
- [results.json](results.json)、[summary.txt](summary.txt)、[changes.patch](changes.patch)。
- 复现：[`bench/pareto/README.md`](../../pareto/README.md)，基线改为 `56aed4f`，候选为本分支；`cases.json` 含新增 `tx-alloc` / `heap-pop`。

```bash
PARETO_REVIEW_DIR="$(mktemp -d /tmp/zfstack-pareto-review.XXXXXX)"
tar -xzf bench/results/2026-10-10-pareto/evidence.tar.gz -C "$PARETO_REVIEW_DIR"
python3 bench/pareto/analyze.py "$PARETO_REVIEW_DIR/results" \
  --cases "$PARETO_REVIEW_DIR/manifest/cases.json" \
  --expected-pairs 6 --bootstrap 20000 --seed 20261010
```

## 9. 下一步（仍未关闭的高杠杆差距）

1. 有 PMU 的机器上用退休指令数复核本轮，尤其 `short_64x4096` 与 `heap_equal` 布局。
2. 剖析小 MTU + TSO 上行相对 sing-box 的 +20% s/GB。
3. 客户端多队列 TUN + 多 shard，对准 8 流代理 CPU。
4. 默认 CC：干净浅缓冲路径用 CUBIC，随机丢包长 RTT 再核 BBR。
5. zfc Driver B 与跨仓 chunk 快路径：不在本库。
