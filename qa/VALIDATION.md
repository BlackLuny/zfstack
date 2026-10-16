# QA 方案实现与验证记录

日期：2026-10-15。方案来源：`zfstack-ci-qa-design-implementation-v1.md`。
本记录区分仓库实现、实际执行证据和正式发布资格。默认性能策略保持 shadow，未伪造产品 SLO 或节点校准。

## 收尾分支与评审入口

生产分支 `codex/pacing-fixes` 从 `df432fd` 开始，仅有 `src/conn.rs`、`src/shard.rs` 两处修复及确定性回归；QA 分支 `codex/qa-framework` 基于该分支，初始 QA 提交 `609c72a` 的完整 tree 与原文档提交 `5daed24` 相同。原 `codex/qa-system` 保留，不重写旧执行身份。新 PR 描述仅保存在本机，没有 push、创建远程 PR 或修改仓库设置。

关键报告、原始文件哈希索引与复验摘要已整理为 [review-evidence](review-evidence/README.md)，维护者任务见 [手工清单](MAINTAINER-CHECKLIST.md)。QueueControl 为 **known-BLOCKED**。[原子恢复 issue](issues/queue-control-atomic-restoration.md) 的持久 FIFO 注入器已实现（见下文“QueueControl 持久 FIFO 注入器”），但未经 held-out 正式运行，不豁免。

## 生产影响

- 原实现保存在 `codex/qa-system`；收尾拆为 `codex/pacing-fixes` 与其上的 `codex/qa-framework`。原工作目录和用户已有实验文件保留。
- `src/budget.rs` 与 `src/tokio_adapter.rs` 仅在现有 `cfg(test)` 中引入测试子模块。后续 QueueControl 诊断定位了独立的生产 pacing 唤醒问题，`src/shard.rs` 增加有限小突发的及时唤醒选择；持续积压保留整量子批处理。后续恢复阶段证据还要求 `src/conn.rs` 记录原计价速率，并按速率变化守恒剩余正字节债务、重新调度旧定时器。发送资格、速率、信用、拥塞窗口、RTO、预算、socket、同步和默认参数均未放宽。
- Tokio `test-util` 只加入 dev-dependencies。核心与启用 Tokio 的 normal 依赖图均检查通过。
- 根 `Cargo.lock` 与原工作目录已有锁文件逐字节一致，SHA-256 为 `f16d0a552ae1785ce8ea646ebb66d52ac2155336e732b38c0760f606f147046d`。新增独立模型/fuzz 锁文件，不加入生产依赖图。
- Bench 仅新增三个实际配置字段的 JSON 输出。L2 注入、路径计数、服务合同及清理均在测试工具/隔离命名空间内执行。

## 可审查的实现范围

| 方案 | 已实现的入口与约束 |
| --- | --- |
| PR-01 | 不可变 plan、case-instance、三轴结果、源码/编译器/依赖/二进制清单、严格 JSON/schema。 |
| PR-02 | L2 完整负载身份、污染与功能失败分离、原始计数与窗口复算、历史案卷缺失不补零。 |
| PR-03 | Cargo/Python/L0 结构化执行、非空测试检查、缺 job/取消/错误 SHA 阻断、受审查测试清单。 |
| PR-04 | 保留 Eager；EventOnly 就绪集合、ConnId generation、唯一通知负对照、watchdog。 |
| PR-05/06 | A0 唯一释放/waker/取消；A1 current/2/4 worker 实际 loopback；跨线程 Bytes/预算与有限 Loom 模型。 |
| PR-07 | 独立随机流、事件脚本、最小重放与缩减、永久正/负语料、三个覆盖引导 fuzz 入口。 |
| PR-08/09 | 预构建 A/B、固定 ABBA/BAAB、物理域租约、原始环境采样、同时区间/非劣门禁、校准入口和可信绝对 SLO。 |
| PR-10 | 隔离 L1、Direct/relay 独立 L2、固定跨仓 2×2 构建入口、测试侧深出口队列负对照。 |
| PR-11 | 峰值→单连接→空闲恢复、owner 账本、Miri/ASan/Loom、四个原生平台工作流。 |
| PR-12 | 可信 reporter、exact merge SHA、CODEOWNERS、白名单归档、独立发布复算及待审查 baseline 候选。 |

截至本轮 pacing 与启动修复，清单冻结了 342 个手写 Rust 入口、327 个 Python 入口及 33 个含 Rust 条件属性的文件；没有删减既有入口或改变其 skip/cfg 属性。
清单检查不能证明宏展开或运行时实际执行；执行记录仍单独验证。7 个 ignored 测试均已登记用途与执行安排。

## 独立生产分支的收尾验证

`codex/pacing-fixes` 的两个提交为 `ecd91aa29c29c671290fe8468bbcddbbdd19c08b` 和 `868ce01c4801f9dc2ef582affcc454be270632f8`；生产候选只改 `src/conn.rs`、`src/shard.rs`。两文件与 `8bf5bb8` 对应生产文件逐字节相同，QA 分支不会重复引入生产修复。此前小突发开发回归的 before 为 `7b38911`（相关生产 preimage 与 `df432fd` 相同），债务回归 before 为已经包含第一处修复的 `ddeed62`，不是两个都在未修复基线运行。关键原始日志纳入受版本控制的摘要材料。

独立生产分支固定 Rust 1.94.1，使用与原工作目录一致但不提交的 Cargo.lock（`f16d0a552ae1785ce8ea646ebb66d52ac2155336e732b38c0760f606f147046d`），不包含 QA 的 dev-dependency/配置改动：

| 检查 | 实际结果 |
| --- | --- |
| `cargo +1.94.1 fmt --all -- --check` | PASS |
| Linux clippy，workspace / all-targets / all-features / locked | PASS，遵循原 CI 不加 `-D warnings`；51 个诊断 warning 头及 6 条 Cargo 汇总原样保留，未新增 allow 或隐藏输出 |
| Linux release workspace / all-features 全量测试 | 300 passed、0 failed、7 个原有 ignored；各 target、集成及 doc test 结果完整保留 |
| Linux 定向既有 `sim::tests::` | 49 passed、3 个原有 ignored，nocapture 日志保留 |
| Linux 定向 `pacing_` | 14 passed、0 ignored，nocapture 日志保留 |
| macOS no-default 核心 | 222 passed、4 个原有 ignored，另 1 个集成测试通过 |
| macOS 定向既有 sim | 49 passed、3 个原有 ignored |
| macOS 完整 workspace clippy/tests | 原 bench 的 Linux 专用 libc API 导致 23 个编译错误，退出 101；失败保留，不修改无关源码来适配。完整 workspace 在 Linux 独立通过 |

