# PR #71 合入决策：独立代码、指令与多次采样

日期：2026-10-16。对象：[PR #71](https://github.com/BlackLuny/zfstack/pull/71)（草稿，`codex/issue65-tx-cost`，生产提交 `2694c14`），对照 main 冻结点 `88a2e3f`（#67 后、#70 前）。跟踪 [#65](https://github.com/BlackLuny/zfstack/issues/65)，对照 [#68](https://github.com/BlackLuny/zfstack/pull/68)（`bf782a5`）。

本报告把**原第二台隔离机**的资格包，和**本机一次独立复测**（代码、fat LTO 反汇编、Callgrind 指令数、6 个 12 对 CPU 窗口）放在同一口径下，只回答一件事：**现在要不要合入 #71。**

原始证据：[`bench/results/2026-10-16-issue71-decision/`](../../bench/results/2026-10-16-issue71-decision/README.md)。图表总览：[PR #71 merge decision](https://cursor.com/canvas/store-4a976e9d-7179-4187-abb1-b32f2681fdd4/38ad9546-2cb8-4413-be68-259679ace480)。

## 结论

**不要合入 #71，不要关闭 #65，保持草稿。**

生产热路径没有可交付收益。Shard 使用 `BlockPool::new(16)`，relay 走 `TxBuf::write_with`，连接级 peer 在非空 TX 上禁止切换。#71 的两个目标场景（cache0、同 TxBuf 交替 peer）都不是这条热路径。在热路径上，候选反而多干活：

- Callgrind（25000 次、含校验器）：同 peer 缓存 `write_with` 64B 指令 **+1.91%**，`push` 缓存 64B **+3.70%**。
- 原隔离机两个加长窗口：缓存 `write_with` 64B CPU **约 +1.4%**，区间排除 +2% 但仍是真实成本。
- 网络资格仍为 **INCONCLUSIVE**：最终 64 对 up8 stack CPU/GB **+0.97% [-1.60, +4.86]**，不能排除 +2%。点估计方向与缓存 `write_with` 成本一致，不能当成已证实的网络回退，也不能当成非劣。

cache0 / 迁移上的微基准收益是真的，但是**给错了场景**。本机 Callgrind 看到 cache0 `push` 指令 **-9.64%**、`write_with` **-14.30%**、迁移 `push` **-7.85%**；原隔离机 1.92 fat LTO 两个窗口 64B cache0 CPU **-4.4%～-7.1%**，迁移 **-4.0%～-5.1%**，区间上界都 &lt; 0。这些数字不能外推成 cache16 + `write_with` 的生产收益。

同一台评测机、同一套 probe，#68 的微基准幅度远大于噪声（cache0 64B **-13.7%**、缓存 64B **-20.1%**、迁移 64B **-40.2%**，区间全部上界 &lt; 0），而且**包含**同 peer 缓存路径。不要把 #71 当“更小更安全所以先合”的台阶：它在热路径上加指令，#68 在热路径上减指令。#68 仍然缺少与 #71 同等级的网络资格，不能改为合 #68；更不能把两份百分比相加或机械合并 `budget.rs` / `buf.rs`。

## 最终置信度（给合入决策用）

| 命题 | 置信度 | 依据 | 对合入的含义 |
|---|---:|---|---|
| 代码按设计删掉了 payload 的重复 peer 表查询，cache0 `put` 走直接析构 | **95%** | 源码 + LTO `put` 序言 `test %r15,%r15; je drop` | 实现与描述一致 |
| cache0 / 迁移的指令数下降是真的 | **95%** | 本机 Callgrind 三组、方向与代码一致 | 目标微基准有收益 |
| 隔离机 64B cache0/迁移 CPU 下降 4%–7% 可复现 | **90%** | 原机 4 个窗口、push/write、A/A ≤0.60% | 只对那台机、那种 probe 成立 |
| 同 peer 缓存 `write_with` 有约 1.5%–2% 成本 | **90%** | 原机加长窗口紧区间 + 本机 Ir +1.91% + 本机 write CPU 方向为正 | **生产热路径无收益，略亏** |
| 本云主机 12 对 CPU 能单独给 #71 做资格 | **25%** | 同二进制 A/A 出现 +9.8% 离群，区间宽到 ±5% | 本机 CPU 窗口只作方向参考 |
| 上下行 8 流网络 CPU/GB 非劣（+2%） | **40%** | down64 对已过；up 三个窗口都跨过 +2% | #65 验收未完成 |
| 合入后正式连接路径更快 | **15%** | 热路径 Ir 上升；网络未过 | 不应用“有优化”来合 |
| 现在合入 #71 | **10%（否）** | 热路径成本 + 网络缺口 + 正确性虽好但不够 | **保持草稿** |

“有没有收益”要按路径拆：

- **有：** cache0 小包、同 TxBuf 跨 peer 迁移（微基准 + 指令）。
- **没有（更可能略亏）：** 生产 shard cache16、同 peer、`write_with` relay。
- **未证明：** socket/TUN 上下行 CPU/GB 或吞吐。

## 纯代码分析

生产 diff 只有 `src/budget.rs` 与 `src/buf.rs`（约 +238/-9，其余是测试与证据）。没有新常驻字段、owner 堆对象或依赖。

1. **`MemoryLease::allocation_scope` / `assign_for_index`**  
   TX index 或 staged `IndexPeerReservation` 已经拿到目标 `Arc<LevelUse>`。后续 payload 不再走 `Budget::physical_peers.entry(peer)`（HashMap + `hash_one`）。同 peer 缓存命中继续用 payload 自己持有的 counter。`reserve_cached_peer` / `finish_cached_assignment` 与公开 `assign` 共用当前 `budget.peer_limit`（#67 的动态限额）。

2. **`BlockPool::get_for_index` 与 `max_cached == 0` 早退**  
   已保留 index 的后续 payload 走 shortcut。`put` 在 `max_cached == 0` 时直接让 block/lease 析构，跳过 eviction / `park_cached` / activity。这只影响 cache0；生产 `Shard` 是 cache **16**。

3. **`push` / `write_with` 的双路径**  
   `if index.is_some() { pool.get(...) } else { pool.get_for_index(...) }`。`index.is_some()` 表示**本轮正在新建 index**，仍走旧 HashMap 路径。warmup 之后、index 留在 `TxBuf` 上时走 shortcut。LTO 后 `TxBuf::push` 从 1627 指令涨到 2058：两条路径都被内联进同一个函数。`write_with` 的 `fill` 闭包抑制了部分内联，`BlockPool::get` 在 write probe 里仍是 532 指令的独立符号，且内部仍调用 `hash_one`。这就是缓存 `write_with` 多出来的控制流/寄存器压力。

4. **生产调用关系**  
   - Relay：`tokio_adapter` → `Shard::write_with_for_adapter` → `Conn::write_with` → `TxBuf::write_with`。  
   - 应用 `AsyncWrite`：先进行 adapter 队列 `push`，不是这条 TX block 热路径。  
   - `Conn::write` 用 `TxBuf::push`。  
   - 非空 TX 禁止换 peer，因此 **migrate 微基准不是连接稳态**。  
   - 首次写入仍走 `get`；优化不消除冷启动。

5. **正确性**  
   预留/回滚、global/port/peer、失败归因、`PeerId` 0/`u64::MAX`、cache0/cache32、两种 API、动态 `peer_limit` 收紧→拒绝→恢复，都有新测试。本机对 U 跑库测试 **232 passed / 0 failed / 4 ignored**（含 `tx_cached_payload_uses_current_peer_limit_after_reconfiguration` 与 `tx_resolved_payload_owner_rolls_back_migration_at_each_budget_level`）。shortcut 上用 `debug_assert` 核对 owner 与 `peer` 一致；release 不执行该断言，调用点目前看起来封闭。GitHub CI 全绿**不能**代替网络非劣。

未采用的后续候选 V（延迟 unwrap index）在原机未达到“收回缓存 write_with 1% 且上界&lt;0”的门槛，本轮不再评估。

## 编译与指令

统一 fat LTO、`codegen-units=1`、`panic=abort`、`incremental=0`。工具链是本环境的 **rustc 1.83.0**（原机 1.92.0）。二进制 SHA 见证据目录。U 与 U2 同源不同路径，hash 不同，符合“路径进入 crate metadata”的既有结论，所以做了 U/U2 A/A。

`BlockPool::put` 序言（U）：

```text
mov    0xc8(%rdi),%r15    ; max_cached
test   %r15,%r15
je     <drop Bytes + drop MemoryLease>
```

T 没有这道早退，栈帧 `0x128` 对 U 的 `0x58`；函数从 612 指令 / 91 个 `lock` 前缀降到 252 / 41。cache0 早退在机器码里是实的。

`get_for_index` / `assign_for_index` / `allocation_scope` 在 push probe 中全部内联（无独立符号）。HashMap/`hash_one` 仍出现在 `Budget::try_allocate_kind` 和新建 index 路径，与“只删掉已解析 owner 上的重复查询”一致。

Callgrind 指令数（进程总 Ir，25000 次，含 `portable_full_scan_v1` 校验；校验器两边相同，百分比会被校验摊薄，方向仍可用）：

| 场景 | T | U (#71) | U/T | W (#68) | W/T |
|---|---:|---:|---:|---:|---:|
| push cache0 64B | 45,891,324 | 41,468,577 | **-9.64%** | 37,718,817 | **-17.81%** |
| push migrate 64B | 43,594,198 | 40,171,767 | **-7.85%** | 26,372,151 | **-39.51%** |
| push pooled 64B | 30,444,476 | 31,571,821 | **+3.70%** | 23,521,850 | **-22.74%** |
| write cache0 64B | 50,168,951 | 42,993,978 | **-14.30%** | — | — |
| write pooled 64B | 34,022,004 | 34,672,166 | **+1.91%** | — | — |

指令数不受 CPU 频率左右。它独立确认：#71 在目标场景少干活，在同 peer 缓存场景多干活；#68 三类都少干活，幅度更大。

## 独立多次采样（本机）

6 个窗口，每窗口 9 case ×（1 warmup + 12 对）× 2 arm = 234 次运行，绑 CPU3，AB/BA 交错，bootstrap 95% 区间 seed65。小包 2e6 次，1400B 4e5 次。

**本机噪声：** 无 cpufreq、广告频率 2400 MHz、steal≈0。但 U/U2 A/A 的 `tx-alloc-pooled_64` 出现 **+9.8%** 单对，A/A 最大 |中位数| **1.44%**，若干格子区间跨过 +2%。12 对在这台 VM 上**分辨不了**原机那种 ±0.3% 的 4% 效应。push 两个窗口的 cache0 64B 甚至换了符号（-0.82% vs +0.99%）。因此本机 **CPU 时间不能单独给 #71 做 PASS/FAIL**。

相对地，`write_with` 两个窗口方向稳定，且与 Ir 一致：

| write 64B | 窗口1 U/T | 窗口2 U/T | 原隔离机窗口 |
|---|---|---|---|
| cache0 | **-2.88% [-4.27, -0.76]** 上界&lt;0 | **-4.44% [-6.87, -1.88]** 上界&lt;0 | -7.07% / -6.35% |
| migrate | -1.58% [-2.99, +0.82] | **-1.84% [-3.78, -1.06]** 上界&lt;0 | -3.96% / -4.24% |
| pooled（热路径同类） | +0.89% [-1.37, +4.51] | +1.46% [-0.93, +2.63] | +0.98% / +1.28%，加长约 +1.4% |

同一套二进制，#68 对 T 的 push 窗口幅度远高于噪声，区间全部上界 &lt; 0：

| push 64B | #68 / T 本机 12 对 |
|---|---|
| cache0 | **-13.74% [-14.47, -12.33]** |
| pooled | **-20.11% [-22.34, -18.82]** |
| migrate | **-40.20% [-40.58, -39.67]** |

这与 #68 自己报告的微基准同数量级。它说明：不是“这台机测不出 TX 差异”，而是 **#71 的净效应接近这台机的噪声，且在热路径上为负或接近零**。

库测试：U **232 passed**。未在本环境做 TUN/网络（无预授权隔离域，且原机网络资格已经 INCONCLUSIVE，重复宽区间实验没有决策价值）。

## 原隔离机证据怎么用

`root@38.207.178.131`、Rust 1.92、fat LTO、独立构建 A/A、两个 push/write 主窗口：目标 64B CPU 上界都 &lt; 0，方法合格。网络补测认真（固定 64 对、不拼接、A/A 不校正）。那些结论**仍然有效**，本轮没有推翻 cache0/迁移在那台机上的 CPU 收益。

本轮补上的是资格包没有独立给出的三件事：

1. 生产调用图是 cache16 + `write_with`，不是 cache0/migrate。  
2. 热路径在本机 LTO 二进制里指令上升。  
3. 同机同 probe 下 #68 的微基准大得多，且覆盖 pooled。

因此：原机微基准 PASS ≠ 应该合入。原机自己已经把总资格标成 INCONCLUSIVE。

## Trade-off

| | 合入 #71 | 保持草稿 | 改去合 #68 |
|---|---|---|---|
| 代码面 | 小，测试充分 | 无回归风险 | +651/-40，peer_cache / lease recycle / fused CAS，重叠不能加和 |
| cache0/迁移微基准 | 正收益 | 不落地 | 更大正收益 |
| 同 peer 缓存 `write_with` | **约 +1.5%–2% 成本** | 保持 #56 | 本机 CPU **-20%** 量级（仍非网络） |
| 网络 CPU/GB | 未过 +2% | 不引入未知 | 更缺同等网络包 |
| #65 | 不能关闭 | 正确 | 同样不能只凭微基准关闭 |
| 建议 | **否** | **是** | **否**（先独立正确性/网络） |

只摘 `max_cached==0` 的 `put` 早退对生产 cache16 几乎无影响，单独不值得一 PR。想收回缓存 `write_with` 成本，需要改掉双路径在命中上的额外分支，而不是再叠一层优化。原机候选 V 已经试过类似方向，未达预登记门槛。

## 建议

1. **#71 保持 Draft，#65 保持 open。** 合入条件至少包括：热路径（cache16/`write_with`/同 peer）指令或 CPU 不再变差，以及预登记的 up8 CPU/GB +2% 在隔离机上区间上界 ≤ +2%。  
2. **不要**把 CI 全绿、本机部分 write cache0 上界&lt;0、或原机 4%–7% 目标收益当成合入理由。  
3. 若继续做 #65：在**同一 head/base、同一 probe、同一隔离机**上正面比较 main / #71 / #68，只在额外收益可复现且正确性/非劣仍在时，从 #68 **选择性**吸收；禁止机械合并两个 `budget.rs`。  
4. #66 的 MTU1500 / 长期 RSS 仍分开。
