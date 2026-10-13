# PR #26 RX progress credit：main 对照性能 A/B（2026-10-13）

日期：2026-10-12/13 UTC。仓库当时没有 pull request **#27**；未合并的代码变更是 [PR #26](https://github.com/BlackLuny/zfstack/pull/26)（`codex/rx-progress-credit`）。本报告是 **main vs #26** 的同机 A/B，不是对一个不存在的 #27 的测量。

| 版本 | Git SHA | 角色 |
|---|---|---|
| 基线 A | `01e3dc3eddd02971a21bde465d1ac1247f21e976` | 当前 `main`（含已合并 #25） |
| 候选 B | `3fe3be95aa9e19716be362a3dfab53489faba2f6` | PR #26 HEAD |

两侧使用**同一份** `examples/pareto_probe.rs`（sha256 `b870da91e59001d8dd9f13cd4bba2bff713bb6d431fa3b9f80d9c94943961157`，与 issue #22 本机复现所用 probe 相同）和同一份 `Cargo.lock`。#26 未改 probe / `cases.json`。库差异见 [changes.patch](changes.patch)（`src/budget.rs`、`src/buf.rs`、`src/conn.rs`、`src/shard.rs`、`src/sim/tests.rs`）。`src/heap.rs` **未改**。

## 1. 结论

1. **固定工作量 CPU（fat LTO，18 case × 6 对 AB/BA）**：RX 微基准与全部 packet pump 的配对 CPU 变化要么跨 0，要么小于本机 A/A 噪声。**不宣称 datapath 变快或变慢。** `heap_mixed` / `heap_pop` 出现 −6%～−9% 且区间不含 0，但 heap 源码未动，按既有口径视为 **LTO 布局**，不归因于 RX credit。
2. **账本**：每条连接在双 Shard pump 上多预留 **16.03 KiB**（两端各 8 KiB `RxProgress`）。64 流为 **+1026 KiB**，与 64 × 16.03 一致。全部 108 对 `final_reserved_bytes == 0`，工作量（包数 / 有效字节 / 校验和）成对相等。
3. **L1 isolate（kernel TCP + 单 TUN，无 WG，`--rate-mbps 0`）**：8 MiB 同端口 16 条重下载挤 1 条 320 B 轻请求时，基线 **1/6** 轻用户跑完，候选 **5/6**；配对重流 goodput 中位 **−4.4%**，栈线程 s/GiB 中位 **+3.9%**（4 vCPU 不限速，离散大）。64 MiB 下候选 **6/6** 通过且最大轻延迟 52–66 ms；基线 **2/6** 通过，失败样本最大延迟到 **4.1 s**。
4. 这不是 WAN、不是 zfc Driver B、不是发行 profile 容量结论。L1 单 TUN + 共享模拟器也**不是** L2 双 WG 监听端口的隔离模型；跨端口格只能说明本 L1 拓扑，不能推翻 PR #26 已有的 L2 跨端口证据。

## 2. 方法

### 2.1 固定工作量 probe

- 机器：KVM，4 vCPU Intel Xeon，Linux 6.12.94+，15 GiB。无硬件 PMU（不报告退休指令）。
- Rust **1.94.1**（与 CI 一致）；`CARGO_PROFILE_RELEASE_LTO=fat`、`codegen-units=1`、`panic=abort`、`CARGO_INCREMENTAL=0`。verbose 构建行含 `-C linker-plugin-lto -C codegen-units=1 -C panic=abort`。
- 绑核 CPU 2；每 case 1 个不计入统计的 warmup 对 + **6** 个正式对；顺序 AB/BA。分析：`analyze.py`，bootstrap 20,000，seed `20261012`。
- A/A：同一 SHA `01e3dc3` 从两个 worktree 路径各编一次。fat LTO 下两份 probe **SHA256 相同**（`f252e7e5…`）；A/A 只反映运行噪声，不含布局差。候选 probe `4a3b5794…`。

### 2.2 L1 isolate

- 基线 worktree 只打上 #26 的 `--budget-mb` harness 补丁（[harness 补丁在 evidence `manifest/harness-budget-knob.patch`](evidence.tar.gz)），**库代码仍是 main**。候选是完整 #26。
- zfbench 用仓库默认 release（16 CGU、无 LTO）。这里看的是轻用户能否完成和秒级尾延迟，不是 1% CPU。
- `--rate-mbps 0 --rr-size 320 --rr-interval-ms 10 --secs 12 --warmup 2 --client-cc cubic`；6 对 AB/BA。exit 1（轻用户 EAGAIN）作为测量结果保留，不中止 runner。

库测试（同一 fat LTO profile）：基线 **184 passed / 3 ignored**；候选 **193 passed / 3 ignored**；`--no-default-features` 构建通过。

## 3. A/A 噪声（相同二进制）

Validation: OK。108 对，warmup 36，拒绝 0，工作量成对相等。