Linux 使用独立 target 实际编译 `868ce01`，保存 10 个 compiler executable 哈希、实际 Cargo 启动行、源码/tree/锁文件身份、依赖图及命令记录，没有用 `8bf5bb8` 测试二进制替代。8 条实际命令全部 exit 0、cleanup PASS，公共 Lease 释放，验证前后源码 clean。最终任务进程/宿主注册命名空间/Lease 均为空，可用空间 4132818944 字节（约 3.85 GiB），原生产业务与宿主网络未操作。

QA 收尾仅增加文档、评审摘要和 `exemption: false` 的 known-BLOCKED 信息；`exemptions` 仍为空，cases/阈值/门禁执行代码未改。针对此信息变更执行的 contract/coverage/relay_queue 共 70 项通过，日志明确属于 `609c72a` 上的工作区元数据/文档状态，不将它追认为新 QA 提交的全量发布资格。

## 已执行的检查

安全运行器修复提交：`854303599729d88bd7677118a79ff55ef5c5bf39`（包含发布样例证据格式修复）。
Worker 原始采样归档与独立复算提交：`15f993fa8a5c899137ea6b8cabd18fc38cb8e425`。
Linux 集成执行候选提交：`ae1783489940f930a0271eee7ffdd26e6f3b16ea`。
最终生产修复、v3 台架及测试清单冻结提交：`8bf5bb86106e5a4f1665f3c1b9c4570d2c80bf72`。
更早执行证据保留其原 SHA，未重贴为新提交的结果。后续仅文档提交不会改变以下执行身份。

| 原生环境 / 范围 | 实际结果 | 证据身份 |
| --- | --- | --- |
| macOS aarch64 / 最新 portable | 17/17 PASS，独立 evaluate PASS；执行前后 clean、SHA 不变 | `8bf5bb8`；plan `697459d6110f5bbe513d6a2cad03121eb048d39ff18b99b1a703c8c85bdccef8` |
| macOS aarch64 / 最新 safety | Miri、Loom 与三个 ASan fuzz 共 5/6 PASS；Darwin ASan+LSan NOT_RUN，独立聚合 BLOCKED | `8bf5bb8`；plan `0305ae17dfacf1b36f0cdb3698fb9d671ae52594a0cd822d9cc397cd6a7ba9ec` |
| Linux / 最新 head-head worker + forwarder | 10 条命令通过，实际构建 75.2 秒，cleanup 与 Lease release PASS | `8bf5bb8`；复用逐字节相同依赖缓存，重新生成该源码的真实 manifest |
| Linux / 最新 C16 Direct、Relay | 两组功能 PASS/VALID；性能 INVALID，单 case Nightly 聚合仍 BLOCKED | `8bf5bb8`；保持原开环、churn、到达及采样精度要求 |
| Linux / 最新 QA 工具、L2、性能工具、双依赖图与清单 | 五项均 PASS/VALID，实际 run exit 0、cleanup PASS；局部 Nightly evaluate 按预期 BLOCKED | `8bf5bb8`；原生工具检查，不代替完整 Nightly |
| Linux / 最新 C11 L1 | Rust 1.94.1 重新编译 zfstack 与 zfbench 后 PASS/VALID，cleanup 与 Lease release PASS | `8bf5bb8`；未用产品构建的 Rust 1.98.1 替代核心工具链 |
| Linux / 最新 C16 QueueControl | 基线与故障组实际 TCP/UDP 均无请求超时；删除前重新积压使注入资格 NOT_RUN/UNKNOWN，聚合 BLOCKED | `8bf5bb8`；详见最终独立复验，不能记为整项 PASS |
| Linux x86_64 / 前次 L2 工具与 worker 证据检查 | 完整 L2 工具 70/70，独立 worker 检查 11/11，均零 skip；实际 `/proc` 归档复算通过；cleanup 与租约释放 PASS | `15f993f`；纯 Python 原生检查，不替代实际 L2 或完整 PR 聚合 |
| macOS aarch64 / 前次 portable | 17/17 PASS，独立 evaluate PASS | `8543035`；plan `af308db258b27170e9244e3d4c22616c4e3aa2ffd86c0fc4472f6df7eb1f1cdb` |
| Linux x86_64 / PR | 21/21 PASS，独立 evaluate PASS | `f50f94f694606bd585f0575377b48ffef2cd2fb8`；原始结果保留 |
| Linux / QA、L2、性能工具、双依赖图与清单 | 5 个 case PASS/VALID；真实 `/proc`→文件→归档→独立复算检查 PASS | `ae17834`；局部执行不代替完整 PR 聚合 |
| Linux / C11 L1 | PASS/VALID，16 个轻用户各有 30 个 baseline 与 80 个 loaded 样本，重流有实际字节，清理 PASS | `f50f94f` |
| Linux / head-head worker + forwarder | 10 条构建命令均成功，公共租约释放与子树清理 PASS；其余三臂 UNEXECUTED，2×2 总体 INCOMPLETE | `ae17834`；Rust 1.98.1、fast-release、jemalloc/mmsg |
| Linux / C16 Direct、Relay | 两组均功能 PASS/VALID、清理 PASS；原始开环 100 Hz、churn=8、20 ms 发生器上限保持；性能证据 INVALID | `ae17834`；真实二进制与业务路径计数已验证 |
| Linux / C16 QueueControl | 基线 PASS；故障组请求超时，负对照总体 FAIL/VALID；清理 PASS | `ae17834`；失败与双臂原始证据保留 |
| macOS aarch64 / safety | 5/6 功能 PASS；Miri 1 项、Loom 2 项、三个 ASan fuzz 目标通过；ASan+LSan 因能力不足 NOT_RUN，独立聚合 BLOCKED | `8543035`；plan `e88b397c2815dd16e7537989e6a1b91641b330ab7078f21da02979e292e7bf12` |
| Linux / 同二进制 A/A shadow | 20 blocks、80 slots、10 ABBA + 10 BAAB，80 次功能 PASS、清理 PASS | `f50f94f`；见下方限制 |

最终 `8bf5bb8` 本机完整 portable 中，核心执行 225 项、adapter 275 项、harness 32 项；QA 工具 190 项中 4 项按平台跳过，性能工具 58 项中 2 项按平台跳过，L2 工具 79 项中 2 项按平台跳过。定向 adapter 9 项与跨线程预算 3 项另有独立 case。

