# 客户端 TUN 协议栈对比（2026-10-15）

在 `df432fd` 的台架修复之后重跑：专用 `CMD_SHORT`、lwIP PCB 拆除、C 代理 epoll/流控。对照 **sing-box 1.15.0-alpha.12** 以及两个 C 实现（lwIP 2.2.1 TUN 代理、hev-socks5-tunnel）。**2026-10-14 的短连接、lwIP 混合 RR、lwIP MTU 1500 下行不可信**，以本目录为准。

原始 JSON：本目录。复现：`bench/client_matrix.py`。设计背景：[`docs/design/0008-client-profile.md`](../../../docs/design/0008-client-profile.md)。图表：[Client TUN stack comparison](https://cursor.com/canvas/store-4a976e9d-7179-4187-abb1-b32f2681fdd4/dcb0e0b7-3cb8-484b-a4e3-597c41a6813a)。

## 结论

这台 4 vCPU 主机上，**用户态栈里 zfstack 在大流吞吐、内存、建连和短连接上都优于 sing-box `go` 和两个 C 实现**。内核 NAT（sing-box `system`）这次没有赢过单流下行。双向大流下的 RR 尾延迟仍是 zfstack 的弱项。

上一轮短连接 go 快 3 倍，是 harness 用无界 `CMD_DOWN` 测短流造成的。修好之后 zfstack **7556 conn/s**，go 6607，lwIP 6097。

- **单流下行** zfstack 28.22 Gbit/s、0.310 CPU·s/GB、峰值 8.2 MB。go 20.23 / 0.397 / 63.8 MB。hev 23.91 / 0.649 / 4.1 MB。lwIP 11.27 / 0.708 / 2.9 MB。system 22.64 / 0.525 / 54.3 MB。
- **单流上行** zfstack **28.80 Gbit/s**，快于 go 21.18、hev 20.60、system 19.23、lwIP 6.54。上行每字节 CPU go 略省（0.379 vs 0.431 s/GB）。
- **8 / 32 流下行** zfstack 仍领先（21.97 vs go 20.38；23.80 vs 17.39）。hev 多流贴着 go。
- **MTU 1500 + TSO** zfstack 下行 26.58、上行 25.13。hev（YAML 开了 TSO）下行 **26.48**，几乎打平。关掉卸载的 zfstack 掉到 3.82 / 6.12，和 lwIP 一档。lwIP 1500 下行 **3/3 成功**（上一轮 1/3 RST）。
- **混合负载**（1 条饱和下行 + 1 KiB RR）：zfstack 28.25 Gbit/s，负载 RR p50/p99 **1.02 / 2.21 ms**；go 20.63、1.28 / 1.94 ms。8 流混合时 go 的 RR 尾延迟明显变差（p99 12.44 vs zfstack 3.53）。lwIP 修好 epoll 之后 RR 不再饿死：单流混合 p99 **1.39 ms**，8 流 2.97 ms。
- **双向混合** zfstack bulk 34.63 Gbit/s，RR p99 **12.60 ms**，差于 go 的 3.34 ms 和 lwIP 的 1.43 ms（lwIP 的 bulk 只有 9 Gbit/s，挤占少）。驱动线程上下行同时泵时往返被拉长。
- **建连** 4000 次、并发 64：zfstack **9875 conn/s**（4000/4000），go 9605，system 8160。lwIP 310 conn/s、成功 3805/4000。hev 308 conn/s、p50 **204 ms**（SOCKS5 握手）。
- **空闲 4000 条**：zfstack **4.9 KB/条**（5.6 → 24.3 MB）。go 9.4 KB/条（54.1 → 90.3 MB）。lwIP 0.39 KB/条、3.6 MB。hev+socks5 合计 9.6 KB/条。
- **短连接**（2000 × 64 KiB，并发 64）zfstack **7556 conn/s**、p50 7.1 ms；go 6607 / 7.9 ms；lwIP 6097 / 9.7 ms；hev 303 / 207 ms。
- 官方 sing-box 1.15.0-alpha.12 **不再带 `with_gvisor`**。

## 方法

```
kernel TCP client ──▶ 198.18.0.2:5201 ──TUN zfcT──▶ proxy ──▶ 127.0.0.1:5201 kernel server
```

代理是独立进程，CPU = `/proc/<pid>` utime+stime，内存 = VmRSS / VmHWM。hev 把 tun2socks 和 `socks5-override` 两个 PID 加总。载荷按 `i % 251` 逐字节校验。

| 实现 | 版本 | 配置 |
|---|---|---|
| zfstack | `df432fd` | `StackConfig::client()` + splice + `IFF_VNET_HDR` + 独立驱动线程；MTU 1500 格另开 TSO |
| sing-box go | 1.15.0-alpha.12，rev `25b0d5ea`，官方 linux-amd64 | 默认 `go` 栈（Go 1.26.8）；`system` 只跑 core 的 down/up/connect |
| lwIP | `STABLE-2_2_1_RELEASE` `77dcd25a` | `bench/c-stacks/lwip-tun-proxy`，`NO_SYS=1` epoll，MSS 8191 |
| hev-socks5-tunnel | `50d7b41a` | YAML 开 TSO/jumbo；CONNECT 被 `socks5-override` 强制打到 127.0.0.1:5201（**多一跳用户态中继**） |

主机：4 vCPU Intel Xeon（通用 VM）、15 GiB、Linux 6.12.94+。每格 10 s、丢弃前 3 s、3 次中位数。61 格 × 3 = 183 次，全部 ok，墙钟 1820 s。相对 2026-10-14 的脚本修复：

- `CMD_SHORT`：短下载发完 `short_bytes` 后关连接；`CMD_DOWN` 保持无界（isolate 重流仍把 flow index 放在 param 里）。
- lwIP：限制每批 TUN 读、跟踪 epoll 兴趣、`tcp_sndbuf` 不再 16 位截断、对端 FIN 后立刻拆 PCB。
- socks5-override：出站缓冲未排空时停掉源端 `EPOLLIN`，避免 level-trigger 空转。

本机绝对吞吐低于 10-14 那次（当时下行 35 Gbit/s），主机更忙；**只比本目录内部的排序**。

## 吞吐、CPU、内存

数字都是 3 次中位数。CPU 是**代理进程**的秒/十进制 GB。

### 默认 MTU 65535，1 / 8 / 32 流

| 格 | zfstack | sing-box go | sing-box system | lwIP | hev |
|---|---:|---:|---:|---:|---:|
| 下行 1 流 Gbit/s | **28.22** | 20.23 | 22.64 | 11.27 | 23.91 |
| 下行 1 流 s/GB | **0.310** | 0.397 | 0.525 | 0.708 | 0.649 |
| 下行 1 流 HWM MB | 8.2 | 63.8 | 54.3 | **2.9** | 4.1 |
| 上行 1 流 Gbit/s | **28.80** | 21.18 | 19.23 | 6.54 | 20.60 |
| 上行 1 流 s/GB | 0.431 | **0.379** | 0.402 | 1.222 | 0.664 |
| 上行 1 流 HWM MB | 11.1 | 60.6 | 54.3 | **2.1** | 4.0 |
| 下行 8 流 Gbit/s | **21.97** | 20.38 | — | 10.77 | 20.65 |
| 上行 8 流 Gbit/s | **23.95** | 20.58 | — | 7.64 | 19.84 |
| 下行 32 流 Gbit/s | **23.80** | 17.39 | — | 6.62 | 20.81 |
| 下行 32 流 HWM MB | 69.9 | 154.9 | — | 20.4 | **12.2** |

单 worker（zfstack 1 个 tokio worker / sing-box `GOMAXPROCS=1`）：下行 28.31 vs 19.39 Gbit/s（0.311 vs 0.413 s/GB），上行 29.98 vs 24.78（0.417 vs 0.338）。上行 go 更省 CPU，和默认 worker 数那格一致。

### MTU 1500（路由器形态）

| | 下行 Gbit/s | 下行 s/GB | 上行 Gbit/s | 上行 s/GB | ok |
|---|---:|---:|---:|---:|---:|
| zfstack TSO | **26.58** | **0.346** | **25.13** | 0.470 | 3/3 |
| zfstack 无卸载 | 3.82 | 2.389 | 6.12 | 2.254 | 3/3 |
| sing-box go（自动 GSO） | 18.01 | 0.458 | 23.26 | **0.346** | 3/3 |
| lwIP | 3.16 | 2.533 | 1.95 | 4.092 | **3/3** |
| hev（YAML TSO/jumbo） | 26.48 | 0.542 | 21.20 | 0.589 | 3/3 |

小 MTU 下行 hev 和 zfstack TSO 打平，但 hev 每字节 CPU 仍高约 57%。无卸载路径仍被每包 syscall 拖死。

## 延迟与混合负载

RR：1 KiB，每 100 ms 一次。先单独跑 3 s 得空闲基线，再叠加 bulk。负载下每次约 70 个样本。

| 格 | bulk Gbit/s | RR 空闲 p50/p99 ms | RR 负载 p50/p99 ms | Δp99 ms |
|---|---:|---:|---:|---:|
| zfstack mixed | **28.25** | 0.46 / 0.71 | 1.02 / 2.21 | 1.33 |
| sing-box go mixed | 20.63 | 0.66 / 0.78 | 1.28 / 1.94 | 1.19 |
| lwIP mixed | 11.60 | 0.56 / 0.70 | **0.68 / 1.39** | 0.75 |
| hev mixed | 19.75 | 0.94 / **204** | **0.45 / 0.73** | −203 |
| zfstack mixed 8 流 | **24.82** | 0.81 / 1.32 | **1.44 / 3.53** | 2.13 |
| sing-box go mixed 8 流 | 18.30 | 0.29 / 0.74 | 9.58 / 12.44 | 11.69 |
| lwIP mixed 8 流 | 10.53 | 0.51 / 0.67 | 2.10 / 2.97 | 2.54 |
| hev mixed 8 流 | 22.26 | 0.45 / 203 | 3.18 / 3.95 | −199 |
| zfstack mixed-bi | **34.63** | 0.45 / 0.99 | 5.86 / 12.60 | 12.04 |
| sing-box go mixed-bi | 24.84 | 0.37 / 0.75 | **2.29 / 3.34** | 2.93 |
| lwIP mixed-bi | 9.00 | 0.37 / 0.54 | 0.81 / 1.43 | 0.82 |
| hev mixed-bi | 22.92 | 0.46 / 203 | 0.68 / 1.61 | −201 |

hev 空闲 p99 ≈ 200 ms、Δp99 为负：RR 连接自身要走 SOCKS5 握手。比较 hev 延迟请看**负载下**的 p50，不要看 Δp99。

lwIP 上一轮混合 RR 秒级排队，是 epoll 把 `EPOLLOUT` 一直武装、TUN 读饿死定时器造成的，不是 `NO_SYS=1` 的宿命。修好后单线程也能在 ~11 Gbit/s 下把 1 KiB RR 维持在 1–3 ms。

双向混合下 go 的 RR 尾延迟更好，zfstack 的 bulk 更高。

## 建连、空闲、短连接

| | zfstack | sing-box go | sing-box system | lwIP | hev |
|---|---:|---:|---:|---:|---:|
| 建连 conn/s | **9875** | 9605 | 8160 | 310 | 308 |
| 成功 / 4000 | 4000 | 4000 | 4000 | 3805 | 4000 |
| 建连 p50 / p99 ms | **5.29 / 25.6** | 5.66 / 27.6 | 6.11 / 49.5 | 4.69 / 22.8 | 204 / 213 |
| 建连代理 CPU s | **0.33** | 0.47 | 0.71 | 0.28 | 0.63 |
| 建连 HWM MB | 8.1 | 88.0 | 63.3 | **3.2** | 5.4 |
| 空闲 KB/条 | 4.9 | 9.4 | — | **0.39** | 9.6 |
| 空闲 RSS → 持有 4000 条 MB | 5.6 → 24.3 | 54.1 → 90.3 | — | 2.1 → **3.6** | 3.8 → 40.5 |
| 短连接 conn/s | **7556** | 6607 | — | 6097 | 303 |
| 短连接 p50 / p99 ms | **7.1 / 28.6** | 7.9 / 39.2 | — | 9.7 / 39.2 | 207 / 215 |

空闲 KB/条 =（持有后 RSS − 空闲 RSS）/ 4000。hev 的持有 RSS 含 socks5-override。

## 口径

- **hev 不是纯协议栈对比**。多一个 SOCKS5 用户态中继，建连/短连接被握手钉在 ~200 ms。把它当成「C tun2socks 产品」。lwIP 代理才是和 zfstack / sing-tun `go` 同拓扑的栈对栈。
- **sing-box 是完整产品进程**。空闲 54 MB 不能直接说「栈比 zfstack 大 10 倍」；增量（9.4 vs 4.9 KB/条）更可比。
- **CPU 争用**：4 vCPU 上客户端、服务端、代理、软中断挤在一起。数字是这台机器上的排序，不是网卡线速。本机绝对吞吐低于 2026-10-14 同机那次，不要把两次 bulk Gbit/s 当回归。
- 183/183 ok。没有 EMFILE。

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
