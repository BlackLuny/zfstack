# 发送内存的连接级公平份额（zfc #674）

## 1. 问题

0005 的分级保证"至少一条连接能推进"，0006 保证受压时新连接能进来，但两者都不管既有连接之间怎么分。0005 §3 留下的缺口在 zfc #674 真机复现：同一 WG 端口上三条客户端流停止读取（对端零窗口）期间，正常流连续零进展 12–20 秒（对照组约 8 秒完成）。三轮采样一致，且 `admit_debt_*` 全为 0——不是准入问题。

根因是缺少随份额调整的、覆盖 core + adapter 的连接级发送内存约束：

- core 的 `sndbuf_limit`（conn.rs）只看 cwnd/inflight + prefetch，不看 port/peer 份额还剩多少、也不看同端口还有多少连接在发。
- adapter 按固定 `tx_cap` 接收应用写入，与 core 的占用互不相知；搬入 core 后队列又能继续补。
- `Budget::level()`（budget.rs）只看 global 用量，漏掉"全局很空、port/peer 已经分配失败"的情形（#674 正是这种）。

于是零窗口连接把 core TX（`sndbuf_limit` 上限可达 `max_snd_inflight + prefetch_max`）加 adapter TX 队列一起堆满整个 port 份额；正常连接的 `try_reserve` 持续失败，`QuotaBlocked` 停驻到停读结束为止。事后才收紧上限只能阻止增长，放不掉停读连接已经占住的字节；"旧连接先占满、正常连接后加入"时后来者拿不到任何房间。

## 2. 做法：连接级发送份额

### 2.1 统一计费口径

连接的发送占用定义为

```
occ(conn) = core tx.len()        # 含已发未 ACK（inflight）与未发字节
          + adapter_tx(conn)     # adapter TX 队列里还没搬进 core 的字节
```

`tx.len()` 本就含 inflight（TX 块租约随 ACK 释放），是天然的基准。adapter 驱动每轮 pump 后把队列长度上报给 core（`Shard::set_adapter_tx`），core 把它计入 `occ`。搬运动作本身（adapter 队列 → core TX 块）瞬态双份不超过一个 64KiB 块：core 先收字节、adapter 随后扣队列，`occ` 在搬运前后不变。

### 2.2 份额

预算按 port 与 peer 两级各维护一个 sender 计数：**当前持有发送字节的连接数**（`occ > 0`）。不计 `want_write` 但两手空空的连接，避免大量空闲/刚就绪的连接稀释份额。

```
cap(conn) = clamp(
    min over levels of share_blocks(level) × TX_BLOCK,
    floor = min(TX_BLOCK, port_limit, peer_limit),
    min(port_limit, peer_limit))

share_blocks(limit, senders) =
    (limit / TX_BLOCK_CHARGE − (senders + 1)) / (senders + 1)   # 饱和减法
```

- 同时受 port 与 peer 约束；peer 内按连接平分（分母是该 peer 的 sender 数）。
- 分母 `+1` 给下一条新连接恒留一份：N 条连接持续发送时合计持有 ≤ N/(N+1)·limit，晚加入者永远有房间（floor 触发时该保证退化，见 §4）。
- 份额按**物理整块**计：TX 块按 65600 B 计费且向上取整，直接按字节平分会让"逻辑有房、物理连一块都分不到"。每 sender 再预留一块覆盖"core 缓冲与 adapter 队列各欠一块"的搬运瞬态，保证兑现的份额在物理限额内也放得下。
- floor = TX_BLOCK（64 KiB）：份额再小也保一条连接的最小推进额度——cwnd 下限只有几 MSS，一块足够最小 inflight 加 adapter 队列周转。floor 越高，停读连接钉死的内存越多；floor=1 块时 zfc #674 场景（12 块端口、6 sender）的兑现份额连同连接状态与搬运瞬态才装得进物理限额，2 块必然超额认购，健康连接会在物理等待队列上经受秒级 stalls（实测 ~1.25 s）。
- 份额**始终生效**，不是压力来了才收紧——只有这样零窗口连接才没有机会先把份额占满。

### 2.3 core 执行点

`send_space = min(sndbuf_limit, cap) − core_tx`（adapter → core 搬运不重复扣除 adapter 队列；应用入口仍按 `cap − occ` 背压）。`write()` 区分两种受限：

- `sndbuf_limit` 受限（连接自己的窗口/拥塞）→ 照旧 `WouldBlock`，等自己的 ACK。
- 份额受限 → 返回 `QuotaBlocked`（语义即"预算级阻塞，等任意释放"），复用现有 release waitqueue 与 shard 的 release-epoch 重试，复用预算释放唤醒机制。