最终 Linux 五项范围中，QA 工具 190 项有 189 项实际通过、1 项平台 skip（Linux supervisor 不继承 mock 的 16-byte 输出上限；同套真实 65 MiB 输出超限负例已执行），L2 工具 79 项零 skip、性能工具 58 项通过，双生产依赖图与清单通过。C11 有 16 个轻用户各 30 个 baseline、80 个 loaded 有效样本；实际新 zfbench SHA-256 为 `01633d7d279d41f6a666a0ea75606077c922677adcbf0847c467b92a7048baf8`。

最终节点收尾确认远端 `8bf5bb8` tracked clean、任务可执行进程为空、公共 Lease 为空、宿主命名空间注册为空，可用磁盘 4968464384 字节（约 4.63 GiB），未操作宿主原有业务。缓存备份辅助脚本的两次失败、显式 dead-owner quarantine 恢复，以及首次辅助编排器把局部 evaluate 的预期 exit 2 当异常停止的记录均独立保留；五项工具没有重跑，C11 随后单独执行。最终严格验收只接受实际执行 completed/exit 0/cleanup PASS，evaluate 仅在解析出真实 BLOCKED 时接受 exit 2，未把编排结束当成功。

额外的开发阶段手工重测试（日志保留，但没有单独冻结执行 SHA / v2 build manifest，未追认为 `8543035` 的执行结果）：256 流模拟吞吐通过（191.07 秒）；两种 write size 均有约 319,309,968 B/s 的虚拟协议进展。256 路 splice 与 4096→1 容量观察通过。这些结果不是物理网络吞吐或 RSS 认证。
此前在原生 macOS 手工执行过三个 Miri buffer 检查、33 次正向 Loom 模型执行及 mutation 负例、三个 ASan fuzz 目标各 512 次固定 seed 运行；这些早期运行基于未提交源码，使用当时实际 manifest 为 2026-07-11 的 nightly 别名，stdout/stderr 只保留于工具会话，不能作为完整 v2 安全资格。后续 `8543035` 干净提交使用固定 `nightly-2026-07-11` 执行六项安全计划，186 秒内完成：三个 ASan dev fuzz 各 30 秒、seed=1，分别执行 510,988 / 59,967 / 238 次，无 crash；3/1/3 个固定种子均从独立副本读取，源码语料前后哈希相同。所有实际子进程 cleanup PASS。该新计划只含一个精确 Miri selector，不能替代此前三个手工 selector 的范围；详细边界见 `tests/qa-adapter.md`。

## 反例与独立复算

- 从 `df8d31b` 实际通过的 17 个 case 复制完整白名单产物，将核心日志改成零测试并重算文件哈希；独立聚合仍返回 BLOCKED。
- 阈值删除/放宽后重算性能产物哈希、伪造指标、丢失 slot、环境污染伴随功能失败，均由回归检查覆盖。
- queue 负对照要求所有 peer 达到最低样本数；其他 peer 的预期 SLO FAIL 不能掩盖 NOT_RUN。
- 所有 Python 测试被 skip 不计为非空通过；删除/改名测试或更改 skip/cfg 属性会触发受审查清单差异。
- Linux detached 后代、超时、失租约和输出预算清理负例已执行；超预算日志现在明确写出字节上限原因。
- 首次 worker 构建曾因 `cargo tree --no-dedupe` 输出 67,427,729 字节而触发 64 MiB 上限。保留该失败，将树改为 Cargo 默认去重；新树为 148,564 字节，没有扩大资源上限。
- `df8d31b` 的后续构建运行 716.25 秒后触发 3 GiB 磁盘余量保护，退出码 -9、子树清理 PASS；不能记为源码编译失败或构建成功。此前外层只有人工串行协调，未持公共 Lease，该时段未被追认。跨仓入口随后补强为整个矩阵必须持公共节点租约，清理/失锁证据随构建保存。

- C16 的 open-loop + churn 原先没有实际实现；现按不可变到达表安排连接代际，保留所有预定/启动/成功/失败/超时/未完成/错过到达及连接生命周期，由独立检查重建并复算。语义和原有到达阈值未放宽。一次原始失败保留于 `open-loop-retained-failure/client.json`：140 次计划、107 次完成、33 次错过；其中 29 次暴露单槽待发队列缺陷，已用有界 FIFO 修复，其余实际超时仍保留为失败事实。
- `9bb392a` 完整 portable 的 16/17 个 case 通过，QA-tools 中 4 个发布合成样例因缺安全索引而失败；原始失败未覆盖。随后只补齐这些合成样例的完整证据，保留严格门禁，定向 release/safety/evaluation 共 40 项通过。
- 对 `8543035` 真实安全产物执行白名单复制，40 文件共 260,995 字节；独立评估仍为 5 PASS + Darwin LSan NOT_RUN，阻断原因与原始一致。另一个副本仅删除 wire_parser 的 `fuzz-seeds.json` 哈希清单条目（文件仍在），精确新增该 case 的缺哈希证据阻断；原始与正副本均再次核验无变化。
- 安全运行器现在将固定语料复制到独立输出，记录种子、失败产物和源码语料前后哈希；明确原生 ASan target、实际 dev profile 与固定 fuzz 预算。零迭代、缺旁证或未纳入哈希清单均不计通过。Darwin 不支持 leak target 时，ASan+LSan case 明确 NOT_RUN，不关闭检测以换取通过。
- 重流、轻流、UDP、目标与探测服务、relay forwarder 均由独立端点采样器记录 CPU/RSS 和进程身份。缺样本、身份变化、计数器回退或精度不足使性能证据无效，不能用功能通过替代。

## Linux L2 实测与失败边界

成功构建的 worker SHA-256 为 `3483f4762f29e51b95bb53533eb3b66a64074fe8b73d46748bc2d3e41d4b5554`，forwarder 为 `68c4c0da8859541cfd39caba58e597ae3bf279813f86ac85ce6e92838442c0bb`。三个 L2 case 均使用该 `ae17834` 构建。每次只执行一个 Nightly case 的独立聚合正确保持 BLOCKED（缺其余 case）；QueueControl 的实测功能失败使对应聚合为 FAIL。

