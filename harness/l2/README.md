# L2：ZFC + WireGuard 集成混合负载

这是可选 Linux 集成入口，复用现有 `zfc-rig` 的拓扑、端口配置生成器、假控制器和路径计数器。它使用真实内核 WG 客户端、ZFC worker 的 WG 解密/UDP 路径、ZFStack 生产 Tokio adapter 与 Direct 出站。主 L0/L1 矩阵不依赖它。

两个真实 WG 监听端口，各有两个独立 peer。peer 0 开四条下载；peer 1 是同端口轻用户，peer 2/3 是另一端口的轻用户。所有轻请求携带 peer 与序号，重流使用逐连接字节模式，接收端逐字节校验。共享一台宿主机上的 Python 内核 TCP 应用端点会限制吞吐，结果属于整条集成路径。

默认阶段是 8 秒空载、16 秒加载、8 秒恢复。轻请求间隔 10 ms；输出每个请求的原始样本、每用户 P50/P95/P99/P99.9、重下载有效字节与 goodput、每 0.5 秒 worker CPU/RSS/线程 CPU、每 5 秒 ZFStack 预算与 adapter 诊断。P99 至少需要 100 个样本，P99.9 至少需要 1000 个。CPU 是整个 worker（含 WG、UDP、控制通道），不能解读为 core CPU。

可选 `L2_LIGHT_CONNS=16 L2_CHURN_REQUESTS=4` 让每个轻 peer 同时维持 16 个请求线程，每完成 4 次请求重建连接。连接建立时间单独记录，所有请求序号在 peer 内唯一，响应逐字节校验。默认仍是一条持续连接。`L2_UDP_PROBES=1` 同时从四个 peer 各发 100 包/秒的 1032 字节 UDP echo，逐包核对内容、响应及 3 秒截止时间；这是混合负载正确性检查，不是 UDP 极限吞吐基准。

资源采样同时记录宿主 CPU pressure 和编译进程。`performance_contaminated=true` 表示采样遇到 cargo/rustc/链接器等进程，此次运行即使功能 `ok=true` 也不能用于性能结论。调用方还应在开始前确认同机 remote-compile 作业已全部退出；采样无法保证捕获间隔内的短作业或识别所有外部负载。

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

旧案卷的 CPU/GiB 是采样子窗口平均 CPU 与整加载期 goodput 的近似组合，只保留为标明局限的描述值。新的 `measurement_window` 在预先排定的 phase 起止处采样，拒绝以内部子窗口代替加载开始；在同一组单调时钟边界同时记录 worker CPU 与累计已验证重流字节，再分别取增量；记录 `/proc` CPU 计时分辨率和每个边界的观测偏差（默认上限 5 ms）。缺边界、计数回退、零有效字节或精度超限时 CPU/GiB 为 null，不能写成零。整个加载窗口的 goodput 继续单列，不覆盖加载开始异常。

`compare.py` 是历史 small/normal/delayed 案卷模式，完整性 `ok` 不等于正式性能 PASS。它拒绝编译污染，比较完整 effective_config（历史输入保留已存在配置字段），只允许显式 `--aa` 使用相同二进制。旧案卷缺完整配置或同窗计数时不能通过 `--max-throughput-drop` / `--max-cpu-rise` 的数值放行；历史性能结论保持 INCONCLUSIVE。

脚本暂时停止 rig watcher 及其独立的 `rig-watch-*.scope` 批次，持有 `watch.lock`，等待原用例的退出清理，再创建自己代际标识的隔离 netns/veth；退出时杀掉测试进程、删除拓扑和 undo timer、恢复原 watcher 状态。建议通过 systemd 或有足够余量的 `timeout 360 bash ...` 运行，节点租约由调用方续期。它不修改面板数据库和已安装的 `zf` 服务。等待命名空间退出默认最多 150 秒，可用 `L2_NAMESPACE_WAIT_SECS` 调整；仍有命名空间或其他 rig owner 时拒绝开始，不删除未知资源。

