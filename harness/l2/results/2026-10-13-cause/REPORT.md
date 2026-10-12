# L2 小预算延迟归因实验

采样：2026-10-12 18:40 UTC 起（本地 2026-10-13）。承接 [首轮 L2](../2026-10-12/REPORT.md) 的 8 MiB / 16 重连接超时。首批使用同一个未加诊断的 worker，SHA-256 `fb32f8b37862d364d7e6abd4cf4bb4d39e3776e5f2f8ddb3ac89d1aebdbd07eb`；核心与 PR #25 当前 head `a951135` 内容相同，来源见首轮 metadata。

已确认根因：重 peer 占满同一端口可供 RX 使用的物理额度；256 字节轻请求所需的 320 字节 backing 使用 `Tier::Bulk`，被端口层拒绝。逻辑 `progress` 额度不覆盖这个物理申请，worker 回复旧 ACK 并丢弃 payload，客户端重传间隔把短暂资源竞争放大到 3–7 秒。下面将原二进制对照、抓包及临时诊断点的直接证据分开记录。

## 已有对照与包级证据

两个真实 WG 端口、四个 peer；peer 0 重下载、peer 1 同端口轻请求、peer 2/3 跨端口轻请求。固定 16 条重连接、256 字节 RR、8/16/8 秒空载/加载/恢复。读超时 30 秒只用于观察长等待，超过原 3 秒标准的结果仍是超时问题；本批 `ok=true` 只表示持续推进、数据校验和路径检查成功。

按 8 → 64 → 64 → 8 MiB 顺序运行，其他应用参数相同。

| 组 | 全局 / 端口 / peer 预算 | 同端口加载请求数 | 最大延迟 | P99 | 发生重传的请求 | 重下载 Gbit/s |
|---|---|---:|---:|---:|---:|---:|
| [budget8-a](budget8-a/analysis.json) | 8 / 4 / 4 MiB | 79 | 3432.3 ms | 样本不足 | 27 | 1.469 |
| [budget64-a](budget64-a/analysis.json) | 64 / 32 / 32 MiB | 706 | 244.1 ms | 30.56 ms | 1 | 1.942 |
| [budget64-b](budget64-b/analysis.json) | 64 / 32 / 32 MiB | 699 | 34.7 ms | 28.82 ms | 0 | 1.984 |
| [budget8-b](budget8-b/analysis.json) | 8 / 4 / 4 MiB | 88 | 7014.7 ms | 样本不足 | 22 | 1.471 |
| [capped-peer8-a](capped-peer8-a/analysis.json) | 8 / 4 / 1.28125 MiB | 1597 | 36.9 ms | 5.68 ms | 0 | 1.175 |

重传数在完整请求窗口统计；8 MiB 的所有 >100 ms 慢请求均有重传。其到达目标之前的等待占延迟至少 96.5%，中位约 98%。跨端口轻请求在两轮 8 MiB 加载期最大 15.5/15.3 ms、40.3/44.5 ms，仍持续推进。

64 MiB 第一轮出现一条重传和一条目标 Python 服务调度延迟，第二轮没有 >100 ms 样本。因此不能宣称大预算完全消除所有尾延迟；本实验定位的是小预算下反复出现的数秒拒收。

## 7.01 秒请求的时间线

`budget8-b`，应用序号 875，窗口第 14.670822 秒发起，在重载仍进行时完成。

| 客户端 wg0 发送时间（窗口秒） | 距上一次发送 | worker 对本次尝试的回复 |
|---:|---:|---|
| 14.670894 | 首次 | 5.71 ms 后回复 ACK，ack 不前进 |
| 14.881447 | 210.55 ms | 4.65 ms 后回复 ACK，ack 不前进 |
| 15.093449 | 212.00 ms | 5.08 ms 后回复 ACK，ack 不前进 |
| 15.529454 | 436.01 ms | 5.08 ms 后回复 ACK，ack 不前进 |
| 16.393451 | 864.00 ms | 4.86 ms 后回复 ACK，ack 不前进 |
| 18.089455 | 1696.00 ms | 7.00 ms 后回复 ACK，ack 不前进 |
| 21.677449 | 3587.99 ms | 数据被接纳，随后回包 |

前六次都使用相同 TCP seq `2178725687`。worker 回包 ack 也为该值，没有确认这 256 字节；窗口 raw=251，SYN-ACK 协商 scale=8，即还通告 64256 字节窗口。成功后 ack 前进 256 字节。

目标侧直到窗口第 21.678046 秒才看到请求（发起后 7007.225 ms）；目标应用收到后处理约 0.033 ms，WG 客户端收到回包为发起后 7014.667 ms。前六次及时的重复 ACK 证明包已到达 worker TCP 处理路径；数秒等待主要由客户端重传间隔构成。

