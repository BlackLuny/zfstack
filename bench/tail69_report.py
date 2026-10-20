#!/usr/bin/env python3
"""Render the issue69 causal evidence without pooling users or hiding failures."""
import json
from pathlib import Path

OUT=Path(__file__).resolve().parents[1]/"docs/experiments/2026-10-20-issue69-tail"


def main():
    s=json.loads((OUT/"summary.json").read_text())
    rows=[];costs=[]
    for c in s["comparisons"]:
        a,b=c["A"],c["B"]
        span=lambda v:"–".join(f"{x:.2f}" for x in v)
        rows.append(f"| {c['block']} | {span(a['p99_range_ms'])} | {span(b['p99_range_ms'])} | {a['mbps']:.3f} → {b['mbps']:.3f} | {(c['throughput_ratio']-1)*100:+.3f}% | {a['max_ms']:.2f} → {b['max_ms']:.2f} |")
        costs.append(f"| {c['block']} | {a['stack_cpu_per_GB']:.3f} → {b['stack_cpu_per_GB']:.3f} | {a['link_cpu_per_GB']:.3f} → {b['link_cpu_per_GB']:.3f} | {a['process_cpu_per_GB']:.3f} → {b['process_cpu_per_GB']:.3f} ({(c['process_cpu_ratio']-1)*100:+.2f}%) | {a['peak_rss_kib']} → {b['peak_rss_kib']} | {a['light_requests']} → {b['light_requests']} |")
    def result_path(case):
        return case+("/result.json" if (OUT/case/"result.json").exists() else "/result.json.gz")
    fixed=[]
    for c in s["cases"]:
        if not c["case"].startswith(("fixed-bbr-","fixed2-bbr-")):continue
        ar=c['arrivals']
        fixed.append(f"| [{c['case']}]({result_path(c['case'])}) | {c['ok']} / {ar['valid']} | {ar['completed']}/{ar['scheduled']} | {ar['max_generator_lag_ms']:.3f} | {c['light_requests']} | {c['worst_p99_ms']:.2f} | {c['mbps']:.3f} |")
    fc=[]
    for c in s["fixed_comparisons"]:
        a,b=c['A'],c['B']
        fc.append(f"固定工作块 `{c['block']}` 总体有效性为 **{c['valid']}**。描述性均值：bulk {a['mbps']:.3f} → {b['mbps']:.3f} Mbps（{(c['throughput_ratio']-1)*100:+.3f}%），测量窗口进程 CPU {a['process_cpu_per_GB']:.3f} → {b['process_cpu_per_GB']:.3f} 秒/GB（{(c['process_cpu_ratio']-1)*100:+.2f}%），栈线程 {a['stack_cpu_per_GB']:.3f} → {b['stack_cpu_per_GB']:.3f} 秒/GB，link 线程 {a['link_cpu_per_GB']:.3f} → {b['link_cpu_per_GB']:.3f} 秒/GB，峰值 RSS {a['peak_rss_kib']} → {b['peak_rss_kib']} KiB。有效性失败时，这些数值不能升级为验收通过或无代价证明。")
    report="""# Issue 69：共享队列长尾归因与队列层修复原型

在本次 Linux TUN 场景中，数百毫秒长尾的直接链条是**共享 FIFO 满载丢掉轻请求的响应，TLP/RTO 等待进一步放大延迟**。逐包跟踪没有发现相同量级的栈内发送等待。保持线速、总队列容量和重连接数量不变的 Peer 队列原型显著改善 P99；已完成的 closed-loop 交替测试中，bulk goodput 下降 0.398%–0.510%，通过事先冻结的 1% 吞吐门。不过 CPU 与部分 RSS 有上升，完整固定到达率验收也未通过，因此不能称为严格无代价修复，更没有作为 ZFC 生产补丁交付。

![Measured tail, throughput and cost](tail-causal.png)

## 场景、层次和验收口径

2026-10-20，本机 macOS arm64 + Docker Linux arm64 VM；基础源码 `5830d2b5afd2e25df55bbafd950a2796eb7de04f`，当前分支 `perf/issue51-wire-checksum`。同一 Shard/iface 上，一个重 Peer 的 200 条持续下载，与三个独立轻 Peer 的 256 字节 TCP echo 争用同一瓶颈。100 Mbps、基础 RTT 10 ms、每方向总队列上限 250000 字节、随机丢包率为零；vCPU 2–7 亲和，未独占物理核。3 秒空载基线、2 秒重载预热、名义 12 秒测量，连接建立不计入吞吐窗口，慢响应使实际窗口延长时使用实际墙钟。

`zfbench` 的网络模拟器位于栈已提交报文之后：`app write → Shard 发送回调 → 测试 channel → 共享限速/排队/丢包 → TUN → Linux TCP`。它不是生产 ZFC 的 UDP socket 队列。完整 ZFC worker、WG 加解密、真实多端口和远端路由器均未重放。生产 `src/**` 没有修改。

归因与性能测量分开：跟踪仅限轻 Peer，内存最多保存 200000 条事件，所有测量线程结束后写盘，尾部校验 omitted=0。最初直接缓冲写文件的 `trace-bbr-200`/`trace-cubic-200` 存在绑定目录 I/O 干扰，只留作诊断，不用于性能验收。A/B、AA 和固定到达率轮次关闭逐包跟踪。RR 分位数独立按 nearest-rank 重算，报告每轮三个用户 P99 的最大值，不合并用户或不同运行的样本；小于 100 样本不报告 P99，也不以 300 样本报告可靠的 P99.9。

## 逐请求证据

用 TCP 端口、SYN 初始 seq、`初始 seq + 1 + 请求序号 ×256` 关联响应，区分旧响应重传。按观察到的两次 TCP 连接顺序区分 baseline/loaded，不按猜测的时间截断。

| 内存跟踪场景 | loaded 请求数 | >50 ms | >50 ms 有队列丢包 / 响应重复发送 | >200 ms | >200 ms 明确响应被丢 |
|---|---:|---:|---:|---:|---:|
| [CUBIC](trace-memory-cubic-200/attribution.json.gz) | 1108 | 27 | 27 / 27 | 5 | 5 |
| [BBR](trace-memory-bbr-200/attribution.json.gz) | 1066 | 33 | 23 / 25 | 6 | 6 |

CUBIC 最慢请求为 Peer 3、端口 48617、响应 seq `2896630696`，总计 290.054 ms：

| 相对请求开始 | 事件 |
|---:|---|
| 5.128 ms | 请求到达栈 ingress |
| 5.261 ms | 应用将响应写入栈 |
| 5.269 ms | 栈首次提交响应 |
| 5.301 ms | 共享队列已有 249920 字节，308 字节 IP 响应被丢 |
| 65.301 ms | 相同 seq 的探测重传（间隔 60.032 ms） |
| 65.322 ms | 探测响应也被共享队列丢掉 |
| 265.355 ms | 相同 seq 再次重传（间隔 200.054 ms） |
| 290.014 ms | 响应交付 Linux TCP |
| 290.054 ms | 客户端读完响应 |

此请求栈 ingress→应用写为 0.133 ms，应用写→首次 egress 为 0.007 ms，首次 egress→实际交付为 284.746 ms。CUBIC loaded 全部请求中应用写→首次 egress 最大 0.337 ms；BBR 对应最大 5.550 ms，且其 433.514 ms 最慢请求在应用写后 0.010 ms 即已首次发出，424.308 ms 花在首次发送至交付这一段。BBR 存在其他 >50 ms 请求未关联到响应丢包，线程/VM 唤醒可贡献数至数十毫秒，不能把全部慢请求都归为同一原因。

TLP 后重置 RTO 的逻辑已在 `src/conn.rs` 核实，与 [RFC 8985 §7.3](https://www.rfc-editor.org/rfc/rfc8985.html#section-7.3) 的探测后重新挂 RTO 规则一致。这些等待是这次丢包后的放大机制，未由此发现应修改定时器的协议错误。未逐包跟踪的 3.090 秒 closed-loop 极值只保留观测，不能声称已证明该单次请求的全部路径。

## 反证对照与最小候选

- **调小栈调度量子**：仅测试配置 `max_quantum=2740`、`round_bytes_cap=32768`。BBR goodput 95.697 Mbps，最差 P99 约 98 ms、max 约 291 ms；29 个 >50 ms 请求仍全部关联队列丢包与响应重传，3 个 >200 ms 响应均被丢。不能治愈此类长尾，未改变生产默认值。
- **把 FIFO 放大到 4 MB**：goodput 降到 86.556 Mbps，三个轻用户各仅 38 样本（不报 P99），全部 114 请求都超过 200 ms，仅一次响应丢包/重传。主要变成约 330 ms 排队，max 约 668 ms。扩大缓冲会隐藏丢包而积累排队，予以拒绝。
- **队列层原型**：A=`fifo-service`，B=`peer-drr-pushout`。两者共享同一按字节串行器和 250000 字节总上限；B 以逻辑端口+Peer 分类，按字节 DRR（quantum=1420）服务。拥塞时允许轻 backlog 新报文从其他最大 backlog 的队尾腾出空间，不丢正在串行化的报文，不重排 Peer 内报文。工作保持，连接数/线速/容量不变，没有按包大小或协议识别“优先请求”。同时改变了服务顺序与拥塞准入策略，未独立拆分这两个因素的贡献。

此原型只存在于 `bench/src/link_queue.rs`，必须显式设置 `ZFBENCH_QUEUE_DISCIPLINE=peer`；未设置时仍是原始 FIFO。它借鉴队列隔离的因果思路，**不是完整 FQ-CoDel/AQM 实现**。[RFC 8290 §6.2](https://www.rfc-editor.org/rfc/rfc8290.html#section-6.2) 说明封装会限制外层分类；生产 WG 路径需在仍有 Peer 元数据的位置做隔离，并验证真正瓶颈在哪。只在栈里轮转 Peer 不能保证栈外 FIFO 的字节隔离；在本地队列调整也不能保证改变远端瓶颈。

## 无跟踪交替测试

计划在运行前冻结：一轮 legacy AA 校准（95.709 Mbps），BBR ABBA/BAAB 与 CUBIC ABBA 三个块，每臂两轮，共 13/13 完成。每块 bulk goodput B/A ≥0.99，载荷完整、sink errors=0、末端 live connections=0，总队列不得超过原上限。冻结 binary SHA256：`8b20b12b0d790aaaa48b9a64b853dc9a4d89641e46a0c7e8ab9eb83d87f01c79`。A/B 的 FIFO 接纳、丢包和离开时序与旧 FIFO 通过确定性突发到达对照校验；AA 为性能校准，不代替属性验证。

| 块 | A 最差用户 P99 范围 ms | B P99 范围 ms | bulk Mbps A→B | bulk 变化 | 最慢单次 A→B ms |
|---|---:|---:|---:|---:|---:|
"""+"\n".join(rows)+"""

范围是每臂两轮的 min/max，不是置信区间。三个块均通过预注册的 1% 吞吐门；这不等于数学意义吞吐零下降，也不足以证明所有负载下无回退。

| 块 | 栈 CPU 秒/GB A→B | link CPU 秒/GB A→B | 进程 CPU 秒/GB A→B | 峰值 RSS KiB A→B | 完成轻请求 A→B |
|---|---:|---:|---:|---:|---:|
"""+"\n".join(costs)+"""

分母均为 heavy 实际接收的十进制 GB。栈/link/进程 CPU 是标记测量窗口的线程/进程 CPU 增量；峰值 RSS 是 Linux `getrusage(RUSAGE_CHILDREN)` 的整轮最大值（含建连、基线、观测与清理），不代表稳定驻留或泄漏。计入吞吐窗口外 CPU 的另有 `resources.json` user/system 全程值，不能和表中的窗口成本混用。B 的 closed-loop 请求完成数约为 A 的三倍，会增加有效工作与网络开销，因此不能将全部额外 CPU 直接解释为数据结构开销；部分轮次 RSS 上升和 CPU 上升仍需显式保留。无 CPU/RSS 非回退证明，未将原型晋升为生产修复。

## 固定到达率：保留失败，不追认验收

独立发送/接收线程、每用户绝对 40 ms 计划（25 Hz）、每个 256 字节帧带唯一编号；下一请求不依赖上次响应。每轮基线 75×3、loaded 300×3，3 秒完成期限；从计划到达时刻计时，保留漏发/失败/超时/未完成，发生晚发不追赶补发。预设任何发生器延迟 >20 ms 都使整轮性能无效。所有原始计划与完成时间见各 `arrivals.json`，分析器独立核对序号、时序、计数与 RR 延迟。

首次 `fixed-bbr-abba-0-A` 有一个空载基线请求晚发 20.2655 ms，超过门槛；原组在首轮失败后停止。保留该失败，再冻结一个四轮完整 ABBA 块（`fixed2-arrival-plan.json`），二进制、25 Hz、20 ms、seed=5 和 vCPU 范围均不变，所有四轮无论有效性都执行，不再追跑择优。晚唤醒发生在无重流基线，不能归因于重载共享队列；具体 VM/宿主原因未进一步证明。

| 运行 | 功能 / 到达计划有效 | 完成 / 全部计划 | 最大发生器晚发 ms | loaded 完成 | loaded 最差 P99 ms | bulk Mbps |
|---|---|---:|---:|---:|---:|---:|
"""+"\n".join(fixed)+"\n\n"+"\n\n".join(fc)+"""

## 改动、验证与可复现证据

只修改基准程序：轻用户可选内存 trace、可选 Peer 队列/同后端 FIFO、量子归因参数、可选固定到达率发送器。默认行为保留；逐包追踪默认关闭。`bench-tests.txt.gz` 的 Linux aarch64-musl 19 项测试全部通过，包括总队列 cap、正在服务的报文保护、Peer 内顺序、200 重包竞争、旧/新 FIFO 时序一致，以及服务器收到全部五请求后才回包的 open-loop 独立发送验证。Linux release 构建成功。没有新增生产网络探测，也没有修改 TCP 定时器。

发布版证据包装见 [PUBLICATION.md](PUBLICATION.md)：原始 JSON/JSONL 大文件以 gzip 保存，解压后字节 hash 保持一致。实测固定二进制仅本机保留，仓库发布其身份和源码快照。PR 从主分支 `f467635a032e0c5ace36536d348263d8f47ee1d0` 集成；下列性能数据仍属于原实测提交，不能改标成此 PR 的性能结果。

复跑需先构建 Linux 基准并保存新的二进制 hash，再准备 Linux TUN、iproute2、Python3；容器名为 `zfstack-issue69-tail`、仓库挂载 `/work`、vCPU 2–7。`bench/tail69_run.py` 每轮创建新目录，保存参数、起止时间、二进制 hash、原始结果与 stderr，拒绝覆盖，执行前后确认 hash 一致。`paired-plan.json`、`fixed*-arrival-plan.json` 保存顺序和门槛，不能按当前结果重新选门。

```sh
cargo zigbuild -p zfbench --release --target aarch64-unknown-linux-musl
python3 bench/tail69_run.py fresh-case --stack zfstack-bbr --seed 2 --binary target/aarch64-unknown-linux-musl/release/zfbench --env ZFBENCH_QUEUE_DISCIPLINE=peer
# 归因请另起新目录加 --trace；逐包事件不混入性能验收。
python3 bench/tail69_analyze.py docs/experiments/2026-10-20-issue69-tail/trace-memory-cubic-200
python3 bench/tail69_summary.py
python3 bench/tail69_report.py
```

冻结闭环源码见 `queue-source-tested.patch.gz`/`queue-source-hashes.json`，最终所有本次测试源码另见 `final-source.patch.gz`/`final-source-hashes.json`，各原始 `identity.json` 保存实跑二进制身份；二者不能混为相同 build。`environment.json`、`cleanup.json` 记录环境和测试资源释放，完整文件校验清单见 `artifact-index.json`。原有其他实验和工作目录改动保留。Issue 原评论中的“尚未提交/推送”描述记录时的状态；本 PR 发布压缩证据及测试源码，原始 README 另存 `original/README.md`。归档校验使用 `python3 bench/tail69_verify.py`，不把未入库二进制当成已发布文件。

当前工程结论：共享队列的准入/服务隔离是有实测支持的修复方向，缩小 TCP 调度量子和放大 FIFO 均不适合本场景。严格吞吐零下降、CPU/RSS 全面非回退及完整 ZFC/WG 集成仍未证明，Issue 69 保持 OPEN；异构 MSS 的有效字节公平问题也未因本次长尾归因而解决。
"""
    (OUT/"README.md").write_text(report)


if __name__=="__main__":main()
