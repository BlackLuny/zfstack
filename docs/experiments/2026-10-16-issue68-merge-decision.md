# PR #68 合入决策：独立代码、指令与多次采样

日期：2026-10-16。对象：[PR #68](https://github.com/BlackLuny/zfstack/pull/68)（草稿，`cursor/issue65-tx-accounting-25d9`）。生产提交 `bf782a5`，可合并 HEAD `0a5bd0c`（已 merge 含 #67 的 main）。对照冻结点 `88a2e3f`（#67 后、#70 前）。跟踪 [#65](https://github.com/BlackLuny/zfstack/issues/65)，对照 [#71](https://github.com/BlackLuny/zfstack/pull/71)（`2694c14`）。当前 main 是 `8ade071`（#70 adapter），不改变 `budget.rs` / `buf.rs`。

本报告把**原隔离机微基准包**和**本机一次独立复测**（代码、fat LTO 反汇编、Callgrind 指令数、6 个 12 对 CPU 窗口、库测试）放在同一口径下，只回答一件事：**现在要不要合入 #68。**

原始证据：[`bench/results/2026-10-16-issue68-decision/`](../../bench/results/2026-10-16-issue68-decision/README.md)。图表总览：[PR #68 merge decision](https://cursor.com/canvas/store-4a976e9d-7179-4187-abb1-b32f2681fdd4/c0515c26-a6a4-4db0-8c68-fa10c03fd64e)。PR 自带的原机包仍在 `docs/experiments/2026-10-15-issue65-tx-accounting.md`。

## 结论

**不要合入 #68，不要关闭 #65，保持草稿。** 把 PR 描述里的「Closes #65」改成「Refs #65」。

和 #71 不同：#68 的微基准收益是**给对了场景**的。Shard 使用 `BlockPool::new(16)`，relay 走 `TxBuf::write_with`，连接级 peer 在非空 TX 上禁止切换。这条热路径上 #68 少干活，#71 多干活：

| 热路径同类（同 peer、缓存、`write_with`、64B） | #71 / T | #68 / T |
|---|---:|---:|
| Callgrind 进程 Ir | **+1.91%** | **-6.84%** |
| 本机 CPU 窗口 1 | +0.89% 区间跨 0 | **-6.52% [-8.67, -4.46]** 上界&lt;0 |
| 本机 CPU 窗口 2 | +1.46% 区间跨 0 | **-8.37% [-9.26, -6.30]** 上界&lt;0 |
| PR HEAD（含 #67）同口径 write | — | **-7.63% [-8.30, -6.84]** 上界&lt;0 |

cache0 / 跨 peer 迁移上的收益更大，且两个独立 push 窗口、两个 write 窗口、原隔离机包方向全部一致。这不是“这台 VM 测不出 TX 差异”——同一套 probe 下 #71 的热路径效应淹没在噪声里，#68 的热路径效应高过 A/A 一个数量级。

仍然不能合：

1. **#65 的网络非劣门槛没有同等资格包。** 原机 `up8`/`down8`/`short64` 是 `pareto_probe` 用户态格子，不是 TUN/socket 上下行 CPU/GB。预登记界是 CPU +2%、吞吐 -5%、P99 +5%。原机 `short64` 已是 `+1.04% [-0.76, +2.77]`，跨过 +2%。本环境没有预授权隔离域，未重跑 TUN。
2. **正确性风险面比 #71 大。** `peer_cache` 8 槽 + 备用 Arc、`MemoryLease` 外壳回收、`assign_with_index` / `park_cached_with_index` / `put_last_block` 融合 CAS。生产提交 `bf782a5` 在同 peer `assign` 上仍用租约快照 `self.peer_limit`；PR HEAD `0a5bd0c` 已改成 `budget.peer_limit` 并写回，本机把 #71 的动态限额回归插进 HEAD 后通过。PR **没有**带上那条测试。融合回退、槽位冲突、回收外壳只覆盖了作者自选的单测，不是 #65 要求的完整动态限额矩阵。
3. **不要把 #68 与 #71 的百分比相加，也不要机械合并两份 `budget.rs`。** 两边都在删热路径上的 HashMap / Arc 重复工作，重叠而不是正交。

## 最终置信度（给合入决策用）

| 命题 | 置信度 | 依据 | 对合入的含义 |
|---|---:|---|---|
| 代码按设计加了 peer_cache、租约外壳回收、index+block 融合、debt=0 时跳过 drain 读 | **95%** | 源码 + LTO `try_allocate_kind` 序言 `and $0x7` / `lea (%rax,%rax,2)` 索引 3 指针槽 | 实现与描述一致 |
| cache0 / pooled / migrate 的指令数下降是真的 | **95%** | 本机 Callgrind push+write，方向与代码一致 | 微基准少干活 |
| 本机 64B CPU 下降可复现，含生产同类 `write_with`+缓存 | **90%** | 2 个 push + 2 个 write + PR HEAD write，上界全 &lt; 0；W/W2 A/A \|中位数\| ≤ 1.38% | 热路径有微基准收益 |
| 原隔离机 1.92 fat LTO 64B 无缓存/迁移/同 peer 缓存 CPU 下降可复现 | **90%** | 原机 12 对、A/A、消融 P1 vs G | 只对那台机、那种 probe 成立 |
| PR HEAD 合入 #67 后仍保留微基准收益 | **90%** | WH write 9/9 上界&lt;0；WH 与 W 的 pooled write Ir 相差 187（25000 次） | 不是“修 #67 就把收益修没了” |
| PR HEAD 在缓存同 peer 路径上使用**当前** `budget.peer_limit` | **85%** | HEAD 源码三条 assign 路径都读 `budget.peer_limit`；插入 #71 回归后通过 | 生产提交单独看会回退 #67，HEAD 不会 |
| 上下行 8 流 TUN/socket CPU/GB 非劣（+2%） | **30%** | 原机只有 probe 格子；`short64` 跨 +2%；本轮无 TUN | #65 验收未完成 |
| 合入后正式连接路径更快 | **55%** | 热路径 Ir/CPU 下降，但没有网卡/P99 证据 | 微基准像优化，网络未证明 |
| 现在合入 #68 | **15%（否）** | 微基准够强，资格包不够 | **保持草稿** |

“有没有收益”要按路径拆：

- **有（微基准 + 指令，含热路径）：** cache0 小包、同 peer 缓存 `push`/`write_with`、同 TxBuf 跨 peer 迁移。
- **未证明：** socket/TUN 上下行 CPU/GB、吞吐、P99。
- **不要当成已交付：** 原 PR 正文里的「Closes #65」和「低于 #20 基线」。

## 纯代码分析

可合并 diff 是 `src/budget.rs` + `src/buf.rs` 约 **+647/-40**（相对 `88a2e3f`），外加实验证据。相对 #71 的 +238/-9，这是更大、更侵入的记账重构。没有新依赖。

生产调用关系与 #71 评估相同：

- Relay：`tokio_adapter` → `Shard::write_with_for_adapter` → `Conn::write_with` → `TxBuf::write_with`。
- `Shard` 的池是 `BlockPool::new(16)`，不是微基准的 cache0 / cache32。
- 非空 TX 禁止换 peer，因此 **migrate 微基准不是连接稳态**，只说明跨 peer 记账变便宜。
- 首次写入仍走未命中路径；稳态命中走缓存块 + 已有 `index_lease`。

四块机制：

1. **`Budget::peer_cache`（8 槽 × `counter` + 2 个 spare `Arc<LevelUse>`）**  
   索引 `(peer.0 as usize) & 7`。命中时 `try_allocate_kind` / `assign` / `prepare_index_peer` 不再走 `physical_peers` HashMap。`take_peer_arc` / `put_peer_arc` 用槽内备用克隆避免热路径 `LOCK XADD`。冲突（如 PeerId 0/8/16）覆盖槽位并回退 HashMap；`physical_peers` 仍是权威表。每 shard 常驻最多 24 个额外 Arc 句柄。原机消融把无缓存 64B 的大约 -14% 记在 P1（槽位 + 外壳回收 + 原子读精简）上。

2. **`PortMemoryHandle::recycled` + `MemoryLease::release_in_place`**  
   `released: bool` 吃掉原有尾部填充，作者声称 `size_of::<MemoryLease>()` 仍为 48B、`Block` 仍为 72B。`max_cached == 0` 或超额驱逐时 `drop_block` 先丢 `Bytes` backing，再 `release_in_place()` **立即**归还 global/port/peer 并停 drain，只留下 Arc 外壳。下一次 `try_allocate_tx_block` 复用外壳；失败时把外壳放回；`reclaim()` / Drop handle 清掉它。账本不靠延迟析构。LTO 里 `BlockPool::put` 从 613 指令 / 大栈帧变成 259 指令，并直接 `call drop_block`。

3. **融合 `assign_with_index` / `park_cached_with_index` / `put_last_block`**  
   空 `TxBuf` 且已有 `index_lease` 时，`push` / `write_with` 走 `BlockPool::get_with_index`：缓存命中一次预留 `index+block`，一次 `release_cache`。合并预留失败则退回两步（`allocation_lock` 下 TxIndex，再 TxBlock，失败回滚 Index），失败计数与唤醒按作者单测与旧语义对齐。`consume` 排空最后一块时 `put_with_index` 一次 `try_cache(block+index)`；cache cap 只够 block 时退回单独 `put` + `trim_empty_index`。这是 pooled / 热路径上相对 #56 的主要额外收益（原机 G/P1 同 peer 64B **-22.33%**）。`TxBuf::push` 静态指令从 1628 涨到 2931：融合路径被内联进同一个函数，**静态变大、动态 Ir 变小**。

4. **`reserve_physical` / `headroom` 原子读精简**  
   `debt == 0` 时不读 `drain`；非 `Bulk` 不读 `admit_reserve`；`egress_reserve` / `admit_minimum` 每轮快照一次。与 T 的 `level_limit` 在 `debt==0` 时本来也不用 drain 的语义一致。只加速未缓存的层级预留，不是 pooled `assign` 的主因。

**#67 动态限额。** T 的 `MemoryLease::assign` 已用 `budget.peer_limit` 并回写租约。`bf782a5` 同 peer `assign` / `assign_with_index` 快路径仍用 `self.peer_limit`，收紧配额后可能继续放行旧额度。`0a5bd0c` 把三条路径都改成 `let peer_limit = budget.peer_limit` 并 `self.peer_limit = peer_limit`。本机在 HEAD 上插入 #71 的 `tx_cached_payload_uses_current_peer_limit_after_reconfiguration`（cache0/32 × push/write_with × PeerId 0/`u64::MAX`）**通过**。PR 本身没有这条测试，也没有 #65 要求的“先放宽、运行中收紧、拒绝、恢复后重试”矩阵。

未在本轮独立量化：高 peer 基数下槽位抖动的 CPU、spare 耗尽后的 Arc RMW、融合路径在部分尾块 + 非空 TX 拒绝迁 peer 上的每条回退。这些是**需要证明的风险面**，不是已确认的账本 bug。GitHub CI 全绿不能代替网络非劣。

## 编译与指令

统一 fat LTO、`codegen-units=1`、`panic=abort`、`incremental=0`。工具链是本环境的 **rustc 1.83.0**（原机 1.92.0）。W 与 W2 同源不同路径，hash 不同。WH 是 PR HEAD，pooled write Ir 与 W 相差 187 / 31,696,014（&lt; 0.001%）。

`Budget::try_allocate_kind` 序言（W，fat LTO）在 HashMap 之前做直接映射：

```text
and    $0x7, %eax              ; slot = peer_id & 7
lea    (%rax,%rax,2), %rax     ; * 3 pointers (counter + 2 spares)
mov    0x88(%rsi,%rax,8), %r11 ; slot.counter
test   %r11, %r11
je     <HashMap miss>
cmp    %r12, 0x10(%r11)        ; counter.peer_id == peer
je     <hit, skip hash_one>
```

T 的同一函数开头是 `call hash_one` + hashbrown probe。peer_cache 在机器码里是实的。WH 序言相同。

Callgrind 指令数（进程总 Ir，25000 次，含 `portable_full_scan_v1`；校验器两边相同，百分比会被校验摊薄，方向仍可用）：

| 场景 | T | U (#71) | U/T | W (#68) | W/T |
|---|---:|---:|---:|---:|---:|
| push cache0 64B | 45,891,324 | 41,468,577 | **-9.64%** | 37,718,817 | **-17.81%** |
| push pooled 64B | 30,444,476 | 31,571,821 | **+3.70%** | 23,521,850 | **-22.74%** |
| push migrate 64B | 43,594,198 | 40,171,767 | **-7.85%** | 26,372,151 | **-39.51%** |
| write cache0 64B | 50,168,951 | 42,993,978 | **-14.30%** | 45,892,916 | **-8.52%** |
| write pooled 64B | 34,022,004 | 34,672,166 | **+1.91%** | 31,696,014 | **-6.84%** |
| write migrate 64B | 47,870,505 | — | — | 34,771,421 | **-27.36%** |

指令数不受 CPU 频率左右。它独立确认：#68 六格都少干活，且**包含**生产同类 write+pooled；#71 在那一格是多干活。

静态大小提醒：W 的 `BlockPool::get`（write probe）860 指令 / 137 lock，T 为 513 / 63。融合路径让 `get`/`push` 变胖，靠少执行的 CAS 和少 HashMap 赚钱，不是靠更小的函数。

## 独立多次采样（本机）

6 个窗口，每窗口 9 case ×（1 warmup + 12 对）× 2 arm = 234 次运行，绑 CPU3，AB/BA 交错，bootstrap 95% 区间 seed65、10000 次。小包 2e6 次，1400B 4e5 次。`portable_full_scan_v1`。

**本机噪声（A/A W vs W2）：** 9 格中位数绝对值最大 **1.38%**（`tx-alloc-pooled_1`），其余 ≤ 0.96%。两格偶然上界&lt;0，幅度 &lt; 1.4%，不能当成优化。这比评 #71 时 U/U2 出现 +9.8% 离群干净。12 对在这台 VM 上**分辨得了** 5%–40% 的 #68 效应，**分辨不了** #71 那种 ±1% 热路径成本。

64B 主格（上界&lt;0 才标收益）：

| 64B | push 窗口1 W/T | push 窗口2 W/T | write 窗口1 W/T | write 窗口2 W/T | write PR HEAD WH/T |
|---|---|---|---|---|---|
| cache0 | **-13.74% [-14.47, -12.33]** | **-14.86% [-15.42, -13.89]** | **-4.87% [-9.55, -3.97]** | **-5.49% [-7.68, -2.46]** | **-4.97% [-5.57, -3.99]** |
| pooled | **-20.11% [-22.34, -18.82]** | **-19.29% [-20.30, -17.83]** | **-6.52% [-8.67, -4.46]** | **-8.37% [-9.26, -6.30]** | **-7.63% [-8.30, -6.84]** |
| migrate | **-40.20% [-40.58, -39.67]** | **-39.98% [-40.61, -39.23]** | **-30.67% [-31.18, -29.12]** | **-30.57% [-32.08, -28.46]** | **-31.39% [-32.65, -30.57]** |

全部 5×3 上界 &lt; 0，+2% 守卫全部 PASS。write 幅度小于 push，因为 `write_with` 的 fill/copy 摊薄记账占比；方向与 Ir 一致。1B / 1400B 九格同样上界&lt;0（见证据 `windows/*/table.txt`）。

库测试：W（`bf782a5`）**232 passed / 0 failed / 4 ignored**；WH（`0a5bd0c` + 插入的动态限额回归）**234 passed / 0 failed / 4 ignored**。含 `issue65_peer_cache_slot_collisions_*`、`issue65_recycled_lease_*`、`issue65_fused_index_and_block_migration_*`、以及插入的 `tx_cached_payload_uses_current_peer_limit_after_reconfiguration`。未在本环境做 TUN/网络。

## 原隔离机证据怎么用

原包：Rust 1.92、fat LTO、seed22、5000 次 bootstrap、相对 #56 基线 `3459562` / `ebcd6dd`（TX 生产代码当时与 #67 之前的 main 一致）。64B：

- 无缓存 **-20.51% [-20.73, -19.48]**
- 同 peer 缓存 **-20.57% [-21.66, -16.87]**
- 交替 peer **-41.94% [-42.62, -41.36]**

消融：P1（槽位+回收+原子读）拿走无缓存的大部分；G 相对 P1 的融合操作拿走同 peer 缓存和迁移的大部分。A/A G2/G 最大中位数约 1.1%。保护性格子里 `up8` **-1.68% [-3.52, -1.22]** 是 **probe**，`short64` **+1.04% [-0.76, +2.77]** 不能排除 +2%。

本机 rustc 1.83 的无缓存 64B 约为 -14% 而不是 -20%，工具链不同，**禁止把两套百分比拼成一条曲线**。同 peer 缓存和迁移与原机同量级。本轮补上的是资格包没有独立给出的三件事：

1. 生产调用图是 cache16 + `write_with`，#68 在这一格 Ir **-6.84%**、CPU 约 **-7%**，#71 是 **+1.9% Ir**。
2. PR HEAD 合入 #67 后收益还在。
3. 同一台评测机证明：不是“测不出 TX”，而是 #71 热路径没有可交付收益、#68 有微基准收益。

因此：原机微基准 PASS ≠ 应该合入。原机自己也没有 TUN 非劣包。Owner 在 2026-10-16 的 follow-up 要求保持 Draft，本轮独立复测**同意**。

## Trade-off

| | 合入 #68 | 保持草稿 | 改去合 #71 |
|---|---|---|---|
| 代码面 | +647/-40，peer_cache / 回收 / 融合 CAS | 无回归风险 | 更小，热路径加指令 |
| cache0/迁移微基准 | 大正收益 | 不落地 | 中等正收益，给错场景 |
| 同 peer 缓存 `write_with` | **Ir -6.8%，CPU 约 -7%** | 保持 #56/#67 | **约 +1.5%–2% 成本** |
| #67 动态限额 | HEAD 已接上，缺专用测试矩阵 | 已在 main | 有专用测试 |
| 网络 CPU/GB | 未过 +2% | 不引入未知 | 同样未过 |
| #65 | 不能关闭 | 正确 | 不能关闭 |
| 建议 | **否** | **是** | **否**（见 #71 决策） |

若继续做 #65：在**同一 head/base、同一 probe、同一隔离机**上跑 TUN mixed/connect/短流/up8/down8，区间必须排除 CPU +2% / 吞吐 -5% / P99 +5%。通过之后再考虑合 #68，并把「Closes #65」放回去。不要 cherry-pick #71 的 shortcut 叠到 #68 上，除非新组合单独跑出额外收益。#66 的 MTU1500 / 长期 RSS 仍分开。

## 建议

1. **#68 保持 Draft，#65 保持 open。** 合入条件至少包括：隔离机上预登记的网络非劣，以及把动态 `peer_limit` 回归（含 `assign` / `assign_with_index` / 回收外壳）写进 PR 自己的测试。
2. **不要**把 CI 全绿、本机 write pooled 上界&lt;0、或原机 -20%/−42% 目标收益当成合入理由。微基准已经足够证明“记账变便宜”，不够证明“#65 完成”。
3. 相对 #71：#68 是更像样的 #65 候选，不是可以先合的台阶。两份百分比不能加减。
4. 建议 PR 正文把 **Closes #65** 改成 **Refs #65**。
