# L2：ZFC + WireGuard 集成混合负载

这是可选 Linux 集成入口，复用现有 `zfc-rig` 的拓扑、端口配置生成器、假控制器和路径计数器。它使用真实内核 WG 客户端、ZFC worker 的 WG 解密/UDP 路径、ZFStack 生产 Tokio adapter 与 Direct 出站。主 L0/L1 矩阵不依赖它。

两个真实 WG 监听端口，各有两个独立 peer。peer 0 开四条下载；peer 1 是同端口轻用户，peer 2/3 是另一端口的轻用户。所有轻请求携带 peer 与序号，重流使用逐连接字节模式，接收端逐字节校验。共享一台宿主机上的 Python 内核 TCP 应用端点会限制吞吐，结果属于整条集成路径。

默认阶段是 8 秒空载、16 秒加载、8 秒恢复。轻请求间隔 10 ms；输出每个请求的原始样本、每用户 P50/P95/P99/P99.9、重下载有效字节与 goodput、每 0.5 秒 worker CPU/RSS/线程 CPU、每 5 秒 ZFStack 预算与 adapter 诊断。P99 至少需要 100 个样本，P99.9 至少需要 1000 个。CPU 是整个 worker（含 WG、UDP、控制通道），不能解读为 core CPU。

## 运行

需要预先安装的 Linux `zfc-rig`、root、`ip`、`wg`、`nft`、Python 3 和构建好的 worker。worker 须明确使用所测 ZFStack 源码；仅看系统已安装 worker 的版本不能证明这一点。记录 ZFC/private_tun/ZFStack SHA、源码修改和二进制 hash。临时编译只选 `-p zf-worker`；本次示例使用 `fast-release`（非发版性能口径）。

在指定测试节点持有 `zfctest` 节点租约，再执行：

```sh
bash harness/l2/run-rig.sh /root/zfc-rig /absolute/path/to/candidate/zf-worker /absolute/path/to/new-output
```

可用 `L2_MEMORY_MB=8 L2_HEAVY_CONNS=16` 探索小预算下的集成推进。是否实际撞到额度，须看 `worker.log` 的 `share_blocked`、`fail_level_*` 等诊断；小额度配置本身不构成压力证据。默认 64 MiB 是测试进程的配置，不修改产品默认值。

轻用户 socket 默认读超时为 3 秒；可用 `L2_REQUEST_TIMEOUT_SECS=30` 做长尾诊断，输出记录此设置。诊断通过不能覆盖默认超时的失败。首次实测及原始样本见 [`results/2026-10-12/REPORT.md`](results/2026-10-12/REPORT.md)：正常预算通过，小预算同端口出现超过 3 秒的尾延迟。

脚本暂时停止 rig watcher 及其独立的 `rig-watch-*.scope` 批次，持有 `watch.lock`，等待原用例的退出清理，再创建自己代际标识的隔离 netns/veth；退出时杀掉测试进程、删除拓扑和 undo timer、恢复原 watcher 状态。建议通过 systemd 或 `timeout 180 bash ...` 运行，节点租约由调用方续期。它不修改面板数据库和已安装的 `zf` 服务。等待后仍有命名空间或其他 rig owner 时拒绝开始。

`result.json` 只有在全部流校验/推进、两端口 ZFStack 启动证据、每个客户端的 WG 路径以及 target → worker 载荷计数成立时才为 `ok=true`。客户端到 target 的物理捷径计数必须为零。原始结果在 `load/client-*.json`，资源采样在 `load/worker-samples.json`，路径计数在 `path-pre.json` / `path-post.json`，worker 配置指纹与预算在 `worker.log`。

案卷包含临时 WG 密钥、控制器密钥与配置；共享结果仅导出结果、原始负载样本、资源/路径计数、环境与构建出处，保留密钥在测试节点。`namespaces-after.txt` 与 `watcher-after.txt` 记录清理结果。

这是小规模功能与负载切换验证。没有声明百端口容量、WAN 丢包/漫游/重建覆盖，也没有 AB/BA 重复或数值回归门槛；不能据此比较 TCP core 与内核的性能。
