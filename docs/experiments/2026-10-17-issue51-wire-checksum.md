# Issue #51：wire 校验和与 IPv4/TCP 头生成优化

## 代码、范围与结论

- 基线：`9f424bfa1526e3f77636299f67917e5edb1f286d`；被测生产代码：`995d526f6ec64c295e755d1ac30533eb16854399`。后续报告修订不改变生产代码。
- 生产改动仅限 [`src/wire.rs`](../../src/wire.rs)：短头累加、AVX2 分块累加、portable 树状归约、固定 IPv4/TCP 头校验和在寄存器中累加。
- **函数级加速证据明确；完整协议栈与 TUN 的收益较小且多数区间跨零。现有数据未检出稳定的整体回归，但不足以排除性能或尾延迟退化，也不构成严格帕累托/非劣效证明。**
- 原始观测不变，见[证据目录](./2026-10-17-issue51-wire-checksum/README.md)。本报告于 2026-10-18 修正统计口径、表格一致性和结论强度。

## 实现与正确性边界

1. `sum_bytes` 先处理尾字节，大块在尾调用位置进入独立 `sum_words`；短切片按 32/16/8/4 字节累加，减少短头入口开销。
2. 默认 x86_64 在运行时检测 AVX2，支持时使用 256B 展开；每 262,144B 分块折叠。块内归约的每个 u32 通道至多累加 32,768 个 u16（上界 `0x7fff8000`），两半相加不会跨低 32 位进位。指针加载限于已确认的切片范围。
3. portable 路径使用 8 个 u64 通道与树状归约；x86_64 无 AVX2 和 AArch64 不依赖新的最低 CPU 能力。
4. `emit` 同步写字段并累加校验和，避免写完头部再读回；保留软件校验与 CHECKSUM_PARTIAL 两种语义。现有选项编码的各组长度均为 4 的倍数，`debug_assert_eq!(i, tcp_hlen)` 验证填充不变量。

新增测试包括独立逐字节校验和参考实现、分块阈值前后/600,000B、未对齐切片、全零/全一/随机内容、IPv4 IHL 20..60 与逐位损坏检查，以及 IPv4/IPv6、多切片载荷和 partial checksum 完成检查。

选项枚举是 80 种输入组合中的 **62 种合法 TCP 头长度组合**，该穷举使用 IPv4/软件校验/固定载荷；这些选项编码的最终补齐字节数均为 0，不能称为覆盖 0..3 字节尾部填充。另一个矩阵覆盖 9 组选项、8 种载荷切片、IPv4/IPv6 与 partial/full 校验。参考封包器共享 `options_len`，独立性主要来自对序列化字节重算校验和，不是整个协议实现的独立预言机。