`result.json` 只有在全部流校验/推进、两端口 ZFStack 启动证据、每个客户端的 WG 路径以及 target → worker 载荷计数成立时才为 `ok=true`。客户端到 target 的物理捷径计数必须为零。原始结果在 `load/client-*.json`，资源采样在 `load/worker-samples.json`，路径计数在 `path-pre.json` / `path-post.json`，worker 配置指纹与预算在 `worker.log`。

案卷包含临时 WG 密钥、控制器密钥与配置；共享结果仅导出结果、原始负载样本、资源/路径计数、qdisc 前后快照与汇总、环境与构建出处，保留密钥在测试节点。`namespaces-after.txt` 与 `watcher-after.txt` 记录清理结果。

这是小规模功能与负载切换验证。没有声明百端口容量、WAN 丢包/漫游/重建覆盖；首轮没有 AB/BA 重复，后续修复对比采用 ABBA，每种二进制每配置两次。报告单列实测代价，没有设默认数值回归门槛，也不能据此比较 TCP core 与内核的性能。


## 通用 QA 证据导出

`export_case.py` 支持任意 case ID，无需 small/normal/delayed 目录命名或强制抓包。它读取 `result.json` 加 `raw.json.gz`，或直接读取新 rig 输出的 `load/client-*.json` / `load/udp-*.json`。JSON 拒绝重复键、NaN/Infinity、负计数与过大输入；gzip 解压上限 64 MiB。输入缺失时不补写成功样本、零丢包、时间戳或源码身份。

```sh
python3 harness/l2/export_case.py /absolute/rig-case \
  --identity /absolute/orchestrator-identity.json --output /absolute/case-v2.json
```

identity 由可信编排提供 QA v2 的 run/block/arm/case/instance、tested SHA、plan/build/environment/config hash、binary SHA256 和真实 execution 生命周期。导出器保存实际结果 artifact hash，功能、测量有效性、性能三个域分别输出。单个 run 的性能始终 INCONCLUSIVE，正式判断使用 `bench/pareto/perf_gate.py`。配置、二进制、WG 路径、必需 peer 或阶段证据不符时功能证据为 UNKNOWN，已观察到的数据错误仍保留为 FAIL。仅编译污染或 CPU 边界精度不足不会抹掉可用的功能结论。

`load_contract.effective_configuration(args, environment)` 是编排与负载生成器共用的配置构造器，覆盖 heavy/light、churn、UDP、请求尺寸/速率、阶段/超时、内存、MTU、netem、方向、runtime/profile、抓包和边界精度。`L2_RUNTIME_ID` 与 `L2_PROFILE_ID` 必须与实际构建/启动 manifest 核对，未设置时记录 null 并拒绝正式性能认证；设置标签本身不是验证。`L2_EFFECTIVE_CONFIG_JSON` 可保存额外的完整编排配置，放入 `orchestrator_manifest`，不能覆盖观察到的负载设置。run-rig 加入实际 worker tuning fingerprints、load/idle 模式，以及 idle 参数；原始 WG 路径前后计数和 startup port 数保留在 `path_evidence`，缺失或回退均拒绝。

## 到达过程与加载开始异常

默认 TCP 轻请求保留历史 closed-loop 模式；`L2_LIGHT_MODE=open-loop` 选择运行前生成的固定到达表，`L2_LIGHT_RATE_HZ` 固定每条逻辑 lane 的绝对速率。UDP 始终使用预生成到达表，`L2_UDP_RATE_HZ` 默认 100。`L2_MAX_GENERATOR_LAG_SECS`（默认 0.02）预先限制发生器迟到；超限 arrival 记为 missed，不补发，也不从 scheduled 分母删除。open-loop 与 `L2_CHURN_REQUESTS=N` 可同时使用：每 N 个计划 arrival 组成连接 epoch，各 epoch 固定在首个请求前 0.25 秒尝试连接；后续 epoch 的开启和发送日程不依赖任何响应。旧连接可等待自己的 deadline，与新连接重叠，绝不推迟新连接、退回 closed-loop 或降低负载。

