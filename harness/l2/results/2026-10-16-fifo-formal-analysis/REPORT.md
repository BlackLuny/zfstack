# 正式 C16 故障臂离线逐帧分析：用户态 FIFO 的容量与隐藏等待

接续 [remote23 正式运行](../2026-10-16-fifo-remote23/REPORT.md)。本目录只读取已保留的故障臂产物（`queue-ledger.bin.gz`、`queue-control.json`、UDP `raw.json.gz`），由 [analyze_formal.py](analyze_formal.py) 确定性地生成 [formal-analysis.json](formal-analysis.json)（重跑字节相同，sha256 `b9c2bf6d…acd8a`）。

**这不是重新判定：正式结果仍是 FAIL / VALID，known-BLOCKED 保留。** 分析的目的只是按交接顺序先给出根因证据，再决定实现方向。

## 结论

1. **单线程用户态转发容量不足。** 队头已迟到时，循环只做发送；这些 10 ms 窗口里的释放速率就是发送容量：318 个饱和窗口 p50 **69.7k fps**、p99 82.7k、max 105.3k，连续发送间隔 p50 10.8 µs。而负载阶段到达虽平均只有 18.95k fps，100 ms 桶 max 为 12869 帧，即约 **128.7k fps** 的突发。突发到来时 FIFO 必然积压，与延迟参数无关。
2. **无界 emit 饿死读取。** 测量窗内有 12 个超过 50 ms 的读取间隔，其中 11 个期间 >99% 的释放都已迟到（线程一直在发送），最长 **340 ms**；间隔结束后 1 ms 内一次补收的帧数多数为 104–132（12 个中最小 13）。读取停顿期间到达的帧在 tap 环里等待，账本看不到。
3. **账本对 tap 环等待不可见（oracle 可靠性缺口）。** UDP 开环请求有已知发送时刻，所以 “准入 − 计划” 能界定注入器读取前的等待：baseline max 5.67 ms，loaded p99 **406 ms**，recovery max **533 ms**（2232 个请求超过 20 ms）。同时账本里 transparent 阶段 p99 仅 70 µs、超限 0。按严格归因（请求在间隔内到期且在间隔结束后 5 ms 内被准入）只有 130 个落在读取间隔内，说明隐藏等待不只来自 emit 饿死，而是读取端整体跟不上，等待积在内核 tap 队列里。只修 emit 有界也无法让账本看见这段等待，也补不上容量。
4. **SEND_FAILED 是后果不是起因。** 172 帧连续（seq 196634–196805），全部在 active 阶段，0.6 ms 内（19.5708 s），20 ms 窗口发送速率 92k fps，这些帧发送前已迟到约 71 ms。按交接要求，没有盲重发 ENOBUFS，也没有忽略 SEND_FAILED。
5. UDP 超时请求的 seq 为 1987–1989 和 2301–2306，分别落在 19.87 s 和 23.01–23.06 s 的积压尾部，被释放时与同 1 ms 内的数十个请求帧挤在一起。

| 阶段 | 帧数 | lateness p50 | p99 | max | >20 ms |
|---|---:|---:|---:|---:|---:|
| pre | 11235 | 0.025 ms | 0.095 ms | 0.445 ms | 0 |
| active | 288987 | 26.8 ms | 143.3 ms | 162.7 ms | 160635 |
| drain | 18536 | 82.7 ms | 157.4 ms | 161.9 ms | 16652 |
| transparent | 36826 | 0.018 ms | 0.070 ms | 15.3 ms | 0 |

## 由此得出的实现方向

上面第 1 点就是交接中要求的 CPU 热点证据，只有在这一前提下才评估更快的实现。约束不变：单一 FIFO、原始字节与顺序、全部输入。

- 每帧经 Python 转发的方案在本负载突发下需要约 1.85×（128.7k / 69.7k）的容量，而且读取端的等待在账本外，所以没有继续给用户态 FIFO 加速或加队列。
- 新方案 `netem-fifo`（schema 5，见 [`netem_fifo.py`](../../netem_fifo.py)、[`netem_fifo_ledger.py`](../../netem_fifo_ledger.py)）把转发交给内核里**一个**持久 root netem。它始终带 rate，所以 `tts = max(t_in + delay, last.tts) + len/rate`，原地 change 也保持 FIFO 链。测量期间不删除、不替换 qdisc。
- 证据改为在队列**前**（clsact matchall mirred mirror 到私有 dummy）和队列**后**（eth0 outgoing）各取一份内核时间戳，因此队列前的任何等待都落在两份捕获之间，oracle 可以看见，不再依赖注入器自报。
- 新方案的仿真自检见 [2026-10-16-netem-fifo](../2026-10-16-netem-fifo/REPORT.md)：出口 FIFO 精确无误，但 full 自检因 veth 下游跨 CPU 投递导致接收端 UDP 乱序而 FAIL。因此它没有进入 case，也没有冻结或正式运行；**旧 SHA 的 PASS 不追认**。
