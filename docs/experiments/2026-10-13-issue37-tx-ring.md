# Issue #37：复用 ACK 前缀，消除最低发送份额的压力阈值

**实机结论已更正：** 本文引用的早期 256 流 +50 ms 吞吐/停顿数据受到 netem 队列丢包污染，不再用于产品回归归因。修正台架后的对照见[更正报告](2026-10-14-issue37-netem-correction.md)；下文的确定性模拟与环形缓冲测试不受影响。

基于 #39 合入后的 `579b7c3`。本次是对用户提供的 rig 218 实测反馈的后续修复；本地没有该 rig、zfc 工作树或 `scratchpad/results/zfs-r2-579b7c3.tgz`，没有重跑或独立审计那份 WireGuard 测量。

## 早期实机反馈（已更正）及当时范围

用户转述的 Claude 报告使用 ref=`37c016d`、base=`939894a`、cand=`579b7c3`，每场景按 ref/base/cand/cand/base/ref 运行两次：

- 8 MiB port 下 1500 条空闲连接已全部准入，RX 信用停在 1 MiB；这部分修复有效。
- 256 下载、额外 50 ms 延迟：base 680/680 Mbps，cand 913/918 Mbps，ref 1220/1224 Mbps。
- 下载中途停顿超过 3 秒的连接数：base 50/37，cand 71/75，ref 1/9。连接已收到数 MiB，因此不能用建连时间解释。
- cand 端口峰值约 24.7/32 MiB，接近 #39 的第二块准入阈值；另一 peer 的探测 p99 约 91 ms，ref 探测超时。

阈值相关性本身不能证明全部长停顿的原因。本次消除可以在代码及确定性测试中验证的发送限制，并新增单流进度检查；仍需真实 WG 复测来判断 3 秒停顿是否解决。

## 实现

原 TX backing 只能向块尾追加。部分 ACK 后，逻辑份额虽有空位，续写却需要另一块 64 KiB backing；#39 在预计占用超过 75% 时禁止该分配，因此恢复整块停等。

单个完整 TX 块现在作为环形缓冲使用。`write` 和 relay `write_with` 直接把新字节放入已 ACK 的前缀，继续保留尚未 ACK 的字节；分段和重传仍最多返回两个 slice。最低份额只需一个完整 backing，移除 75% 判定及其缓存回收请求，逻辑份额和实际分配硬限继续生效。

2 KiB 首块仍可与一个完整块共存，小块 ACK 后进入同一环形路径。较大份额需要扩展时，已绕回的单块先重排一次再申请后续块；稳态单块写入不搬移旧数据。扩展失败和 readv 错误不提交新字节。缓冲排空或连接销毁后照常释放 backing。

## 确定性对照

Linux x86_64、Rust 1.94.1、release；对照为独立工作树中的 `579b7c3` 生产代码。双方 Cargo.lock SHA-256 均为 `d5e4eac18bd1ed92dcc061b7d71e00ffd7fd185c6d4d9dd23cc369e5550acce6`，使用独立 target 目录。

复用 #39 的固定 seed 3702 场景：64 流 / 8 MiB port、peer / 1 Gbit/s 链路，双向各 25 ms 延迟。预热 2 秒，测第 3 秒有效字节；每虚拟毫秒检查各流进度、不变量和物理占用。

| 写入大小 | #39 有效吞吐 | 本次有效吞吐 | #39 / 本次端口峰值 | #39 / 本次最大无进展间隔 |
|---|---:|---:|---:|---:|
| 1 KiB | 319.81 Mbps | 638.65 Mbps | 6,611,200 / 5,534,336 B | 93 / 58 ms |
| 64 KiB | 381.84 Mbps | 638.65 Mbps | 8,379,232 / 5,500,416 B | 119 / 58 ms |

有效字节分别为旧版 39,976,320 / 47,730,560 B/s，新版两种写法均 79,831,168 B/s。回归门槛提高到两种写法均超过 560 Mbps，并要求开始收数据后的每条流无进展间隔小于 1 秒。旧版按相同新测试失败；新版通过。独立单块测试也证明旧版部分 ACK 后实际分配了 131072 B payload，新版始终保持 65536 B。

显式运行的 256 流 / 32 MiB / 4 Gbit/s 扩展也通过：两种写法均为 319,309,968 B/s（2.554 Gbps），峰值分别 22,035,584 / 22,001,664 B，最大无进展间隔均为 58 ms。此表述只报告本次完整规模结果；没有用 64 流旧版数字冒充 256 流对照。两种规模均通过逐流字节、无队列丢包、硬额度与最终释放检查。

## Tokio splice 与验证边界

新增真实回环 TCP upstream → Tokio splice → 栈客户端测试。每条传输 4 MiB，客户端方向包排队延迟 50 ms，host egress backing 计入预算；64 流使用 8 MiB port，256 流使用 32 MiB port。每 250 ms 记录每条流字节，连续 8 个相同的活动期样本立即失败，同时要求所有连接收到完整正确字节及 EOF，关闭后物理账本归零。连接准备期和 EOF 后不计入中途停顿，但所有连接仍必须在 40 秒内完成。

64 流和显式启用的 256 流扩展均通过；本次针对性运行中，最大连续相同活动样本数分别为 1 和 3（后者相邻样本跨度 0.5 秒）。**旧版的同类 splice 测试也通过，没有在这里复现实机报告的 3 秒停顿**。这个测试验证本地传输推进和数据正确性，不是实机停顿修复证明，也不作为 CPU/GiB 或 WG goodput 对照。

验证命令：

```sh
cargo test -p zfstack --release --features tokio,test-peer --lib
cargo test -p zfstack --release --features tokio,test-peer --lib issue37 -- --include-ignored --test-threads=1 --nocapture
cargo test -p zfstack --release --features tokio,test-peer --test issue37_throughput -- --include-ignored --test-threads=1 --nocapture
cargo test -p zfstack --release --features tokio,test-peer --test rx_capacity_lifecycle --test tx_default_trace
cargo build -p zfstack --no-default-features
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --features zfstack/tokio
```

完整核心测试 241 项通过，4 项默认忽略（含新增的 256 流 splice）；针对性命令显式运行了该扩展。原停读邻居隔离、受压小请求信用、1500 空闲连接准入、丢包/重传/jumbo 分段和配置容量测试继续通过。新增环形跨边界读写、错误与扩展回滚、三层压力下无新分配即可续写和通知 Writable 的检查。

生命周期和 trace 集成测试 3 项、zfharness 14 项、zfbench 默认 14 项及 all-features 18 项、Python L2 8 项及 pareto 29 项通过。无默认 feature 构建、zfbench release 构建、fmt 和 Clippy 通过；Clippy 保留仓库既有警告。

后续 rig 218 已比较 `579b7c3`、`ef92e54`、`c1323fa`：修正台架后两个候选在延迟场景均无 >3 s 超时，吞吐较 base 约增加 10%，见[更正报告](2026-10-14-issue37-netem-correction.md)。该实测仍不是 1 KiB 小写入验证，不应与本地小写入模拟混用。