每条 lane 使用一个事件循环，最多同时保留 64 个 epoch；每个 epoch 的待发送 FIFO 上限为 32 个请求。正常调度合并多个仍在预算内的 arrival 时，按原顺序非阻塞发送直到 EAGAIN；每条请求首次实际发送前仍检查原 scheduled + max_generator_lag，不延后日程。队列满或超过原 deadline 才记 miss；部分 frame 发送后超时必须关闭该 epoch，避免复用错帧流。一个 peer 的所有 lane 合计最多 200000 个预定 arrival、最多 128 条 lane。容量耗尽、迟到、连接失败、发送背压和请求 timeout 都保留对应连接/请求记录。每条 lane 保存 `schedule` 及其 SHA256、每个 sample 的 epoch/scheduled/started/outcome、所有连接实际生命周期和最大并发数。独立分析器从实际全量配置重建计划，核对每个 epoch、请求、聚合计数与原始 lane 列表；不能用“offered_rate_hz=100”标签代替实际供给证据。固定日程预计算的上限是显式能力限制，超出时拒绝配置，不能静默截短计划。

每个请求/数据报保存 scheduled/started/completed/failed/timed_out/unfinished/missed、phase、seq、实际起止和 generator lag。超时与迟到回包分别保留，成功分位数不会吸收超时伪值。UDP load-onset 首秒的失败额外保存为 `startup_anomalies`，所有原始样本仍在文件中；它们不是被删掉的预热段。发生器 missed arrivals 使性能证据无效，已送出请求的超时或 payload mismatch 仍是功能失败。根目录的 `test_mixed_load.py` 回环检查只检验已送出响应完整性并核对全部 missed 记录；它不认证未绑核宿主的 20 ms 调度精度。

这些新增 Python 观测只在测试端执行，未修改生产协议栈、adapter 默认缓冲或工作线程。精密性能仍须在受控 Linux 节点做真实 A/A、held-out 已知退化校准及真实 WG 重放；本地单元测试不能替代这些证据。

## 两端进程资源观测

`load/endpoint-samples.json` 保存 heavy/light/UDP 发生器、业务 TCP/UDP 目标服务、Relay 探测目标和实际 forwarder 的 CPU/RSS 时间线；idle 模式另含 idle 发生器。TCP 和 UDP 目标共用一个 Python 进程，因此只报告该进程总量，不伪造协议级拆分。身份字段限于角色、namespace、PID、进程 starttime、comm 和 executable；不读取 cmdline、进程环境或配置/密钥。

端点采样在线程中独立进行，包含 baseline/loaded/recovery 的完整边界及每 0.5 秒观测点，记录每个进程自己的读数起止偏差和 CPU 时钟分辨率。它不扩展 worker CPU/已验证字节读取之间的窗口，也不声称各端点完全同时采样。父进程在最后一个端点边界完成后才回收发生器；已退出的 zombie 可保留最终 CPU，RSS 必须为 null，不能用零伪造撤载回收。每个端点按冻结的 `boundary_tolerance_ns` 单独验收。

`evidence.load_case` 自动把该文件收入 typed raw，正式 runner 的 `freeze_l2_raw` 将其包含在既有 `raw.json.gz` 白名单中。离线复算从原始样本检查完整角色集合、进程身份、阶段边界、CPU 转换、计数回退及实际偏差，并输出 `endpoint_diagnostics`。缺端点或采样失败只使性能证据 INVALID，独立功能 PASS/FAIL 保留；观察器异常不能跳过发生器、队列或原有 worker 样本的清理。原来的 worker CPU/GiB 定义保持不变，这些端点数值用于识别发生器和目标瓶颈，不从 worker CPU 中相减，也不称为纯核心成本。