被测代码对应的 CI core 236 项、adapter 290 项通过（各自的忽略项保留），7 项 wire 测试全部通过；L0、lint、bench、tools 与 qa/gate 通过。见 [CI run](https://github.com/BlackLuny/zfstack/actions/runs/39057629177)。

## 统计口径与可复现性

运行 `python3 docs/experiments/2026-10-17-issue51-wire-checksum/summarize.py` 由提交的 JSONL 原始块重建 3 份微基准、2 份 packet-pump summary 和[全部生成表格](./2026-10-17-issue51-wire-checksum/tables.md)；加 `--check` 验证一致性。脚本不修改原始观测。

- 微基准：每项 12 个 ABBA/BAAB + AA 块，表中 ns 为通常定义的样本中位数（偶数样本取两个中央值均值），AB/AA 变化为**逐块百分比的算术均值**。旧表把该均值标为中位数，且 ns 选取中央单点，现已修正。
- Packet-pump：6 个配对块 + AA，CPU/GB 与逐块变化均取中位数。包数在不同运行间有变化，改列实际范围，避免单个包数被当作所有运行的固定值。
- 上述区间统一按整块有放回 bootstrap（2,000 次、每指标固定种子）计算，保留 AB 与 AA；生成表与 JSON 来自同一次计算，旧正文中不一致的 CI 已撤下。
- TUN summary 保留原 runner 的配对中位数与固定种子 bootstrap。实际顺序为每块 base/cand/AA 或 cand/base/AA，**不是 ABBA**；AA 总在块末，不能完全控制时间漂移。6 场景 × 6 块 × 3 次 = 108 runs。
- AB 区间包含 0 表示当前数据不能确定方向，不证明等价；与 AA 区间重叠也不是非劣效检验。未预设可接受回归阈值，不能从这些区间得出“无任何回归”。

## 函数级结果

[生成表格](./2026-10-17-issue51-wire-checksum/tables.md)与原始 JSONL 是数值真源。代表性的**配对平均耗时变化**为：

| 场景 | x86_64 默认 | x86_64 native | AArch64 / M1 Max |
|---|---:|---:|---:|
| 1460B 校验和 | -16.80% | -8.73% | -46.87% |
| 1460B 软件校验封包 | -21.27% | -18.89% | -43.67% |
| 1448B 软件校验解析 | -13.66% | -8.20% | -18.44% |
| ACK / offload 封包 | -27.42% | **+2.35%** | -4.44% |

native 的 offload ACK 有小幅退化，约 19.05→19.50ns；不能声称所有构建与路径均获益。微基准使用 `rustc 1.94.1`、`-C opt-level=3` 与 64B 函数对齐；这些是特定机器/编译布局的函数级结果，不能直接换算成 zf-worker CPU 或吞吐提升。

### 指令数与代码体积

64B 的旧“421→87 / -79%”混入首次 AVX2 特性检测，已废弃。预热后同一计数边界为 123→98（-20.33%），1460B 为 362→306（-15.47%），详见 [warm counts](./2026-10-17-issue51-wire-checksum/instruction-counts-warm-named.json) 与 [GDB 脚本](./2026-10-17-issue51-wire-checksum/count-ir-warm-script.txt)。这些是 GDB 单步计数，不是硬件 PMU retired-instructions 事件。

[symbol-sizes.json](./2026-10-17-issue51-wire-checksum/symbol-sizes.json) 记录独立 `-C opt-level=3` 构建的相关符号合计 4,968→5,622B（+654B、+13.2%）。这是该测量构建的代码体积，不能当作完整 worker 的 L1i 占用或 RSS 增量。

## Packet-pump 与 TUN：观察到什么、尚未证明什么

双端 sans-IO packet-pump 的 x86_64 配对 CPU/GB 中位变化约 -2.56%..+1.17%，AArch64 约 -3.81%..-1.92%；多数区间跨 0，AA 也有波动。完整数值见[生成表格](./2026-10-17-issue51-wire-checksum/tables.md)，不把点估计当作稳定收益。

TUN runner 改用 `--client-proxy zfstack`（无 vnet 时 RX+TX 完整软件校验），关闭 `--driver-thread`，绑定核 0,1,2，`--secs 3 --warmup 1`。这排除了旧工作点中的跨线程竞争因素；测试方式变化本身不能证明补丁消除了生产长尾。实际运行与指标见 [TUN rows](./2026-10-17-issue51-wire-checksum/tun-clean-rows.jsonl)、[summary](./2026-10-17-issue51-wire-checksum/tun-clean-summary.json) 和 [runner](./2026-10-17-issue51-wire-checksum/run-tun-clean-script.txt)。

| 场景/指标 | 配对中位变化 | 95% CI |
|---|---:|---:|
| soft / MTU1500 / 上行吞吐 | +2.62% | [-4.65%, +5.12%] |
| soft / MTU1500 / mixed 吞吐 | -2.74% | [-4.91%, +4.99%] |
| soft / MTU1500 / mixed proxy CPU/GB | +3.27% | [-4.52%, +6.44%] |
| soft / MTU1500 / mixed P99 | -10.15% | [-33.99%, +838.33%] |
| soft / MTU65535 / 下行 proxy CPU/GB | -4.34% | [-14.80%, +1.97%] |
| offload / MTU1500 / mixed 吞吐 | -2.61% | [-32.20%, +8.65%] |

软件 mixed 的一块 P99 为基线 8.416ms、候选 147.738ms，归因尚未闭环；MTU65535 的一块基线吞吐仅 0.514Gbps。原始异常均保留。每轮 mixed 仅约 100 个 RR 样本，P99.9 接近样本最大值，不足以稳定估计高分位长尾。不能把置信区间 ±4.9% 说成运行方差收敛到 ±4.9%，也不能据此认定全部尾延迟落在无回归范围。

对 zfc 的判断是：WG 用户态 TCP 的软件校验路径能命中优化，新增 SIMD/手工公式提高维护成本，现有正确性测试与局部收益支持合入。实际 zfc 运行时、boringtun 加解密、出站 IO 与调度占比不同，更新依赖后的 CPU/GB、吞吐、尾延迟和 UDP 兼容仍需完整 WG 集成验证。本报告不替代该验证。
