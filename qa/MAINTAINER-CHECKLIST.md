# 维护者手工启用清单

以下操作尚未执行。本次仅提交仓库实现、原始结果和本地 PR 描述，不 push、不开远程 PR，也不改变仓库设置。QueueControl 保持 [known-BLOCKED](issues/queue-control-atomic-restoration.md)，性能默认 shadow；不能把代码合入当作全部资格完成。

- [ ] 审查并将 `QA_TRUSTED_REF` 指向受保护、可信的 reporter 完整 40 字符提交 SHA；确认不从待测 PR 获取可信评估器，检查 fork 权限及 artifact 身份边界。
- [ ] 在分支保护/规则集中将实际 `qa/gate` 配置为 required check；通过试验 PR 验证缺 job、错误 SHA、失败、取消和被篡改证据都阻止合并。
- [ ] 将 `.github/CODEOWNERS` 中负责 QA 策略、阈值、评估器、工作流与 baseline 的所有权落实到真实维护者/团队，并启用相应审查保护。
- [ ] 配置专用、可证明物理资源与完整 cgroup 祖先限额的性能节点；审查产品 SLO/预算，完成独立构建 A/A、held-out null/slowdown 校准，再决定是否从 shadow 进入强制门禁。现有共享节点性能 INVALID 不可豁免。
- [ ] 在 Linux aarch64 与 Windows 原生环境执行同一发布 SHA 的必要测试与安全能力检查；缺平台/缺能力必须保留 NOT_RUN/BLOCKED。
- [ ] 冻结跨仓双方 base/head 与工具链，实际执行完整 2×2 四组合并保存 provenance；现有 head-head 不代表完整归因。
- [ ] 在具备必要能力的隔离宿主完成 QueueControl 原子恢复的 held-out 正式运行。持久 FIFO 注入器（case v2 `fifo-delay-line`）已实现并冻结，需要 worker namespace 内的 `/dev/net/tun`、`sch_ingress`（clsact）、`cls_matchall`、`act_mirred`、AF_PACKET 的 `PACKET_VNET_HDR`/`PACKET_QDISC_BYPASS` 及 iproute2 JSON 输出；不需要 NFQUEUE。先在该宿主的 `qa/isolate-linux.sh` 内跑 `harness/l2/fifo_selftest.py` 三种模式确认能力，再冻结 worker 构建，执行一次正式 C16-l2-queue-control 并保留结果；不择优重跑，失败照实保留。通过前不移除 known-BLOCKED。

  2026-10-16 本机后续诊断和取消修复见 [原始证据与复验](../harness/l2/results/2026-10-16-fifo-diagnosis/REPORT.md)：`1b19023` 保持路径直到父进程回收客户端并结束测量，cancel/overflow 负例通过；正常 full 仍迟到超限，正式 C16 尚未运行。旧报告使用未经结清的宿主累计计数作为 CPU 上界，相关归因范围以后续同步观测报告中的修订为准。不能把本机负例通过当作完整时序资格。

  修复后增加同步观测的 [CPU 调度对照](../harness/l2/results/2026-10-16-fifo-scheduling/REPORT.md) 保留六轮诊断 full 和一组三模式无插桩复验：154.76 ms 窗口在宿主 CPU 计数结清后仍主要缺少 vCPU 执行时间；guest 绑定、SCHED_FIFO 与 VM 10→4 vCPU 均未消除超限。无插桩 full 最大迟到 24.05 ms，仍高于冻结的 20 ms；cancel/overflow 负例通过，worker 构建与正式 C16 继续 NOT_RUN，known-BLOCKED 保留。VM 资源设置已恢复。

  用户指定新 KVM VPS 的 [三模式自检及正式 C16](../harness/l2/results/2026-10-16-fifo-remote23/REPORT.md)：自检通过，full 最大迟到 10.51 ms；原生 worker/fakectl 冻结构建后，只执行一次正式 baseline/fault。基线通过，故障臂有 35 个 UDP 超时、172 次发送失败，最大释放迟到 162.72 ms、透明切换晚 3.17 ms，独立评估 FAIL/VALID。20 ms 上限未改，known-BLOCKED 保留。准备阶段的 Python 补丁包升级及完整失败构建记录一并保留，现有服务 PID/容器健康核对见报告。

  正式故障臂的 [离线根因分析](../harness/l2/results/2026-10-16-fifo-formal-analysis/REPORT.md) 显示，失败来自用户态发送容量不足和账本外的读取前等待。候选 `netem-fifo` 的 [仿真自检](../harness/l2/results/2026-10-16-netem-fifo/REPORT.md) 中，出口 FIFO 精确，负例通过，但 full 因 veth 下游跨 CPU 投递导致 UDP 乱序而 FAIL。预注册对照中普通 netem 未出现乱序，因此不放宽判据。下一步是在真机上做三模式自检，步骤见 PR #75；全部通过之前，不要以 `netem-fifo` 登记 case、冻结 worker 或执行正式运行。若采用它，宿主还需 `sch_netem`、`dummy` 及 AF_PACKET TPACKET_V3。
- [ ] 复验完整发布 SHA 的平台、Nightly、安全、集成与性能证据闭包，并按团队策略将本地完整归档上传到受访问控制的持久 artifact 存储；本次只在本机保存 tar.zst。

具体配置与威胁模型见 [README](README.md)、[RELEASE](RELEASE.md)、[PERFORMANCE](PERFORMANCE.md)，实际执行边界见 [VALIDATION](VALIDATION.md)。
