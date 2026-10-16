# Issue 48 / PR #70：隔离 TUN 持续大流与 Mixed RR 配对测量

日期：2026-10-16。被测 head `b98c5c33be1d0f8775064706a8aa81d373a1f744`，基线 `88a2e3f7a2e86fb6948223054a377e8a7f831af1`。

这是对 PR #70 先前缺口的补充测量，不是正式性能准入。口径在跑数前写入 [plan.json](plan.json)。测量只调用仓库已有的 `bench/pareto/issue23_tun_case.py` 与 `issue23_tun_load.py`（`--tun-proxy --client-profile --splice --vnet-hdr --driver-thread`）。没有改生产热路径，没有给未合并 PR 调用 `qa-perf.yml` 受保护节点。

## 结论（先看这个）

| 验收项 | 判定 | 说明 |
| --- | --- | --- |
| 标准 CI 六项、287 功能测试、就绪/EOF/half-close 回归 | 已覆盖，未重跑 | GitHub Actions `38791221407`；空闲微基准仍以 [2026-10-15 记录](../2026-10-15-issue48-readiness/README.md) 为准 |
| 空闲 25 万次 pump CPU / TX ledger | 已覆盖 | 候选/基线 CPU 几何均值 0.3417；额外 TX ledger +66,112 B → 0 B。不能外推到持续可读大流 |
| 持续可读大流 goodput、代理进程 CPU/GB | 隔离 TUN 诊断：未见超出本机 A/A 噪声的回退 | 8 对 ABBA/BAAB；下行 CPU/GB 配对中位 −2.27%（描述性 95% 区间 [−6.49%, 0%]） |
| Mixed RR / 短流 P99 | 有原始样本，噪声太大，INCONCLUSIVE | 下行 mixed P99 配对中位 +9.37%，区间 [−13.5%, +24.9%] 穿过 0；同机 A/A 已有 ±8–33% |
| 短流 standalone RR 与大流公平性 | 隔离诊断：未见回退 | RR64 phase1 P99 中位 −3.32%；Jain 指数约 0.999（下行）/ 0.997（上行） |
| 无效 fill/write_with 热路径计数 | 仍缺 | 未在生产 pump 上加计数器；空闲路径仍只由既有 ledger 回归证明 |
| 正式 enforce / 独占测量节点 / WG·L2 | 未完成 | 本机不是 `qa-perf.yml` 物理域；没有 WireGuard。**PR #70 保持 Open，不能合并为性能 PASS** |

不要把 idle microbenchmark 的 −65.8% 写成生产收益。不要把本次 TUN 诊断写成校准非劣。

## 证据盘点

| #48 验收点 | 状态 | 身份 |
| --- | --- | --- |
| 确定性：Pending→ready、stale ready→WouldBlock、quota/socket wait、静默上游/core-close、EOF/RST/half-close | 已覆盖 | `src/tokio_adapter/tests/qa.rs`；CI test job PASS |
| 无数据路径准备成本下降 | 已覆盖（微基准） | idle 250k pump；共享宿主、release 测试二进制，非正式 fat-LTO 生产像 |
| 就绪大流不发生超出噪声的 CPU/吞吐回退 | 先前缺失，本次补隔离 TUN | 本目录；代理进程 `/proc` 时钟，非 core-only |
| 混合 RR 尾延迟保护 | 先前缺失，本次有数但 INCONCLUSIVE | 同一次 issue23 用例的 `mixed_rr` |
| 纯上行 / 延迟上游 / ACK 无数据 / 正常下行的无效 fill 计数 | 仍缺 | 现有 adapter 无这些计数器；本次未插桩 |
| 受保护默认分支 + 校准节点 enforce | 未做 | `.github/workflows/qa-perf.yml` 只接受已审查默认分支 SHA；未绕过 |

## 环境

Cloud Agent 隔离虚拟机，不是 self-hosted `qa-performance` runner。

- Linux 6.12.94+ KVM，4 vCPU Xeon，cpuset `0-3`
- `cpu.stat.nr_periods=0`，全程 `throttled_usec` 增量 0；未证明物理独占
- `/dev/net/tun` 存在；本会话用 passwordless sudo 打开 TUN、创建 netns；安装了 `iproute2`（原镜像没有 `ip`）
- Rust 1.94.1 (`e408947bf`)、Python 3.12.3、fat LTO + `codegen-units=1` + `panic=abort`、`--locked`
- 两边 `Cargo.lock` SHA-256 `f16d0a552ae1785ce8ea646ebb66d52ac2155336e732b38c0760f606f147046d`
- 基线二进制 `cd28d8b83b4627de5900a469da2c89c49921d032d76b654965bb324f240ff5b3`
- 候选二进制 `68af29edfa28a416b07606575885d130bcdb9b80add4deae98e2f5939ff999e4`
- 代理绑核 `0,1`，loader 绑核 `2,3`（机器只有 4 核，不能复用旧 6 核 affinity）
- issue23 case_config SHA-256（24 个性能槽全部相同）`b20591d8fdce0ef03cdfacea5961c10fd89843ab754f841095c603809ea23356`

