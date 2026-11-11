# QueueControl 持久 FIFO 注入器：隔离 VM 自检

对应 [#59](https://github.com/BlackLuny/zfstack/issues/59) 与 [issue 文本](../../../../qa/issues/queue-control-atomic-restoration.md)。本目录是开发期的特权自检与设计实验，**不是 held-out 正式 C16 运行**，不能据此移除 QueueControl 的 known-BLOCKED。全部运行（含失败）原样保留，失败后没有调整资格常量。

## 环境与口径

- 主机内核缺 NFQUEUE/netem，嵌套 KVM 不可用；在 virtme-ng 的 TCG 纯模拟 VM 中运行 Ubuntu `6.8.0-147-generic` 内核，4 vCPU、2 GiB。每次都在 `bash qa/isolate-linux.sh` 内执行，`isolation.verify()` 必须通过。
- `harness/l2/fifo_selftest.py` 建立 `rig-xxxxxx-worker/target` veth 对，用真实 `FifoQueueControl` 启动注入器，阶段 8/16/8 秒、500 ms 注入、250 ms guard、100000 包上限（overflow 模式为允许的最小 100 包），安装比 measurement_start 早约 7 秒。
- 负载：worker → target 的 UDP 500 pps × 1032 B（带序号与逐包载荷模式，`SO_SNDBUF` 256 KiB、非阻塞，统计 EAGAIN）以及 2 MB/s 逐字节核对的 TCP 流（产生 GSO 帧）。接收端用 `SO_RCVBUFFORCE` 64 MiB，记录 target namespace 的 UDP MIB 增量。
- 独立核对项与 oracle（`fifo_ledger.validate`）分开计算：UDP 恰好一次且有序、载荷、TCP 字节流、接收 socket 无丢弃、发送方无 EAGAIN、两端链路零 drop、eth0 qdisc 恢复为原 `noqueue`、cleanup PASS；full 另要求 loaded 期 UDP 延迟 ≥ 500 ms、recovery 期 < 125 ms。

重判任一运行：

```sh
cd harness/l2 && python3 -c "import json,sys,fifo_ledger as L; d=sys.argv[1]; r=json.load(open(d+'/queue-control.json')); print(L.validate(r, r['config'], open(d+'/queue-ledger.bin.gz','rb').read()))" results/2026-10-16-fifo-injector/final/full
```

## 设计实验

[backpressure.py](backpressure.py)（[输出](backpressure.log)）：同一 veth、500 ms 延迟、1000 pps × 1400 B 非阻塞 UDP 3 秒、`SO_SNDBUF` 256 KiB。

| 路径 | 发送/计划 | EAGAIN |
|---|---|---|
| 无注入 | 2155 / 2155 | 0 |
| netem | 2130 / 2130 | 0 |
| NFQUEUE（raw netlink 持有 500 ms） | 869 / 2136 | 2608 |
| tap FIFO | 2088 / 2087 | 0 |

NFQUEUE 排队项仍计入发送 socket，会引入原 netem 注入没有的发送缓冲反压；tap 路径与 netem 一样 orphan skb。因此未采用 issue 中的候选 NFQUEUE。

[clsact_probe.py](clsact_probe.py)（[输出](clsact-probe.json)）：持续 UDP 时对 noqueue veth 挂/删 `clsact` 30 次，丢失 18 与 16 个包，`tx_dropped` 仍为 0；不挂删时 0 丢失。qdisc graft 会 `dev_deactivate`，期间 noop qdisc 静默丢包。所以安装须在负载客户端启动前、拆除须在客户端全部回收且过了 `measurement_end` 之后——`FifoQueueControl` 与 `load.py` 的调用顺序保证这一点，oracle 检查安装/拆除时刻。

## 运行记录

`development/` 是逐步修正中的运行（源码为当时工作区，未单独提交）；`final/` 为提交 `12fb97860010617819483884b7fbc5665204467c` 上一次启动 VM 顺序执行的 full、cancel、overflow，见 [run.log](final/run.log)。

| 运行 | 结论 | 说明 |
|---|---|---|
| development/run1-failed | FAIL | mirred 动作 78739 包 ≠ 账本/tap 45742 帧：动作按 GSO 段计数。以 66 B 头复算得 78739 包、97720961 B，精确一致，于是账本增加逐帧 L2–L4 头长并按段闭合（该旧格式账本现被 oracle 拒绝）。另缺 46 个 UDP：接收端 `SO_RCVBUF` 被 `rmem_max` 截断且每轮只读一包，改为 `SO_RCVBUFFORCE`、整批读取并记录 UDP MIB。 |
| development/run2-failed | FAIL | 其余检查全过；oracle 发现 326 个非透明帧晚于透明切换时刻：主循环在 emit 前读时钟，长 flush 后仍用旧时间记录切换。改为 emit 后读时钟。 |
| development/run3-pass | PASS | 全部独立检查与 oracle 通过；收尾期最大迟到 19.47 ms，距 20 ms 上限仅 0.53 ms。 |
| development/cancel-run1-failed | 预期 FAIL；暴露缺陷 | 取消被记录、oracle 拒绝、清理通过，UDP 恰好一次但 15 处乱序：先删过滤器再放积压，直连新帧超越旧积压。改为重定向仍在时按序放完积压再删过滤器。 |
| development/cancel-run2 | 预期 FAIL | 乱序降为 4 处、缺 1 包，均在过滤器删除之后（tap 环残留被超越；缺包发送于 clsact 删除瞬间，见上方探针）。自检据此改为检查“异常只出现在重定向删除之后”。 |
| development/overflow-run1 | 预期 FAIL | 100 包上限下 oracle 拒绝溢出；接收端 UDP 缺失数恰等于账本中溢出的 UDP 帧数。 |
| **final/full** | **FAIL** | 其余 12 项独立检查全过（18475/18475 UDP 有序、TCP 73899784 B 一致、发送方 0 EAGAIN、链路 0 drop、loaded 延迟 500.52–529.92 ms、recovery ≤ 33.85 ms）。oracle 拒绝：收尾期 GSO 大帧按 2 Gbit/s 排程，帧 27467 迟到 20.05 ms，最大 26.20 ms，超过冻结的 20 ms。 |
| final/cancel | 预期 FAIL，自检通过 | 取消被记录、oracle 拒绝；FIFO 内帧全部交付，tap/动作段计数闭合；UDP 无重复，2 处乱序与 1 个缺包均在重定向删除之后；qdisc 恢复、cleanup PASS。 |
| final/overflow | 预期 FAIL，自检通过 | oracle 以 seq 28142 溢出拒绝；接收 13620/18475，缺失数等于账本溢出 UDP 帧数；qdisc 恢复、cleanup PASS。 |

final/full 账本 39510 帧（15607 个 GSO 帧），动作 77497 段、tap 39510 帧均与账本闭合；透明切换早于 recovery 边界 215.69 ms，拆除晚于 measurement_end 101.15 ms，最大占用 324 帧 / 2404212 B。

## 收尾期迟到

三次 full 的收尾期最大迟到依次为 18.45、19.47、26.20 ms，其他阶段 ≤ 10.4 ms。收尾约 2.4 MB 在策略下 9.6 ms 内放完（2 Gbit/s ≈ 250 MB/s），TCG 中单线程实际只能以约 70–85 MB/s 写出，迟到随之累积。这是模拟 CPU 的吞吐上限，不是释放策略差错；但 20 ms 是预注册常量，不因这次结果放宽，也不再重跑择优。该 VM 不足以证明收尾期满足迟到上限，须由具备真实 CPU 的隔离宿主上的 held-out 正式运行判定。

## 结论

实现与资格常量（`fifo_ledger.py` schema 4）已冻结。自检证明了路径持久、透明切换只依据 FIFO 自身为空、无反压、链路零丢、逐帧闭合与取消/溢出负例都能明确失败；但 final/full 在 TCG 上未满足收尾迟到上限，且尚无 held-out 正式 C16 运行。QueueControl 继续 known-BLOCKED。
