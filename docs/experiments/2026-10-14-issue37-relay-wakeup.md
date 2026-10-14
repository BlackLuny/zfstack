# Issue #37：relay 等待登记缺口

在 PR #40 的 `ef92e54` 上继续排查。#37 保持开放，PR 保持草稿；本次不合并。

## 用户提供的 rig 218 反馈

以下来自用户转述的测试汇总，原始 scratchpad 文件不在当前工作区，未独立审计。每个臂运行两次，base 为 `579b7c3`，candidate 为 `ef92e54`。

| 256 流 + 50 ms 场景 | 吞吐 Mbps | 请求超时 >3 s 或零字节流数（每次共 256 流） | CPU s/GiB |
|---|---:|---:|---:|
| ref `37c016d` | 1183（提供的汇总值） | 6 / 0 | 约 13 |
| base `579b7c3` | 883 / 924 | 93 / 73 | 约 13.3 |
| candidate `ef92e54` | 1390 / 1366 | 34 / 50 | 约 9.8 |

候选吞吐和 CPU 已改善，但超时/零字节比例仍约 16%，明显高于 ref 的约 1%。该计数是两类失败的并集，不能据此把所有失败都归为同一种中途停顿。三个臂都触发了严格 3 秒失败判据，`ok=false` 本身不是新的回归。

用户还报告：无额外延迟 256 流 base/candidate 为 1496/1602 Mbps；4 流为 2102–2106 / 2067–2079 Mbps；1500 空闲连接全部准入，附带下载为 1666/1581 Mbps，后者约 5% 差距仅测两次，尚不能确定是噪声或回归。第二 peer p99 约 91.6 ms，与 base 持平。

## 两个可确定复现的等待问题

### 1. 失败后读取释放序号

普通 pump 在写入前读取 `release_epoch`，relay readv 却在返回 `MemoryBlocked` / `QuotaBlocked` 后读取。以下顺序会永久错过唯一可用通知：

1. core TX 已空，upstream socket 仍有数据。
2. TX 分配失败并请求缓存回收。
3. 另一个 owner 释放内存，额度足够重试，但 relay 尚未登记。
4. relay 读取**释放之后**的序号并登记等待；登记认为没有发生竞态。

此时没有在途 TX 数据，后续 ACK 不能唤醒这个 relay；没有新的预算释放或 socket 事件时，它会一直停住。

`issue37_relay_does_not_miss_release_during_failed_allocation` 使用实际栈握手（模拟链路）及回环 upstream TCP socket，直接驱动生产 `relay_pump`。通过预算已有的 cache-holder Waker 在分配失败的回收请求内释放占用，确定性模拟另一个 owner 的释放时机，不修改生产分配路径或加入测试 hook。检查：失败已发生、TX 仍空、重试额度已足够，且该 relay 必须被标记重试。`ef92e54` 在最后一项失败，修复后通过并读取 socket 中的数据。

### 2. splice 接管队列没有预算等待者

relay 先发送应用在 splice 前排队的字节，但旧分支对所有写入失败直接 `break`，没有登记预算等待。当首次 core backing 分配失败且 core TX 为空时，同样没有 ACK 可以补救。

`issue37_splice_backlog_waits_for_memory_and_preserves_order` 保留此前排队的前缀，填满 Block 等级额度，再运行生产 pump。旧版物理等待者数为 0，期望为 1，测试失败；修复后一次外部释放即可标记重试。随后实际发包验证 `queued prefix:socket tail` 的顺序，真实客户端 ACK 后验证 core TX 和 sender 数均归零。

## 修复与证据边界

- 三个写入口共用 `track_tx_wait`，均在写入之前采样释放序号；失败后复用原有预算/物理等待协议。
- splice 前缀队列补齐等待登记及关闭处理；每次推进后同步剩余 adapter TX 数，避免排空后继续计为活跃 sender。
- 写入成功、核心关闭、仅等待 TCP 或 upstream socket 时清理过期等待项。

这两个缺口能独立导致 relay 停顿，且都已在旧代码上获得失败证据。**尚无实机逐流诊断或抓包把 rig 218 剩余的 34/50 条失败逐一归因到这些缺口，不能声称本次消除了所有实机停顿。** 稳态 64/256 流本地 splice 测试在此前版本也会通过，因此仍需相同 WG 矩阵复测。

## 本地验证

```sh
cargo test -p zfstack --release --features tokio,test-peer --lib
cargo test -p zfstack --release --features tokio,test-peer --lib issue37_splice_256 -- --ignored --nocapture
cargo test -p zfstack --release --features tokio,test-peer --test rx_capacity_lifecycle --test tx_default_trace --test issue37_throughput -- --nocapture
cargo clippy --workspace --all-targets --features zfstack/tokio
cargo build -p zfstack --no-default-features
cargo fmt --all -- --check
```

Rust 1.94.1、release：完整核心 243 项通过，4 项默认忽略。额外显式运行 256 流 splice，256 × 4 MiB 全部字节及 EOF 正确，最大连续相同进度样本数为 2（相邻采样间隔 250 ms），最终物理账本归零。停读邻居、晚到流、受压准入、双向 splice、FIN/reset、普通 Tokio 写入和原环形缓冲测试继续通过。

64 流确定性吞吐回归仍为两种写法均 79,831,168 B/s（638.65 Mbps），最大无进展间隔 58 ms；本次不修改 core 环形缓冲。生命周期/trace 集成 3 项、zfharness 14 项、zfbench all-features 18 项通过；无默认 feature 构建、zfbench release 构建、fmt、Clippy 通过，Clippy 保留既有警告。

下一轮 rig 比较应以 `ef92e54` 为对照，分别统计建连/首字节失败和收到数据后的停顿；保留第二 peer 成功率及 p99、goodput、CPU/GiB。若仍停顿，需要在停顿窗口关联单流的 core TX/unsent/pipe、snd_wnd、want_write、预算等待状态、upstream socket 进度和抓包，区分 TCP 重传、远端零窗口与 relay 未重试。
