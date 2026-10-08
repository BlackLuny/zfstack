# zfstack：预算、RX 暂存与定时器优化实测

日期：2026-10-08 UTC。基线：`71feaedafca7ce53e1823490a59b37bd8afcd028`。
改动与当前检查：[PR #16](https://github.com/BlackLuny/zfstack/pull/16)。

## 结论

本轮实现并交叉审查了三项局部优化。持续传输的若干固定负载测到约 4%–6% 的单位字节 CPU 节省，临时预算分配的单项 CPU 成本下降约 23%。全部收益来自实际机器执行记录，不使用合成协议时间计算速率。完整功能回归通过，未发现本轮改动引起的功能失败。

这份证据覆盖真实机器上的库代码与包泵处理成本。作业环境没有 TUN、网络管理 capability 或真实网络端点，因而没有获得 WireGuard/TUN/NIC 实网吞吐、真实 RTT 下的延迟或整机服务 CPU 数据。包泵包括两端 Shard、IP 包物化、应用遍历和数据验证。不能将下面的 Gbit/s 当作实网吞吐，也不能将双端 CPU 成本当作单端成本。

加长且固定 12 对的确认试验中，短连接 CPU/有效字节的配对变化为 **−3.11%**（95% 描述区间 **[−4.75%, −2.14%]**），没有复现首轮 +5.54% 的退化信号；单流 paced 下行为 **−3.45%**（**[−3.95%, −0.99%]**）。65535 字节 RX 为 **+0.22%**（**[−0.37%, +9.24%]**），仍受环境时间漂移影响，既不能承诺收益，也不能把区间跨 0 当成已证明等价。

纯改期 heap 对照观察到 **+2.56%** 的配对 CPU 成本（**[+0.30%, +3.82%]**），属于应明确接受的小幅路径代价。数据中有明显时间漂移，每对范围很宽，保留了全部 12 对，包括末对 +58.03%；不能把两版独立中位数之比的 +16.27% 当作代码效应，也不把 2.56% 当作所有机器上的固定代价。这是一组有实测收益、且带已知局部折中的优化，尚不构成全负载严格帕累托支配的证明。


## 保留的改动与代价

| 改动 | 原因与结果 | 功能约束 / 代价 |
|---|---|---|
| `MemoryHandle::into_allocation` | 成功后直接把临时 handle 的 global/port/peer 三个 Arc 转移给 lease，省去 3 次 clone 和 3 次 drop，即 6 次原子引用计数修改 | 原有锁、三级预留、失败统计、cache reclaim、回滚和释放唤醒次序保留；公共 API 不变 |
| RX 首个新 chunk 暂存在栈上 | 单新 chunk 的常见 append 不再创建临时 VecDeque backing；第二个及后续 chunk 才进入 overflow | 增加短期栈槽与条件判断；不增加 RxQueue / 每连接常驻字段；任意长度输入、多 chunk、旧 tail 与 descriptor 失败仍保持提交前预留 |
| 相同 deadline 提前返回 | IndexedHeap 不再对未变 key 执行写入与下调检查 | 实际改 key 时有额外等值判断；纯改期负载另有对照，不能仅从等键热点外推全部调度成本 |

另一个单元素 DRR 队列内联候选未进入生产 diff：它会扩大 peer HashMap value，bucket 容量可在 peer 删除后继续保留，现有预算未明确覆盖新增驻留。为小幅分配收益扩大未计费内存不符合本轮目标。没有改变 DRR、拥塞控制、pacing、校验策略、默认缓冲上限或 quota。

## 环境、构建与测量方法

- 独立开发作业报告 CPU 为 Intel Xeon E5-1630 v4 @ 3.70 GHz，4 核 / 8 线程；Linux `6.1.0-41-amd64`、x86_64。
- Rust/Cargo 1.98.1；统一 release、`tokio,test-peer` features；`CARGO_BUILD_JOBS=4`、`CARGO_INCREMENTAL=0`、offline、locked。所有 v1 候选使用相同 Cargo.lock。
- cgroup CPU quota 为 5 CPU，内存上限 12 GiB。测量进程用 `taskset -c 2`，单个串行工作线程。没有并行运行我们自己的构建、测试或其他 benchmark；没有排除宿主机其他负载或锁定频率。环境快照与波动保留在原始记录中。
- 原 workspace 的 smoltcp Git revision `8014f8b21e12faf89b3b453ceea32027344721af` 不在离线缓存。首次完整 workspace 依赖解析失败已保留；只在实验副本把 `members = ["bench"]` 改成 `members = []`。生产 Cargo.toml 没有变化，库 `.rs` 基线来自上述精确提交。仓库 CI 另外检查正常 workspace 的 zfbench。
- v1 单项筛选：8 场景，每格 3 对；组合比较：16 场景，每格 6 对。每格先有一对 warmup，正式交替 AB、BA。跟进确认固定为 3 场景、每格 12 对，仍用原 v1 二进制。纯改期对照用 v2 同一 harness、原库与仅 Heap 候选，12 对。
- 主要指标为进程 user + system CPU 时间 / 有效字节，或 metadata case 的 CPU ns/op。报告中的 GB 为十进制；payload 参数按原始字节固定。
- 变化先逐对计算 `100 × (candidate / baseline - 1)`，再取中位数。表中的 B/C 绝对数是各版本独立中位数，因此它们的比值未必等于配对变化中位数。CPU 负值更好，处理速率正值更好。没有把不同场景平均为一个“总提升”。
- 95% 区间为 20,000 次 paired bootstrap 的描述性区间（固定 seed），只描述本机、固定工作量和这批观测；不是跨机器/网络的性能保证。全部正式记录保留，未删除不利样本。不同迭代数的确认试验未与 v1 混池。

## 组合候选：完整 16 场景

### Budget、RX、Heap 热点

| 场景 | CPU 基线 → 候选 | 配对 CPU 变化 | 95% 描述区间 | 配对 wall 处理速率变化 |
|---|---:|---:|---:|---:|
| `budget_1400_p1` | 169.567 → 131.712 ns/op | -22.02% | [-22.81%, -21.49%] | — |
| `budget_handle_control_p1` | 114.338 → 112.127 ns/op | -1.28% | [-2.77%, +0.68%] | — |
| `rx_64_p1` | 4.801 → 3.914 s/GB | -18.48% | [-23.31%, -6.78%] | +22.68% |
| `rx_1400_p8` | 0.496 → 0.434 s/GB | -11.82% | [-14.44%, -10.01%] | +13.69% |
| `rx_65535_p1` | 0.245 → 0.242 s/GB | +1.32% | [-2.95%, +4.26%] | -1.30% |
| `rx_tail_1400` | 0.544 → 0.463 s/GB | -14.60% | [-16.25%, -13.64%] | +17.08% |
| `heap_equal_4096` | 10.140 → 8.368 ns/op | -17.90% | [-20.69%, -14.33%] | — |
| `heap_mixed_4096` | 11.589 → 10.504 ns/op | -7.05% | [-14.29%, -2.13%] | — |

`budget-handle` 为长期保留 handle 的对照，预期不受临时所有权转移直接影响。`rx-tail` 包含分段 append、部分 read 后再次 append。Heap mixed 是 60% 等键、20% 减小、20% 增大。Heap 完整 pop/order/key/uniqueness 校验在更新循环计时之外；v1 JSON 的通用 includes_validation 字段对此描述不够准确，本报告在此更正，原始记录没有改写。

### 两端真实 Shard 的 packet pump

| 场景 | CPU 基线 → 候选 | 配对 CPU 变化 | 95% 描述区间 | 配对 wall 处理速率变化 |
|---|---:|---:|---:|---:|
| `down_1_paced` | 1.752 → 1.587 s/GB | -3.30% | [-13.08%, +5.23%] | +3.50% |
| `up_1_paced` | 1.837 → 1.757 s/GB | -6.47% | [-8.97%, -1.41%] | +6.99% |
| `up_8_peers_paced` | 1.688 → 1.613 s/GB | -5.54% | [-10.84%, -3.39%] | +6.61% |
| `bidi_8_one_peer` | 1.701 → 1.615 s/GB | -4.30% | [-7.36%, -3.28%] | +5.23% |
| `up_64_peers_paced` | 1.558 → 1.492 s/GB | -4.27% | [-8.17%, -2.03%] | +4.46% |
| `up_8_owned_control` | 1.463 → 1.414 s/GB | -3.64% | [-4.53%, -2.33%] | +3.79% |
| `down_8_unpaced` | 1.458 → 1.386 s/GB | -4.93% | [-6.43%, -1.11%] | +5.19% |
| `short_64x4096` | 3.323 → 3.540 s/GB | +5.54% | [-6.82%, +10.54%] | -5.15% |

方向按 server 视角区分：down 为 server 发送、up 为 server 接收；bidi 为双向。`owned_control` 使用普通 owned ingress；它仍走受管复制路径，不能代表 leased PacketBuf ingress。`paced` 的 pacing 由合成协议时钟驱动，实际 CPU/wall 为分母。完整参数见 `metadata.json` 与 `bench/pareto/cases.json`。

首轮短连接每次 CPU 工作只有约 83–98 ms，出现 +5.54% 的疑似代价；单流下行和 65535 字节 RX 也受明显共同时间漂移影响。这些结果没有被隐藏或直接判为持平，触发了下一节预先固定的确认试验。

## 针对性确认：同一 v1 二进制、12 对

短连接迭代 50 → 1000；单流下行 4 → 24；65535 RX 75,000 → 225,000。其他参数保持不变。三格分别分析，未与首轮合并。

| 场景 | CPU 基线 → 候选 | 配对 CPU 变化 | 95% 描述区间 | 配对 wall 处理速率变化 |
|---|---:|---:|---:|---:|
| `short_64x4096_confirm` | 3.615 → 3.615 s/GB | -3.11% | [-4.75%, -2.14%] | +3.17% |
| `down_1_paced_confirm` | 1.546 → 1.493 s/GB | -3.45% | [-3.95%, -0.99%] | +3.57% |
| `rx_65535_p1_confirm` | 0.233 → 0.247 s/GB | +0.22% | [-0.37%, +9.24%] | -0.23% |

## 定时器全部改期：独立 v2 对照、12 对

v2 增加 `heap-changing`，每次对目标 key 异或 256；同一节点在相邻 pass 间交替 ±256，严格没有等键，也不存在饱和导致的等键。最终排序、键值和唯一性检查全部执行。v2 同时修正 heap 的 timing_scope 描述；只把同一 v2 harness 的两边互相比较。

| 场景 | CPU 基线 → 候选 | 配对 CPU 变化 | 95% 描述区间 | 配对 wall 处理速率变化 |
|---|---:|---:|---:|---:|
| `heap_changing_4096` | 11.569 → 13.451 ns/op | +2.56% | [+0.30%, +3.82%] | — |

## 单项筛选：用于归因，3 对 / 场景

| 独立候选 | 场景 | 配对 CPU 变化中位数 | 每对变化范围 |
|---|---|---:|---:|
| 仅 Budget | `budget_1400_p1` | -23.17% | [-23.26%, -22.47%] |
| 仅 Budget | `budget_handle_control_p1` | -1.39% | [-2.29%, +1.88%] |
| 仅 RX staging | `rx_64_p1` | -6.87% | [-7.85%, -5.60%] |
| 仅 RX staging | `rx_1400_p8` | -7.57% | [-7.78%, -3.09%] |
| 仅 RX staging | `rx_65535_p1` | -5.56% | [-15.30%, +0.23%] |
| 仅 RX staging | `rx_tail_1400` | -6.27% | [-12.71%, -4.01%] |
| 仅 Heap | `heap_equal_4096` | -19.61% | [-20.88%, -19.05%] |
| 仅 Heap | `heap_mixed_4096` | -9.99% | [-14.41%, -8.95%] |

这些是分别只启用一项修改时的筛选，不是组合候选的总体加速率。样本数仅 3 对，未给出置信区间；不能把三项百分比相加。

## 内存与工作量一致性

| 场景 | 计费采样峰值 B → C（KiB） | 进程峰值 RSS B → C（MiB） |
|---|---:|---:|
| `budget_1400_p1` | 2.06 → 2.06 | 0.97 → 0.96 |
| `budget_handle_control_p1` | 2.06 → 2.06 | 0.98 → 0.97 |
| `rx_64_p1` | 0.38 → 0.38 | 0.97 → 0.97 |
| `rx_1400_p8` | 4.06 → 4.06 | 0.99 → 0.96 |
| `rx_65535_p1` | 64.31 → 64.31 | 2.37 → 2.37 |
| `rx_tail_1400` | 3.94 → 3.94 | 0.97 → 0.98 |
| `heap_equal_4096` | 0.00 → 0.00 | 2.26 → 2.21 |
| `heap_mixed_4096` | 0.00 → 0.00 | 2.28 → 2.23 |
| `down_1_paced` | 65790.44 → 65790.44 | 66.72 → 66.78 |
| `up_1_paced` | 65790.44 → 65790.44 | 66.78 → 66.79 |
| `up_8_peers_paced` | 65983.81 → 65983.81 | 66.89 → 66.86 |
| `bidi_8_one_peer` | 66278.12 → 66278.12 | 67.26 → 67.15 |
| `up_64_peers_paced` | 132010.06 → 132010.06 | 131.35 → 131.35 |
| `up_8_owned_control` | 65983.81 → 65983.81 | 66.85 → 66.93 |
| `down_8_unpaced` | 65983.81 → 65983.81 | 66.96 → 66.94 |
| `short_64x4096` | 8637.12 → 8637.12 | 11.14 → 11.13 |

`sampled_peak_reserved_bytes` 是受管额度的采样下界；VmHWM 是包含 setup 和验证的进程生命周期峰值。不能据此证明所有 allocator 瞬时峰值完全相同。生产 diff 没有新增队列/连接常驻字段；所有有效记录结束后的预算余量均为 0。

分析器检查每对的操作数、有效字节、checksum、包数、wire bytes、driver rounds 与虚拟协议工作量；任何不同均须说明。详细一致性、RSS 和 cgroup 限流增量见各 summary.json。包数包含 cleanup，不能再除以不含 cleanup 的 work_time 推导 pps。逐字节 payload 为周期 251 的模式，多个流模式相同；它不能排除所有跨流错投或整周期偏移，demux/重排等仍以功能套件为补充。

## 功能回归与代码审查

| 独立机器 gate | 基线 | 组合候选 |
|---|---:|---:|
| `cargo test --offline --release --features tokio -- --test-threads=4`（候选另加 `--locked`） | 153 passed、0 failed、2 ignored | 157 passed、0 failed、2 ignored |
| `tests/rx_capacity_lifecycle.rs` | 1 passed | 1 passed |
| `cargo build --offline --no-default-features` | exit 0 | exit 0（另加 locked） |
| `cargo test --release --example pareto_probe --features tokio,test-peer` | — | 2 passed（包含 heap 源文件测试） |

两个原有 ignored 项是 BBR trace / steady-state 诊断项，没有修改它们的 ignore 状态。运行的原有测试覆盖乱序、丢包/重复 ACK、SACK/重传、MTU 下降、零窗口、borrowed ingress 生命周期、配额压力/释放、fairness、Tokio adapter、半关闭与错误路径等。新增测试针对临时/保留 handle × RX/TX × global/port/peer 拒绝回滚与唤醒，RX 多 chunk/旧 tail/导出 owner 保费及后续 chunk 拒绝，和等 deadline 后改期。

生产 diff 经独立只读交叉审查，没有发现应阻止交付的所有权、API、回滚或边界缺陷。审查发现并更正了 heap timing_scope 文案；原始测量值未改变。随后按仓库 rustfmt 要求做四处格式修正，生产逻辑与实测候选一致。

独立机器没有 rustfmt / clippy，因此不能将其本地调用失败算成通过。正式格式与 lint、正常 workspace 构建和完整库回归由仓库现有 [CI / PR Checks](https://github.com/BlackLuny/zfstack/pull/16/checks) 提供，固定 Rust 1.94.1；没有删减或跳过仓库 gate。CI 状态以对应提交的在线记录为准。

这里的“无功能回归”指本轮已运行的检查未发现回归，并不等于形式化证明所有平台、并发时序和实际网络条件都无问题。没有实测 p99 延迟、NIC 中断、WireGuard 加解密、TUN 系统调用、多核扩展或长时间生产 soak。

## 证据与复现

- [evidence.tar.gz](evidence.tar.gz)：逐次原始 JSONL、每次的命令/顺序/退出码/CPU/wall、环境快照、metadata、源码与二进制 SHA256、锁文件、实机功能日志、v1/v2 harness 快照。
- [results.json](results.json)：各实验核心汇总，保留独立实验边界；完整 summary 在证据包中。
- [SHA256SUMS](SHA256SUMS)：交付文件校验值。
- [复现步骤](../../pareto/README.md)、[runner](../../pareto/run_ab.py)、[analyzer](../../pareto/analyze.py)、[完整 case 配置](../../pareto/cases.json)。

从仓库根目录分析已有证据，无需再次运行 benchmark：

```bash
PARETO_REVIEW_DIR="$(mktemp -d /tmp/zfstack-pareto-review.XXXXXX)"
tar -xzf bench/results/2026-10-08-pareto/evidence.tar.gz -C "$PARETO_REVIEW_DIR"
python3 bench/pareto/analyze.py "$PARETO_REVIEW_DIR/evidence/combined-v1"
python3 bench/pareto/analyze.py "$PARETO_REVIEW_DIR/evidence/focused-v1"
python3 bench/pareto/analyze.py "$PARETO_REVIEW_DIR/evidence/heap-changing-v2"
```

精确 v1 原库二进制 SHA256：`75800bdb65fba8cbcc6dd7330ed77cf94034716bac93e83dd8479ea9d6a08378`。
组合候选二进制 SHA256：`ca10f13f43daaae0cae455e0a8e0254d44a8e39213ef7719f55d56ffe33282f5`。
这两个二进制在初轮和针对性确认间没有重编译。完整构建输入在 `build-manifest-v1.json`；v2 单独见 `build-manifest-v2.json`。最终源码中仅有已说明的 formatter 排版变化和 harness 新 case / 元数据修正，不能把新 harness 的 hash 当成 v1 实测 hash。

精确性能锁文件为包内 `evidence/probe/Cargo.lock`，SHA256：`a6a32b2ec5e297ff608cf4487f83f4e00781836f6e5d630338a8fe41bd44b02b`。`evidence/base/Cargo.lock` 属于首次功能回归副本，仅多一个末尾空行，依赖条目和版本相同；两份原文分别保留。
