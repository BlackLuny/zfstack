# Issue #37：空闲准入与最低发送份额回归

基于 `5836346`（PR #38 合入后的 main）；该版本仍包含 issue #37 报告的 RX 信用和最低发送份额实现。验证使用云环境 Linux x86_64、Rust 1.94.1、release 构建，两份源码使用相同 Cargo.lock。对照源码与修复源码分别编译；结果是确定性 sans-IO 模拟，**不是 WireGuard L2 实测**。

## 修复

- RX 信用分别最多占 global / port / peer 的 1/8。超限或无法预付时连接仍准入，按普通 Bulk RX 工作；只有 core/stream 状态分配失败才拒绝并记录状态欠账。物理租约和信用上限计数一起跟随最后一个 owner 释放，包含跨线程持有的外部切片。
- 最低逻辑发送份额仍为 64 KiB。三层在预计新增一块后均不超过 75%，且 Bulk headroom / admission debt 允许时，部分 ACK 后可以续写第二块。压力下保留一整块限制，2 KiB 首块另允许一个整块，避免每 RTT 2 KiB 的停等。空闲 TX cache 阻挡这个检查时触发现有缓存回收。
- 新增三层信用字节诊断；跳过信用的计数与拒绝 SYN 区分。合同与降级范围见 [0010](../design/0010-rx-progress-credit.md)。

这是 issue 建议的有界可选信用方案。前面获得信用的连接保留受压小请求保障，超过信用上限的连接可能需要 TCP 重传；没有宣称共享信用池或所有连接都获得保障。

## 修复前失败、修复后通过

8 MiB 端口、同 peer 1500 条空闲连接，模拟显式预留每连接 1024 B host stream 状态：

| 用例 | 修复前 | 修复后 |
|---|---:|---:|
| 空闲连接建立数 | 696 / 1500 | 1500 / 1500 |
| 修复后空闲端口占用 | — | 6,652,576 B / 8,388,608 B |
| 保持空闲连接时，另一 peer 的 4 条 256 KiB 下载 | 准入断言失败 | 全部完成且字节正确 |
| 最低份额、ACK 掉半个 64 KiB 块后的可写空间 | 0 | 32768 B |
| 最低份额、首个 2 KiB 块写满后的可写空间 | 0 | 63488 B |

这些连接数与 issue 的真实 worker 不同，因为这里是 core + 显式 host-state 预留的模拟；不能把 696 与 L2 的 677 当作相同接线的两次测量。

## 50 ms RTT 的确定性吞吐对照

64 个真实发送连接，8 MiB port/peer，1 Gbit/s 链路，双向各 25 ms 传播延迟，固定 seed 3702。先注册每个 sender，再持续写；预热 2 秒，测量第 3 秒收到的有效字节。模拟器每虚拟毫秒检查不变量和端口用量，并验证全部流的 payload、无队列丢包及最终全局账本归零。

| 应用写入大小 | main 有效吞吐 | 修复后有效吞吐 | 变化 |
|---|---:|---:|---:|
| 1 KiB | 11.53 Mbps（1,441,792 B/s） | 319.81 Mbps（39,976,320 B/s） | 27.7 倍 |
| 64 KiB | 344.46 Mbps（43,057,152 B/s） | 381.84 Mbps（47,730,560 B/s） | +10.9% |

修复后采样峰值分别为 6,611,200 B、8,379,232 B，均不超过 8,388,608 B。测试门槛为这个固定场景的小写入 280 Mbps、大写入 360 Mbps，低于修复后实测值并能拒绝旧实现。窗口/RTT 的理论上限不作为可达 goodput 保证。这个比较不测墙钟 CPU/GiB，也不外推成真实 WG 的吞吐提升。

```sh
cargo test -p zfstack --release --features tokio,test-peer --test issue37_throughput -- --nocapture
# 同一个测试文件可复制到 5836346 工作树，使用独立 target 目录跑负对照。
# 可选的完整规模：256 sender / 32 MiB / 4 Gbit/s 链路，保持每流比例。
cargo test -p zfstack --release --features tokio,test-peer --test issue37_throughput -- --ignored --nocapture
```

本次也显式运行了默认忽略的 256 连接扩展用例，结果通过：

| 写入大小 | 修复后有效吞吐 | 32 MiB 端口采样峰值 |
|---|---:|---:|
| 1 KiB | 1.283 Gbps（160,317,800 B/s） | 26,224,384 B |
| 64 KiB | 1.607 Gbps（200,869,968 B/s） | 33,545,440 B |

两组均完成字节、不变量、无队列丢包、硬上限和最终释放检查。完整规模的表仅报告候选结果，没有将其与不同配置的 64 流基线直接比较。

## 验证与边界

- 完整核心库测试：238 通过，3 个仓库既有用例默认忽略。包括 409600 B peer 的停读邻居隔离、晚到连接、受压小请求信用及其关闭信用负对照、cookie / TIME_WAIT 替换、准入欠账、Tokio writer/splice。
- 新增三层信用上限与失败回滚、跨 shard 并发信用上限、外部切片最后 owner 释放、受压整块限制、小首块和缓存回收回归。
- `cargo build -p zfstack --no-default-features`；`cargo fmt --all -- --check`；`cargo clippy --workspace --all-targets --features zfstack/tokio`。
- 生命周期/trace 集成测试 3 项通过；`zfharness` 14 项通过；`zfbench` release 构建通过，默认 14 项及 all-features 18 项通过；Python L2 8 项、bench/pareto 29 项通过；L2 shell 语法检查通过。

当前环境没有 `/dev/net/tun`、WireGuard 工具、zfc-rig 或所测 worker，因此没有运行真实 WG ABBA。新增 [L2 空闲模式与 RTT 命令](../../harness/l2/README.md) 保留原来的四 peer 轻请求、下载校验、路径与资源证据，额外检查空闲连接建立数和最终存活数。其 socket 逻辑已有本地回环测试；完整 netns/worker 接线仍须在指定的 rig 节点验证。