## 端口与 peer 对照

默认 ZFC tuning：core RX=16320 KiB、TX=16384 KiB、stream RX/TX 各 64 KiB、prefetch=1024 KiB。`spawn_zfstack_netstack` 的单流缓冲下限为 35008 KiB；peer ceiling 的公式是：

```text
peer_bytes = min(port_bytes, max(port_bytes × peer_percent, single_flow))
```

所以在 8 MiB 全局、4 MiB 端口时，peer ceiling 同样为 4 MiB。一个重 peer 可以占到整个端口物理预算；25% 是公式输入，不代表实际只允许占端口的 25%。这些是硬上限，未为其他 peer 预留独占额度。

`capped-peer8-a` 保持全局和端口预算，实验性组合设置 RX_MAX_KB=64、TX_MAX_KB=64、PREFETCH_MAX_KB=64、STREAM_RX_KB=16、STREAM_TX_KB=16。实际 prefetch 为 16 KiB，单流下限和 peer ceiling 为 1312 KiB。该对照同时改变缓冲大小及其导出的 peer ceiling，用于把物理压力限制在重 peer，不能据此单独评估某一个配置项的性能。

| 组 | 端口分配失败累计最大 | peer 分配失败累计最大 | RX backing 分配失败累计最大 | egress 内存拒绝 / 队列满 |
|---|---:|---:|---:|---:|
| budget8-a | 449518 | 42 | 49 | 52195 / 0 |
| budget8-b | 447798 | 65 | 36 | 51488 / 0 |
| capped-peer8-a | 0 | 558995 | 0 | 50894 / 0 |

三组全局分配失败均为 0。小预算组实际卡在 4 MiB 端口层，并非用尽 8 MiB 全局预算。限制重 peer 后，peer 仍有大量分配失败、egress 仍有约五万次拒绝，但同端口轻请求零重传。这也表明不能仅凭 `sink_full` 或 CPU 总量把数秒延迟归因于出口调度。

## 临时诊断点直接确认

给相同核心加 [14 行诊断点](core-diagnostic.patch)：仅记录指定 256 字节轻请求的逻辑/物理拒收，以及小 RX allocation 返回的拒绝层级，没有修改额度、tier、调度或 TCP 状态变化。使用同一 ZFC checkout、features、fast-release/musl 构建，6m17s 完成；日志中的已有 warning 未作无关修复。诊断二进制 SHA-256 `7959abec49621f5730c0c9c0b72df035a0eef33131269b5004d5ef01fe2b19d1`，源码 patch 与 hash 在 [diagnostic-source.json](diagnostic-source.json)，构建信息在 [metadata.json](metadata.json)。

| 诊断组 | 同端口加载请求数 | 最大延迟 | P99 | RX 拒收日志 | 重下载 Gbit/s |
|---|---:|---:|---:|---:|---:|
| [diag-budget8-v2](diag-budget8-v2/analysis.json) | 67 | 6659.2 ms | 样本不足 | 38，全部 Port / backing | 1.483 |
| [diag-capped-peer8-v2](diag-capped-peer8-v2/analysis.json) | 1597 | 23.7 ms | 3.77 ms | 0 | 1.193 |

38 次全部为 `bytes=320 level=Port`，对应 `cause=backing progress=true rx_charged=0`；没有逻辑额度拒收。每条拒收日志都能匹配同一应用序号的 WG 内层发送，间隔为 0.16–0.68 ms。allocation 与 drop 日志相隔最多 131 µs。

其中应用序号 831 的 6.66 秒请求，前五次发送分别产生相同序号的 backing 拒收日志，之后第六次才到目标。这将包级重传与具体失败分支直接对应起来。

首条 allocation 日志记录：轻 peer 已用 6760 字节，端口已用 4190664 字节、上限 4194304，global 已用 4216344 字节、上限 8 MiB；Bulk headroom=90336 字节，故端口 Bulk 分配线为 4103968 字节。即使全局和该轻 peer 都有空间，320 字节申请仍在端口层失败。日志中的用量是在分配失败后读取的快照，其他线程会同时释放内存；拒绝层级是原分层原子检查的返回值，不以稍后快照反推。

同一诊断二进制的 peer 受限对照零 RX 拒收、零重传，进一步复现了原二进制对照。诊断输出有额外开销，不用它评估精确性能差异。

## 源码路径

