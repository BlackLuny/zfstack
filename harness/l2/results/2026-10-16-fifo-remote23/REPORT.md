# 新宿主：三模式自检通过，正式 C16 失败

2026-10-16，用户指定 `root@23.136.204.72:23826`。测试源码冻结为 **`16b0d13a0fba59dc4748b530bd854a9cccd5ea70`**；注入器代码与取消修复 `1b19023` 相同。接续 [本机同步观测](../2026-10-16-fifo-scheduling/REPORT.md)。

**三种自检通过；随后只执行一次正式 C16 baseline/fault，结果为 FAIL / VALID。20 ms 上限、case v2、业务负载和合同均未改，known-BLOCKED 保留。没有择优重跑，没有关闭 issue 或改变 PR 状态。**

## 宿主及执行边界

- Debian 12、Linux **6.1.0-29-amd64**，KVM 原生 x86_64，呈现 Xeon Platinum 8260、4 vCPU（2 core × 2 thread）、约 8 GiB RAM，初始磁盘可用约 210 GiB。不是 TCG。
- 这台 VPS 有现有 ZFC、Docker、数据库等服务，**物理 CPU 独占和 hypervisor 争用情况没有证明**。node 的 `isolated` 指由 `qa/isolate-linux.sh` 验证的私有 network/mount/PID namespace 及可重置测试拓扑；node 同时明确记录 `shared_KVM_VPS: true`、`physical_CPU_exclusivity: UNPROVEN`、`performance_certified: false`。不能把功能证据当成独占性能节点资格。
- tun、clsact、matchall、mirred、AF_PACKET VNET_HDR/QDISC_BYPASS 均实际探测通过；另在同一私有 namespace 验证 WireGuard、bridge、nft。见 [能力证据](selftests/evidence/capabilities.json)、[初始环境](selftests/evidence/host-before.json)。原有服务、watcher 和宿主网络未由测试拓扑入口管理。
- 自检、准备和构建/测量使用公共 QA 租约目录 `/var/lock/zfstack-qa`，域为 `zfstack-qa-vps-23.136.204.72-23826`，generation 为实测 boot ID。该租约串行化本次 QA 操作，不声称约束现有生产服务或外部 hypervisor。
- 全部运行保持默认调度，没有 taskset、实时优先级或修改宿主 CPU 资源配置。没有并行启动其他测试/编译进入测量窗口。

## 三模式：各一次，原始账本复算

先写 [自检计划](selftest-plan.json)，顺序 full → cancel → overflow，每种一次，默认参数、20 ms 全程上限不变。三次均在原 wrapper 内执行，公共租约正常释放。

下表单位 ms，来自每帧 `out_offset - due_offset`，不是报告 residence summary。

| 模式 | pre | active | drain | transparent | 自检 / oracle |
|---|---:|---:|---:|---:|---|
| full | 4.263076 | 8.804668 | 0.994427 | **10.513299** | PASS / PASS |
| cancel | 1.500405 | 9.304065 | 1.121251 | 7.361675 | 负例 PASS / 因取消 FAIL |
| overflow | 6.240597 | 4.301528 | 0.172371 | 0.519474 | 负例 PASS / 因溢出 FAIL |

full 的 **60266** 帧全部 DELIVERED，18475/18475 UDP 恰好一次、有序、内容一致，TCP 字节一致，0 EAGAIN、零链路 drop，500 ms 延迟及透明恢复、qdisc 恢复、cleanup 均通过。cancel 的 **60258** 帧全部交付，18475/18475 UDP 全到、有序、零链路 drop，路径保留到客户端回收与测量结束之后；它不提供正常运行资格豁免。overflow 的 **4951** 帧溢出，其中 **4882** UDP，恰好等于 18475 发 / 13593 收的缺口；无额外链路 drop。

见 [复算](selftests-recomputed.json)、[原始三模式执行及租约](selftests/evidence/selftest-execution.json)。这是内核能力及低负载注入器自检，不能外推正式业务负载也满足同样时序。

## 冻结构建与保留的准备失败

[构建及正式计划](build-and-formal-plan.json) 在导出、构建前写入。原生 Rust **1.98.1**、`fast-release`、默认 features 加 `mmsg/jemalloc`、最多 2 个编译任务，使用新的任务目录和 target cache。冻结输入为：