QueueControl 的实际时间线相对共同起点为：8.005403 秒注入；23.258404 秒停止增加延迟；23.785798 秒证明积压已排空；23.814156 秒恢复原 qdisc；24 秒开始 recovery。原始 qdisc 报告无执行错误、观察到的 drop 计数为零，cleanup PASS。
peer 2 的请求 812 在 19.016308 秒发送、目标在 19.516829 秒收到并立即回复，但客户端在 22.019468 秒超时；peer 3 的请求 815 也在 3 秒 deadline 到期后超时。第一处超时发生在 drain 之前，因此不能简单归因于删除未排空队列。peer 2/3 的闭环发生器遇错后退出，导致 recovery 缺样本；这不能单独证明底层恢复失败。peer 1 的 recovery 有 603 个成功请求。故障期间各 peer 的固定 250 ms 合同确实触发，但已观察到的真实超时及缺测不能被预期 SLO 失败掩盖。

Direct 与 Relay 的性能错误来自端点采样精度不足和 verified-byte 缺样，独立评估保留 INVALID；这与 A/A 的共享宿主/cgroup 限额证据缺失是不同的限制。进一步检查显示，Direct 在 +11 秒、Relay 在 +17 秒各有一个内部字节采样为 null；实际阶段边界字节值存在，但既定检查要求整个窗口计数完整，仍保留 INVALID。端点采样按顺序读取，最大偏差分别为 11.555 / 9.315 ms；Direct 的两个 loaded 结束边界也超过 5 ms，Relay 超限点在窗口内部。没有把这些点忽略或放宽冻结的 5 ms 容差。

旧版封存 raw.json.gz 遗漏了 worker 原始时间线。四个 arm 的原始数字采样及目标请求时间事件已另行只读导回并核验哈希，诊断副本明确不是旧结果的 declared artifacts；既有封存结果、哈希和判定均未重写。

`15f993f` 已补齐这条闭包：`worker-samples.json` 随 raw 归档，独立评估从原始时间线重算三个阶段的 CPU、字节、RSS 及编译污染，拒绝不一致的派生摘要。逐个测量样本检查固定 5 ms 计划偏差和 CPU/字节读取预算，缺样与计数器回退仍 INVALID。锁竞争只在原 CPU/字节读取的 5 ms 绝对 deadline 内有限重试，不延长预算、不改端点采样时序或负载。
新增回归覆盖实际文件→封存→归档→删除原文件→干净进程独立复算、派生/原始证据篡改、功能失败保留、跨进程锁竞争，以及窗口内部迟到/提前的负例。使用四个旧 arm 的单独数字侧录进行离线复算，均得到原始摘要；20 个派生 CPU/字节/RSS/污染或缺原始数据的负例全部被识别。旧 Direct/Relay 的内部缺样和故障组采样错误仍保留，未被“修复”为有效性能数据。这个离线检查不是新提交的实际 L2 重跑，旧三组 L2 仍只属于 `ae17834`。

所有测试完成后的节点检查确认：候选源码 tracked clean、宿主命名空间注册列表为空、公共租约为空、无本任务可执行进程残留，可用磁盘约 5.60 GiB。宿主原有业务服务没有作为测试目标或被替换。

## QueueControl 后续定位（保留失败）

使用同一 `ae17834` worker 的隔离诊断 r1 保留了 11 份有界抓包。peer 2 的请求 808 在目标回复后，于 +22.519988 秒到达 worker；worker 直到 +25.022386 秒才发出对应 WireGuard 回包。客户端在 +25.022278 秒超时关闭并发送 FIN，迟发回复已确认该 FIN。worker→client 2 的 4059 个 WireGuard transport counter 全部能在客户端外层找到，client 3 的 4772 个也全部对应。该 TCP 失败发生在 worker 内部发包前，不能归因于这些外层 UDP 包丢失。

同时，超时连接的 BBR pacing 速率约为 505 B/s。原实现允许发送的时刻与 pacing 堆的唤醒时刻相差一个完整量子；低速小回复已具备发送资格，却继续等待整批信用或新的控制包。修复只在设置阻塞定时器时，对总待发及在途占用不超过完整量子的有限小突发选择最早资格时刻，持续大流保留原批处理。已经设置的定时器不因随后占用变化而自动重算，本轮未扩大调度器修改。

独立提交 `6f74213` 的六个确定性回归在旧生产代码上为五失败、一通过；修复后六项通过。505/632 B/s 的自然重传场景在旧实现中均于第六个 256 字节回复达到 3 秒超时，修复后各 24 个回复完成，最长等待 1.12 秒及 980 毫秒，仍保留真实 RTO 和重传。资格前一纳秒禁止发送、完整字节充账、量子边界、在途/adapter 占用、round budget 及 bulk 批处理均有检查；四个既有吞吐、执行间隔、CC 与公平性检查通过。

合并后的 `ddeed62ae34d7f5af3d4ccce6a61ad4b8c7eb11b` 已通过本机 portable 17/17 及独立 evaluate，plan hash 为 `22538c274e425147ebe12890e735a7ccd571f0113b662307995e6ed25af9f1f7`，执行前后源码 clean、SHA 不变。Linux 新 worker 构建与实际回归另行记录，不以该本机结果替代。

UDP 是另一条台架问题。r2 中，旧 `delay 0` 收尾使 50 个迟到的小包落后已接收的 WireGuard counter 约 21045–44905；外层收到这些包，内层未交付相应回复。这支持大幅乱序触发反重放的解释，但未解密的 counter 与应用 sequence 对应仍是推断。小型序列实验中，旧收尾收到 200/200 包但有 49 个乱序；仅在收尾增加 100 Gbit/s 的 FIFO deadline 约束后为 200/200、零乱序。

该候选没有因为小型实验通过而被验收：原完整负载 r3 中，四个 UDP peer 仍各有约 94–95 个超时。worker eth0 的 TX dropped 增加 15241，target RX dropped 增加 338，client 0 RX dropped 增加 4564；qdisc 自身 drops 和 UDP RcvbufErrors 均为零。原始包包含最大 65226 字节的 GSO 包。保序后的集中排出暴露了虚拟链路突发丢包，100 Gbit/s 收尾候选必须保留为失败。后续需同时约束突发并核查原恢复窗口；不能通过扩大目标 socket 缓冲、忽略 link drop 或延长 deadline 来掩盖此证据。

`ddeed62` 的 Linux head-head 构建 10 条命令全部通过，新 worker SHA-256 为 `5b10d001e682a2c43a6973b0158955d7350100f30ec80bbba0d52f63147e08de`。其 QueueControl 基线全部 TCP/UDP 完成，但故障组仍 FAIL：TCP peer 1/2/3 分别在 sequence 817/818/818 达到原 3 秒 deadline，UDP 各有 86–88 次超时。该冻结版本没有 link 原始采样，因此 link 状态是未观测，不能把辅助分析里的空列表解释为零丢包。

