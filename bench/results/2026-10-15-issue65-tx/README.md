# Issue 65 TX owner 复用：第二台机器证据

实验开始日期：2026-10-15；测试机 `root@38.207.178.131:22100`，独立目录 `/root/zf65-20261015`。实验跨日执行；时间以原始记录的 UTC epoch 为准。

本轮保留的生产候选源码为 `2694c1406b7ebb5d1eef381a0c1a69e27f1ea743`（U），主线对照为 `88a2e3f7a2e86fb6948223054a377e8a7f831af1`（T），U2 是独立构建。它们都包含 #67 的当前 peer limit 修复。F56 是 #56 的冻结 FV probe；其历史网络结果不与本轮混算。

## 原始标签与范围

| 标签 | 说明 |
|---|---|
| M | 起始 main `ebcd6dd` |
| P/Q | 早期 eager scope 候选，试测，不用于最终收益 |
| N | M 仅增加 cache0 直接析构，用于消融 |
| C/C2 | eager scope 及独立构建；C 不是 #22 的 #21 版本 C |
| R/R2 | 延迟构造 scope，`3ae01f6`，在旧主线上完成的定位与保护实验 |
| T/U/U2 | 最新配额修复基线、最终方案和独立构建；最终资格仅使用此组 |
| V/V2 | 延迟 index 检查，`aefc7dc`，未达到预定缓存收益及保护门槛，未采用 |
| F56 | `3459562746e689fc5ec78c5b1764b0bba3465363` 对应的冻结 FV |

`scope-*`、无前缀的 window 与 `refined-*` 分开统计；`current-quota-*` 才是最终候选的矩阵。`current-quota-preheat` 整组为排除预热，即使其中记录的 rep >= 0，也不算最终正式样本。最终 push/write_with 各两个窗口、每 case 12 对及一次排除 warmup；不把选择候选的试测合并进去。

旧 `network-before-main-advance.*` 是发现 #67 后取消的旧基线网络前缀，不作为已完成网络实验。取消原因与目标进程记录见 `cancelled-for-main-advance.json`；隔离 namespace 退出后宿主 TUN 未变。新的 `network.jsonl` / meta 才是最终 T/U 对照。

## 构建与口径

Rust1.92.0，fat LTO、codegen-units=1、panic=abort、incremental=0、build jobs1，固定依赖锁。性能公共 root manifest/lock 沿用既有 FV，实际 bench 源码取各阶段固定 main，各臂一致；归档记录实际编译输入。原生 Linux 功能检查使用 checks-U 的当前仓库 manifest/lock，包含 dev-only test-util，target 与性能二进制分开。

`pareto_probe` 不修改 #56 的 `portable_full_scan_v1`；精确比较全部字节和 checksum 留在 work timer。`make_sidecars.py` 从同一文件生成所有版本的 write_with probe。allocator 计数使用单独的 `tx_*_diag`，只能用其 work_alloc_calls/free/bytes 解释实际调用及请求字节，不能将其 CPU 当正式性能；请求字节不含 allocator 内部开销，也不等于 RSS。`inspect_current_quota_layout.py` 核对最终 T/U/U2 的校验热循环、源码及相同页内布局，不靠改校验换取收益。

## 统计复现

推荐从本目录执行 `python3 replay_stats.py /绝对路径/新的空目录`：它解包原始记录、覆盖为随附的严格分析脚本，在新目录重算并逐字节核对全部统计输出。原始记录的绝对远端路径保留在命令元数据中；统计复现不执行这些远端程序。

也可以在解包得到的 `remote/` 副本中执行（先从本目录复制 analyze.py / network_current_quota_analyze.py / qualify.py，脚本会覆盖派生 summary、CSV）：

```sh
python3 analyze.py current-quota-preheat \
  current-quota-push-window1 current-quota-write-window1 \
  current-quota-push-window2 current-quota-write-window2 \
  current-quota-pump-guards current-quota-f56-context \
  current-quota-write-tiny-long-window1 current-quota-write-tiny-long-window2 \
  current-quota-pump-long-window1 current-quota-pump-long-window2
python3 network_current_quota_analyze.py
python3 network_current_quota_analyze.py network-connect-confirmation
python3 network_current_quota_analyze.py network-bulk-confirmation
python3 qualify.py
python3 plot.py  # 仅绘图需要 matplotlib
```