- ZFC `zf-worker/crates/zfw-wireguard/src/inbound/zfstack.rs:463–467`：解密后的 TCP 包经 `ingress_borrowed` 进入 core。
- [conn.rs:1617–1643](https://github.com/BlackLuny/zfstack/blob/ee0e7284c24f0162e10238f8805d7cf96901d8b2/src/conn.rs#L1617)：in-order 小请求可通过逻辑 `progress` 额度；接着仍要分配实际 RX backing。失败会释放逻辑额度、增加 `dropped_no_mem`、回复旧 ACK，`rcv_nxt` 不前进。
- [buf.rs:649–653](https://github.com/BlackLuny/zfstack/blob/ee0e7284c24f0162e10238f8805d7cf96901d8b2/src/buf.rs#L649)：256 字节请求的 payload backing 需要 320 字节（256 + 64 元数据）；RX descriptor 扩容还需要独立额度。
- [budget.rs:900](https://github.com/BlackLuny/zfstack/blob/ee0e7284c24f0162e10238f8805d7cf96901d8b2/src/budget.rs#L900)：`RxChunk` 使用 `Tier::Bulk`，同样受端口物理预算及 bulk headroom 限制。逻辑 `progress` 不会提升它的物理分配优先级。
- [conn.rs:2151–2172](https://github.com/BlackLuny/zfstack/blob/ee0e7284c24f0162e10238f8805d7cf96901d8b2/src/conn.rs#L2151)：通告窗口右边界不后退，TX/缓存压力不会直接关闭健康连接窗口；因此可能出现窗口仍开着、实际 backing 无法分配的状态。

## 仪器与边界

[instrumentation.patch](instrumentation.patch) 只给测试脚本添加同端口轻流的 WG 内层抓包、目标 payload 抓包、目标收到/回复事件，以及更长的 watcher 清理等待。两个 tcpdump 均记录 0 kernel drop。全部完成的加载请求都有目标事件；所有重流模式校验、WG 路由和路径字节检查通过，物理客户端到目标捷径计数为零。

每组保存 `result.json`、完整 `raw.json.gz`、`analysis.json`、`diagnostics.log`，以及只含测试轻请求的 `light-inner.pcap.gz` / `light-target.pcap.gz`。[analyze.py](analyze.py) 不依赖第三方库，可在本目录运行 `python3 analyze.py` 重算包时间线。时钟以同宿主 monotonic/realtime 锚点对齐；数毫秒内的内核调度和时间戳误差不影响秒级拒收判断。

初次启动因 watcher 原有命名空间未在 10 秒内释放而退出，未取得流量结果；五组原二进制实验使用最多 50 秒等待。诊断首次启动也被保护检查拦住、没有流量结果，后续改为最多 150 秒等待再开始。未手动删除未知命名空间；canonical `topo_up` 按其代际/已退出 owner 检查回收遗留状态。等待差异不改变负载或 TCP 参数。

本实验没有验证 WAN、大量端口、长稳态或所有低内存情况下的公平性。缓冲限制对照是归因用配置，没有修改生产默认值，也不是产品修复方案。已经确认的限制应在 RX 物理进展额度/各 peer 资源隔离层解决；上调超时只改变报错时间，上调全局预算只避开本次触发条件。

## 清理与复现

七组完成用例的 `exit-code=0`、自己的 `namespaces-after.txt` 为空。所有本次 systemd unit 已 inactive，原 `zf` 与 watcher active，节点租约已释放。隔离 ZFStack checkout 的诊断源码已恢复，核心 hash 重新为 `bedf3b672b40ebe40db72bbac22943065e11efb7339ad9deb978083b22ba0742`。每个用例复制的 worker hash 与各组声明的 baseline/diagnostic hash 一致。

共享 rig 的缓存二进制在本轮由 `bf792a2…` 更新为 `a75d816…`；其来源为干净的 ZFC `73a1d977…`，mtime 18:44:40 UTC，早于诊断构建日志创建时间 18:45:01，未包含本次 `L2_RX_DROP` 诊断点。这是共享 rig 缓存，不是本轮用例使用的隔离候选；不把缓存 hash 不变写成清理通过条件。完整前后 hash、来源和服务状态保存在 metadata。

复现时在指定测试节点持租约，把 L2 脚本复制到独立目录并应用 `instrumentation.patch`，使用原二进制及 `L2_HEAVY_CONNS=16 L2_REQUEST_TIMEOUT_SECS=30`，分别设 `L2_MEMORY_MB=8/64/64/8`。peer 受限配置及实际运行脚本保存在 metadata。诊断构建另外应用 `core-diagnostic.patch`，构建后恢复源文件；诊断运行仅将 namespace 等待从 50 增至 150 秒。节点完整案卷保留于 `/root/zfc-rig/local/pr25-cause-v2-569a9e61/`，私钥和 worker/controller 配置没有导出。

此归因阶段只新增实验证据与说明。后续 [RX 物理信用修复与 12 组 ABBA](../2026-10-13-rx-credit/REPORT.md) 保持产品默认缓冲，分别记录修复、边界回归与实测代价。
