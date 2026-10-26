# 代理客户端 TUN：zfstack 与 mipstack

测量开始于 2026-10-26 13:29（Asia/Shanghai）。机器是 4 vCPU、16 GB 的 Linux 6.12.94 虚拟机（Intel Xeon，有 AVX2/AVX-512）。`/proc/stat` 里的 steal 基本是 0。这不是安静的物理机，两轮之间有的格子会差一截，下面把这种格子的两轮都写出来。

对比的是**代理客户端这一跳**，不是整份 mihomo 或 zf-worker：

```text
内核 TCP 客户端 ──▶ 198.18.0.2:5201 ──TUN zfcT──▶ 用户态栈 ──▶ 127.0.0.1:5201 内核服务端
```

字节都按 zfbench 的模式校验过。吞吐是预热之后 6 秒窗口里接收方的有效载荷。CPU 是同一窗口里代理进程的 utime+stime，除以十进制 GB。内存是该进程的 VmRSS / VmHWM。两端栈各跑两轮，轮流交替。

| 侧 | 构建 | 代理配置 |
|---|---|---|
| zfstack `9d6fb76` | Rust 1.94.1，`cargo build --release`（默认 release，没有 fat LTO） | `StackConfig::client()`，驱动单独一个 current-thread runtime，`TcpStream::splice` |
| mipstack `9a80a07`（2026-10-19） | Go 1.22.2，`go build` | 库默认：CUBIC，接收缓冲起步 1 MiB、上限 16 MiB，发送起步 256 KiB、上限 16 MiB；`GOMAXPROCS` 未设置，即 4 |

mipstack 这一侧是 `bench/mipstack-proxy`：透明 TCP forwarder，每条连接用 `io.Copy` 接到固定上游。它不跑 mihomo 的规则、嗅探或加密。zfstack 一侧也不是完整的 zf-worker。

桌面 MTU 65535 时，zfstack 按文档打开 `IFF_VNET_HDR` 和校验和卸载，这一跳两边都不扫描载荷。mipstack 没有 TX 校验和卸载，也没有 TSO；默认会自己验校验和、自己算校验和。MTU 1500 时 zfstack 额外打开 TSO。这是各自在 Linux 代理客户端上的推荐接法，不是把两边的卸载开关拧成一样。

同机直连回环（不经过代理）跑了一轮：下行 55.7 Gbit/s，上行 31.8 Gbit/s。它只作参照。上行低于开了卸载的 zfstack，说明回环路径和 TUN 大包路径不是同一个上限。

## 结论

在这条本机 TUN 代理跳上，zfstack 的优势是每 GB 的 CPU、常驻内存，以及单流上行。mipstack 在桌面 MTU 的单流下行已经能到二十多 Gbit/s，不是一个数量级的差距；把它限制在一个 OS 线程上时，上行会掉到大约 0.3 Gbit/s。MTU 1500 且没有 TSO 时，两边差得更多。

延迟没有拉开。空闲和一条大下载旁边的 1 KiB 往返，两边 p50 都在 1 ms 上下，负载下粗略 p99 是几毫秒。

## MTU 65535，推荐接法

两轮都列出来。CPU 是代理进程秒/GB，RSS 是峰值。

| 测试 | zfstack（校验和卸载 + splice） | mipstack（默认，4 线程） |
|---|---|---|
| 下行 1 流 | 32.6 / 37.4 Gbit/s，0.27 / 0.24 s/GB，RSS 8.3 MB | 26.5 / 26.7 Gbit/s，0.64 / 0.64 s/GB，RSS 62 / 58 MB |
| 上行 1 流 | 47.8 / 44.5 Gbit/s，0.27 / 0.29 s/GB，RSS 10 / 12 MB | 10.8 / 10.1 Gbit/s，0.71 / 1.00 s/GB，RSS 21 MB |
| 下行 8 流 | 18.8 / 18.7 Gbit/s，0.48 s/GB，RSS 25 / 26 MB | 16.9 / 26.9 Gbit/s，1.19 / 0.74 s/GB，RSS 249 / 274 MB |
| 上行 8 流 | 22.5 / 45.9 Gbit/s，0.52 / 0.28 s/GB，RSS 28 / 31 MB | 17.4 / 17.4 Gbit/s，1.02 / 1.01 s/GB，RSS 33 / 34 MB |

单流下行 zfstack 更高，CPU 大约是 mipstack 的 40%。单流上行大约 4 倍，CPU/GB 大约一半到三分之一。8 流下行的吞吐叠在一起：mipstack 有一轮 26.9 Gbit/s，高于 zfstack 稳定的 18.8，但那一轮仍是 0.74 s/GB 和 274 MB，zfstack 是 0.48 s/GB 和 26 MB。8 流上行 zfstack 两轮差了一倍（内核客户端看到重传，SRTT 大约 4 ms），两轮都高于 mipstack 的 17.4 Gbit/s，CPU/GB 也更低。

空闲进程 RSS：zfstack 大约 5.5 MB，mipstack 大约 7 MB。大流之后的峰值才拉开。

## 关掉卸载之后

同一台架、同样的 splice 和单驱动线程，但不开 vnet 头，zfstack 自己算校验和。用来看卸载贡献了多少，而不是把卸载算进栈本身。

