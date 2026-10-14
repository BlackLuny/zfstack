# L2：ZFC + WireGuard 集成混合负载

这是可选 Linux 集成入口，复用现有 `zfc-rig` 的拓扑、端口配置生成器、假控制器和路径计数器。它使用真实内核 WG 客户端、ZFC worker 的 WG 解密/UDP 路径、ZFStack 生产 Tokio adapter 与 Direct 出站。主 L0/L1 矩阵不依赖它。

两个真实 WG 监听端口，各有两个独立 peer。peer 0 开四条下载；peer 1 是同端口轻用户，peer 2/3 是另一端口的轻用户。所有轻请求携带 peer 与序号，重流使用逐连接字节模式，接收端逐字节校验。共享一台宿主机上的 Python 内核 TCP 应用端点会限制吞吐，结果属于整条集成路径。

默认阶段是 8 秒空载、16 秒加载、8 秒恢复。轻请求间隔 10 ms；输出每个请求的原始样本、每用户 P50/P95/P99/P99.9、重下载有效字节与 goodput、每 0.5 秒 worker CPU/RSS/线程 CPU、每 5 秒 ZFStack 预算与 adapter 诊断。P99 至少需要 100 个样本，P99.9 至少需要 1000 个。CPU 是整个 worker（含 WG、UDP、控制通道），不能解读为 core CPU。

## 运行

需要预先安装的 Linux `zfc-rig`、root、`ip`、`tc`（支持 JSON 统计）、`wg`、`nft`、Python 3 和构建好的 worker。worker 须明确使用所测 ZFStack 源码；仅看系统已安装 worker 的版本不能证明这一点。记录 ZFC/private_tun/ZFStack SHA、源码修改和二进制 hash。临时编译只选 `-p zf-worker`；本次示例使用 `fast-release`（非发版性能口径）。

在指定测试节点持有 `zfctest` 节点租约，再执行：

```sh
bash harness/l2/run-rig.sh /root/zfc-rig /absolute/path/to/candidate/zf-worker /absolute/path/to/new-output
```

可用 `L2_MEMORY_MB=8 L2_HEAVY_CONNS=16` 探索小预算下的集成推进。是否实际撞到额度，须看 `worker.log` 的 `share_blocked`、`fail_level_*` 等诊断；小额度配置本身不构成压力证据。默认 64 MiB 是测试进程的配置，不修改产品默认值。

轻用户 socket 默认读超时为 3 秒；可用 `L2_REQUEST_TIMEOUT_SECS=30` 做长尾诊断，输出记录此设置。诊断通过不能覆盖默认超时的失败。首次实测及原始样本见 [`results/2026-10-12/REPORT.md`](results/2026-10-12/REPORT.md)：正常预算通过，小预算同端口出现超过 3 秒的尾延迟。后续 [预算对照与包级归因实验](results/2026-10-13-cause/REPORT.md) 记录 8→64→64→8 MiB 重复、小请求重传时间线，以及把物理压力限制在重 peer 的对照。

RX 信用修复的 [12 组 ABBA 对比](results/2026-10-13-rx-credit/REPORT.md) 保持 3 秒超时和产品默认缓冲，覆盖 8 / 64 MiB，以及 8 MiB 下增加 50 ms 客户端上行延迟、持续加载 64 秒。`L2_UPLINK_DELAY_MS` 在每个客户端的 `wg0` 出方向加 netem，影响请求和下载 ACK；不是双向 WAN 仿真。`L2_BASELINE_SECS` / `L2_LOADED_SECS` / `L2_RECOVERY_SECS` 可调整观察窗口，默认仍为 8 / 16 / 8 秒。

netem 显式使用 `L2_NETEM_LIMIT_PACKETS=100000`（单位：包），可覆盖为其他正整数，但同一对照的各臂必须相同。该上限是延迟队列容量，不是额外时延。以前隐式使用的 1000 包上限在 256 流 ACK 方向造成大量丢包，污染了 #37 的延迟吞吐/停顿结论，见[更正](../../docs/experiments/2026-10-14-issue37-netem-correction.md)。增大上限仍不能替代核对丢包计数。

每次负载前后，四个客户端均保存 `qdisc-pre-N.json` / `qdisc-post-N.json`（`tc -j -s`）和同名 `.txt`（`tc -s`）；`qdisc.json` 及 `result.json.qdisc` 汇总实际 root qdisc、参数和 bytes/packets/drops 增量，控制台也打印各客户端丢包数。中断退出时尽量在销毁拓扑前保留 post 快照。该入口是仅加延迟的台架：测量期出现任何 qdisc 丢包、统计缺失/无效、计数器重置或队列配置改变，均作为台架错误使 `ok=false`。原始统计和负载数据仍保留，不能把它们用于声称无丢包条件下的产品回归。