`occ` 归零或连接关闭时 `sender_del`；sender 减少意味着其他人的份额变大，故 `sender_del` 调 `wake_waiters` 让停驻者重评估。shard 用带 slot generation 的集合记录份额受限写者，将其 epoch 并入 `budget_wait_epoch()`；release-epoch 变化时只重查这个集合，空间恢复后重发一次 `Writable`，连接销毁/压缩时移除等待项，裸 API 用户不依赖 adapter 也能被唤醒。adapter 的 pump 已自行登记预算等待者，通过内部写入口避免为同一写入再登记一份 core 等待；ACK 触发的 Writable 机制保持不变。

### 2.4 adapter 执行点

在份额耗尽**之前**背压，而不是等 `try_reserve` 失败：

- driver 每轮 pump 计算 `room = cap − occ` 并发布到流状态；
- `poll_write` 的有效队列上限 = `min(tx_cap, room)`——应用写入在连接份额耗尽处停住，而不是在固定 `tx_cap` 处；
- 唤醒写者的条件同步改用有效上限。

**唤醒断链**：修复前的不变量是"app 停驻 ⟺ 队列满 ⟹ pump 必调 core `write()` ⟹ 写不动时 `want_write` 置位 ⟹ ACK 触发 `Writable` 重跑 pump"。`room` 让 app 在 core+queue 合计占满份额时停驻，**此时队列可能为空**——空队列的 pump 不调 `write()`，`want_write` 不置位，此后 core 被 ACK 排空也没有事件，pump 不再运行，`room` 永远不重发，写者睡死。两处补丁：pump 发布 `room` 后若写者仍停驻，经 `shard.note_write_parked` 在 core 置 `want_write`（语义成立：app 确实还有数据要写），排空时 `notify_writable` 必发 `Writable`；`poll_write` 停驻时 `ctl.mark` 触发一次即时 pump，消除"停驻瞬间 core 恰好已排空、此后再无 ACK"的竞态。floor 保证空连接的 `room` ≥ min(64 KiB, tx_cap) > 0，故"停驻 ⟹ core 非空 ⟹ 健康连接的 ACK 必来"成立；停读连接的停驻是设计意图，恢复读取时 ACK 推进、事件随之而来。

`room` 由 driver 在每轮 pump 重算（连接增减、ACK 排空、pump 搬运都会改变它）；core 的 `send_space` 是硬闸，`room` 只是提前背压，短暂的过期高估由 core 兜住。

### 2.5 压力感知补全

`Budget::level_for(peer)` 取 global、port、peer 三级各自用量/份额按同一组比例（3/8、5/8）计算后的最高档，替换接收路径上只看 global 的 `level()`：接收窗口增长（`rcvq` 自调）与 OOO 丢弃（conn.rs 收包路径）从此同时感知 port/peer 压力，堵住"全局很空、端口已满"时窗口继续涨、OOO 继续留的缺口。

端口/peer 压力不用于冻结当前接收窗口右沿：压力可能来自其他停读连接的 TX 或空闲缓存，即使接收应用持续读取也不会消退。`calc_window` 与发包窗口字段保留原有全局压力策略；局部压力只抑制 autotuning 增长及 OOO 缓存，实际 RX 分配仍受 global/port/peer 硬限约束。

## 3. 公平性与推进论证

- **无竞争**：单条连接的 cap = limit/2（sender=1，含预留的一份）。zfc 生产配置中 port 份额（默认 50% × 8% 系统内存）远大于任何现实 BDP，cap 不绑定，行为与现状一致；只有份额接近 BDP 的小额配置才用一半份额换晚加入保证（§4）。
- **N 条持续发送**：每条 ≤ limit/(N+1)，合计 ≤ N/(N+1)·limit，下一条恒有房间；连接排空/关闭后 sender 减少，cap 自动回升并唤醒停驻者。
- **零窗口**：停读连接持有 ≤ cap + 一个搬运块，吃不掉正常连接的份额；正常连接在停读窗口内持续推进。停读恢复后 ACK 排空，`occ` 下降，份额让出。
- **推进**：cap ≥ floor = TX_BLOCK 时连接总能再写一轮；floor 触发（sender 数 × floor > limit）时本层保证退化，由既有逻辑硬限（`try_reserve`）+ 物理等待队列兜底——本变更只收紧 P2/P4 数据缓冲的分配，不放宽任何上限，0005 §3 的推进论证（链条终止于出口）不受影响。
- **唤醒边界**：份额变大的事件（`sender_del`、任意 `release`）都会 `wake_waiters`；份额变小不需要唤醒（cap 只影响下一次写入）。

## 4. 代价与边界