## 共用宿主机的隔离入口

共用生产宿主不得调用默认 watcher/service 管理路径。由 `qa/isolate-linux.sh` 建立私有 mount、network、PID namespaces、私有 `/run/netns` 并屏蔽 host systemd/dbus sockets 后，才可设置 `L2_ISOLATED=1`。隔离检查在任何 rig 服务操作前运行，要求 wrapper 标记，并核对 `QA_HOST_NETNS_ID` / `QA_HOST_MNTNS_ID` / `QA_HOST_PIDNS_ID` 与当前 namespace 不同；没有相应隔离证据即拒绝。

隔离模式不执行任何 watcher/scopes `systemctl`，也不创建或取消 host undo timers；退出记录明确为 watcher `not_applicable`。拓扑状态和锁固定在本次新输出目录的 `isolated-rig-state`，已有工具/build 路径仅用于读取。外层 namespace wrapper 负责终止和回收自身子树，测试不清理宿主的未知 netns、服务、路由或其他工作区。默认专用 rig 入口的行为保持原样。

## Relay / private_tun 的独立实际路径

默认 `L2_OUTBOUND=direct` 保持原有路径及配置。`L2_OUTBOUND=relay` 使用冻结 rig 的 `wg-portmap.py --outbound relay` 和 `lib/fwd.sh`，在 fwd1 启动真实 `forwarder-server`；原来的第二个客户端移到 fwd4，避免 fwd1 的客户端 WG 路由把转发流量送回 worker。业务端口仍为 19001，健康探测改为 19002。前后原始计数要求 target → fwd1 → worker 两条边均承载已验证重流字节，同时业务端口 worker → target 捷径计数必须为零，forwarder → target 计数必须增长。仅在构建图中链接 private_tun 不计为这项覆盖。

构建固定源码矩阵时显式增加 forwarder：

```sh
python3 -m qa.crossrepo build --archive /absolute/frozen-source-matrix \
  --output /absolute/new-build-output --cell head-head --forwarder
```

构建器执行同一 toolchain/profile/features 的锁定构建，核对实际 forwarder 的 `--build-info` 中 ZFC 和 private_tun commit，并在 worker manifest 中冻结其二进制 hash、大小和依赖图。正式节点配置增加绝对 `forwarder_binary`，QA 验证后通过 `L2_FORWARDER_BINARY` 传给现有 `run-rig.sh RIG WORKER NEW_OUTPUT`。Relay case 的配置在 Direct 全量配置上增加 `outbound: relay`、`probe_port: 19002`、`topology.client_nodes: [client,fwd4,fwd2,fwd3]`，其余字段仍逐项冻结。缺构建、能力、路径计数或执行身份只能产生未执行/证据不足结论。

## 共享出口队列负对照

`l2-queue-control` 是测试侧诊断 case：同一租约先执行健康基线，再执行带 `queue_control` 的相同负载，验证绝对服务延迟合同确实能抓住深出口排队。它不声明复现历史 #44 的具体实现或根因，也不进入干净性能比较。推荐冻结 closed-loop、churn 0，并使用如下唯一受支持的注入形状：

```json
{"kind":"fifo-delay-line","node":"worker","interface":"eth0","phase":"loaded","delay_ms":500.0,"limit_packets":100000}
```

只有经过验证的 QA namespace 隔离模式允许注入。`fifo-delay-line`（schema 4，`fifo_injector.py`）在测量开始至少 0.5 秒前，于 worker eth0 原有 `noqueue` 根旁挂 `clsact`，用唯一的共享 `tc` mirred 动作（index 44）与 matchall 过滤器（pref 44）把全部出口帧重定向到私有 tap `qa44fifo`（tap 根为 `noqueue`、关闭 IPv6/ARP）。单线程进程逐帧读入同一个有界 FIFO，再经绕过 qdisc 的 AF_PACKET 把原样的 vnet 头与帧字节写回 eth0。从 baseline 到 recovery 结束，所有包始终经过这一条路径，测量中不替换、不重置任何队列：

