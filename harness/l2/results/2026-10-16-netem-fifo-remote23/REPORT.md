# 真机 netem-fifo gate：FAIL，停止注册和正式运行

2026-10-16，在用户指定的 `root@23.136.204.72:23826` 执行 PR #75 的真机步骤。冻结源码 **7811fda9e067cb5a021cf4988e0a31b479f9c656**；`remote_selftests.py` **只调用一次**，先写 plan、持公共 QA Lease，所有能力探测和网络操作均由原 `qa/isolate-linux.sh` 包装。

**gate FAIL：full 的接收端有 9 个 UDP 被超越，全部属于 loaded。停止第 2–4 步；C16 继续 case v2 / fifo-delay-line、known-BLOCKED。没有注册 v3、重做 gate、构建 worker 或执行新的正式 C16。20 ms 和全部冻结参数未改。**

## 结果与独立复算

[执行计划](evidence/stage1/plan.json)、[逐次执行记录](evidence/stage1/execution.json)、[远端 gate](evidence/stage1/gate.json)、[本地复算](recomputed.json) 均保留。取回后对 51 个原始文件逐个核对大小和 SHA-256，再用冻结 oracle 对原始账本复算，并直接从 receiver 序号统计超越、缺失和阶段。

| 项目 | 结果 | UDP 发 / 收 | 被超越 | 账本与清理 |
|---|---|---:|---:|---|
| capabilities | PASS | — | — | 两相位 netem 参数回读、netlink change/qlen、TPACKET_V3 rings 通过 |
| full | **FAIL** | 18475 / 18475 | **9，全部 loaded** | oracle PASS，60617 入 / 出逐帧闭合、出口严格有序、零丢帧；cleanup PASS |
| cancel | 负例 PASS | 18475 / 18475 | 0 | 74098 入 / 出闭合；oracle 因取消及不完整相位明确拒绝；cleanup PASS |
| overflow | 负例 PASS | 18475 / 13567 | 0 | 60491 入 / 55486 出，5005 丢失帧中 UDP 缺口 4908；oracle 拒绝；cleanup PASS |
| plain netem 1 | 对照完成 | 18475 / 18475 | 0 | 删除完成早于 recovery 235.961056 ms |
| plain netem 2 | 对照完成 | 18475 / 18475 | 0 | 删除完成早于 recovery 235.464867 ms |
| plain netem 3 | 对照完成 | 18475 / 18475 | 0 | 删除完成早于 recovery 228.267151 ms |

三次 plain netem 是 full 乱序触发的预注册对照，均只执行一次，采用原历史 v3 参数和时间线。它们此次零丢失、零乱序；没有重现先前 TCG 对照中的晚删除及两包丢失，但也不构成旧 v3 原子恢复的资格证明。

full 唯一失败检查为 `udp_exactly_once_in_order`。UDP 无缺失/重复、载荷完整，TCP 字节一致，发送方无 EAGAIN，接收 socket 无丢失，链路 drop 为零，延迟、透明恢复、qdisc 恢复等其余检查均通过。9 个被超越包从出口到接收的时间为 p50 462037 ns、最大 491551 ns；出口与超越者间隔最大 313701 ns，详见原始 gate 的 order 分析。

**真实 CPU 上也出现了候选路径出口有序而接收端乱序，问题并非只在 TCG 出现。** 本次没有内核 trace，因此不能把本次每个包的 CPU 归属或具体 veth 交付路径说成已经证明；与先前仿真跨 CPU 交付观测一致只是一种解释。三次普通 netem 仍为零乱序，不支持放宽 UDP 顺序判据。后续应在允许 trace 的非生产隔离环境继续定位候选路径的出队/交付行为。

驱动在 overflow 分支没有 `udp_exactly_once_in_order` 检查键，条件对照触发实际上依赖 full/cancel；本地额外从 overflow 原始接收序号确认超越数为 0。本次 full 已触发全部三次对照，该实现缺口没有改变本次 FAIL；未来若仅 overflow 乱序，需要单独修正驱动后重新冻结，不能据现有驱动宣称覆盖了该触发分支。

## 宿主边界与收尾

Debian 12、6.1.0-29-amd64、原生 x86_64 KVM，4 vCPU。运行前可用内存约 5.2 GiB、磁盘约 200 GiB，公共租约空闲。该机仍有生产服务，未建立物理 CPU 独占资格。

没有安装/升级系统包，没有更改调度、绑核或优先级；没有运行 trace、停止/重启/重新配置生产服务，也没有在宿主 namespace 修改网络、qdisc 或防火墙。`dummy` 被内核自动加载，未卸载任何模块；`sch_netem` 在 gate 前已加载。旧任务目录 `/root/zfstack-qa-pr75-20261016-remote23-01` 未改动。