- 单连接在份额上的突发上限从 `port_limit` 降到 `port_limit / 2`（预留一份给晚加入者）。生产配置无感；把 port 份额配到接近单流 BDP 的小额部署里，单流吞吐约减半——这是晚加入保证的价格。
- 连续多条新连接同时加入时，`+1` 只保证第一条的房间；其后的连接等第一轮排空。份额闲置上限为一份。
- floor 触发时（连接数 × 64 KiB + 连接状态 > 份额）公平保证退化为硬限 + 物理等待队列，与现状相同；物理上界不变。此时健康连接可能在物理等待上经历亚秒到秒级 stall（0005 的等待仲裁决定其长度），推进保证退化为"最终完成"。
- 只覆盖发送侧。接收侧（core RX + adapter RX）仍由窗口自调 + `try_reserve` + 份额硬限约束，连接级接收公平不在本变更范围。
- 重传/发送记录等元数据不走份额（P0/P1 不变）。

## 5. 诊断

- `PortStats` 新增 `share_blocked` 累计（write 因份额受限返回 `QuotaBlocked` 的次数）。
- 快照新增 `senders`（port 级 sender 数）与 `share_blocked`，供宿主诊断输出。首次 #674 真机复测使用旧 zfc 基线，diag 尚未打印这两个新字段，逐流采样仍是最终推进判据。

## 6. 验收

本地（tokio_adapter 测试，复用 #664 的 8 MiB / 819200 B 端口份额场景）：

- 停读复现：3 条流停读约 8 秒 + 3 条正常流。停读窗口内每条正常流持续有进展，且在停读结束前完成，接近无停读基线；修复前正常流在窗口内零进展。
- 晚加入：旧连接占住各自份额时新连接能写入并完成。
- 多 peer：peer 份额与 port 份额同时约束，peer 内按连接平分。
- 全局受压：global 份额绑定时各 port 仍各自推进。
- 零窗口恢复：停读结束后连接排空、sender 计数回落、份额让出。
- 硬预算：全程 `reserved ≤ high`、端口物理占用 ≤ port 份额（沿用现有断言）。
- 0005/0006 全部既有测试保持通过。

真机：vps3 复测 #674 场景（3 停读 + 正常流），要求正常流在停读结束前完成、接近 8 秒基线；diag 行观察 `share_blocked` 与 `senders`。


## 7. 本轮评审修复验证（2026-09-28）

本轮修复两处评审问题：局部 TX/缓存压力误冻结正常接收窗口，以及份额增长后 core API 没有 Writable 通知。补充读取/控制事件先于调度轮到达的通知竞态；份额等待集合只登记直接使用 core API 的写者，adapter 继续使用自己的预算等待者，避免双重登记。

- 正式回归在 `src/sim/tests.rs`：端口和 peer 分别受压时仍能完成 1 MiB 接收；其他 sender 退出后不用等待自身 ACK 即产生 Writable；无关释放不误通知、通知不重复、读取路径不吞通知、等待连接退出后清理。
- remote-compile `t-01M3M03SJJEBE9XTQKJPMYDGZ8`：6 项定向测试通过。包含上述回归、份额计算、单 peer 停读、晚加入，以及在仓库外验证快照中将现有多 peer 测试连续运行三次的包装测试。
- 中间版本曾出现多 peer 失败，修复版以最后一次定向测试结果为准；没有放宽停滞断言。
- 全量库测试未取得最终版本的通过结果；见下一节的真机复测结果。

补充核验：`t-01M3M063AY0A3T5RCNV6ZADM0Q` 默认 feature 的 `cargo check -p zfstack` 通过；`t-01M3M0710P05R2S1JF07S02MPZ` 的 `send_share_` 三项测试通过，Writable 回归同时覆盖直接进入 run 和先处理读取再进入 run 两条路径。GitNexus 未索引 zfstack，impact/detect-changes 不可用，本轮以源码调用点、diff 检查和定向运行测试补证。

## 8. #674 真机复测（2026-09-28）

使用 zfc `a560a4889` 基线和本设计对应的 zfstack 修复源码构建 `zf-worker`，在 worker 307 复用原复现场景：8 MiB 全局预算、819 KiB 端口份额，6 条 1 MiB 流按 128 KiB/s 读取，其中 3 条在 64 KiB 后停读 20 秒。两轮全读对照均 6/6 于 8.000–8.001 秒完成；三轮停读中，所有正常流也均于 8.000–8.001 秒完成，最长零进展仅 0.25–0.26 秒，停读流在 20 秒后恢复完成。修复前同场景 3/3 轮拖停正常流 12.75–20.23 秒。

候选二进制 SHA-256 `2cfef4c2c228ed14d2ebebbe682f919afac5196f0a835b72242d41985f37636a`，remote-compile task `t-01M3M3E3QRR90Z55731X09ZFBG`。原始采样、诊断及清理核验见 zfc `docs/issues/2026-09-28-zfstack-674-zero-window-stall/fixed-verification.md`。此验证覆盖该受限预算和单 peer 六流场景，不代表其他配置或长时间负载已验证。
