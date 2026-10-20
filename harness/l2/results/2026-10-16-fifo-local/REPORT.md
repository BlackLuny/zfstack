# QueueControl 本机原生 ARM64 自检：资格未通过

2026-10-16（Asia/Ho_Chi_Minh）。接手 [PR #75](https://github.com/BlackLuny/zfstack/pull/75) 后，按用户要求尝试本机运行。执行源码为 `077f54d9c264f82f82d8a22cd58e8d40db48b3bb`，注入器、oracle、case v2、负载及 20 ms 上限均未修改。

**三轮 full 自检全部 FAIL；overflow 负例通过；cancel 负例额外出现 1 个链路 drop，自检未完整通过。没有冻结 worker 构建，没有执行 held-out 正式 C16，QueueControl 保持 known-BLOCKED。** 这些记录不能用于解除 #59 或签发 Nightly/性能资格。

## 环境与执行边界

- Apple M1 Max，ARM64，64 GiB；现有 Docker Desktop Linux VM 原生 `aarch64`，内核 `7.0.12-linuxkit`，10 vCPU、约 8 GiB。不是 x86/TCG 模拟。
- 新建独立 `zfstack-pr75-qa` 容器，限制 6 CPU、6 GiB、1024 PID；镜像身份与挂载见 [container-provenance.json](container-provenance.json)，VM 信息见 [docker-environment.json](docker-environment.json)。宿主有其他桌面负载，没有物理资源独占证明。
- 每种模式均通过原 `qa/isolate-linux.sh` 建立私有 mount/network/PID namespace，并由 `isolation.verify()` 验证后创建测试拓扑。tun、clsact、matchall、mirred、AF_PACKET vnet/qdisc bypass 已在实际数据路径中使用；另在私有 namespace 实测了 WireGuard、bridge 和 nft JSON，见 [能力探测](local-capabilities.log)。
- 首轮 full 与 native L2 单元测试重叠，明确保留为受本任务额外负载影响的诊断。随后先写入 [固定次数计划](local-selftest/repetition-plan.json)，再串行执行恰好两轮 full；这两轮没有并行单元测试或编译。没有继续重复直到通过，也没有选择最好一轮作为资格结果。
- 三模式执行顺序为 full → cancel → overflow，再执行预先固定的 full-2 → full-3。overflow 与初次 native QA 工具检查存在重叠；它不提供时序资格。全部尝试均保留。
- 容器 cgroup 未记录 CPU quota throttling 或 OOM，见各轮 `*-before.log` 及 [最终状态](container-final-state.log)。这不能排除 macOS/VM 调度或其他宿主干扰，也不能单凭这几轮断定迟到根因。

## 逐帧复算

以下最大迟到均从 `transmit_offset - release_offset` 重算，单位 ms；不采信报告里的结论或 summary。

| full 运行 | pre | active | drain | transparent | 判定 |
|---|---:|---:|---:|---:|---|
| full（与工具测试重叠） | 130.921083 | 76.213917 | 1.199384 | 13.022792 | FAIL |
| full-2（串行补测） | 16.465125 | 40.402208 | 3.873807 | 13.183208 | FAIL |
| full-3（串行补测） | 10.612125 | 74.383001 | 0.856438 | 24.315750 | FAIL |

三轮均没有早发；其余 12 项自检检查通过：每轮 18475 个 UDP 全到、有序、载荷一致，TCP 字节一致，发送方 0 EAGAIN，链路 drop 增量为零，FIFO 中帧全部 DELIVERED，GSO 保留，注入延迟不低于 500 ms，recovery 透明、qdisc 恢复、cleanup PASS。这里的业务检查来自各轮保存的原始发送/接收文件与自检结果；逐帧 oracle 和上表可通过下方入口重新计算。

三个 oracle 分别先在 seq 4888、21147、22250 拒绝迟到超限。收尾期三轮均低于 20 ms，但 active/pre/transparent 的失败仍使整项失败。本机没有复现此前 TCG 的收尾吞吐限制；这不证明任何真实 CPU 都能满足整个注入器的时序要求。

| 负例 | 自检 | 证据与限制 |
|---|---|---|
| cancel | FAIL | oracle 拒绝取消和不完整阶段；账本中帧全交付、TCP 一致、无重复、qdisc 恢复、cleanup PASS。但 worker `tx_dropped` 增量为 1，UDP 缺 1 个；自检将该 UDP 的发送归到 redirect 删除之后。没有逐时 eth0 原始计数快照，不能把这 1 个 drop 确定归因于内核版本或删除动作。 |
| overflow | PASS（预期 oracle FAIL） | oracle 以 seq 22259 溢出拒绝；UDP 缺失数等于账本中的 UDP 溢出数；其余自检检查通过，qdisc 恢复、cleanup PASS。该模式被接受不表示它的正常释放时序合格。 |

所有每帧账本、注入器原始内核计数、发送/接收文件、自检 stdout 和退出码在 [local-selftest](local-selftest)。取消时的提前 flush 属于明确失败的负例，不得作为正常调度的早发豁免。

```sh
python3 harness/l2/results/2026-10-16-fifo-local/recompute.py
```

输出应与 [recomputed.json](recomputed.json) 逐字段相同。整个目录的大小/哈希清单见 [artifact-index.json](artifact-index.json)（索引不包含自身）。

## 工具检查与保留的准备失败

- Linux native L2：96 项通过、0 skip，见 [linux-l2-tests.log](linux-l2-tests.log)。
- Linux native QA：208 项、1 个原有 skip，通过，见 [linux-qa-tests-checkout.log](linux-qa-tests-checkout.log)。
- macOS QA：208 项、5 项平台 skip，通过，见 [qa-tests.log](qa-tests.log)；macOS L2 为 96 项、2 项平台 skip，输出在工具会话中，未保存独立日志。
- 测试清单审计通过，见 [coverage.json](coverage.json)。
- 首次 macOS QA 失败是系统 Python 缺 `jsonschema`；随后建立临时 venv，严格安装 `qa/requirements.lock` 后重跑。该首轮仅工具会话保留输出，本目录不宣称有完整原始日志。初次 native QA 从 `/src` 执行时，worktree 的 `.git` 指向容器外路径，187 项中 1 个错误；[失败日志](linux-qa-tests.log) 保留，随后从同一冻结 SHA 的独立干净 clone 执行完整 208 项。
- 容器第一次准备命令早于 `docker run` 就绪，未执行，见 [container-setup-before-ready.log](container-setup-before-ready.log)；实际依赖安装见 [container-setup.log.gz](container-setup.log.gz)。
- 已从之前冻结的 ZFC/private_tun/shadow_tls/rig 身份及本次 ZFStack SHA 导出允许的构建输入，见 [prepared-source-archive-manifest.json](prepared-source-archive-manifest.json)。这只是源码准备，**没有构建 worker，也没有四组合构建或正式运行结果**。

## 资格与下一步

按照 [维护者清单](../../../../qa/MAINTAINER-CHECKLIST.md)，当前本机仍未通过自检资格，因此正式 held-out C16 保持 NOT_RUN。需要先获得满足固定 20 ms 上限、且取消负例也完整通过的隔离执行环境，再冻结 native worker、公共物理域 Lease 与磁盘/内存守卫，执行一次正式 baseline/fault 双臂并独立重判。不能用这些自检或工具测试替代该正式证据。

原 TCG 三轮及所有历史失败均未覆盖；本次没有修改生产代码、资格常量、case、known-failures、PR 草稿状态或 issue 状态。宿主与容器的执行环境限制如实保留。

本次独立容器已停止并删除，见 [cleanup.json](cleanup.json)；临时构建输入随后清理，已有 Docker VM 保持原状态。