多数 pump 中位 CPU 变化在 ±1% 内、CI 跨 0。`short_64x4096` 中位 **+3.54%**，95% 描述区间 **[+1.48, +5.31] 不含 0**——六对在该短连接格上可以做出伪显著。因此 A/B 里小于约 5% 且方向与 A/A 同号的 CPU 差不归因于代码。`heap_pop` A/A 中位 −4.01%，CI 跨 0。

## 4. Probe A/B（main → #26）

Validation: OK。108 对，warmup 36，拒绝 0，工作量成对相等，无 cgroup throttle。

CPU 负值 = 候选更省。区间跨 0 的格不宣称。

### 4.1 微基准

| 场景 | CPU 基线 → 候选 | 配对 CPU 变化 | 95% 描述区间 |
|---|---:|---:|---:|
| `budget_1400_p1` | 102.46 → 99.63 ns/op | −1.59% | [−4.69, +1.33] |
| `budget_handle_control_p1` | 100.86 → 100.16 ns/op | −0.41% | [−1.83, +5.38] |
| `rx_64_p1` | 2.694 → 2.696 s/GB | +0.56% | [−0.79, +1.29] |
| `rx_1400_p8` | 0.287 → 0.288 s/GB | +0.03% | [−2.70, +1.90] |
| `rx_65535_p1` | 0.188 → 0.187 s/GB | −0.13% | [−9.14, +1.28] |
| `rx_tail_1400` | 0.342 → 0.335 s/GB | −2.48% | [−13.26, −0.46] |
| `tx_alloc_64k` | 0.183 → 0.184 s/GB | +0.21% | [−1.17, +0.68] |
| `heap_equal_4096` | 3.355 → 3.296 ns/op | −1.74% | [−2.85, +0.20] |
| `heap_mixed_4096` | 4.678 → 4.229 ns/op | −9.23% | [−10.63, −7.30] |
| `heap_pop_4096` | 266.1 → 251.5 µs/op | −6.76% | [−10.60, −2.69] |

`rx_tail_1400` 区间不含 0，但 A/A 在邻近 RX 格上也出现过不含 0 的噪声；**不宣称 RX 变快**。`heap_*` 见 §1：heap 源码未改。

### 4.2 Packet pump

| 场景 | CPU 基线 → 候选 | 配对 CPU 变化 | 95% 描述区间 | 计费峰值 Δ |
|---|---:|---:|---:|---:|
| `down_1_paced` | 1.043 → 1.056 s/GB | +0.56% | [−2.24, +2.90] | +16.03 KiB |
| `up_1_paced` | 0.989 → 0.989 s/GB | +0.07% | [−0.78, +2.06] | +16.03 KiB |
| `up_8_peers_paced` | 0.932 → 0.938 s/GB | −0.45% | [−1.94, +2.53] | +128.25 KiB |
| `bidi_8_one_peer` | 0.975 → 0.983 s/GB | +0.94% | [−2.56, +2.40] | +128.25 KiB |
| `up_64_peers_paced` | 1.071 → 1.081 s/GB | +1.35% | [−0.31, +5.37] | +1026 KiB |
| `up_8_owned_control` | 0.990 → 1.016 s/GB | +2.45% | [−1.27, +5.42] | +128.25 KiB |
| `down_8_unpaced` | 0.962 → 0.956 s/GB | −0.16% | [−1.20, +1.59] | +128.25 KiB |
| `short_64x4096` | 1.586 → 1.639 s/GB | +2.43% | [−1.65, +7.30] | +1026 KiB |

全部 pump CPU 区间跨 0。`short_64x4096` 的 +2.43% 小于该格 A/A 的 +3.54%。进程峰值 RSS 仅该短连接格明显变化：中位 **5.89 → 7.16 MiB**（账本 +1.00 MiB；RSS 还含分配器保留页）。

计费增量 = 16.03 KiB × 流数，对应两端各预付 `RX_PROGRESS_MIN_CHARGE`（8 KiB）。微基准不建连，峰值不变。

## 5. L1 isolate A/B

默认 release 的 zfbench，不是 fat LTO。无限速链路，4 vCPU 上客户端 + 两条 link 线程 + 栈线程争用；heavy Mbps 和 s/GiB **离散很大**，只作伴随指标。主指标是轻用户是否完成、样本数、P99/max。

### 5.1 同端口 8 MiB / 16 重 / 1 轻（#26 要修的格）

| | 基线 | 候选 |
|---|---:|---:|
| 轻用户 `ok`（无 EAGAIN） | **1/6** | **5/6** |
| 轻样本数中位 | **0** | **802** |
| 有 P99 时的 P99 中位 | 13.3 ms（n=1） | 13.3 ms（n=5） |
| 重下载 Mbps 中位 | 1701 | 1609 |
| 配对 goodput 变化中位 | | **−4.40%** |
| 配对栈线程 s/GiB 变化中位 | | **+3.86%** |