对同一新 worker 的 r4 抓包进一步确认了恢复阶段的第二个业务问题。peer 2 sequence 805 的响应在 +24.933379 秒进入 worker，直到 +29.148679 秒才以 WireGuard 回包发出，客户端 +29.148694 秒收到；客户端早在 +27.936267 秒发送超时 FIN，qdisc 则已在 +23.864680 秒恢复。这次 4.215 秒迟发位于 worker 内部。独立自然 BBR 服务端/Cubic 客户端模拟也复现：速率恢复后，旧低速累积的绝对 EDT 债务仍阻塞已入队的 256 字节响应。诊断日志进程成功退出只表示采集完成，实际 3 秒合同失败单独保留，不能记为功能通过。

`4eb1171` 新增 QueueControl 六个端点的原始接口计数闭包，记录 namespace/interface 身份和全部 rx/tx counters，独立复算跨整个负载及恢复阶段的 drop/error 增量。缺样、重置、替换接口或任一正错误增量均使台架不合格，不会覆盖真实 payload FAIL。13 项定向及 9 项集成检查通过；一次既有无 Queue 的开环 wall-clock 检查出现 140 次计划中 34 次错过该 CLI 检查的 50 ms 到达期限（正式 L2 仍为 20 ms），失败 JSON/日志已保留，单项诊断复查通过，未调整阈值或测试语义。

第二处生产修复先在独立提交 `0cb4c4` 验证，再合并为 `e478bdb`：每连接增加一个 `u64` 计价速率（由现有 `size_of::<Conn>()` 自动计入预算），未来 EDT 债务按旧/新速率向上取整换算，不清空已消耗额度；降速会延长等待。速率变化时旧 heap 条目重新进入就绪队列，速率不变时保留原批处理。两项永久自然 BBR 回归在旧代码上为一失败、一通过，修复后均通过，恢复阶段分别完成 760/759 个请求、最大 19 ms。计费、重传、恒定速率、堆重定位和既有模拟保护共 17 项通过，独立审查未发现阻断问题；它们不替代真实节点 CPU/吞吐验证。

冻结提交 `bd05d4f15bd7e7771bde66eec426cd2d8dd8c644` 的完整 portable 实际为 16/17 PASS、独立 evaluate FAIL，plan `fbbea0aa75b3a3af35bdc666927f8d9ef18a8a6b9d3b0971b63e58fa1af75419`，源码前后 clean。唯一失败是 CLI 发生器初始化耗时约 622/640 ms，超过测试起跑前的预留时间，140 次计划中 80 次 missed、60 次完成；没有业务 timeout/corruption。`cfb6fc0` 仅修复测试 fixture：解释器和真实 CLI 模块初始化 ready 后，父进程一次固定绝对起跑时间，再执行原 `__main__` 参数解析和发生器。准备阶段故意等 600 ms 后全部 140 次完成；ready 后故意停顿 1.3 秒则全部 140 次 missed 并失败。原 50 ms 测试到达期限、正式 L2 20 ms 期限、速率、churn 和请求数均保持；8 项定向检查通过，旧完整计划失败不覆盖。

预注册的 slot 容量矩阵已全部执行并拒绝：32 KiB/125 µs 在最后 draining 样本虽为空，cleanup 又有 45248 字节积压；64 KiB/250 µs 与 128 KiB/500 µs 分别晚于固定恢复边界 55/77 ms。三组 link counters 无错误增量，但不能据此认定排空合格。名义 250 MiB/s 不是保证服务率或任意时间窗突发上界；尾部实际到达约 294 MB/s，会再次积累队列。原 observer 的捕获记录时间戳倒退也保留为未完成分析，未把排序后的记录冒充真实 WireGuard 接收顺序。随后另行预注册有限 per-skb 2/3/4 Gbit/s、无 slot 的同 qdisc 排空；不得回写旧三组判定。

有限速率容量矩阵绑定 `bd05d4f` 新 worker `36977c8ee4a02595af6ef0cb463654069a22e4e6c6d6d169b9722d0678ca37c1`，最终 plan `0c3a3d7131fe87bbb00fd484f50f8cf42e3fa49df592ba87e4f5b3d8709b344b`。按预定 2/3/4 Gbit/s 顺序全部执行，仅 2 Gbit/s 合格：初始积压 29630406 字节，提前固定 recovery 72.752 ms 恢复；从实际 drain 前 750 ms 至恢复的 74564 个 WG 身份全部一一到达、无缺失或重复，六端 link 零错误增量，四个 UDP peer 各 3200/3200。TCP 各完成 1629/1630/1631 次，恢复各 800 次、最长 9.628/9.264/10.894 ms，无 3 秒超时；TCP 结果不用于选择。3 Gbit/s cleanup 有积压，4 Gbit/s 链路丢包与交付/UDP 不完整，均原样保留失败。记录顺序与真实线上接收顺序不混用，严格 wire order 仍 NOT_PROVEN；新捕获标准在运行前冻结，未回写旧 slot 判定。

v3 固定该 2 Gbit/s、无 slot 的收尾策略，拒绝旧 v1/v2 或 100 Gbit/s 旁证，JSON/text 两种实际参数都必须精确相符，且保留整个负载和恢复期原始 link 计数。13 项定向、9 项 Queue 集成检查通过。该容量选择只是独立正式复验的候选，不把诊断运行改记为最终源码的 QueueControl PASS。

`bd05d4f` 的新 worker Direct/Relay 两组正式实测均功能 PASS/VALID、清理 PASS；仍执行原 100 Hz 开环、churn=8 和 20 ms 到达限制。性能证据因多个端点采样超过固定 5 ms 精度而 INVALID，单 case Nightly scope 聚合保持 BLOCKED。没有以正确性通过替代完整性能或发布资格。

## 最终冻结源码的独立 Queue 复验

`8bf5bb8` 的新构建 manifest 记录实际 worker `36977c8ee4a02595af6ef0cb463654069a22e4e6c6d6d169b9722d0678ca37c1`、forwarder `68c4c0da8859541cfd39caba58e597ae3bf279813f86ac85ce6e92838442c0bb`。二进制恰与 `bd05d4f` 相同，因为其间未更改生产 Rust 或构建依赖；仍实际执行该冻结源的完整构建流程，不复用旧 manifest 贴新身份。正式复验没有诊断抓包 observer。