[前后宿主快照](evidence/preflight) 和 [比较结果](host-comparison.json) 显示所核对的 worker、Docker/containerd、Redis、数据库等进程 PID 未变，7 个容器均 healthy；宿主注册 netns 前后为空，公共租约结束后为空。三模式 cleanup 均 PASS。PID/健康检查不等于完整业务影响测量。

新任务目录 `/root/zfstack-qa-pr75-20261016-netem-01` 保留 bundle、源码及全部运行；有效 checkout 位于 `source-verified`。本地归档仅收集自检/对照 JSON、账本、日志和只读快照，不包含源码 checkout、WireGuard 密钥、业务运行时配置或生产流量 trace。

## 准备阶段的失败（未进入 gate）

完整记录见 [preparation.json](preparation.json)。我在 SCP 尚未结束时过早启动了校验/clone，观察到不完整 bundle 的不同哈希及 checkout 失败；随后一次 `git bundle verify` 在非仓库目录运行也被拒绝。传输完成后哈希为 `ccdf9472fcb58799769dc60336ca4c6b2287b6bc9c1c6427bbcd4e781a377c69`，与本地一致，仓库内 verify 通过。

另发现说明中的 `git bundle create ... origin/分支` 只打包远程跟踪 ref，普通 clone 不导入该 ref，因而完整包也得到空 checkout。最终对同一 bundle 显式执行 `git fetch ../pr75.bundle refs/remotes/origin/cursor/queue-control-fifo-injector-0477`，再 checkout 固定 SHA，并验证 clean。没有改源码、负载、阈值或重复运行 gate；失败准备目录也留在远端。未来第 0 步应显式 fetch bundle 中的 ref，或在本地以可 clone 的临时 heads ref 打包固定提交。

## 复算与索引

```sh
PYTHONDONTWRITEBYTECODE=1 python3 harness/l2/results/2026-10-16-netem-fifo-remote23/recompute.py
```

[原始收集索引](evidence-index.json) 绑定原始 `evidence.tar.gz` 和其中 51 个成员；[全部归档索引](artifact-index.json) 覆盖本目录文件（不含自身）。既有失败记录未覆盖。PR 保持 draft、#59 保持 open，留给维护者和 Claude 继续判断。

## 接手分析：失败时刻是整机暂停（离线，不改判定）

由 [analyze_pauses.py](analyze_pauses.py) 只读上述原始文件生成 [pause-analysis.json](pause-analysis.json)，重跑字节相同。两份文件是取回后新增的分析，不在上面的 artifact 索引内。

- **9 个被超越的 UDP 来自同一次约 21.7 ms 的停顿。** 判断依据如下：
  - 发送端按 2 ms 的绝对时刻表发送，6985–7000 在发送和 mirror 处都准时，其中 mirror 晚于发送 4–49 µs；
  - 但出口从 loaded 开始后 1.4741 s 起 21.677 ms 没有释放任何帧（含 TCP），同一时段发送端也有 21.684 ms 没有发送，之后按时刻表一次补发 10 个；
  - 注入器 100 ms 一次的时钟采样同时被拉长到 113.088 ms；
  - 500 ms 后出口出现对应的 21.729 ms 空档，正好对应停顿期间没有入队。

  发送线程、注入器进程和持有 qdisc 出队的 CPU 在同一区间都停止运行，这是 guest 整体（至少全部相关 vCPU）的暂停，不是注入器内部的阻塞。
- **乱序发生在暂停恢复时。** 6987 在暂停前出口，但 21.9 ms 后才到达接收端；6988–6997 在恢复时一次释放。300 µs 后另一路径交付的 6998 先于其中 9 个到达 socket。同一次暂停也使 6988 的出口迟到 **19.691 ms**，距冻结的 20 ms 上限仅 0.309 ms。
- **普通 netem 对照经历过同量级的暂停，但没有乱序。** 发送端停顿超过 5 ms 的次数在每次运行中都有：control-2 在 loaded 期有 **18.011 ms** 的停顿，control-3 有 11.7 ms 和 10.4 ms，full 另有一次 13.35 ms 的停顿也没有乱序。乱序需要暂停恰好落在某个 CPU 积压中还有已释放帧的时刻，因此单次事件既不能说明 `netem-fifo` 比普通 netem 更易乱序，也不能排除这一点。预注册对照的 0/3 只说明没有观察到，不放宽严格顺序判据。
- 这次 gate 没有记录逐 CPU 的 `/proc/stat`，无法判断暂停是否被计为 steal。同一 VPS 早先 remote23 自检约 44 s 的 steal 仅约 0.02%，但那是另一时段的运行，不能用于判断本次暂停。

**结论：在这台共享 VPS 上，13–22 ms 的整机暂停会在测量中出现。它本身就足以让任何注入器逼近 20 ms 的迟到上限，并造成恢复时的跨 CPU 乱序。** 在该机上继续重跑只会把结果交给暂停发生的时刻，因此不建议在这台机器上再执行 gate 或正式运行。下一步需要一台物理 CPU 独占、非生产、允许 trace 的隔离宿主。
