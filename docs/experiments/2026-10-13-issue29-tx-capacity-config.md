# Issue #29：独立 TX 容量正式配置与回归验证

实现基于 `939894adf9287cfd8d90af253917e7cd6ddf6c25`（本轮开始时的最新 main），工作分支为 `codex/configurable-tx-capacity`。没有改变生产默认、物理预算、分配器或缓存策略，也没有导入实验 CPU 探针。

## 已同步的稳定复测

[issue #29 的稳定复测记录](https://github.com/BlackLuny/zfstack/issues/29#issuecomment-6144555994) 已更新，正文也已加入入口并保留历史调查。OVH 裸机上同一 x86 实验二进制、固定 offload/物理预算，A/A 与两轮 8 流正反序矩阵通过。2.5 MiB → 256 KiB 的分组均值变化为 readv CPU/GB −4.00%、readv 路径 copy self −9.41%、代理 CPU/GB −7.37%、吞吐 +8.38%。

这是冻结 `b170affa` 实验基线的测量，不是本次正式配置实现的新增吞吐结果。用户态校验和、分配复用等也有变化，copy 的单对估计存在采样噪声；纯缓存机制、历史版本/平台及与 sing-box 的吞吐归因继续保留，issue 未关闭。

## 配置契约与影响范围

```rust
let cfg = zfstack::StackConfig {
    max_snd_buf: 256 * 1024,
    ..zfstack::StackConfig::client()
};
```

- `max_snd_buf: u64` 限制每连接核心 TX 的已发送未确认＋待发送字节数，先与原窗口/预取公式取最小值，再受原预算 fair share 和物理分配准入约束。
- `default()` / `client()` 均默认 `u64::MAX`，原计算的值不变；采用 u64 避免把原本可超过 u32::MAX 的窗口与预取之和截小。
- 任意字节容量均可使用。正容量小于 `write_low_watermark` 时降低有效 Writable 阈值；零容量禁止核心写入且不发 Writable，接收、ACK 和 FIN 仍可运行。未限容时的零 watermark 行为也保留。
- 本项不预分配、不更改全局/端口/peer 预算，不限制 adapter 自己的队列或空闲块缓存，不保证物理分配量小于逻辑容量。Tokio AsyncWrite 仍可先将数据放入 adapter 队列。
- 两个写入口 `write` / `write_with` 共用 `admit_write → send_space → sndbuf_limit`；普通 Tokio writer 和 splice 因此都受同一核心上限约束。`notify_writable` 与 share 等待判定共用有效阈值。
- 这是库配置 API；宿主在构造 `Shard` 或 Tokio driver 前显式设置。本次没有给 benchmark 或 ZFC 产品添加新的环境变量/面板字段，也没有把 256 KiB 设为默认。

GitNexus 的 impact / detect_changes 均返回 zfstack 未索引，不能作为完整图检查。已按源码核对上述调用点及全部 watermark 引用，并用直接单元测试、模拟 wire 流程和真实 Tokio/socket 用例补证。公开结构体增加字段后，外部穷举所有字段的 struct literal 需添加该字段；使用 `..Default::default()` / `..StackConfig::client()` 的调用点保持原行为。

## 验证方法

构建与 Rust 测试走 remote-compile，Linux x86_64、rustc 1.96.1；只同步跟踪文件及本次新增测试的隔离源码快照，本会话未跟踪的旧实验脚本/数据未混入。

本地忽略的旧 Cargo.lock 缺少 main 已有的 zfharness 工作区条目，首次 --locked 检查被拒绝。仅在验证快照用 `cargo update --workspace --offline` 补齐这一条，其他依赖版本未改，两边随后使用同一 lockfile（SHA-256 `f16d0a552ae1785ce8ea646ebb66d52ac2155336e732b38c0760f606f147046d`）。仓库内原 lockfile 未改。

新增覆盖：

- default/client、CUBIC/BBR/Brutal、有/无 RTT 样本、多种 inflight 的原公式等价；容量不能放大自然窗口；原计算超过 u32::MAX 的边界。
- 1 / 4097 / 65537 / 262144 字节容量的短写、满容量不调用 fill、阈值前后一次性 Writable、两种写 API 共用上限。
- write_with 错误及短填充回滚、abort 后不调用 fill、逻辑和物理计费归零。
- 显式容量不绕过物理分配失败或公平份额，其他 sender 释放份额后唤醒。
- 零容量＋零/正常 watermark 不发虚假 Writable；真实 wire 接收、ACK、FIN 仍成功。
- 8 流、两个 peer、双向 pattern、1% 丢包、2% 乱序、1% 重复、接收暂停、FIN、队列容量采样与最终资源归零；另测带未确认数据的 RST。
- 实际 Tokio AsyncWrite 的 4097 字节容量反复唤醒；真实 loopback socket splice 的 4097 / 65537 / 262144 字节容量，MTU 1500/65535、TSO on/off、3 MiB 双向数据与半关闭。

首轮新测试的一处夹具试图在已有逻辑占用时修改预算，触发既有初始化断言；修正为其他 sender 退出释放 fair share，生产实现无需修改。独立轨迹测试初版的配置所有权编译错误已修正。macOS 本地 Python 测试有 Linux 平台前置条件失败，完整 Python 回归改在 Linux 验证。

## 最终结果

| 检查 | 结果 |
|---|---|
| zfstack release，tokio + test-peer | 231 单元测试 + 3 集成测试通过，3 项原有 ignored |
| zfstack release，无默认 feature | 200 单元测试 + 1 集成测试通过，3 项原有 ignored |
| zfharness release | 14 项通过 |
| zfbench release，默认 / all-features | 14 / 18 项通过 |
| Linux Python，harness/l2 + bench/pareto | 6 + 29 项通过 |
| 核心库无默认 feature check | 通过 |
| 核心库 all-targets，tokio + test-peer Clippy | 完成，无错误；仍有 style/dead-code warnings，未使用 -D warnings |
| git diff --check | 通过；提交整理仅手工调整测试排版，按仓库指令没有运行 cargo fmt / rustfmt |

默认行为还通过了独立 wire 对照：把同一份 `tests/tx_default_trace.rs` 放入未修改 main 与本次实现的隔离快照，使用同一编译器、依赖锁、种子，测试 default/client × 0%/1% 丢包，均包含 8 流、两个 peer、双向 pattern、乱序/重复和 FIN。两边四组数据包内容＋发送时间摘要、模拟步数及结束时间逐项相同：

| profile / loss | A→B trace | B→A trace | steps |
|---|---:|---:|---:|
| default / 0 | 1012386856904262608 | 14735374268152377536 | 3243 |
| default / 0.01 | 15131358514405341826 | 3563422507619093462 | 3377 |
| client / 0 | 5097782612151735710 | 9010366701968043393 | 3434 |
| client / 0.01 | 2067165275589368151 | 4579696300789593477 | 3348 |

四组结束时间均为 Instant(11000000000)。这些是相同工具链下的回归证据，未将标准库 hasher 的输出当作跨 Rust 版本稳定的协议 golden。

远端任务：

- 修改前 wire 基线：`t-01M4TGYTJSPZFP9CGTFGF5N48X`。
- 完整 Rust/Python 回归：`t-01M4TH3EJD5FJSH5QJ0CT8G5R2`。
- check / Clippy：`t-01M4TH5H3NF6ES3TZ6C73VBMFE`。

本地原始日志、相同 lockfile、对照校验和源码清单位于 `bench/results/2026-10-13-tx-capacity-config/`。正式变更仅包括配置、核心准入/唤醒、文档与定向测试，未带入旧实验探针、二进制或结果目录。这些原始证据仅保留在本地，不随正式配置变更提交；没有部署。