Queue 基线三路 TCP 各完成 3199 次，四路 UDP 各完成 3200 次。故障组三路 TCP 分别完成 1600/1624/1630 次，全部请求完成且无超时；加载期各有 17/16/16 次超过原 250 ms 合同，最大请求耗时约 1.003 秒，仍小于原 3 秒 deadline。恢复阶段分别完成 782/800/800 次，包含发生器等待的最大到达延迟为 2.321/2.954/4.888 ms。peer 1 最后一个 loaded 请求在 +23.821355 秒开始、+24.184830 秒完成，闭环随后才开始 recovery，因此少 18 次不是 missed arrival。四路 UDP 各 3200/3200 完成、恢复各 800 次，恢复最大到达延迟分别 5.457/4.595/5.966/6.069 ms。此前业务内部的 3 秒超时在本次没有再出现；这不是完整负对照资格通过。

台架仍被严格拒绝：停止新增延迟后 +646.397790 ms 的 draining JSON backlog 为零，但同一采样稍后取得的文本已有 46322 字节、2 包。+656.620124 ms 的 cleanup JSON 又有 90496 字节、2 包，文本为 45248 字节、1 包。+668.803346 ms 恢复原 root，距固定 recovery 尚有 73.393691 ms。这是持续流量下读空与删除之间的竞态，不是恢复 guard 越界。qdisc/link 原始 drop/error 增量均零、资源清理 PASS，也不能证明删除时没有释放尚未发送的包；没有 reset 时刻的观测，更不能把 90496 字节直接当作已确认丢失量。

固定 `8bf5bb8` 校验器对原始报告仍返回未证明删除前排空，正式结果维持 NOT_RUN/UNKNOWN，独立 scope BLOCKED。附带的 “lacks actual loaded phase interval” 是控制证据被拒绝后的派生理由；从原始时钟和阶段配置可重建完整 planned/actual 区间，并非时间戳文件缺失。138 个导出文件的大小/哈希、raw.gz 与独立侧录的一致性、所有请求身份及计数、六端接口身份及原始计数均经独立重算。