- baseline：到达即释放（`due = t_in`）；
- 注入（loaded 开始至提前 `delay_ms / 1000 + 0.25` 秒）：`due = max(t_in + 500 ms, 上一帧 due)`；
- 收尾（注入结束且 FIFO 非空）：新帧排在积压之后，`due = max(t_in, 上一帧 due) + len·8 / 2 Gbit/s`；
- 透明：注入结束后，同一线程在 FIFO 为空时切换（在帧入队时判断，或在 emit 之后读时钟判断），之后 `due = t_in`。不保留 2 Gbit/s 整形，不暂停或减少业务输入。

过滤器只在 `measurement_end` 之后删除，随后排空 tap 环与 FIFO，读取最终 action/tap 计数，再删除动作和 `clsact`，并核对 eth0 只剩原 `noqueue`。挂载或删除 `clsact` 会让内核对 eth0 执行 `dev_deactivate`，其间出口包被 noop qdisc 静默丢弃且不计入链路计数（`results/2026-10-16-fifo-injector/clsact_probe.py`：30 次挂/删丢 16–18 个包，`tx_dropped` 为 0）。因此安装在负载客户端启动前完成，拆除只在 `load.py` 回收全部客户端且过了 `measurement_end` 之后进行。取消信号只记录取消，路径继续由父进程持有；父进程回收客户端且测量窗口结束后，才按序排空 FIFO 并撤掉重定向。父进程死亡或等待超过有界期限时明确记录失败并清理；取消的运行一律判 FAIL。全部 loaded 请求和统计窗口照常保留，发生器不会删掉未完成请求或缩短服务样本窗口。

选用 tap 而非 NFQUEUE：`tun_net_xmit` 像 netem 处理延迟包一样 orphan skb，而 NFQUEUE 排队项仍计入发送 socket，会增加原注入没有的发送缓冲反压。6.8 内核实验（`results/2026-10-16-fifo-injector/backpressure.py`）中，同样 500 ms 延迟下 NFQUEUE 让非阻塞 UDP 发送方出现 2608 次 EAGAIN，netem 与本 FIFO 均为 0。

`fifo_ledger.validate` 不信任报告摘要，只从 gzip 原始账本（每帧 seq、阶段、结果、GSO 类型/大小、L2–L4 头长、长度、到达、应释放、实际发送和 blake2b-64 摘要）与内核原始计数复算：

- seq 连续、无缺失/重复/乱序，所有帧 `DELIVERED`；溢出、发送失败或未发送一律失败；
- 每帧阶段与 `due` 按上述冻结策略精确复算，`due ≤ t_out ≤ due + 20 ms`，发送单调；收尾期不能在队列已空后继续整形，透明帧不能排在未完成积压后；
- 透明切换时刻在注入结束与 recovery 边界之间，且晚于全部非透明帧的实际发送；
- 占用按到达/发送复算，不超过 100000 包与 256 MiB；必须观测到真实排队的 loaded 流量及 baseline、透明期流量并覆盖到测量结束；
- tap tx 帧数/字节与账本逐一闭合；mirred 动作按 GSO 段计数（`qdisc_pkt_len`），与账本复算的段数及 `len + (段数 - 1)·头长` 字节精确闭合；任何 action drop/overlimit、tap 丢弃/错误、tap rx 都失败；
- eth0 在安装前、安装中、恢复后的原始 qdisc 列表，tap 自身无队列；安装早于测量开始 0.5 秒，删除不早于测量结束；取消、执行错误或 cleanup 非 PASS 均失败。