逐对百分比 `100*(candidate_cpu/baseline_cpu-1)`，中位数与 10000 次 bootstrap percentile CI（seed65）。CPU/latency 越小越好，吞吐越大越好。CPU +2%、吞吐 -5%、P99 +5% 是事先登记的保护界，区间跨界为 INCONCLUSIVE；64B cache0 与迁移两目标的收益还要求各窗口 CPU CI 上界严格小于0。P99.9 单列描述，不将有限尾部样本说成稳定性保证。

## 重建和复跑

`inputs.tar.gz` 仅保存实际性能构建所需的 Cargo manifest/lock、src、examples、bench/src 等，排除 target、二进制和历史结果目录。每一项都有 inputs-sha256；源码匹配以 source-proof 中 T/U/U2 与 Git 的比较为准。

解包到新目录，为所有 arm 使用新的 target。保持同一工具链及下列变量，再构建 `--features test-peer --example pareto_probe --example tx_write_probe`；网络用相同源码 `-p zfbench`。不要覆盖远端黄金二进制。

```sh
export CARGO_PROFILE_RELEASE_LTO=fat CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1
export CARGO_PROFILE_RELEASE_PANIC=abort CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=1
cargo +1.92.0 build --locked --release --features test-peer \
  --example pareto_probe --example tx_write_probe \
  --manifest-path "$experiment_inputs/U/Cargo.toml" --target-dir "$experiment_inputs/target-U"
```

复制 config，修改全部 executable 路径、tag 和输出目录，再用 run_micro.py。输出采用独占新建，不能复用旧 tag；meta 保存顺序、种子、命令、warmup 与二进制 SHA。所有构建、CPU、采样和网络运行串行，公用 flock，禁止一边编译一边计时。

原始派生入口 `run_current_quota_network.sh` 引用前轮协议模板；该模板不必在新机器上存在，归档已保存本轮最终、自包含的 `remote/network.py` 与 `isolate-linux.sh`。复跑时修改该 network.py 的实验根目录/二进制路径，放到新输出目录（文件独占创建，保留原数据），直接在下述隔离入口执行；相同 seed、secs12/warmup3、每 case 12 对和队列协议都保留。

原始网络入口先在宿主取得锁，再把 fd 传入独立 mount/network/PID namespace，私有 /run 和 /sys。元数据同时记录宿主与测试 namespace ID；脚本不接触宿主 systemd/WG 服务。实验必须有预授权 root、TUN、ip/tc/unshare；真实机器上的其他 TUN 不能作为本次测试对象。元数据包含队列和 qdisc 配置；MTU1420 的结论不外推物理 NIC、WireGuard 或 #66 的完整矩阵。

新目录中的生成协议可按以下方式启动（只针对已预授权的实验机）：

```sh
exec 9>/var/lock/zfstack-issue65-perf.lock
flock -n 9
export ZF65_PERF_LEASE_FD=9
bash isolate-linux.sh bash -c 'mount -t sysfs sysfs /sys; exec python3 /absolute/new-experiment/network.py'
```

## 功能证据

最终本地 Rust1.94.1 QA portable：17/17 PASS，冻结计划、manifest、每项执行/清理记录和聚合报告在 `qa-current-quota-evidence.tar.gz` 及相应 JSON/Markdown 中。Linux 检查命令见 `run_current_quota_checks.sh`，结果以 linux-U-* 日志为准；不要把“命令返回0但没有匹配测试”当通过，压力测试要核对包含1项实际执行。

新动态配额回归先红后绿，日志保留；纯新增测试未添加 ignore/skip。正确性输出和性能资格分别报告，不将本机 portable 功能资格当成 Linux 性能资格。

## 文件与完整性