现有 stock Linux `tc` 的快照与删除不是条件原子操作，缩短采样间隔、增加连续零读数或批量发送 netlink 命令都不能消除竞态。root graft 的停用/reset 还会使删除后的零计数失去删除前无丢失的证明能力。依据与调用路径见本地独立审查案卷；上游参考为 [Linux 5.4 sch_api.c](https://github.com/torvalds/linux/blob/v5.4/net/sched/sch_api.c)、[sch_generic.c](https://github.com/torvalds/linux/blob/v5.4/net/sched/sch_generic.c) 和 [sch_netem.c](https://github.com/torvalds/linux/blob/v5.4/net/sched/sch_netem.c)。实际节点 release 为 `5.4.0-216-generic`；源码调用链核对基于 upstream v5.4，Ubuntu `216.236` 固定 tag 获取返回 403，未声称已审计供应商全部补丁。2 Gbit/s 的容量候选单次通过不能被解释成原子恢复保证。

本轮不再靠重跑、改变速率、暂停/减少负载、延长 guard 或放宽合同换取通过。完成该台架仍需独立实现并验证持续存在的 FIFO 注入器，在同一路径原子切换为透明转发、整个测量结束后才拆除，或者具备等价原子切换能力；需要保持原负载和窗口，并新增完整交付、恢复边界、队列所有权与清理的证据。没有在共享生产宿主加载内核模块或修改网络栈来绕过此限制。最终只读能力核查显示 `nfnetlink_queue` 与 `xt_NFQUEUE` 均为可加载模块而非内建，目前未加载，且未发现 libnetfilter_queue 库或头文件；NFQUEUE 方案需要另行具备这些能力的隔离环境与新实现，不能直接宣称当前节点已具备。生产 pacing 修复与已通过的 Direct/Relay 可独立审查，Queue 资格缺口继续阻断发布。

## 收尾追加：固定 8bf5bb8 的两次业务超时复验

按明确的收尾要求，在同一节点对同一 `8bf5bb8` worker（SHA-256 `36977c8ee4a02595af6ef0cb463654069a22e4e6c6d6d169b9722d0678ca37c1`）又执行恰好两次原正式 QueueControl。没有第三次择优重试，没有改变 500 ms 注入、2 Gbit/s 收尾、8/16/8 秒窗口、250 ms guard、3 秒请求 deadline、250 ms 合同或业务负载。每次仍有独立 baseline/fault 双臂、原始结果与公共 Lease。

所有 baseline/fault TCP/UDP 的实际 scheduled 请求均完成；各阶段 timeout/failed/missed/unfinished 均为零，原始 errors 数组为空。所有 baseline 臂及 fault recovery 阶段均无完成请求超过 250 ms。以下故障期超限完整保留，不能把“无 3 秒超时”写成“无 250 ms 超限”或台架资格通过：

| 运行 | TCP 全程完成（peer 1/2/3） | loaded TCP 超 250 ms / loaded 总数 | loaded UDP 超 250 ms（四 peer 各自 / 1600） | loaded TCP 最大到达延迟 | 台架 / 聚合 |
| --- | --- | --- | --- | --- | --- |
| 首轮正式复验 | 1600 / 1624 / 1630 | 17/18、16/24、16/30 | 1561 / 1561 / 1561 / 1561 | 约 1.003 s（最大请求耗时同约 1.003 s） | NOT_RUN / BLOCKED |
| 追加第 1 次 | 1611 / 1631 / 1632 | 16/18、16/32、16/32 | 1562 / 1562 / 1562 / 1562 | 1089.099 ms | NOT_RUN / BLOCKED |
| 追加第 2 次 | 1618 / 1618 / 1618 | 16/21、16/21、16/21 | 1563 / 1563 / 1563 / 1563 | 1006.520 ms | NOT_RUN / BLOCKED |

两次追加的 UDP 各 peer 全程均 3200/3200；恢复各 800 次。TCP 是原来的 closed-loop，planned 指真实 scheduled records，不能套用 open-loop 的固定 3200 次要求。两次 fault recovery 的 TCP 最大到达延迟分别 20.307/22.538 ms，UDP 分别 6.035/33.573 ms。完整逐 peer、逐 phase 的 service（started→ended）与 arrival（scheduled→ended）统计单独保存，不混用。

追加第 1 次 cleanup 仍有 45248 B 积压，未证明删除前排空；恢复距边界尚有 76.492910 ms。追加第 2 次 cleanup 读数为零，但恢复晚于固定 recovery 边界 52.097270 ms，故仍未合格。两次均保持 NOT_RUN/UNKNOWN，独立聚合 BLOCKED，资源清理及租约释放 PASS；不扩大 guard 接纳第二次迟到。112 项追加导出文件的大小/哈希已核验，原首轮及之前失败均未覆盖。完整原始文件在归档 `qa/results/validation-20261015/8bf5bb8-queue-two/`，评审用摘要见 [受版本控制的证据](review-evidence/README.md)。三轮无超时是新增观察证据，不是对所有负载永不超时的保证。

## QueueControl 持久 FIFO 注入器（#59，未 held-out）

`C16-l2-queue-control` 升为 case v2，`queue_control.kind` 为 `fifo-delay-line`；500 ms、100000 包、8/16/8 秒、250 ms guard、3 秒 deadline、250 ms 合同和业务负载均未改变。v3 `netem-delay` 只用于复判已归档报告，旧三轮失败不覆盖、不追认。设计、预注册常量和 oracle 见 [L2 README](../harness/l2/README.md#共享出口队列负对照)。

新增 17 项可移植测试（`harness/l2/test_fifo_ledger.py`）按冻结策略回放生成账本，并逐项验证缺失、重复、乱序、溢出/发送失败/未发送、早释放、迟到、阶段错标、收尾空队列后继续整形、透明帧排在积压后、切换越过 recovery 或早于积压排空、GSO 段/字节/tap 帧不闭合、动作/tap 丢弃、tap 自带队列、qdisc 未恢复、账本摘要/帧数不符、取消、清理失败、安装过晚、拆除过早、覆盖不足、占用越界，以及父进程未就绪、提前退出、挂起、报告缺失或畸形均失败。`harness/l2` 96 项、`qa/tests` 208 项（1 项既有 skip）通过，`audit-coverage` 以纯新增通过。

隔离 VM（TCG、6.8 内核）特权自检的全部运行见 [REPORT](../harness/l2/results/2026-10-16-fifo-injector/REPORT.md)。开发中发现并修正了 GSO 段计数闭合、接收端缓冲截断、透明切换时间戳早于最后积压帧、取消时直连帧超越积压四个问题，失败运行均保留。最终提交上的 cancel 与 overflow 负例按预期被拒且清理通过；final full 其余独立检查全过，但收尾期迟到 26.20 ms 超过预注册的 20 ms，原因是模拟 CPU 无法以 2 Gbit/s 写出，未放宽常量或重跑择优。另实测 NFQUEUE 引入 2608 次发送 EAGAIN（netem/FIFO 为 0），以及挂/删 clsact 会静默丢包，据此确定 tap 路径与安装/拆除时机。

QueueControl 仍为 known-BLOCKED：须在具备真实 CPU 和所需内核能力的隔离宿主上，冻结 worker 后执行一次 held-out 正式运行（见 [手工清单](MAINTAINER-CHECKLIST.md)）。

### 正式失败的根因分析与候选 `netem-fifo`（未进入 case）

- 根因：[离线逐帧分析](../harness/l2/results/2026-10-16-fifo-formal-analysis/REPORT.md) 复算 remote23 的正式故障臂，结论仍为 FAIL / VALID。
  - 单线程发送容量 p50 69.7k fps，而负载突发约 128.7k fps。
  - 有 12 次超过 50 ms 的读取停顿，最长 340 ms。
  - UDP “准入 − 计划” 在 recovery 期最大 533 ms，但账本中透明期 p99 只有 70 µs，即读取前的等待对 oracle 不可见。
  - 172 次 SEND_FAILED 发生在帧已经迟到约 71 ms 之后。
- 候选：据此实现 `netem-fifo`（schema 5，持久内核 netem 加队列前后两份内核时间戳捕获），设计见 [L2 README](../harness/l2/README.md#共享出口队列负对照)。新增 17 项可移植测试（`harness/l2/test_netem_fifo_ledger.py`），覆盖链式释放上下界、偏斜界、丢失、重复、旁路、全局和逐流乱序、早释放和迟到（含 drain 速率）、相位边界、读回和截止、recovery 积压、计数闭合、时钟漂移和空隙、生命周期、GSO、占用，以及 netlink 报文布局和分派。`harness/l2` 122 项、`qa/tests` 208 项（1 项既有 skip）通过，`audit-coverage` 通过。
- [仿真自检](../harness/l2/results/2026-10-16-netem-fifo/REPORT.md)（TCG 6.8，代码 `96a3fcc`，计划先于执行提交，每种一次）：
  - cancel 和 overflow 负例 PASS，信息性 full-heavy PASS。
  - full **FAIL**：出口顺序精确且 oracle PASS，但接收端 99 个 UDP 被超越。
  - 预注册的 tracefs 诊断再次出现乱序，并定位到 netem 多 CPU 出队后经 veth 按 CPU 积压投递（32/32 跨 CPU）。
- 因此 C16 保持 case v2 `fifo-delay-line`，没有冻结或正式运行 `netem-fifo`，也没有放宽判据。远端宿主本次不可达（无凭据）。

## 性能与发布资格的限制

真实 A/A experiment hash 为 `43fab4881c29993120e2612e9d88ccf3206c994c0f2df8c5b9239e20f22f7b8a`，A/B 二进制均为 `c07844345f0783f22b72c62807970049360f4e9d9efe5f26258986d8160480cb`。
节点是共享宿主，未证明物理独占，且 cgroup v2 祖先限额文件不可用；因此 80 行性能证据全部 INVALID，统计结论为 BLOCKED/INCONCLUSIVE，`calibrated=false`。功能 shadow 聚合 PASS 不代表性能通过。

以下资格仍需独立完成，当前发布门禁会保持阻断：

1. 经审查的产品 SLO/预算、合格测量节点、独立构建 A/A 与 held-out null/slowdown 校准。
2. Linux aarch64、Windows 原生运行，以及同一发布 SHA 上完整平台、Nightly、安全、集成与性能证据。
3. 跨仓四种实际组合的完整执行；只构建 head-head 不宣称 2×2 归因成立。
4. 维护者审查并设置 `QA_TRUSTED_REF`、required `qa/gate` 与 CODEOWNERS 规则，使用试验 PR 验证真实合并阻断；指定 L2/perf 节点环境和启用变量。
5. QueueControl 业务超时修复已得到确定性回归和真实节点观察；台架实时删除竞态已由持久 FIFO 注入器（case v2）取代，但仍须通过新的独立 held-out 正式复验，当前 NOT_RUN 不能当作成功负对照。

共享节点上的 `ae17834` 跨仓构建前两次分别在 worker、forwarder 阶段被 3 GiB 磁盘守卫取消，第三次完成构建；所有失败尝试、构建进度、子树清理与租约释放证据均保留，未覆盖成重试结果。只对本任务已完整备份且验证哈希的缓存/归档副本进行空间回收；没有调低保护阈值或更换冻结 profile。

private_tun 实际路径、服务合同负对照和 #43 UDP 的结果须按对应原始案卷判断。测试侧 500 ms 队列注入的 250 ms 检测阈值是负对照设置，不是产品承诺，也不等于 #44 历史根因复现。生产 pacing 修复依据实际回包时间线及修复前失败的确定性回归，未放宽检测阈值。

## 受版本控制的摘要与完整归档

[评审入口](review-evidence/README.md) 共 30 个文件、**3,976,223 字节**（小于 5,000,000 字节），包含 [portable 报告](review-evidence/portable-report.md)、[safety 报告](review-evidence/safety-report.md)、[Queue 独立审查](review-evidence/queue-independent-review.md)、[三轮请求复算](review-evidence/queue-repetitions.md)、[生产回归](review-evidence/production-regressions.md)、[独立生产验证](review-evidence/production-native-summary.md) 和 [完整文件索引](review-evidence/artifact-index.json)。这些文件随 QA PR 提交，评审者无需访问 ignored 的 `qa/results` 即可阅读关键结果和失败边界。

完整证据目录 `qa/results` 已冻结并打包，包含全部已有构建缓存和重复保留副本：**12,381 个文件、2,509,219,016 logical bytes**。这是真实完整范围，不按此前名义约 2.2 GB 推算，也不声称覆盖机器上所有 `/tmp` 文件。完整索引 SHA-256 为 `3d6fb38d7be4df890d23986e3d6bbbe3d33c19dc733896084eba9e3e88609bc5`。

- 本机归档：`/Users/lulin/zfstack-qa-evidence-20261015/qa-results-20261015.tar.zst`（即 `~/zfstack-qa-evidence-20261015/qa-results-20261015.tar.zst`）。
- 压缩大小：626,522,907 字节。
- SHA-256：`f5c392ef4339a4f616909b8a8b3d64141610675268865917abd809d9017784ee`。
- [逐成员核验](review-evidence/archive-verification.json)：PASS；所有路径、大小与内容 SHA-256 符合索引，包括 1,524 个 tar 硬链接解析后的目标内容；无缺失或额外文件，zstd 完整解压校验退出 0。未解包覆盖任何原始证据。
- 首次归档校验脚本不支持标准 tar 硬链接而失败；原失败 JSON 与脚本保留在归档旁，路径/哈希见核验记录。补齐硬链接校验后验证的是同一个未改写归档，并非删除失败样本或改写索引。

下文的历史 `qa/results/...` 引用均对应归档内相同 member 路径；索引中的相对 path 需加 `qa/results/`。旧的子集索引也原样保留。完整包目前只在本机，没有上传或生成远程下载链接；维护者持久存储事项列在手工清单中。

两份本地 PR 描述分别为 `/Users/lulin/zfstack-qa-evidence-20261015/pr-descriptions/pacing-fixes.md` 和 `/Users/lulin/zfstack-qa-evidence-20261015/pr-descriptions/qa-framework.md`。

## 本地证据入口

- `qa/results/8bf5bb8-portable-report.md`：最终冻结源码的完整 portable，17/17 PASS。
- `qa/results/8bf5bb8-safety-report.md`：最终安全计划，5/6 PASS、Darwin LSan NOT_RUN。
- `qa/results/validation-20261015/8bf5bb8-linux-l2/`：最终完整构建、Queue 保留失败、Direct/Relay 及白名单原始侧录。
- `qa/results/validation-20261015/8bf5bb8-linux-native/`：最终五项 Linux 工具、C11 L1、严格执行验收与节点收尾。
- `qa/results/validation-20261015/8bf5bb8-queue-independent-review/`：独立哈希、载荷、阶段、队列与链路复算。
- `qa/results/validation-20261015/queue-atomic-restoration-review/`：stock tc 恢复竞态与后续注入器边界审查。
- `qa/results/15f993f-portable-report.md`：前次本机完整计划报告。
- `qa/results/8543035-portable-report.md`：安全修复提交的前次完整计划报告。
- `qa/results/8543035-safety-report.md`：完整安全计划报告，包含 Darwin LSan 的 NOT_RUN。
- `qa/results/8543035-safety-stage-acceptance.json` 与 `8543035-safety-staged.index.json`：真实归档闭包与删除哈希条目负例。
- `qa/results/9bb392a-portable-report.md`：保留的发布样例旧格式失败。
- `qa/results/df8d31b-negative-zero-tests-report.md`：真实产物零测试注入的阻断报告。
- `qa/results/validation-20261015/linux-f50/summary.json`：Linux PR/L1 与保留的早期 worker 失败。
- `qa/results/validation-20261015/aa-f50/inputs/summary.json`：真实 20-block A/A 及其明确限制。
- `qa/results/validation-20261015/linux-ae17834/summary.json`：Linux 五类工具、三组 L2 和成功构建；同目录保留两次磁盘保护失败。
- `qa/results/validation-20261015/linux-ae17834-diagnosis/ae-diagnostic-report.md`：请求/队列时间线与采样缺口；独立 supplement 保留原始数值及来源身份。
- `qa/results/15f993f-real-worker-closure.json`：该提交校验器对四组独立侧录的离线复算与 20 个负例；不追认新 L2 运行。
- `qa/results/validation-20261015/linux-15f993f-python/summary.json`：此前 `15f993f` 的 Linux 70/70 与 11/11 原生工具检查。
- `qa/results/validation-20261015/artifact-index.json`：收尾追加前封存的 5577 个文件、1281317892 字节范围索引，SHA-256 `7142f08dac031a6fc8796c33d5762a25ecb2a8e834c33ecd523a2ef17235338d`。此前 920 项逐字节保持，原索引及核验记录保存在 `index-history/`；混合源码身份与失败记录不构成统一发布资格。

上述大型原始证据保留在本地 ignored 输出目录，未混入源码提交。操作入口见 [QA README](README.md)、[性能运行说明](PERFORMANCE.md) 和 [发布准入说明](RELEASE.md)。