完整 uname/lscpu 见 [environment.txt](environment.txt)。

## 冻结负荷

与 PR #31 审查用的 issue23 用例同形，只把每流 bulk 从 8 MiB 提到 64 MiB，以便持续可读窗口里 mixed RR 能凑够 P99 所需的 ≥100 个样本：

- MTU 1500，8 流，双向 down/up
- 每流 64 MiB 已校验应用字节；每方向每窗 512 MiB
- idle 2 s + settle 1 s + reburst
- standalone RR：64/512/1400/4096 × 1000
- mixed RR：bulk 期间 1 ms 间隔、64 字节 echo
- case timeout 240 s；实际每槽约 6.5–6.8 s

一次 diagnostic smoke（100 个 RR、1 MiB bulk）证明 runner 可跑，不进入统计。随后 4 对 A/A（全基线，顺序 AB AB BA BA），再 8 对 ABBA/BAAB。失败槽不替换。25/25 `ok` 且 `cleanup_ok`。

## 配对结果

CPU 是 **整个代理进程** 的 `/proc` utime+stime，分母是已校验应用字节。Python 对端和 loader 绑在另外两核。区间是配对百分比的描述性 bootstrap，没有多重比较校正，也没有登记的非劣界。

A/A（n=4，不足 6，无 CI；第二槽/第一槽）：

| 指标 | 中位 A | 中位 B | 配对中位 % |
| --- | --- | --- | --- |
| down CPU s/GB | 1.406 | 1.416 | +2.04 |
| up CPU s/GB | 1.360 | 1.360 | 0.00 |
| down goodput Mbps | 6256 | 6142 | −1.30 |
| up goodput Mbps | 8061 | 7997 | −0.07 |
| down mixed P99 µs | 8240 | 7880 | −7.60 |
| up mixed P99 µs | 15591 | 14335 | −14.82 |
| RR64 phase1 P99 µs | 53.06 | 52.36 | −1.30 |
| down Jain | 0.99993 | 0.99994 | +0.001 |
| up Jain | 0.99687 | 0.99738 | +0.045 |

A/B（n=8；候选相对基线）：

| 指标 | 基线中位 | 候选中位 | 配对中位 % | 描述性 95% 区间 |
| --- | --- | --- | --- | --- |
| down CPU s/GB | 1.434 | 1.383 | −2.27 | [−6.49, 0.00] |
| up CPU s/GB | 1.341 | 1.327 | −0.69 | [−2.76, 0.00] |
| down goodput Mbps | 6118 | 6359 | +2.65 | [+0.32, +6.77] |
| up goodput Mbps | 7814 | 8006 | +0.90 | [−0.67, +3.69] |
| down mixed P99 µs | 8260 | 8628 | +9.37 | [−13.49, +24.94] |
| up mixed P99 µs | 12359 | 12885 | +3.48 | [−6.52, +13.86] |
| RR64 phase1 P99 µs | 54.66 | 53.63 | −3.32 | [−7.71, +1.57] |
| down Jain | 0.99994 | 0.99993 | −0.002 | [−0.006, +0.007] |
| up Jain | 0.99803 | 0.99738 | −0.038 | [−0.222, +0.035] |

性能槽 mixed 样本：下行最少 261，上行最少 173（合并 initial+reburst；P99 要求 n≥100）。

读法：

- 持续可读大流上，多出来的每 pump 一次 `poll_read_ready` **没有**在本机噪声里打出 CPU/GB 或 goodput 回退。下行 goodput 区间不含 0，但同机 A/A 已有 ±7% 量级，不把它记成收益。
- Mixed P99 在 A/A 里就已经大幅摆动。A/B 区间穿过 0，**不能**写成短流尾延迟保护通过。
- 8 流字节合同全部为 64 MiB；Jain 指数接近 1，没有公平性回退信号。
- `/proc` 时钟约 10 ms。单窗 wall 约 0.5–0.7 s，CPU 增量约 0.7–0.8 s，分辨率够做配对，不够做细到 1% 的绝对认证。

原始 `loader.jsonl`（每槽约 1 MiB，合计约 26 MiB）留在测量机 `/tmp/zf48/runs/`，未提交。逐槽压缩指标见 [compact-metrics.json](compact-metrics.json)，推断摘要见 [summary.json](summary.json)。

## 明确仍未证明的事

1. 校准非劣、产品 SLO、独占物理域、cgroup 祖先限额。
2. WireGuard / L2 / 生产 worker CPU/GiB。
3. 热路径上无效 `write_with` / backing 初始化字节 / WouldBlock 次数。空闲收益仍只由微基准和 ledger 回归支撑。
4. 与 PR #68 / #71 叠加后的组合性能。

编排脚本 [run_matrix.py](run_matrix.py) 只负责按冻结计划调用既有 issue23 runner，不引入新的准入阈值。
