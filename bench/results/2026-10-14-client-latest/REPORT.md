# 客户端 TUN 协议栈对比（2026-10-14）

zfstack client profile 对 **sing-box 1.15.0-alpha.12**（当前官方 linux-amd64 最新预发布）以及两个 **C 语言**同类实现：vanilla **lwIP 2.2.1** TUN 代理、生产级 **hev-socks5-tunnel**。同一拓扑测延迟、吞吐、内存、CPU、1/8/32 流、混合负载、建连、空闲连接和短连接。

原始 JSON：本目录。复现：`bench/client_matrix.py`。设计背景：[`docs/design/0008-client-profile.md`](../../../docs/design/0008-client-profile.md)。图表：[Client TUN stack comparison](https://cursor.com/canvas/store-4a976e9d-7179-4187-abb1-b32f2681fdd4/dcb0e0b7-3cb8-484b-a4e3-597c41a6813a)。

## 结论

在这台 4 vCPU 主机上，**用户态栈里 zfstack 在大流吞吐、每字节 CPU、常驻内存上全面优于 sing-box `go` 和两个 C 实现**；内核 NAT（sing-box `system`）仍能在单流下行上更快。短连接和双向大流下的 RR 尾延迟是 zfstack 的弱项。

- **单流下行** zfstack 35.32 Gbit/s、0.251 CPU·s/GB、峰值 8.3 MB。sing-box go 27.38 / 0.293 / 64.4 MB。hev（C tun2socks）20.98 / 0.759 / 4.0 MB。lwIP 12.66 / 0.632 / 2.8 MB。sing-box system（内核 TCP+NAT）38.35 Gbit/s，吞吐最高，但 CPU 0.342 s/GB、内存 54 MB。
- **单流上行** zfstack **50.45 Gbit/s**，明显快于 go 30.56、system 24.11、hev 19.33、lwIP 8.57。
- **8 流 / 32 流下行** zfstack 仍领先 go（30.17 vs 25.18；25.63 vs 23.18）。相对 2026-10-06 那次（当时 8 流下行 go 略快），这次多流也不再落后。绝对值与 10-06 不可比：主机不同。
- **MTU 1500 + TSO** 时 zfstack 下行 31.29、上行 51.41 Gbit/s；关掉卸载后掉到 4.63 / 10.36，和 lwIP 一个量级。Linux 路由器形态必须开 TSO。
- **混合负载**（1 条饱和下行 + 1 KiB RR）：zfstack 36.96 Gbit/s，负载下 RR p50/p99 **0.74 / 1.39 ms**，与 go 的 1.07 / 1.42 ms 同档，bulk 快约 39%。8 流混合时 go 的 RR 尾延迟明显变差（p99 8.90 ms vs zfstack 2.82 ms）。
- **双向混合** 时 zfstack bulk 47.78 Gbit/s，但 RR p99 **7.21 ms**，差于 go 的 2.95 ms。大流把驱动打满后，往返被挤到后面。
- **建连** 4000 次、并发 64：zfstack **13075 conn/s**（4000/4000），go 11388，system 9789。lwIP 310 conn/s 且只成功 3793/4000。hev 309 conn/s、p50 **204 ms**——这是 SOCKS5 额外一跳的握手，不是协议栈 RTT。
- **空闲 4000 条**：zfstack **4.9 KB/条**（空闲 5.6 MB → 持有 24.2 MB）。go 9.5 KB/条（空闲 53.9 → 90.3 MB）。lwIP 极瘦：0.39 KB/条、持有 3.5 MB。hev+socks5 合计 9.6 KB/条。
- **短连接**（2000 × 64 KiB，并发 64）go 明显更快：**4196 conn/s** vs zfstack 1383 vs lwIP 1776 vs hev 302。浏览器那种大量短流，sing-box go 目前更合适。
- **lwIP** 作为公平的 C 协议栈对照：内存最小，单线程 `NO_SYS=1` 在混合负载下 RR 会被大流饿死（8 流混合负载下 RR 样本数为 0；双向混合 p99 约 2.9 s）。MTU 1500 下行 3 次里 2 次 RST。
- 官方 sing-box 1.15.0-alpha.12 **不再带 `with_gvisor`**，所以这次没有 gvisor 列。

## 方法

```
kernel TCP client ──▶ 198.18.0.2:5201 ──TUN zfcT──▶ proxy ──▶ 127.0.0.1:5201 kernel server
```

代理是独立进程，CPU = `/proc/<pid>` utime+stime，内存 = VmRSS / VmHWM。hev 把 tun2socks 和 `socks5-override` 两个 PID 加总。所有载荷按 `i % 251` 逐字节校验。

| 实现 | 版本 | 配置 |
|---|---|---|
| zfstack | `c2ae504` | `StackConfig::client()` + splice + `IFF_VNET_HDR` + 独立驱动线程；MTU 1500 格另开 TSO |
| sing-box go | 1.15.0-alpha.12，rev `25b0d5ea`，官方 linux-amd64 | 默认 `go` 栈（Go 1.26.8）；`system` 只跑 core 的 down/up/connect |
| lwIP | `STABLE-2_2_1_RELEASE` `77dcd25a` | `bench/c-stacks/lwip-tun-proxy`，`NO_SYS=1` epoll，MSS 8191，窗口与 hev 同档 |
| hev-socks5-tunnel | `50d7b41a` | YAML 开 TSO/jumbo；CONNECT 被 `socks5-override` 强制打到 127.0.0.1:5201（**多一跳用户态中继**） |

主机：4 vCPU Intel Xeon（通用 VM）、15 GiB、Linux 6.12.94+。每格 10 s、丢弃前 3 s、3 次中位数。矩阵 61 格 × 3 = 183 次，墙钟约 31 min；idle 4000 条第一轮撞上 sudo 默认 `nofile=1024`（约第 510 条 EMFILE），zfbench 把 `RLIMIT_NOFILE` 提到 262144 后重跑，4 栈均 4000/4000。

## 吞吐、CPU、内存

数字都是 3 次中位数。CPU 是**代理进程**的秒/十进制 GB；HWM 是峰值 RSS。

### 默认 MTU 65535，1 / 8 / 32 流

| 格 | zfstack | sing-box go | sing-box system | lwIP | hev |
|---|---:|---:|---:|---:|---:|
| 下行 1 流 Gbit/s | **35.32** | 27.38 | **38.35** | 12.66 | 20.98 |
| 下行 1 流 s/GB | **0.251** | 0.293 | 0.342 | 0.632 | 0.759 |
| 下行 1 流 HWM MB | 8.3 | 64.4 | 54.4 | **2.8** | 4.0 |
| 上行 1 流 Gbit/s | **50.45** | 30.56 | 24.11 | 8.57 | 19.33 |
| 上行 1 流 s/GB | **0.254** | 0.263 | 0.319 | 0.935 | 0.635 |
| 上行 1 流 HWM MB | 10.6 | 59.2 | 54.4 | **2.1** | 4.1 |
| 下行 8 流 Gbit/s | **30.17** | 25.18 | — | 12.00 | 20.94 |
| 上行 8 流 Gbit/s | **50.89** | 25.39 | — | 9.53 | 19.37 |
| 下行 32 流 Gbit/s | **25.63** | 23.18 | — | 10.81 | 20.50 |
| 下行 32 流 HWM MB | 62.7 | 169.7 | — | 18.6 | **12.0** |

单 worker（zfstack 1 个 tokio worker / sing-box `GOMAXPROCS=1`）：下行 36.16 vs 26.40 Gbit/s（0.246 vs 0.303 s/GB），上行 50.30 vs 28.49（0.256 vs 0.293）。和默认 worker 数几乎一样，说明这条路径本来就不是靠多 worker 堆出来的。

### MTU 1500（路由器形态）

| | 下行 Gbit/s | 下行 s/GB | 上行 Gbit/s | 上行 s/GB |
|---|---:|---:|---:|---:|
| zfstack TSO | **31.29** | **0.306** | **51.41** | **0.251** |
| zfstack 无卸载 | 4.63 | 1.979 | 10.36 | 1.294 |
| sing-box go（自动 GSO） | 23.10 | 0.358 | 29.82 | 0.269 |
| lwIP | 3.94（1/3 成功） | 2.033 | 2.24 | 3.566 |
| hev（YAML TSO/jumbo） | 21.23 | 0.751 | 19.95 | 0.578 |

无卸载的 zfstack 和 lwIP 都被每包 syscall / 软件校验和拖死。hev 开了卸载，吞吐贴着 sing-box go，但每字节 CPU 仍大约是 zfstack TSO 的 2 倍。

## 延迟与混合负载

RR：1 KiB，每 100 ms 一次。先单独跑 3 s 得空闲基线，再叠加 bulk。负载下每次约 70 个样本（lwIP 除外）。

| 格 | bulk Gbit/s | RR 空闲 p50/p99 ms | RR 负载 p50/p99 ms | Δp99 ms |
|---|---:|---:|---:|---:|
| zfstack mixed | **36.96** | 0.83 / 1.25 | **0.74 / 1.39** | 0.01 |
| sing-box go mixed | 26.58 | 0.38 / 0.91 | 1.07 / 1.42 | 0.60 |
| lwIP mixed | 13.52 | 0.33 / 0.45 | 85 / **1179** | 1179 |
| hev mixed | 20.05 | 0.32 / **203** | **0.11 / 0.38** | −202 |
| zfstack mixed 8 流 | **31.00** | 0.80 / 1.01 | **1.09 / 2.82** | 1.84 |
| sing-box go mixed 8 流 | 25.35 | 0.29 / 0.82 | 6.42 / 8.90 | 8.49 |
| lwIP mixed 8 流 | 12.06 | 0.23 / 0.60 | **无样本** | — |
| hev mixed 8 流 | 20.24 | 0.64 / 203 | 0.45 / 1.22 | −203 |
| zfstack mixed-bi | **47.78** | 0.52 / 1.00 | 4.39 / 7.21 | 6.39 |
| sing-box go mixed-bi | 28.53 | 0.47 / 0.83 | **1.96 / 2.95** | 2.49 |
| lwIP mixed-bi | 11.59 | 0.22 / 0.41 | 2344 / 2910（n=1） | 2910 |
| hev mixed-bi | 20.73 | 0.60 / 202 | 0.18 / 0.77 | −201 |

hev 空闲 p99 ≈ 200 ms、Δp99 为负：RR 连接自身要走 SOCKS5 握手，空闲基线的尾部是握手不是 RTT；握手完成后负载下 RR 反而很快。比较 hev 延迟请看**负载下**的 p50，不要看 Δp99。

lwIP 单线程：大流占满 `tcp_tmr` / 中继循环后，RR 要么饿死要么秒级排队。这是 `NO_SYS=1` 的结构限制，不是调参能抹平的。

双向混合下 go 的 RR 尾延迟更好，zfstack 的 bulk 更高：驱动线程在上下行同时泵时，入口轮询间隔被拉长。

## 建连、空闲、短连接

| | zfstack | sing-box go | sing-box system | lwIP | hev |
|---|---:|---:|---:|---:|---:|
| 建连 conn/s | **13075** | 11388 | 9789 | 310 | 309 |
| 成功 / 4000 | 4000 | 4000 | 4000 | 3793 | 4000 |
| 建连 p50 / p99 ms | **4.14 / 18.9** | 4.54 / 29.9 | 5.10 / 63.4 | 3.57 / 24.1 | 204 / 212 |
| 建连代理 CPU s | **0.27** | 0.38 | 0.57 | 0.27 | 0.84 |
| 建连 HWM MB | 8.1 | 90.6 | 63.5 | **3.3** | 5.3 |
| 空闲 KB/条 | 4.9 | 9.5 | — | **0.39** | 9.6 |
| 空闲 RSS → 持有 4000 条 MB | 5.6 → 24.2 | 53.9 → 90.3 | — | 2.1 → **3.5** | 3.8 → 40.4 |
| 短连接 conn/s | 1383 | **4196** | — | 1776 | 302 |
| 短连接 p50 / p99 ms | 45.2 / 107 | **12.6 / 39.3** | — | 34.4 / 79.7 | 208 / 222 |

空闲 KB/条 =（持有后 RSS − 空闲 RSS）/ 4000。hev 的持有 RSS 含 socks5-override。

短连接 zfstack 慢，和 0008 的「浏览器大量短连接」目标相反：每次都要 TUN 入栈 + splice 交接，固定成本被 64 KiB 除完之后仍然很高。go 的短流路径更轻。这是下次该打的点，不是测量噪声。

## 口径与失败

- **hev 不是纯协议栈对比**。多一个 SOCKS5 用户态中继，建连/短连接被握手钉在 ~200 ms；大流 RR 在握手之后可以很快。把它当成「C tun2socks 产品」而不是「C TCP 栈」。lwIP 代理才是和 zfstack / sing-tun `go` 同拓扑的栈对栈。
- **sing-box 是完整产品进程**（Go runtime、入站出站、规则）。空闲 54 MB 不能直接说「栈比 zfstack 大 10 倍」；增量（9.5 vs 4.9 KB/条）和峰值减空闲更可比。
- **lwIP MTU 1500 下行** 3 次中 2 次 `Connection reset by peer`、窗口内 0 字节。中位数只来自成功的那一次。
- **CPU 争用**：4 vCPU 上客户端、服务端、代理、软中断挤在一起。代理 `cpu_util` 常年 ≈ 1.0–1.6。数字是这台机器上的排序，不是网卡线速。
- 第一轮 idle 全部失败（harness EMFILE ~510）。报告里的 idle 数字来自抬高 `RLIMIT_NOFILE` 后的重跑；其它格未重跑。

## 和 2026-10-06 的关系

0008 §6 是另一台 4 vCPU 机（当时写的是 Linux 6.18）、sing-box 1.15.0-alpha.10（带 gvisor）。那次单流下行 zfstack 15.4 vs go 9.3 Gbit/s；这次 35.3 vs 27.4。主机更快，**不要把两次绝对值当回归**。方向一致的部分：zfstack 大流更快、更省内存；go 在部分多流/短流上仍有优势。新信息：官方包去掉 gvisor；8 流下行这次 zfstack 也领先；C 对照给出了内存下限（lwIP）和产品 tun2socks（hev）的位置。

## 复现

```sh
bench/c-stacks/build.sh
B=target/release/zfbench
SB=bench/third_party/bin/sing-box
sudo ZFBENCH_LWIP=bench/third_party/bin/lwip-tun-proxy \
     ZFBENCH_HEV=bench/third_party/bin/hev-socks5-tunnel \
     ZFBENCH_SOCKS5=bench/third_party/bin/socks5-override \
     bench/client_matrix.py $B $SB bench/results/YYYY-MM-DD-client 3 all
```

需要 root、iproute2、TUN。`build.sh` 会 clone 钉住的 lwIP / hev，并下载官方 sing-box linux-amd64。二进制在 `bench/third_party/bin/`（gitignore）。