issue #37 增加两个必须单独比较的配置。空闲模式先在 peer 1 建立只连不发的连接，再运行原有四 peer 的下载与轻请求矩阵；全部测量结束后才关闭空闲连接，并检查 EOF/RST。`result.json.idle` 记录请求数、建立数、连接错误和最终存活数；建立不足或中途断开判失败，仍保留负载、路径和资源证据。脚本按连接数提高进程 fd 软限制，节点的硬限制必须足够。

```sh
# 8 MiB port + 1500 idle connections, then four downloads from peer 0.
L2_MODE=idle L2_MEMORY_MB=16 L2_IDLE_CONNS=1500 L2_HEAVY_CONNS=4 \
  bash harness/l2/run-rig.sh /root/zfc-rig /absolute/path/to/zf-worker /absolute/path/to/idle-output
# Minimum send share, with and without 50 ms extra ACK-path delay.
L2_MEMORY_MB=64 L2_HEAVY_CONNS=256 \
  bash harness/l2/run-rig.sh /root/zfc-rig /absolute/path/to/zf-worker /absolute/path/to/floor-output
L2_MEMORY_MB=64 L2_HEAVY_CONNS=256 L2_UPLINK_DELAY_MS=50 L2_LOADED_SECS=32 \
  bash harness/l2/run-rig.sh /root/zfc-rig /absolute/path/to/zf-worker /absolute/path/to/rtt-output
```

每种配置按 base/candidate/candidate/base 运行，输出目录独立；不能用低并发的原有矩阵代替。空闲建连阶段默认最多 120 秒，可用 `L2_RAMP_SECS` 调整；这段不计入 goodput 分母。运行外层 timeout 时应包含该阶段。以上是复现入口，不代表当前修改已经完成真实 WG ABBA 实测。

`L2_CAPTURE=1` 开启同端口轻 peer 的内层 TCP 与 target 载荷抓包，要求节点已有 `tcpdump`；同时记录单调钟/实时时钟映射及 server 请求序号时间线。共享前仅导出白名单案卷，排除密钥与 worker/controller 配置。以下比较器要求先生成 `analysis.json`，检查候选请求错误、各阶段推进、原始 3 秒截止时间、抓包丢包、重传、重流完整性、同一负载与固定二进制。它保留基线失败，不用成功请求分位数隐藏超时：

```sh
python3 harness/l2/results/2026-10-13-cause/analyze.py harness/l2/results/2026-10-13-rx-credit
python3 harness/l2/compare.py harness/l2/results/2026-10-13-rx-credit
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s harness/l2 -p 'test_*.py'
```

比较器对基线和候选都检查 qdisc 证据，并把 netem 包上限加入负载一致性检查。上述历史案卷的 delayed 组未保存该证据，现在会明确判为比较证据不足；不要补写零丢包，应在修正台架重跑。历史无延迟案卷仍可读取。

CPU 按加载期 `worker_cpu_sec / sample_window_sec` 折算平均核数，再除以有效吞吐得到 CPU 秒 / GiB；RSS 取加载期采样峰值。比较器可显式传 `--max-throughput-drop <百分比>` / `--max-cpu-rise <百分比>` 作为本次测试门槛，没有隐藏的性能阈值。这些值是整条集成路径的指标，仍不能当作 core 微基准。

脚本暂时停止 rig watcher 及其独立的 `rig-watch-*.scope` 批次，持有 `watch.lock`，等待原用例的退出清理，再创建自己代际标识的隔离 netns/veth；退出时杀掉测试进程、删除拓扑和 undo timer、恢复原 watcher 状态。建议通过 systemd 或有足够余量的 `timeout 360 bash ...` 运行，节点租约由调用方续期。它不修改面板数据库和已安装的 `zf` 服务。等待命名空间退出默认最多 150 秒，可用 `L2_NAMESPACE_WAIT_SECS` 调整；仍有命名空间或其他 rig owner 时拒绝开始，不删除未知资源。

`result.json` 只有在全部流校验/推进、两端口 ZFStack 启动证据、每个客户端的 WG 路径以及 target → worker 载荷计数成立时才为 `ok=true`。客户端到 target 的物理捷径计数必须为零。原始结果在 `load/client-*.json`，资源采样在 `load/worker-samples.json`，路径计数在 `path-pre.json` / `path-post.json`，worker 配置指纹与预算在 `worker.log`。

案卷包含临时 WG 密钥、控制器密钥与配置；共享结果仅导出结果、原始负载样本、资源/路径计数、qdisc 前后快照与汇总、环境与构建出处，保留密钥在测试节点。`namespaces-after.txt` 与 `watcher-after.txt` 记录清理结果。

这是小规模功能与负载切换验证。没有声明百端口容量、WAN 丢包/漫游/重建覆盖；首轮没有 AB/BA 重复，后续修复对比采用 ABBA，每种二进制每配置两次。报告单列实测代价，没有设默认数值回归门槛，也不能据此比较 TCP core 与内核的性能。
