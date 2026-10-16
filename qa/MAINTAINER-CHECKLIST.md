# 维护者手工启用清单

以下操作尚未执行。本次仅提交仓库实现、原始结果和本地 PR 描述，不 push、不开远程 PR，也不改变仓库设置。QueueControl 保持 [known-BLOCKED](issues/queue-control-atomic-restoration.md)，性能默认 shadow；不能把代码合入当作全部资格完成。

- [ ] 审查并将 `QA_TRUSTED_REF` 指向受保护、可信的 reporter 完整 40 字符提交 SHA；确认不从待测 PR 获取可信评估器，检查 fork 权限及 artifact 身份边界。
- [ ] 在分支保护/规则集中将实际 `qa/gate` 配置为 required check；通过试验 PR 验证缺 job、错误 SHA、失败、取消和被篡改证据都阻止合并。
- [ ] 将 `.github/CODEOWNERS` 中负责 QA 策略、阈值、评估器、工作流与 baseline 的所有权落实到真实维护者/团队，并启用相应审查保护。
- [ ] 配置专用、可证明物理资源与完整 cgroup 祖先限额的性能节点；审查产品 SLO/预算，完成独立构建 A/A、held-out null/slowdown 校准，再决定是否从 shadow 进入强制门禁。现有共享节点性能 INVALID 不可豁免。
- [ ] 在 Linux aarch64 与 Windows 原生环境执行同一发布 SHA 的必要测试与安全能力检查；缺平台/缺能力必须保留 NOT_RUN/BLOCKED。
- [ ] 冻结跨仓双方 base/head 与工具链，实际执行完整 2×2 四组合并保存 provenance；现有 head-head 不代表完整归因。
- [ ] 在具备必要能力的隔离宿主完成 QueueControl 原子恢复的 held-out 正式运行。持久 FIFO 注入器（case v2 `fifo-delay-line`）已实现并冻结，需要 worker namespace 内的 `/dev/net/tun`、`sch_ingress`（clsact）、`cls_matchall`、`act_mirred`、AF_PACKET 的 `PACKET_VNET_HDR`/`PACKET_QDISC_BYPASS` 及 iproute2 JSON 输出；不需要 NFQUEUE。先在该宿主的 `qa/isolate-linux.sh` 内跑 `harness/l2/fifo_selftest.py` 三种模式确认能力，再冻结 worker 构建，执行一次正式 C16-l2-queue-control 并保留结果；不择优重跑，失败照实保留。通过前不移除 known-BLOCKED。

  2026-10-16 本机后续诊断和取消修复见 [原始证据与复验](../harness/l2/results/2026-10-16-fifo-diagnosis/REPORT.md)：`1b19023` 保持路径直到父进程回收客户端并结束测量，cancel/overflow 负例通过；正常 full 仍迟到超限。捕获的调度窗口证明 VM vCPU 在宿主上没有及时执行，正式 C16 尚未运行。不能把本机负例通过当作完整时序资格。
- [ ] 复验完整发布 SHA 的平台、Nightly、安全、集成与性能证据闭包，并按团队策略将本地完整归档上传到受访问控制的持久 artifact 存储；本次只在本机保存 tar.zst。

具体配置与威胁模型见 [README](README.md)、[RELEASE](RELEASE.md)、[PERFORMANCE](PERFORMANCE.md)，实际执行边界见 [VALIDATION](VALIDATION.md)。