`queue-control.json.send_diagnostics` 另存发送尝试/失败总数、前 1024 个失败的 seq、phase、errno、期望/返回字节数和原始发送起止时刻，以及最长一次 send 的墙钟区间。详情截断时显式记录 `omitted_failures`，逐帧账本不省略失败；没有自动重试。墙钟包含系统调用及失去调度的时间，不能据此单独归因内核阻塞或 CPU 抢占。诊断字段不参与资格判定，SEND_FAILED 和超过 20 ms 仍由原始账本严格拒绝。该新增诊断尚未进行特权自检或正式 C16 复验，不能追认旧 SHA 的通过结果。

这些常量随 schema 4 预注册冻结。`fifo_selftest.py` 是需要 root 的端到端检查，只能在 `bash qa/isolate-linux.sh python3 harness/l2/fifo_selftest.py --output /abs/new.json` 中运行；它用独立的 UDP 序号/载荷、TCP 字节流、接收端 UDP MIB、发送端 EAGAIN 与链路计数核对 oracle，`--mode cancel`、`--mode overflow` 必须得到明确 FAIL。它不能替代 held-out 正式 C16 运行。

历史 v3（`netem-delay`）只用于复判已归档报告，新运行不再使用。v3 证据明确区分持续注入与收尾：持续阶段仍为原 delay-only 配置；收尾在同一个 netem 上改为 `delay 0ms rate 2000000000bit`，保留现有积压，再等待 backlog 清空并删除自己的 qdisc。非零 rate 给新包分配顺序的序列化时间，避免直接清零 delay 时越过旧积压；有限的 2 Gbit/s 排空速率同时约束集中排出。它来自预注册的 2/3/4 Gbit/s 台架容量对照中最低合格候选；最终 `8bf5bb8` 独立完整复验因读空后重新积压而被严格拒绝（NOT_RUN），不能作为已完成的台架。它不是产品带宽承诺、性能通过阈值或任意负载的排空保证。旧 v2 的 100 Gbit/s 排空在完整负载下产生链路丢包，已拒绝，不能作为 v3 成功证据。

`queue_negative_evidence(case, baseline_output, fault_output, worker)` 独立复算两次原始证据。PASS 要求基线服务 PASS、故障期 payload/路径 PASS、故障期服务合同 FAIL、真实 backlog > 0 且 drops = 0、恢复期相同服务合同 PASS、完整排空及清理证据。基线、数据完整性或恢复失败仍为 FAIL；缺能力、丢包、配置漂移或缺计数为 NOT_RUN。基线会显式清空 `L2_QUEUE_DELAY_MS`、`L2_QUEUE_LIMIT_PACKETS` 和 `L2_QUEUE_KIND`，防止继承故障运行的参数。服务 peer ID 是 `client-1`、`client-2`、`client-3`，延迟包含 scheduled 到 started 的发生器排队及请求本身耗时。

QueueControl 同时归档六个端点全部接口的前后原始计数与身份；任意 drop/error 增量、身份改变、缺样或计数器回退均使延迟注入不合格。独立校验器要求原 500 ms 注入与 250 ms guard、JSON/text 两种实际 tc 参数、零 qdisc 丢包、draining 和 cleanup 均无积压，并在固定 recovery 边界前恢复原 qdisc。TCP/UDP payload 失败不能被预期 SLO 失败或台架不合格覆盖。容量抓包的记录顺序不是严格线上接收顺序的证明；实际交付配对及 UDP 完整性另行保留。

持续负载下，v3 的 `tc show` 读空与 `tc qdisc del` 之间不存在原子排空保证，甚至同一采样的 JSON 与文本计数也可能不同。缩短间隔、反复读零或重复运行不能修复该竞态；零 link/qdisc drop 也不覆盖删除时的队列 reset。`fifo-delay-line` 以上述持久路径取代实时替换 root；它须先通过 held-out 正式运行，QueueControl 在此之前仍为 known-BLOCKED。未以该缺口修改生产配置或放宽校验，细节见 [验证记录](../../qa/VALIDATION.md)。
