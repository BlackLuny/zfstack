# PR 94 本地对比：main、kernel、smoltcp、sing-box

这次在本机把 [PR 94](https://github.com/BlackLuny/zfstack/pull/94)（`9e2af0d`）和 main（`9d6fb76`）放在同一台 4 vCPU Linux 上重跑。生产 `src/` 两边一致。PR 94 没有换一份 TCP 栈，它验证的是测试链路里的队列：FIFO，还是按 peer 做字节轮转并在拥塞时丢掉积压更多的那个 peer 的队尾。

lwIP 没有接入这套 harness，没有给它编造数字。这台内核只有 reno 和 cubic，没有内核 BBR。内核也没有 `sch_netem`，所以 sing-box / gVisor 的客户端路径加不上 RTT 和丢包；那 24 次尝试都在 0.3 秒内返回设置错误。

完整格子在 [TABLES.md](TABLES.md)，每次运行的指标在 [summary.json](summary.json)，原始 JSON 在 `raw.tar.gz`。复现脚本是 [run_compare.py](run_compare.py)。

## 怎么测的

服务端三条栈共用同一个用户态瓶颈：100 Mbps、双向独立随机丢包、队列用同一套序列化器。无丢包时测 8 秒（前面 4 秒预热不算进吞吐）。多用户用例是 zfstack 的 isolate：一个重 peer 打下载，1/3/8 个轻 peer 以 25 Hz 开环发送 256 字节回显。开环没有发生器漏发，也没有请求超时。

| 路径 | 谁 | 能变的条件 |
|---|---|---|
| 模拟瓶颈 | kernel-cubic、smoltcp-cubic/bbr、zfstack-cubic/bbr | RTT 20/80/250 ms，丢包 0/1%，1 或 8 条流 |
| 同上，多用户 | 只有 zfstack。kernel 和 smoltcp 的适配器是单地址，进不了 isolate | 轻用户数、重连接数、FIFO 对 peer |
| 本机 TUN 代理 | zfstack-client（splice + vnet + TSO）、sing-box `go`、sing-box `gvisor`、无代理的 direct | 无 RTT/丢包。这是另一条路径，不能和 100 Mbps 表混读 |

Rust 1.94.1，release，不是 fat LTO。多数格子 1 次，80 ms 无丢包和一部分公平格子 2 次，表里是中位数。198/222 次通过正确性检查；失败的 24 次全是客户端 netem。

## PR 94 和 main

同一条件、两个二进制：80 ms、3 个轻用户、8 条重连接、无丢包。

| 宿主 | 队列 | 吞吐 Mbps | 轻用户平均延迟 | 最差 P99 | 栈 CPU s/GB | 进程 RSS MiB |
|---|---|---:|---:|---:|---:|---:|
| main | FIFO | 95.82 | 139.8 ms | 159.8 ms | 4.00 | 15.0 |
| PR 94 | FIFO | 95.67 | 139.6 ms | 159.8 ms | 4.25 | 15.5 |
| main | peer | 95.86 | 80.4 ms | 80.6 ms | 4.27 | 15.0 |
| PR 94 | peer | 95.71 | 80.4 ms | 80.6 ms | 4.15 | 15.5 |

两个宿主的差在重复噪声里。延迟几乎逐毫秒相同。PR 94 的预算重试没有改变这条没把预算打满的路径。下面的跨栈数字都来自 PR 94 的测试宿主，栈代码就是 main。

## 无丢包时，peer 把交互延迟收到基线 RTT

3 个轻用户、8 条重连接、CUBIC、队列约 1 BDP。三个轻用户彼此几乎一样（延迟 Jain ≈ 1），所以公平性不是“轻用户之间谁更快”，而是轻用户还要不要排在重用户的队列后面。

| RTT | FIFO 平均 / 最差 P99 | peer 平均 / 最差 P99 | 吞吐 FIFO → peer |
|---:|---|---|---|
| 20 ms | 37.2 / 40.0 ms | 20.4 / 20.5 ms | 96.16 → 96.22 Mbps |
| 80 ms | 142.7 / 159.8 ms | 80.4 / 80.5 ms | 96.00 → 95.70 Mbps |
| 250 ms | 433.4 / 500.1 ms | 250.3 / 250.6 ms | 89.40 → 87.86 Mbps |

peer 的平均延迟和 P99 都贴着基线 RTT。FIFO 大约再多一个 RTT，和 1 BDP 队列排满时的排队一致。80 ms 最差 P99 从 159.8 ms 到 80.5 ms，约 −50%。吞吐变化在 1 Mbps 以内。栈 CPU 约 4.0–4.1 s/GB，RSS 10 / 16 / 23 MiB，两边差不多，RSS 随 RTT 涨是因为窗口和队列，不是因为 peer。

换用户数和重连接数，无丢包时 peer 仍把 P99 钉在 RTT 上：

- 轻用户 1、3、8，重连接 8：peer P99 为 20.5、80.5、250.6–251.0 ms。FIFO 在 250 ms、8 个轻用户时最差 P99 到 777 ms，吞吐 91.8 Mbps；peer 的 P99 是 251 ms，但这格吞吐掉到 81.3 Mbps。
- 重连接 1（队列改为 2 BDP）时差距更大：80 ms FIFO 平均 209 ms、P99 240 ms，peer 仍是 80.4 / 80.7 ms，吞吐都是 94.7 Mbps。
- 重连接 32：peer P99 仍是 20.6 / 80.5 / 250.5 ms。250 ms 时 peer 吞吐 88.9 Mbps，高于 FIFO 的 84.4 Mbps。

BBR、80 ms、3 轻用户、8 重连接：FIFO P99 210 ms，peer 80.5 ms；吞吐 94.3 → 93.4 Mbps。栈 CPU 约 6.3–6.6 s/GB，高于同格 CUBIC 的 4.1。

单地址的 down/mixed 上，peer 和 FIFO 分不开用户（地址不是 `10.210.x`）。单流峰值没有掉：20/80/250 ms 的 peer 单流是 96.3 / 94.4 / 96.2 Mbps，和 FIFO 同级。这不是 PR 里 10 Gbps、1–2 连接没通过的那个峰值保护；本机瓶颈是 100 Mbps。

## 1% 双向丢包时，peer 几乎不改变延迟

丢包打在两个方向。CUBIC 单条重流量会掉到线速的一小部分，peer 救不了这个。

| RTT | 吞吐 FIFO → peer | 平均延迟 | 最差 P99 |
|---:|---|---|---|
| 20 ms | 49.0 → 49.2 Mbps | 21.6 → 21.9 ms | 都是 80.4 ms |
| 80 ms | 12.8 → 12.6 Mbps | 87.0 → 84.7 ms | 200.9 → 200.7 ms |
| 250 ms | 9.9 → 9.5 Mbps | 274.7 → 273.7 ms | 648.8 → 570.7 ms |

80 ms 上再扫用户数和重连接，peer 与 FIFO 的吞吐和 P99 仍然缠在一起。1 条重连接、1% 丢包时两边都只剩约 1.6 Mbps。BBR 在同样的 80 ms、1% 丢包下好得多（FIFO 76 Mbps、peer 69 Mbps），但 peer 用掉了大约 7 Mbps。

所以：无丢包、瓶颈队列看得见用户时，peer 把交互延迟收到 RTT，吞吐和 CPU、内存基本不动。随机丢包主导时，收益消失，BBR 上还有吞吐代价。这和 PR 94 自己的结论一致：它是受控瓶颈上的候选，不是生产默认。

## 和其他栈比吞吐、CPU、内存、P99

100 Mbps FIFO，队列 2 BDP（下行）或 1 BDP（一条下载加一条 1 KiB 回显）。栈 CPU 只包括用户态栈线程。内核 TCP 跑在软中断里，`内核线程 s/GB` 只有系统调用那一点，不能拿去和 zfstack 的 4 s/GB 比“谁更省 CPU”。进程 CPU 把两边的链路模拟线程也算进去了，100 Mbps 下模拟器本身就很贵，只适合同格横向看。

无丢包、1 条流的有效吞吐：

| RTT | kernel-cubic | smoltcp-cubic | zfstack-cubic | 同格的 BBR |
|---:|---:|---:|---:|---|
| 20 ms | 96.3 | 87.7 | 96.3 | 未测 |
| 80 ms | 96.3 | **16.1** | 94.9 | smoltcp 96.2，zfstack 92.8 |
| 250 ms | 96.3 | 97.2 | 96.2 | 未测 |

80 ms 的 smoltcp-cubic 两次都是 14.5 和 17.6 Mbps，瓶颈各丢了两千多个包，不是单次事故。同一条链路上 kernel 和 zfstack 都能顶到 95 Mbps 以上。smoltcp-bbr 在这格能回到 96 Mbps。8 条流、80 ms、无丢包：kernel 96.3，zfstack-cubic 95.0，zfstack-bbr 96.2，smoltcp-cubic 62.9，smoltcp-bbr 73.8。

1% 丢包、1 条流，CUBIC 三家都垮：80 ms 是 kernel 1.3、smoltcp 2.2、zfstack 1.4 Mbps；250 ms 都低于 1.2 Mbps。8 条流会好一截（80 ms 约 12–15 Mbps）。BBR 才拉开：80 ms 单流 smoltcp-bbr 55 Mbps，zfstack-bbr 27 Mbps。这台机器跑不了 kernel-bbr。

8 条批量流抢同一条 FIFO 时，吞吐并不均匀。无丢包时流间 Jain 大约 0.41–0.82，最慢的流往往只分到总吞吐的 2%–6%。1% 丢包时 Jain 升到 0.96 以上，因为大家都慢。peer 队列在这个单地址测试里不会按流拆开。

混合流量的回显 P99（加载后；空载大约是 RTT + 1 ms）：

| RTT | 丢包 | kernel | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---:|---:|---:|---:|---:|---:|
| 20 | 0 | 39.9 | 36.9 | 40.0 | — |
| 80 | 0 | 159 | **616** | 220 | **112** |
| 250 | 0 | 499 | 346 | 429 | — |
| 20 | 1% | 264 | 1021 | 101 | — |
| 80 | 1% | 519 | 382 | 386 | **82** |

无丢包、短 RTT 时三家 P99 都在 40 ms 上下。80 ms 时 zfstack-cubic 比 kernel 高大约 60 ms，zfstack-bbr 比 kernel 低。smoltcp-cubic 的尾延迟在 80 ms 明显更差，和它没跑满带宽是同一格。250 ms 无丢包时 smoltcp 的 P99 反而最低，同时吞吐是满的。

内存是整个 zfbench 进程的峰值 RSS，含客户端和模拟器，只能在同一类测试里比。单流大约 kernel 7–14 MiB、zfstack 9–15 MiB、smoltcp 11–13 MiB。8 条流时 smoltcp 到 40–48 MiB，zfstack 12–29 MiB，kernel 9–17 MiB。

栈线程 CPU，无丢包单流：zfstack-cubic 大约 3.4–3.8 s/GB，smoltcp-cubic 在能跑满的格子上大约 6–11 s/GB，80 ms 的 smoltcp-bbr 是 4.1 s/GB。8 条流时 zfstack-cubic 约 4.0–5.4 s/GB，smoltcp-cubic 约 9–16 s/GB。

## 客户端：sing-box 和 gVisor

本机回环，没有瓶颈、没有额外 RTT。zfstack 开了 splice、vnet 校验和卸载和 TSO；sing-box 用它自己的默认 TUN。字节都校验过。direct 是不经过代理的回环上限：单流下行 59.1 Gbps。

| 代理 | 下行 1 流 | 下行 8 流 | 上行 1 流 | 代理 CPU s/GB（下行 1） | 峰值 RSS | 400 条空闲连接 |
|---|---:|---:|---:|---:|---:|---|
| zfstack-client | 38.3 Gbps | 31.5 Gbps | 58.5 Gbps | 0.230 | 8.2 MiB | 4.5 KiB/连接，进程 8.1 MiB |
| sing-box go | 27.2 Gbps | 25.7 Gbps | 26.7 Gbps | 0.295 | 45.9 MiB | 11.5 KiB/连接，进程 40.5 MiB |
| sing-box gvisor | 10.4 Gbps | 26.3 Gbps | 21.4 Gbps | 1.044 | 42.4 MiB | 27.4 KiB/连接，进程 51.6 MiB |

把 worker 收成 1 个之后，zfstack 仍是 38.2 Gbps，sing-box go 28.3 Gbps，gvisor 掉到 4.6 Gbps。

加载下的 1 KiB 回显 P99：gVisor 0.41 ms（同时只有 10.7 Gbps）、sing-box go 1.45 ms（27.4 Gbps）、zfstack 2.41 ms（37.4 Gbps）。空载 P99 分别是 0.93、1.15、0.79 ms。zfstack 空闲更低，打满时的回显尾延迟比另外两家高大约 1–2 ms。

## 不要外推的地方

- 生产代码没有因为 PR 94 改变。peer 队列仍是实验开关，不是默认。
- 轻用户之间本来就公平。peer 改善的是轻用户相对重用户队列的等待。同 peer 内的连接仍是 FIFO。
- 100 Mbps、8 秒、1–2 次重复，不能覆盖 PR 里 10 Gbps 少连接峰值没通过的那一格，也不能代替 12 对确认实验。
- 内核 CPU 缺软中断。进程 RSS 不是栈自己的账本。
- 客户端和带 RTT 的服务端不是同一条路径。lwIP 没测。内核 BBR 没测。