| 测试 | zfstack 无卸载 | mipstack 默认 |
|---|---|---|
| 下行 1 流 | 19.6 / 21.5 Gbit/s，0.43 s/GB，RSS 8 MB | 26.5 / 26.7 Gbit/s，0.64 s/GB，RSS 60 MB |
| 上行 1 流 | 21.1 / 24.2 Gbit/s，0.53 s/GB，RSS 11 MB | 10.8 / 10.1 Gbit/s，0.71 / 1.00 s/GB，RSS 21 MB |

无卸载时单流下行 mipstack 更高，每 GB CPU 仍然更多。单流上行 zfstack 大约两倍，CPU/GB 更低。和上一节比，校验和卸载把 zfstack 单流下行从大约 20 Gbit/s 提到 33–37，上行从大约 23 提到 45 左右，CPU/GB 从 0.43–0.53 降到 0.24–0.29。

给 mipstack 打开输入方向的 IPv4/TCP 校验和信任（它仍然要算发出去的校验和）没有变快：下行掉到 19.1 Gbit/s、0.87 s/GB，上行掉到 3.5 / 4.8 Gbit/s。这组不作为推荐配置。

## 一个 OS 线程

zfstack 的数据面本来就在那个驱动线程上。mipstack 再设 `GOMAXPROCS=1`：

| 测试 | mipstack，1 线程 |
|---|---|
| 下行 1 流 | 6.9 / 8.2 Gbit/s，0.89 s/GB，进程 CPU 大约 0.8 核 |
| 上行 1 流 | 0.34 / 0.31 Gbit/s，无重传，SRTT 大约 1.4 ms，进程 CPU 大约 0.05 核 |

上行不是把核打满，而是几乎在等。TUN 读、TUN 写和两个方向的 `io.Copy` 分在不同 goroutine 上，一个 P 没法叠起来。zfstack 把 splice 放在同一个驱动线程里，单线程上行仍有 21–24 Gbit/s（无卸载）或 44–48 Gbit/s（有卸载）。

## MTU 1500

路由器常见的小 MTU。zfstack 开 TSO 和校验和卸载，一个超级段大约 64 KiB。mipstack 按 MSS 发，没有对应的分段卸载。

| 测试 | zfstack TSO | mipstack |
|---|---|---|
| 下行 1 流 | 22.0 / 22.4 Gbit/s，0.42 s/GB，RSS 8 MB | 3.6 / 3.4 Gbit/s，4.5 s/GB，RSS 24 MB |
| 下行 8 流 | 19.8 / 20.5 Gbit/s，0.46 s/GB，RSS 23 MB | 2.0 / 2.0 Gbit/s，8.5 s/GB，RSS 32 MB |
| 上行 1 流 | 28.9 / 35.4 Gbit/s，0.44 / 0.37 s/GB，RSS 11 MB | 4.6 / 4.7 Gbit/s，3.8 s/GB，RSS 20 MB |

这里的倍数主要来自分段卸载，不是拥塞控制。

## 建连、空闲连接、大流旁边的往返

MTU 65535，推荐接法。建连 2000 条、并发 64，两轮全部成功。

| | zfstack | mipstack |
|---|---|---|
| 建连速率 | 9700 / 11700 次/s | 6600 / 6700 次/s |
| 握手延迟 p50 / p99 | 0.56 / 2.1 ms，0.47 / 2.5 ms | 2.9 / 10.5 ms，2.3 / 9.3 ms |
| 含一次请求的总延迟 p50 / p99 | 4.7 / 29 ms，4.4 / 20 ms | 8.4 / 34 ms，8.0 / 41 ms |
| 建连期间峰值 RSS | 7.4 MB | 56 / 54 MB |

2000 条空闲连接（连上后不发数据，量进程 RSS 增量）。第一轮默认文件描述符上限是 1024，两边都在大约 510 条时失败；下面是把上限提到 65535 之后的重跑，二进制没变。

| | zfstack | mipstack |
|---|---|---|
| 每条连接 RSS | 4.64 / 4.63 KB | 54.8 / 54.8 KB |
| 2000 条时 RSS | 14.6 MB（空闲基线 5.5 MB） | 114 MB（空闲基线 7.2 MB） |
| 建连耗时 | 0.26 s | 0.51 s |

mipstack 的每条连接里含两个 goroutine 和 `io.Copy` 的缓冲区；zfstack 的 splice 没有这条用户态中继缓冲。4.6 KB 对 55 KB 是整个代理进程的差，不全是 TCP 控制块。

1 KiB 往返，每 100 ms 一次。空闲基线大约 30 个样本，负载段大约 60 个，p99 很粗。

| | zfstack | mipstack |
|---|---|---|
| 空闲 p50 / p99 | 0.46 / 0.82 ms，0.97 / 1.08 ms | 0.43 / 0.99 ms，0.38 / 1.05 ms |
| 旁边一条下行时 p50 / p99 | 1.03 / 5.5 ms，0.93 / 2.3 ms | 0.79 / 5.0 ms，0.74 / 3.3 ms |
| 同时的下行吞吐 | 28.4 / 29.3 Gbit/s | 16.5 / 16.6 Gbit/s |

往返没有明显赢家。同样的延迟下 zfstack 多带走大约 12 Gbit/s。

## 复现

```sh
rustup run 1.94.1 cargo build --release -p zfbench
cd bench/mipstack-proxy && go build -o /tmp/mipstack-proxy .
python3 bench/mipstack_matrix.py
```

原始 JSON 在本目录。空闲连接那一格如果文件描述符上限太低会在中途失败，需要 `ulimit -n 65535`（`sudo` 会把它带给代理子进程）。