| 仓库 | SHA |
|---|---|
| ZFC | `975c677d2920c86a14dcd1e08ae743abf7b7be1c` |
| ZFStack | `16b0d13a0fba59dc4748b530bd854a9cccd5ea70` |
| private_tun | `ca922c518992426bfaea4a2d60abf653c5c694de` |
| shadow-tls-tokio | `787b8e261a919689f90d9d89ee9362b344da56df` |
| rig | `8c9e737ed505306b97c9f2971635c75808fbc04d` |

只构建 head-head，其余三组合是 UNEXECUTED；base/head 使用相同身份，不提供 2×2 归因。[冻结源码清单](frozen-source-archive-manifest.json) 与 [最终 worker manifest](collected/build-receipts/build-4/head-head/worker-manifest.json) 保留允许列表、源码转换、锁文件、依赖图及实际命令。

四次构建入口的记录都保留，前三次没有进入正式测量：

1. macOS BSD tar 自动加入 **8288 个 AppleDouble `._` 旁文件**，严格源码 inventory 校验拒绝，零编译命令。原失败包保留在远端；[双包成员清单](package-member-inventories.json.gz) 和传输哈希可核对。随后用 Python tarfile 传输同一允许列表字节，新目录不含旁文件。
2. 冷缓存 `cargo metadata` exit 0，但下载信息 stderr 与 JSON stdout 被现有 runner 合并；严格 JSON 解析拒绝该日志。原始混合输出保留。随后使用 [Cargo 官方日志静默选项](https://doc.rust-lang.org/cargo/reference/config.html#termquiet) `CARGO_TERM_QUIET=true`，只改变信息输出，编译参数和源码不变。
3. 首次实际 worker 编译在 bindgen 处失败，缺少 `libclang` 共享库。补齐依赖后使用新输出目录继续，复用已完成的依赖编译缓存。
4. worker 与 fakectl 实际构建成功，9 条命令全部 PASS，命令累计约 **789.36 秒**；构建租约 release PASS。没有将先前未完成或系统已装 worker 作为本次构建。

前三次失败的 worker manifest / command logs / cleanup 与 release 均见 [构建收据](collected/build-receipts)，摘要见 [准备失败](build-preparation-failures.json)。最终 worker SHA-256 为 **`fd6e102ccbc68c4452494adda2dce75f872d821987dae8b2b822595b9986f05a`**，两臂使用同一二进制。构建释放租约后固定等待 5 秒，正式 runner 再取同域租约。

## 正式 C16：一次完整双臂，FAIL / VALID

冻结 case **C16-l2-queue-control v2**：500 ms、100000 包、8/16/8 秒、250 ms guard、3 秒请求 deadline、250 ms 服务合同，以及原来的 4 heavy、3 light、4 UDP 业务均保持。正式执行约 **89.18 秒**，运行命令及退出码见 [执行记录](collected/evidence/build-formal-execution-4.json)，测量前绑定见 [预注册](collected/evidence/formal-preregistration.json)。

远端独立 verifier 和取回后的本地独立 verifier 都判 **FAIL / VALID**，execution cleanup PASS，未用 expected loaded latency failure 覆盖真实载荷失败。见 [原始 v2 结果](formal-artifacts/97fea515847c672e859bfd6fe4d1c746d6f4c08de7b30e437a76eb0917c29fea/result.json)、[本地完整评估](local-evaluation.json)、[逐帧及业务复算](formal-recomputed.json)。

| 观察 | 基线 / 故障结果 |
|---|---|
| 基线 | raw functional PASS、服务合同 PASS；4 个 UDP peer 各 3200/3200 完成、零超时 |
| 故障 loaded 服务合同 | 如预期 FAIL，但这不足以使负对照通过 |
| 故障 UDP | 4 peer 分别 **9 / 9 / 8 / 9** 个超时，共 **35** 个，均为 loaded 请求；没有漏发/未完成来替代它们 |
| recovery 的 3 个 light 服务合同 | PASS；这不能覆盖 UDP 失败或注入器资格失败 |
| FIFO 释放最大迟到 | pre **0.444710**、active **162.718680**、drain **161.861810**、transparent **15.280892 ms** |
| 超 20 ms 的已交付帧 | **177115**，不符合资格 |
| 透明切换 | 比冻结 recovery 边界晚 **3.168893 ms**，严格拒绝；没有套用采样容差豁免 |
| FIFO 账本 | **355584** 帧：**355412 DELIVERED、172 SEND_FAILED**，零 OVERFLOW |
| 内核链路计数 | worker eth0 tx.dropped **+172**，client0 eth0 rx.dropped **+5** |
| tap / mirred | 355584 帧、447310894 字节逐一闭合，各自 drops/errors 为零 |
| 占用与清理 | 最大 22526 帧 / 30089968 字节，未超 100000 包 / 256 MiB；qdisc 恢复、cleanup PASS |

**172 次发送失败全部发生在 ACTIVE，测量开始后 `19.570766853–19.571394318 s`**，与 worker 出口 drop 增量精确等数。安装早于测量开始，拆除晚于测量结束；这 172 个失败都在中间的 loaded 测量窗口。账本保留了 SEND_FAILED，但原实现没有保存 errno / 短写长度；这次又未插入逐时调度及内核 drop trace，不能进一步断言是 ENOBUFS、宿主抢占、虚拟 CPU 供给不足还是某个具体内核拒绝路径，也不能将每个加密帧与 35 个 UDP 超时逐一配对。client0 的 5 个 rx drop 是另外保留的观测。

正式负载的帧形状/吞吐与自检不同。自检通过证明这台内核支持路径；正式数据证明它在本次完整业务负载下仍不合格。没有把大量 active/drain 迟到简单归为本机 Mac 已捕获的那种宿主停顿，也没有用“机器负载”抹去 SEND_FAILED 和 UDP 超时。

报告里的 `queue control lacks the actual loaded phase interval` 是前置 control_errors 非空后落入通用 else 的派生诊断；独立复算确认 `start_at` 和完整 planned 窗口逐字段匹配。它不是另一个已经证明的时间戳缺失根因，真实的迟到、发送失败和 drop 仍足以严格拒绝。

只执行该 C16 实例，其他 Nightly case 未执行；不能宣称完整 Nightly 或发布资格。独占性能节点资格同样未建立。

## 宿主变更与收尾

准备依赖时 apt 新装 CMake、venv 及 libclang 开发依赖。**venv 的依赖解析同时将 7 个 Python 3.11 Debian 包升级到 `3.11.2-6+deb12u9`**；`--no-upgrade` 没有挡住间接依赖升级，这是准备过程未预先拦住的宿主变更。libclang 那次安装为 0 upgraded / 9 newly installed。原事务日志完整保留，没有将其描述为“宿主完全未变”。原执行准备脚本也单独保存，当前脚本文档已更正。

核对时现有 ZFC worker、Web、控制面、Redis、Docker/containerd 等主 PID 与准备前一致，7 个现有容器全部 healthy，见 [变更后检查](collected/evidence/package-impact-check.json) 与 [最终状态](collected/evidence/host-final.json)。needrestart 的列表不被当作服务已经重启的证据；PID/健康检查也不等于完整业务影响测量。没有重启机器、切内核或回滚这些补丁包。

原 wrapper 已回收测试进程和私有拓扑；公共租约为空，宿主注册 namespace 已核对。冻结源码、二进制、缓存、失败包和全部原始运行留在任务专用目录 **`/root/zfstack-qa-pr75-20261016-remote23-01`**，便于后续定位；不会清理原有生产工作区。见 [收尾检查](cleanup-remote.json)。测试生成的 WG keys、runtime 配置及二进制不进入本地 allowlist 正式归档。

**known-BLOCKED 保留，issue 和草稿 PR 状态不变。** 下一步的证据缺口是完整负载下 AF_PACKET 写回失败的具体错误，以及注入器/guest/hypervisor 的同步调度观察；本次没有为得到绿色结果再次运行正式 C16。

离线复算（依赖 `qa/requirements.lock`，不会新发业务流量）：

```sh
python3 harness/l2/results/2026-10-16-fifo-remote23/recompute_selftests.py
python3 harness/l2/results/2026-10-16-fifo-remote23/recompute_formal.py
```

取回时的原始收据哈希见 [collected-artifacts.index.json](collected-artifacts.index.json)，正式 allowlist 包及其成员索引保留于 `collected/evidence/`。本次全部文件大小/SHA-256 见 [artifact-index.json](artifact-index.json)，索引不含自身；压缩日志保持原字节，解压大小/哈希见 [compressed-logs.json](compressed-logs.json)。此前 TCG、本机、取消缺陷和调度对照的全部失败记录未覆盖。
