# PR #25 修复与 L2 首轮集成结果

采样日期：2026-10-12，基于 PR head `ee0e7284c24f0162e10238f8805d7cf96901d8b2`。本报告与评审修复、L2 入口一并交付于 PR #25。

## 已修复的评审问题

- 故障对照流原本在故障前结束：`sink-full-iface` 的健康流改为 50 ms 开始，须在 40–200 ms 故障窗口内完成；重建用例的另一 iface 改为 256 KiB 长流，并检查重建前后同一批活连接保留。
- quota 原本未撞到额度：四个 peer 各发送 512 KiB，必须观测到发送 share 阻塞或物理预算分层失败，之后完成传输。JSON 增加 global/port/peer 失败计数。
- L1 的零轻用户窗口提前结束：测量截止时间独立于轻用户线程，`--lights 0` 仍运行完整窗口。

## 验证

| 检查 | 结果 / 远端 task |
|---|---|
| harness release 单测 | 13/13，`t-01M4RCA2RPTVGKRT5P9TDC2TME` |
| bench release 单测 | 9/9，`t-01M4RCA8V2QB9PP44979K49RM5` |
| L0 `matrix --full` | 20/20，含 64/128 端口；`t-01M4RCQB85K71CA7DQCVE7T1RQ`；[完整矩阵](../../../examples/l0-matrix.md) |
| CI 参数 clippy（各 package、all-targets、zfstack/tokio） | 通过；`t-01M4RCV69S00SGZMX5QQTGJNC8`、`t-01M4RCV4VVAS5AA0PCKPNRX72S` |
| L2 应用仪器 | 本机 happy-path 597 个请求、约 9 GB 模式校验无误；故意篡改回包时返回失败 |

额外的 `-D warnings` 检查被已有 core dead-code/style 警告挡住；上述 clippy 使用仓库 CI 的参数。未运行 cargo fmt/rustfmt。GitNexus 未索引 zfstack，影响检查用源码引用与定向测试补证。

## L2：三个独立结果

两个真实 WG 监听端口、四个 peer；peer 0 为重下载，peer 1 同端口轻请求，peer 2/3 跨端口轻请求。空载/加载/恢复各 8/16/8 秒，轻请求每 10 ms 一轮。全部接收字节校验，目标物理捷径计数为零；每个客户端 WG 外层与 target → worker 载荷计数均成立。

| 组 | 全局预算 | 重连接 | 轻用户读超时 | 结果 | 重下载 Mbit/s |
|---|---:|---:|---:|---|---:|
| [normal-v3](normal-v3/result.json) | 64 MiB | 4 | 3s | PASS | 2038.3 |
| [small-budget](small-budget/result.json) | 8 MiB | 16 | 3s | FAIL | 1476.6 |
| [long-deadline-probe](long-deadline-probe/result.json) | 8 MiB | 16 | 30s | PASS | 1465.1 |

默认超时的 8 MiB 组仍为失败：同端口轻用户在加载期间只有 96 个成功请求，随后读超时；因此没有其恢复阶段样本。另两个端口的轻用户加载阶段各约 1600 个请求，P99 为 2.17/1.76 ms，重下载全程推进、模式无误。

30 秒读超时仅用于诊断，保留原失败。此轮同端口轻用户加载阶段 113 个成功请求，P95 643.9 ms、P99 1735.5 ms，最大 3410.5 ms（窗口第 14.688–18.099 秒，仍在重载期间）；恢复阶段 788 个请求、P50 0.727 ms。这证明可继续推进和恢复，没有证明满足默认 3 秒超时。没有修改 core、生产默认值或额度分配策略。

8 MiB 组实际触发物理压力：端口层分配失败最大 445487，peer 层 56，`share_blocked` 582，`sink_full` 51730；port 与 peer ceiling 均为 4 MiB（以 worker tuning 记录为准）。同端口延迟与此资源压力同时出现，精确的调度/分配根因尚未通过消融或包级跟踪确认。不能把它归因成 PR #25 引入的 core 回归：该 PR 没有 core 修改。

正常 64 MiB 组同端口轻用户加载 P99 16.43 ms（n=1491）；跨端口为 3.67/3.20 ms（各 n=1600）。这些是一次集成采样，不是 AB/BA 性能结论。P99 至少 100 样本，P99.9 至少 1000；小预算原失败的同端口 P99 显式为 null。

## 开销与资源释放

| 组 | 加载 worker CPU 秒 / 采样墙钟秒 | 加载 RSS 峰值 | 恢复 RSS 末值 |
|---|---:|---:|---:|
| normal-v3 | 27.15 / 15.56 | 126.6 MiB | 118.9 MiB |
| small-budget | 29.83 / 15.56 | 95.9 MiB | 95.6 MiB |
| long-deadline-probe | 29.98 / 15.56 | 95.9 MiB | 93.5 MiB |

worker CPU 含 crypto/UDP/管理任务，不含客户端内核 WG 的 CPU。正常组撤载后预算约 992 KiB，小预算组约 986 KiB，`egress_mem_waiting` 恢复 false；RSS 不等于预算，8 秒恢复窗口不足以证明长空闲 RSS 收缩。每组退出后自己的命名空间为空，测试 unit 均已停止；原 `zf` 服务与 watcher 为 active，原 rig worker hash 保持不变，节点租约已释放。

## 构建出处和复现

- ZFStack `ee0e7284c24f0162e10238f8805d7cf96901d8b2`；`Cargo.toml + src/*.rs` 递归内容 hash 为 `bedf3b672b40ebe40db72bbac22943065e11efb7339ad9deb978083b22ba0742`，与本地修复 worktree 相同。
- ZFC `2cd672c97b18fcd3d9ab01f53190359fa9653014`，仅在隔离 checkout 的 Cargo.toml 加 path patch 指向 PR；完整 diff 在 [metadata.json](metadata.json)。
- private_tun `91bdeab6d2988814dc9c7a21c327f6f609906a78`；shadow-tls-tokio `282e66194f76b813e1731282a70e0a8974cc9997`。
- 测试 worker SHA-256 `fb32f8b37862d364d7e6abd4cf4bb4d39e3776e5f2f8ddb3ac89d1aebdbd07eb`。
- Linux 6.1，8 个逻辑 CPU（Xeon E5-1630 v4，4 核/8 线程）；Rust 1.98.1、Zig 0.16、x86_64 musl，fast-release，mmsg + jemalloc。该口径与 release 的 opt-level/LTO/cgu 不同。
- worker 采用 BBR；WG 内部 MTU 1340，客户端 wg0 1420，以诊断记录为准。应用端点为同机 Python 内核 TCP，宿主另有原服务负载；不做 TCP core 对照或 WAN 容量推断。

入口：[run-rig.sh](../../run-rig.sh)、[运行说明](../../README.md)。正常组使用默认配置；小预算组使用 `L2_MEMORY_MB=8 L2_HEAVY_CONNS=16`；诊断组额外使用 `L2_REQUEST_TIMEOUT_SECS=30`。

每组 `raw.json.gz` 保存完整请求/重流样本、worker/线程 CPU 时间线、路径原始计数、路由/机器环境与清理记录；`diagnostics.log` 保存预算、CC、配置指纹和 adapter 诊断。节点完整案卷保留在 `/root/zfc-rig/local/pr25-l2-f55ea791/`。发布的证据没有 WG 私钥或控制器配置。

未覆盖：大量真实 WG 端口、WAN 丢包/漫游/重建、长稳态、严格 per-peer 公平性和百端口容量。L0 虚拟时间、L1 无 WG 和本 L2 不可直接比较。
