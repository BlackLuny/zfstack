# Issue draft: QueueControl 原子恢复（known-BLOCKED）

状态：对应 [#59](https://github.com/BlackLuny/zfstack/issues/59)。持久单 FIFO 注入器已实现，资格标准随 schema 4 冻结。新 KVM VPS 三模式能力自检通过，随后唯一一次正式 C16 为 **FAIL / VALID，QueueControl 仍为 known-BLOCKED**；VPS 的物理 CPU 独占资格也未建立。详细原始记录见 [远端报告](../../harness/l2/results/2026-10-16-fifo-remote23/REPORT.md)。

当前 `C16-l2-queue-control` 的业务请求超时与台架资格分别判定。冻结 `8bf5bb8` 的首轮及追加两轮均保留真实判定；请求完成不能把台架 NOT_RUN/BLOCKED 改成 PASS。`known-failures.json` 的标注不是豁免，required case、阈值和聚合规则不变。

## 问题及证据

持续负载下，`tc qdisc show` 读到 backlog=0 后、`tc qdisc del` 执行前可以再次入队。首轮 draining JSON 为零时，同轮稍后的文本已有 46322 B；cleanup JSON 为 90496 B。恢复虽然早于原 24 秒边界，仍不能证明删除没有丢弃当时待发包。零 link/qdisc drop 与 cleanup PASS 也不能覆盖 reset 的不可观测释放；90496 B 同样不能被冒称为确认丢失量。重复读零、缩短查询间隔、调整排空速率或重复跑到通过都不解决原子性。

首轮故障期 TCP 三 peer 有 17/16/16 次超过固定 250 ms，最大请求耗时约 1.003 s；这是保留的负对照事实。完整三轮摘要和原始证据索引见 [review-evidence](../review-evidence/README.md) 与 [VALIDATION](../VALIDATION.md)。

## 实现状态

- case v2 的 `queue_control.kind` 为 `fifo-delay-line`。`harness/l2/fifo_injector.py` 用 `clsact` + 共享 mirred 动作把 worker eth0 全部出口帧重定向到私有 tap，单线程有界 FIFO 原样经 AF_PACKET 写回；路径在测量开始至少 0.5 s 前安装，在 `measurement_end` 且负载客户端全部回收后才拆除。透明切换由同一线程依据 FIFO 自身为空决定，之后不再整形；不暂停或减少业务输入。
- 未采用候选 NFQUEUE：同等 500 ms 延迟下它让非阻塞 UDP 发送方出现 2608 次 EAGAIN（netem 与 FIFO 为 0），即增加原注入没有的发送缓冲反压。
- `harness/l2/fifo_ledger.py` 只从逐帧原始账本与内核原始计数复算 seq、阶段、释放时间、顺序、占用、tap 帧/字节与 mirred GSO 段计数闭包、安装/切换/拆除时刻及 qdisc 恢复；`harness/l2/test_fifo_ledger.py` 覆盖缺失、重复、乱序、溢出、早/晚释放、切换越界、空队列后继续整形、计数不闭合、取消、清理失败等负例。
- 隔离 VM（6.8 内核）自检的 full/cancel/overflow 结果及此前保留的失败见 [REPORT](../../harness/l2/results/2026-10-16-fifo-injector/REPORT.md)。v3 旧三轮失败原样保留，不追认。

## 原后续方案（已按上述实现，待 held-out）

采用持续存在的单 FIFO 注入器，让所有包始终经过同一路径；候选为 NFQUEUE 配合有界队列。在同一串行状态机或锁内完成空队列确认与透明转发切换，在整个测量结束后才拆除规则/资源。不得在恢复阶段继续保留 2 Gbit/s 整形来冒充原始无整形网络，也不得暂停、减少业务输入来制造空队列。

应在专用隔离宿主准备内核 NFQUEUE 支持、libnetfilter_queue 与开发依赖，避免影响共享生产宿主。当前节点 `5.4.0-216-generic` 配置为模块，`nfnetlink_queue`/`xt_NFQUEUE` 未加载，库及头文件未发现；本轮没有加载模块、安装依赖或新增规则。

## 2026-10-16 接手状态

- `1b19023` 修复取消时的路径所有权：信号只记录取消，父进程回收客户端且测量结束后才撤掉重定向。新 VPS 的 cancel 全帧送达、零链路 drop。此修复已在远端冻结源码 `16b0d13` 中。
- 本机同步观测与 CPU 绑定、SCHED_FIFO、缩减 VM vCPU 的全部失败对照见 [调度报告](../../harness/l2/results/2026-10-16-fifo-scheduling/REPORT.md)。相关观测证明过一段 vCPU 执行时间供给不足，不能直接外推为新 VPS 的根因。
- 新 VPS full 自检最大迟到 10.513299 ms；冻结原生 worker 后只跑一次正式 baseline/fault。正式基线通过，故障臂最大迟到 162.718680 ms，172 次 SEND_FAILED、35 个 UDP 超时，切透明晚 3.168893 ms，独立评估 FAIL / VALID。172 次失败集中于测量开始后 19.570766853–19.571394318 s，与 worker tx.dropped +172 等数，均在 loaded 中；另有 client0 rx.dropped +5。不能归到装拆窗口，也没有证据逐个关联 UDP 请求与加密帧。
- 本次新增 `queue-control.json.send_diagnostics`：保存发送尝试/失败总数、前 1024 个失败的 seq/phase/errno/返回字节数/原始起止时刻，以及最长单次 send 的原始墙钟区间。超出详情上限时显式累计 omitted_failures；逐帧账本仍保留所有结果。发送完成时钟同时用于账本和下一次 due 检查，保持每次 send 两个时钟读数。墙钟耗时包含失去调度的时间，不能当成纯内核或 CPU 时间。
- 诊断字段为附加信息，独立 oracle 不采信它们；没有加入自动重试、改为非阻塞或调度策略变化，SEND_FAILED/迟到仍严格失败。新增诊断只做可移植测试，**没有对新源码再跑特权自检或正式 C16，也没有证明性能改善**。旧的自检 PASS 仅绑定旧冻结 SHA。

后续先在冻结诊断版本上进行预注册的完整负载诊断，同时记录 guest 注入线程的调度/CPU、send 入口/出口和内核 drop 原因。新 VPS 是共享 KVM，无法观测物理 hypervisor 时须明确该缺口。AF_PACKET 的 `PACKET_QDISC_BYPASS` 在设备发送队列繁忙时可能增加 drop（[Linux packet(7)](https://man7.org/linux/man-pages/man7/packet.7.html)）；这只是候选机制，尚不能证明本次 errno。先据原始 errno 和内核路径区分未被接收的可重试错误与可能已消耗的帧，再决定是否保留队头重试，不能盲重发、忽略 drop 或更改 due 来掩盖失败。

若同步观测显示线程自身 CPU 热点，再评估有界发送批次、避免阻塞整个循环、减少 Python 逐帧分配/哈希/系统调用或 Rust 单线程实现。所有候选仍须保持唯一 FIFO、字节内容、顺序、500 ms、2 Gbit/s、20 ms、原业务输入及透明恢复语义。修复后先重新做三模式自检，再冻结 worker 按流程正式验证；全部失败保留，不择优重跑。

### 接手后：根因与候选 `netem-fifo`

- 根因先于修改，结论见 [离线逐帧分析](../../harness/l2/results/2026-10-16-fifo-formal-analysis/REPORT.md)。
  - 容量：用户态单线程发送容量 p50 69.7k fps，而负载突发约 128.7k fps。
  - 读取停顿：emit 期间读取停顿最长 340 ms。
  - 隐藏等待：读取前的等待（UDP 在 recovery 期最大 533 ms）不在账本内。
  - SEND_FAILED：发生在帧已迟到约 71 ms 之后，是后果而非起因。

  因此没有盲重发 ENOBUFS，没有忽略失败，也没有新增队列或修改 due。
- 这就是 CPU 热点证据，据此评估了更快的实现：候选 `netem-fifo` 由内核里单个持久 netem 承担 FIFO，在队列前后各取一份内核时间戳，因此队列前的等待对 oracle 可见。它只用于自检，未写入 case。
- [仿真三模式自检](../../harness/l2/results/2026-10-16-netem-fifo/REPORT.md)：
  - cancel 和 overflow 负例 PASS。
  - full **FAIL**：出口 FIFO 精确，但接收端 UDP 乱序。
  - 预注册 trace 诊断证明，乱序来自 netem 多 CPU 出队后 veth 按 CPU 积压投递。
- 要继续推进，需要维护者二选一：
  1. 重新预注册“顺序义务止于 eth0 出口”的自检判据（原 v1 netem 注入理论上也有这一属性，但本次未验证），这不追认本次结果；
  2. 找到不改变发送端、不新增队列、不改对端设备的单路径交付实现。

  在此之前 C16 保持 case v2，known-BLOCKED 不变。

远端可继续使用 `/root/zfstack-qa-pr75-20261016-remote23-01` 的冻结输入、worker、缓存和失败记录；本次因没有 SSH 凭据没有执行远端操作。该机有生产服务；继续使用 `qa/isolate-linux.sh` 和公共 QA Lease，不停止现有服务、不清理未知资源。准备 venv 曾间接升级 7 个 Python 3.11 补丁包；最终核对主 PID 未变、7 个容器 healthy，完整事务与局限均在报告中。

## 验收要求

- 保持原 500 ms 注入、100000 包上限、8/16/8 秒阶段、250 ms guard、3 秒请求 deadline、250 ms 服务合同及原 TCP/UDP/重流负载，不改到达语义。
- 对入队、出队、交付、取消和有界资源占用建立完整原始账本；缺失、重复、乱序、溢出、丢包或未达阶段都要明确失败，不能仅依赖计数器为零。
- 原 24 秒 recovery 边界前原子转为透明路径；证明不改变包内容、负载、生产默认值及恢复期服务语义，保持完整 recovery 观察。
- 实际链路/队列计数、时间与身份闭包、公共 Lease、磁盘/内存守卫、取消/异常/失租约清理及负例必须独立复算。
- 先冻结实现与预注册资格标准，再用独立 held-out 正式运行验证；保留旧三轮失败，不覆盖、不追认、不按结果挑选参数。
- 该 issue 完成前，QueueControl 继续 known-BLOCKED；生产 pacing PR 可单独审查，但不能借此宣称完整 Nightly 或发布资格。