`SHA256SUMS` 覆盖本目录所列的可交付文件（清单自身除外），先运行 `shasum -a 256 -c SHA256SUMS`。`allocation-table.csv` 给出48个冷/热配对 case 的实际请求与账本，完整192条诊断记录在原始包中。`raw-evidence.tar.gz` 的 `remote/` 保存第二台机器的原始 JSONL/config/meta、构建及测试日志、诊断、反汇编和运行脚本；`local/` 保存补充的本机回归日志、早期候选与 QA 证据。未提交性能二进制或 perf.data。`inputs.tar.gz` 按 F56/M/…/T/U/U2 分目录保存源码输入，禁止把候选 C 与 #22 历史版本 C 混淆。

`qualification.json` 分别保留 `non_passing` 原始不确定项、两轮长采样的 `resolutions`，以及最终仍未解决的 `unresolved`。补测 1B/64B 缓存 write_with 使用 5 倍迭代；短流 256 与下行 pump 使用 10 倍迭代，每项两轮各 12 对。它们不与原样本拼接。

完整实验结论见 [生产与验证报告](../../../docs/experiments/2026-10-15-issue65-tx-owner.md)。

网络原始12对中的 mixed 已通过保护界，connect 与上下行的区间偏宽；`network-connect-confirmation` 固定192对、每次4000连接/1000并发不变。原始包中的 `remote/network-bulk-confirmation-plan.json` 从首轮区间选择未通过的 bulk 项，每项固定24对、secs30/warmup5，其他拓扑参数不变。统计复现还需分别执行 `network_current_quota_analyze.py network-connect-confirmation` 与 `network_current_quota_analyze.py network-bulk-confirmation`；replay_stats.py 已包括它们。各原始不确定项及补测结果都保留。

## 本轮结论

`qualification.json` 的 performance_verdict 为 INCONCLUSIVE。CPU矩阵、F56保护、mixed、连接确认和长窗口吞吐已满足各自界限；2026-10-16 的固定64对补测还解决了下行 CPU/GB，但上行 CPU/GB 仍未充分排除 +2%。源代码候选 U 尚不满足 #65 全项验收，不建议直接合并。未采用的 V/V2 另见 raw 包中的 lazy-index-*-pilot 数据、lazy-index-summary.json、lazy-index-selection-plan.json 与 lazy-index-decision.json；复制严格分析脚本到新副本，可用两个 lazy-index pilot 标签重算，不能覆盖唯一原始目录。

allocator 插桩的 realloc 按一次 alloc 和一次 free 请求计数；本轮 TX 热循环的必需 Box 请求数、冷/缓存循环的 free=0 等计数一致，未观察到额外 realloc 请求。统计是 Rust allocator API 请求，不是底层 allocator 内部事件或 RSS。


## 最终 bulk 补测

`bulk-final-evidence.tar.gz` 仅包含新阶段27个原始文件，解包后的 `remote/` 与旧 raw 包的文件不重名。保留失败的按角色放置 A/A、因该精度门槛失败取消的 T/U warmup 前缀、回到原 CPU0–3 的12对同二进制 A/A 和64对 T/U、预登记计划、runner SHA 及结束后的身份/环境核对。没有把取消前缀计算为候选分数。

`bulk_confirmation_analyze.py` 检查原协议、完整样本、二进制身份、线程放置审计和已取消前缀状态，调用相同 bootstrap 分析，再输出 `bulk-final-summary.json`。`replay_stats.py` 已包含两个 raw 包的安全解包、全部新旧统计和逐字节核对；也可在合并的 `remote/` 副本中执行 `python3 bulk_confirmation_analyze.py`，再执行 `python3 qualify.py`。A/A 不用于修正比值，旧12/24对与新64对不合并。

最终下行 CPU/GB -0.06% [-1.54, +0.95]，上下行吞吐分别 +0.25% [-1.42, +1.66] / -1.95% [-3.16, +0.47]，均通过；上行 CPU/GB +0.97% [-1.60, +4.86] 仍为 INCONCLUSIVE。`qualification.json` 保留历史 `non_passing`、明确的 `resolutions` 和上行三个窗口的 `unresolved`，不将 CI 全绿替代性能验收。