配对明细：基线 5/6 次轻请求 0 样本并以 `Resource temporarily unavailable` 失败；唯一成功的 pair 2 两侧 P99 都约 13.3 ms。候选 pair 4 在 77 个样本后也 EAGAIN（max 仍 14.0 ms），不算 6/6。重流在基线失败时仍然 ~1.6–1.9 Gbps，说明这是轻请求物理额度而不是整机塌缩。

方向与 PR #26 正文的 L2 8 MiB 表一致（goodput −4.28%，worker CPU +4.37%；同端口 P99 从数百毫秒降到 ~10 ms）。本 L1 基线多数是 **零完成** 而不是高 P99，因为 5 s 读超时直接记 EAGAIN。

### 5.2 同端口 64 MiB / 16 重 / 1 轻

| | 基线 | 候选 |
|---|---:|---:|
| 轻用户 `ok` | **2/6** | **6/6** |
| 轻样本数中位 | 223 | 231 |
| 轻 P99 中位（有值时） | 49.7 ms | 53.3 ms |
| 轻 max 中位 | **968 ms** | **57 ms** |
| 重下载 Mbps 中位 | 4374 | 4432 |
| 配对 goodput 变化中位 | | **+1.73%** |
| 配对 s/GiB 变化中位 | | **+0.25%** |

64 MiB 不限速时整机约 4.4 Gbps，轻 RTT 被调度抬到 ~40–55 ms（10 s 窗口约 230 个样本）。基线失败 run 的 max 到 **4.13 s**；候选 max 始终 ≤ 66 ms。配对吞吐/CPU 在噪声内。与 PR #26 L2 64 MiB（goodput −0.49%，CPU +1.79%）同数量级。

### 5.3 跨端口 8 MiB（L1 弱隔离）

L1 一条 TUN、一个模拟器瓶颈、一个 shard 上的两个 iface。这比 L2「两个 WG 监听端口」弱，harness 文档已说明。

基线 **1/6** 轻用户成功；候选 **6/6**，P99 中位 13.4 ms。不能把 L1 跨端口基线失败读成「#26 的跨端口 L2 结论被推翻」；它只说明在本机共享 TUN 拓扑里，预付 RX credit 仍然让轻请求更容易拿到物理额度。

## 6. 和 PR #26 已有 L2 数字的关系

PR #26 的 L2 ABBA 是 fast-release、整 worker（含 WG/UDP）、每配置 2 次。本作业补了：

- 与 zfc 发行一致的 **fat LTO** 固定工作量 CPU，以及 A/A；
- 每格 **6 对** 而不是 2 次；
- 无 WG 的 L1 同形状 8/4/4 MiB。

没有重跑真实 ZFC/WG L2（本环境没有 zfc-rig 租约）。L2 路径/重传/恢复仍以 PR #26 的 [12-case 报告](https://github.com/BlackLuny/zfstack/blob/codex/rx-progress-credit/harness/l2/results/2026-10-13-rx-credit/REPORT.md) 为准。

## 7. 局限

- 无 PMU，不能用退休指令交叉验证 heap 布局假说。
- Probe 无 socket / TUN / WG；isolate 无 WG、无限速、4 vCPU 争用。
- isolate zfbench 非 fat LTO；不能把 isolate s/GiB 与 probe s/GB 混成同一指标。
- 6 对对 1% 级 CPU 不够；A/A 已证明 `short_64x4096` 可伪显著。
- 未测客户端 TUN、`mixed`/`connect` P99.9、百端口、WAN 丢包。

## 8. 证据与复现

- [evidence.tar.gz](evidence.tar.gz)：lockfile、补丁、probe `cases.json`、A/A 与 A/B 的 `runs.jsonl` / `metadata.json`、isolate 原始 JSON、分析器输出、构建日志。
- [summary.txt](summary.txt)：probe A/B 文本表；[summary-aa.txt](summary-aa.txt)：A/A；[isolate-summary.json](isolate-summary.json)：L1。完整 `summary-ab.json` / `runs.jsonl` 在 evidence 包内。
- [changes.patch](changes.patch)：相对 `01e3dc3` 的库 diff。

```bash
# probe A/B（需已按 bench/pareto/README.md 编出两份 fat-LTO 二进制）
python3 bench/pareto/analyze.py /path/to/results-ab \
  --cases bench/results/2026-10-13-pr26-ab/cases.json \
  --expected-pairs 6 --bootstrap 20000 --seed 20261012
```

Probe 基线 SHA256 `f252e7e5b23e0f86e05e783e94f50215053d9f422e81104bea0bb6ba3f41a0db`；候选 `4a3b5794a845041163c627edf261692fbbdb6acd4546b3cb2ec8d0fd73b50094`。zfbench 默认 release：基线 `cd1ed2f1…`，候选 `40a601ad…`。
