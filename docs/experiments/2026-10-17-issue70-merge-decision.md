# PR #70 合入后决策：独立代码、指令与多次采样（保留 / 回退）

日期：2026-10-17。对象：[PR #70](https://github.com/BlackLuny/zfstack/pull/70)（**已于 2026-10-16 15:01 UTC 合入** `8ade071`）。对照冻结点 `9a06056`（#70 的 merge parent，#74 之后）。跟踪 [#48](https://github.com/BlackLuny/zfstack/issues/48)（仍 OPEN）。TUN 大流证据复用 [PR #77](https://github.com/BlackLuny/zfstack/pull/77)，不重跑、不调用 `qa-perf.yml`。

#70 已经在 main 上。本报告把**原隔离机微基准包**、**PR #77 TUN 诊断**和**本机一次独立复测**（代码、fat LTO 反汇编、Callgrind 指令数、6 个 12 对 CPU 窗口、库测试）放在同一口径下，只回答一件事：**现在要不要回退 #70，以及能不能关 #48。**

原始空闲包：[`docs/experiments/2026-10-15-issue48-readiness/`](2026-10-15-issue48-readiness/README.md)。本机证据：[`bench/results/2026-10-17-issue70-decision/`](../../bench/results/2026-10-17-issue70-decision/README.md)。图表总览：[PR #70 keep/revert decision](https://cursor.com/canvas/store-4a976e9d-7179-4187-abb1-b32f2681fdd4/71ce8e16-e1f8-4b55-8dc8-ceb523485694)。

## 结论

**保留 #70，不要回退。#48 保持 open，不要关。**

和 #71 / #68 不同：#70 是 **adapter 就绪门**，不是 TX 记账。生产 diff 相对 `9a06056` 是 `tokio_adapter.rs` +27/−1、`conn.rs` +5/−1、`shard.rs` +5，外加 4 条 `issue48_*` 回归。Relay 热路径仍然是 `write_with`；改动只发生在 `relay_pump` 的 socket→core 段之前。

空闲路径上 #70 少干了 `write_with` 整段准备（配额预留、`TX_FIRST_WRITE_CHARGE` 块/索引、`try_read_vectored` WouldBlock）。就绪大流上多一次每 pump 的 `poll_read_ready`。这两件事要分开看：

| 路径 | 本机独立复测 | 原包 / TUN |
|---|---|---|
| 空闲 25 万次 pump CPU | **−62.6% / −63.0%**，两窗口上界都 &lt; −62.4% | 原包几何均值比 0.3417（约 −65.8%），非 fat LTO |
| 空闲 TX ledger | 每样本 **+66,112 → 0** | 原包相同；`TX_FIRST_WRITE_CHARGE = 65536+8×72` |
| 空闲 Callgrind 进程 Ir | **−65.77%**（964.5M → 330.2M） | 与原包 CPU 比同量级 |
| 双向 splice 单测 CPU | **+2.18% / +1.88%**，下界 &gt; 0，+2% 守卫 INCONCLUSIVE | 原包 0.9322，区间跨 1 |
| TUN 持续可读 8×64MiB CPU/GB | 未重跑 | PR #77：下行 −2.27% [−6.49, 0]，噪声内无额外成本 |
| TUN mixed P99 | 未重跑 | PR #77：**INCONCLUSIVE** |

不要把空闲 −63% 写成生产吞吐收益。也不要因为 splice 单测约 +2% CPU 就回退：那是 35ms 量级的测试进程 CPU（含 setup），TUN 大流才是就绪路径的保护证据。

## 最终置信度（给保留 / 回退决策用）

| 命题 | 置信度 | 依据 | 对决策的含义 |
|---|---:|---|---|
| 代码按设计在每 pump 一次 `poll_read_ready` 之后才 `write_with`；Pending 清 waiter；静默上游仍观察 core close | **95%** | 源码 + LTO 里 inlined `Registration::poll_ready`、状态位图 `0x26`（SynReceived\|Established\|CloseWait）、`fin_off` 判定 | 实现与 #48 描述一致 |
| 空闲路径不再走 `try_reserve` / `BlockPool::get` / `try_read_vectored` | **95%** | Callgrind：这些符号只出现在 T 的 flat profile | 归因成立，不是计时巧合 |
| 本机空闲 CPU 下降可复现 | **95%** | 两窗口 12 对，上界全 &lt; −62%；每样本 ledger 合同成立 | 空闲收益是真的 |
| 空闲指令数下降与原包 0.3417 同量级 | **90%** | 本机进程 Ir −65.77%；原包是非 LTO / rustc 1.98，禁止把百分比拼曲线 | 方向一致 |
| 就绪路径多一次 poll（静态约 +30 指令） | **90%** | C `relay_pump` 2151 vs T 2121；C 另有 24 指令的 `poll_read_ready` | 就绪路径有成本，不是零 |
| splice 单测 CPU 无回退 | **40%** | 两窗口中位约 +2%，下界 &gt; 0；A/A 中位 −0.88%、有一对 +28% 离群 | 不能当就绪路径收益，也不能当回退理由 |
| TUN 大流 CPU/GB 无额外成本 | **70%** | 复用 PR #77，本轮未重跑；A/A 已有 ±7% | 保留的主要就绪保护 |
| mixed / 短流 P99 已保护 | **25%** | PR #77 INCONCLUSIVE | **不能关 #48** |
| 热路径无效 fill 计数已交付 | **10%** | 生产 pump 没有这些计数器 | #48 验收仍缺 |
| **回退 #70** | **10%（否）** | 空闲收益大且可归因；功能回归在；TUN 未见大流回退 | **保留** |
| **关闭 #48** | **15%（否）** | P99 / 计数器 / 校准节点仍缺 | **保持 open** |

“有没有收益”按路径拆：

- **有（空闲 pump + 指令 + ledger）：** ACK/core 唤醒但上游无数据时，不再准备 64KiB TX backing。
- **就绪大流：** 多一次 poll；本机 splice 单测约 +2% CPU；TUN 大流 CPU/GB 落在噪声里，**不宣称生产加速，也不宣称必须回退**。
- **不要当成已交付：** 原 PR 的性能资格、#48 的无效 fill 计数、mixed P99 保护。

## 纯代码分析

可合并生产 diff（相对 `9a06056`）约 **+140/−2** 行源码，外加实验文档。没有新依赖，不改 `budget.rs` / `buf.rs`、线格式、默认预算或公开 API。

`relay_pump` 在抽干 splice backlog 之后、进入 `write_with_for_adapter` 之前增加一次就绪门：

1. **每 pump 一次 `poll_read_ready`，不是每个 readv 一次。**  
   `socket_read_ready` 是循环外的布尔值。持续可读大流仍然在 `while` 里反复 `write_with` + `try_read_vectored`，不在内环重新 poll。这是有意把空闲跳过和就绪内环分开。

2. **过期 Ready 仍落到原来的 WouldBlock 分支。**  
   门返回 Ready 后若 `try_read_vectored` 得 WouldBlock，继续 `poll_read_ready` 停车。`issue48_stale_readiness_parks_then_observes_eof` 覆盖这条。

3. **Pending 时 `clear_tx_wait`。**  
   空闲 socket 不再把过期的 quota waiter 留在 global 上，避免随后的无关 release 把 relay 吵醒再走一遍无效准备。`issue48_idle_socket_clears_obsolete_quota_wait` 覆盖。

4. **`Conn::accepts_writes` / `Shard::accepts_writes_for_adapter`。**  
   谓词就是原来 `admit_write` 的状态检查：`Established | CloseWait | SynReceived` 且 `fin_off.is_none()`。Pending 时若 core 已经不能写，置 `sock_eof`，不依赖静默上游再给一个可读事件。`issue48_closed_core_does_not_wait_for_silent_socket` 覆盖。这是关闭进展合同，不是性能优化。

空闲浪费的根因在 `TxBuf::write_with`：`RELAY_DOWN_READ = 256KiB` 大于 `SMALL_TX_BLOCK`，第一次就会按 `TX_BLOCK` 取块并准备 8 槽索引。`TX_FIRST_WRITE_CHARGE = 65536 + 8 * size_of::<Block>() = 66112`。fill 返回 WouldBlock 后这块空 backing 留在 `TxBuf` 里，ledger 钉在 +66,112，直到连接结束。#70 在 WouldBlock 之前就不调用 `write_with`，所以 ledger 不变。

就绪路径的代价是每次 `relay_pump` 多一次 `poll_read_ready`（Tokio 已就绪时通常是 reactor 标志，不一定是新的 epoll 系统调用）。ACK 驱动但上游无数据时，这笔 poll 换掉整段 `admit_write` + 取块 + `try_read_vectored`。持续可读时，它是纯附加。

**与 #68 / #71 正交。** 那些 PR 改 `budget.rs` / `buf.rs`。#70 不碰记账。不要把空闲 −63% 和 #68 的 write pooled −7% 相加。

未在本轮独立量化：生产 worker 上 ACK 风暴的绝对 CPU、WireGuard/L2、热路径 fill 计数器。GitHub CI 全绿不能代替 P99 资格。

## 编译与指令

统一 fat LTO、`codegen-units=1`、`incremental=0`。库测试含 panic 可观察用例，**没有** `panic=abort`（生产 adapter 默认 unwind 也不是 abort）。工具链是本环境 **rustc 1.83.0**（原空闲包 1.98.1，TUN 包 1.94.1）。评估树为了离线编译把 workspace `members=[]`、把测试里的 `is_multiple_of` 改成 `%`；生产树未改。C 与 C2 源码相同、debuginfo=0，SHA-256 **完全相同**，A/A 只测宿主噪声。

`relay_pump` 静态指令（一个 LTO 单态，窗口截到下一个符号）：

| | T | C | Δ |
|---|---:|---:|---:|
| `Driver::relay_pump` | 2121 | 2151 | **+30** |
| 独立 `TcpStream::poll_read_ready` | 无此符号（WouldBlock 路径直接 `Registration::poll_ready`） | 24 指令 thunk | 门的 call 目标 |

C 在丢掉 TX 队列锁之后、`try_read_vectored` 之前，内联了门：

```text
cmpb   $0x0, -0x6(%r14)             ; sock_eof?
jne    <skip read>
xor    %ecx,%ecx                    ; ready interest = read
call   Registration::poll_ready
cmp    $0x2, %al                    ; Pending
je     ...
cmp    $0x3, %al                    ; Ready(Ok)
; Pending: xchg driver_tx_wait / remove_waiter   ; clear_tx_wait
mov    $0x26, %edx
bt     %ecx, %edx                   ; state in {SynReceived, Established, CloseWait}
cmpl   $0x1, (%rax)                 ; fin_off
movb   $0x1, -0x6(%r14)             ; sock_eof = true
```

`0x26 = bits 1,2,5` 正好是 `State::{SynReceived, Established, CloseWait}`。`accepts_writes_for_adapter` 被内联进 `relay_pump`，没有独立符号。门在机器码里是实的。

Callgrind 指令数（进程总 Ir，含测试 setup；250k idle pump）：

| | T | C | C/T |
|---|---:|---:|---:|
| 进程 Ir | 964,507,291 | 330,191,005 | **−65.77%** |
| `relay_pump` self Ir | 167,000,000（668 / pump） | 57,250,000（229 / pump） | **−65.72%** |

T 的 flat profile 里有、C 里消失的符号（空闲归因）：

| 符号 | T Ir | C |
|---|---:|---|
| `BuildHasher::hash_one` | 163.8M | 45.5M（setup 残留） |
| `Budget::try_reserve` | 25.3M | **无** |
| `BlockPool::get` | 23.0M | **无** |
| `BlockPool::put` | 15.8M | **无** |
| `Budget::cancel_reserve` | 14.0M | **无** |
| `MemoryLease::assign` / `park_cached` | 28.5M / 25.0M | **无** |
| `TcpStream::try_read_vectored` | 4.5M | **无** |

`Registration::poll_ready` 两边都是 30.25M：T 在 WouldBlock 后 poll，C 在门上 poll，次数同量级。C 并没有省掉 poll，省掉的是 poll **之后** 的记账和 backing。

指令数不受 CPU 频率左右。它独立确认：空闲少干活；就绪门在静态码里变长。

## 独立多次采样（本机）

6 个窗口，每窗口 1 warmup + 12 对 × 2 arm，绑 CPU3，AB/BA 交错，bootstrap 95% 区间 seed65、10000 次。CPU 是子进程 `ru_utime+ru_stime`（与原 issue48 包相同），含测试 setup/teardown。空闲测试注入两边相同的 `issue48_measure_idle_pump`（250k `driver.pump`）。

**本机噪声（A/A C vs C2，二进制 SHA 相同）：** 空闲中位 −0.38% [−1.33, +2.10]；12 对里有一对 +28%（28ms 量级的测试对一次调度抖动敏感），中位仍贴 0。splice A/A 中位 −0.88% [−1.85, +1.70]。12 对在这台 VM 上**分辨得了** 60% 的空闲效应，**分辨不了** 1% 量级的就绪路径成本——和评 #71 时热路径 ±1% 被噪声淹没是同一台机器的同一结论。

| 窗口 | 中位 % | 95% 区间 | geo 比 | ledger Δ | +2% 守卫 |
|---|---:|---|---:|---|---|
| idle w1 C/T | **−62.62** | [−62.88, −62.45] | 0.375 | +66112 → 0 | PASS（收益） |
| idle w2 C/T | **−63.00** | [−63.32, −62.65] | 0.371 | +66112 → 0 | PASS（收益） |
| idle A/A C/C2 | −0.38 | [−1.33, +2.10] | 1.015 | 0 → 0 | INCONCLUSIVE |
| splice w1 C/T | +2.18 | [+1.01, +4.29] | 1.026 | — | INCONCLUSIVE |
| splice w2 C/T | +1.88 | [+0.75, +4.17] | 1.022 | — | INCONCLUSIVE |
| splice A/A C/C2 | −0.88 | [−1.85, +1.70] | 0.998 | — | PASS |

空闲 12/12 对 ledger 合同成立。本机空闲几何比约 0.37，原包 0.3417：本机把 setup 打进 rusage，Callgrind 进程 Ir 比 0.342 更接近原包。**禁止**把 0.37 和 0.34 平均成一条生产曲线。

库测试：C fat LTO 二进制 **284 passed / 0 failed / 6 ignored**；`issue48_*`（含注入的 measure）**5 passed**。未在本环境重跑 287 项资格测试，也未调用 `qa-perf.yml`。

## 原隔离机证据和 TUN 怎么用

原空闲包：Rust 1.98、非 fat LTO 的 release 测试二进制、8 个 ABBA 块。空闲 CPU 比 0.3417 [0.3331, 0.3510]；splice 0.9322 [0.8670, 1.0014]。首次共享 target 目录的测量已作废。本轮用独立 target、独立 SHA、fat LTO，空闲方向和 ledger 与原包一致。

[PR #77](https://github.com/BlackLuny/zfstack/pull/77) 在本同一类 Cloud Agent VM 上用未改的 `issue23_tun_case.py`（`--tun-proxy --splice`）测了 `88a2e3f7` vs `b98c5c33`（#70 合入前的 PR HEAD，adapter 与 `8ade071` 同类）。本轮**复用、不重跑**：

- 下行 CPU s/GB −2.27% [−6.49, 0]
- 上行 CPU s/GB −0.69% [−2.76, 0]
- 下行 mixed P99 +9.37% [−13.5, +24.9] **INCONCLUSIVE**（A/A 已 ±8–33%）
- 不要把 idle −65.8% 外推到这条大流

Owner 在合入前要求补大流/mixed 保护，TUN 包明确写了「不要作为性能 PASS 合入」，随后 #70 仍被合入。本轮独立复测同意功能方向和空闲收益，也同意 **#48 的性能资格仍未完成**。合入已经发生，对应动作是**保留代码、保持 issue 打开**，不是再开一次合入投票。

## Trade-off

| | 回退 #70 | 保留 #70，#48 打开 | 保留并关闭 #48 |
|---|---|---|---|
| 空闲 ACK 唤醒 | 恢复每次 66,112 B backing 和 ~3× CPU | **保持少干活** | 同样 |
| 就绪大流 | 少一次 poll | 多一次 poll；TUN 未见 CPU/GB 回退 | 把 INCONCLUSIVE 的 P99 当成已过 |
| 关闭进展 | 静默上游可能卡住 upload tail | `accepts_writes` 门已有回归 | 同样 |
| #48 验收 | 空闲缺口退回去 | 空闲已交付；计数器/P99/校准节点仍缺 | **错误** |
| 与 #68/#71 | 无组合 | 正交，不要加百分比 | 同样 |
| 建议 | **否** | **是** | **否** |

若继续做 #48：在同一 head/base 上补无效 fill/readv 计数（诊断与计时分离），以及校准域上的 mixed P99。不要为了 #70 再发明一套 QA 框架，也不要把本机 splice 单测 +2% 登记成非劣门槛。

## 建议

1. **#70 留在 main，不要 revert。** 空闲路径的收益、归因和回归测试足够支撑这次 adapter 改动。
2. **#48 保持 open。** 缺热路径计数、mixed P99 保护、校准节点 enforce。合入 adapter ≠ 关闭 issue。
3. **不要**把本机空闲 −63%、Callgrind −65.8%、或原包 0.3417 写成生产 goodput 收益。
4. splice 单测约 +2% CPU 记为就绪路径的微成本信号，用 PR #77 的 TUN 大流（噪声内无额外 CPU/GB）约束它，不够作为回退令。
5. 与 #68 / #71 组合时只做必要回归，禁止把不同层次的百分比相加。
