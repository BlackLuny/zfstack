此次正式 Queue 的业务请求未超时，但注入器无法证明安全排空，原 NOT_RUN / UNKNOWN 必须保留。2 Gbit 能力诊断的单次成功没有消除持续入队与非原子删除之间的竞争，本报告没有用此前 BD 或诊断结果代替正式结果。

审查对象为 `8bf5bb86106e5a4f1665f3c1b9c4570d2c80bf72` 的正式 baseline/fault 两 arm，worker SHA256 为 `36977c8ee4a02595af6ef0cb463654069a22e4e6c6d6d169b9722d0678ca37c1`。原始目录 `/tmp/zfstack-qa-linux-preflight/8bf5bb8-l2-local/8bf5bb8-l2`。独立脚本验证了全部 138 个索引文件的路径、大小和 SHA256，逐项比对 raw.json.gz 与补充原始样本，重算所有请求身份、计数及到达至完成延迟，并从冻结 8bf 提取 queue_control / queue_links 验证器复核。脚本、日志、完整 JSON 均在本目录，未修改源代码或原始案卷，也未连接或重跑远端。

故障 arm 的结果如下；延迟是 `(started_at − scheduled_at) × 1000 + latency_ms`，包括发生器排队时间。

| 端点 | baseline / loaded / recovery 已完成数 | loaded 超过 250 ms | recovery 最大到达至完成延迟 |
|---|---:|---:|---:|
| TCP client-1 | 800 / 18 / 782 | 17 | 2.321064 ms |
| TCP client-2 | 800 / 24 / 800 | 16 | 2.953593 ms |
| TCP client-3 | 800 / 30 / 800 | 16 | 4.888088 ms |
| UDP 0 | 800 / 1600 / 800 | 1561 | 5.456693 ms |
| UDP 1 | 800 / 1600 / 800 | 1561 | 4.594740 ms |
| UDP 2 | 800 / 1600 / 800 | 1561 | 5.966239 ms |
| UDP 3 | 800 / 1600 / 800 | 1561 | 6.068986 ms |

两 arm 的全部 TCP/UDP 样本均 completed，failed/timed_out/unfinished/missed 全零，序号连续且计数吻合。UDP 各 3200 个预定 arrival 保留，800/1600/800 的三个 phase 以及每路 3,302,400 verified bytes 一致。四路 heavy flow 均有正字节量，原始 errors 为空。baseline arm 的三 TCP loaded 最大到达延迟为 22.620 / 18.643 / 18.558 ms，均满足相同 250 ms 服务合同；fault loaded 的服务合同确实失败，fault recovery 的观测请求均满足合同。这里不推断未观测请求的尾部分布。

client-1 的 782 个 recovery 请求不是 18 个 missed arrival：其闭环最后 loaded 请求 seq817 在相对负载起点 23.821129 s 计划、23.821355 s 启动、24.184830 s 完成，服务耗时 363.474183 ms、到达至完成约 363.700353 ms。下一次 seq818 以该完成时间作为计划时间，24.184910 s 才启动 recovery；之后至 31.994897 s 共 782 个请求，全部完成。其归属仍为 loaded，未将跨边界请求删去或改归 recovery，也没有以 800 个开放环 arrival 要求替换冻结的闭环模式。

以下时间相对实际 drain change 完成 `Tc`；每行 `at_ns` 是两次独立 tc 命令完成后的用户态时间，并非内核采样时间，JSON 与文本也不是同一瞬间。

| 记录 | 相对 Tc | JSON backlog / qlen | 随后的文本 backlog |
|---|---:|---:|---:|
| 最后 draining | +646.397790 ms | 0 B / 0 | 46,322 B / 2 |
| cleanup | +656.620124 ms | 90,496 B / 2 | 45,248 B / 1 |
| restored noqueue | +668.803346 ms | 0 B / 0 | 0 B / 0 |

恢复发生在固定 recovery 边界前 73.393691 ms。所有原始 qdisc drops 为零；六个节点、全部接口的前后 link counters 身份稳定、无 reset，drops/errors 增量全部为零。原始时间线说明“曾观察到空队列”不等于“之后没有新入队”，甚至同一 draining row 的 JSON 与文本已不同；清理状态 PASS 只说明命令执行与资源清理完成。

**丢包因果仍不确定。** 90,496 B 是 cleanup JSON 采样时存在的两项队列工作，不是已经证实被删除的字节量。它们可能在后续文本采样或 delete 之前发出，也可能有更晚的新入队在 delete 时被释放。当前正式 case 冻结为 capture=false，没有删除时刻的内核 reset trace，也没有逐包发送/接收匹配；全部业务请求完成不能排除 TCP 通过重传掩盖包丢失。反过来，不能仅凭这个快照宣称发生了丢包。严格冻结验证器只得到 `queue control did not prove a drained queue before removal`，与 NOT_RUN 一致。

另一个次要诊断说明：result 中的“queue control lacks the actual loaded phase interval”来自 evidence.py 在已有 control_errors 时的分支。独立按真实 start_at、8/16/8、500 ms 和原 250 ms guard 重算，planned 的三个时间戳全部一致；这里不是原始 interval 缺失。此附带理由不改变上述真实未排空证明的阻断。

持续流量下，当前 stock tc 操作不能提供“若此刻为空则删除，否则不动”的原子恢复。Linux 5.4 的 RTM_DELQDISC 只检查目标身份，然后 qdisc_graft 对运行设备调用 dev_deactivate；dev_deactivate_queue 在锁内切换并 reset 旧 qdisc。GET 与 DEL 之间没有占用量条件，rtnetlink 配置锁也不阻止数据面入队，因此缩短命令间隔、tc batch、增加几次零采样都只能减小竞争窗口，不能消除它。[Linux sch_api.c](https://raw.githubusercontent.com/torvalds/linux/v5.4/net/sched/sch_api.c)、[Linux sch_generic.c](https://raw.githubusercontent.com/torvalds/linux/v5.4/net/sched/sch_generic.c)

netem_reset 会释放仍在时间队列中的 skb；这一 reset 路径不会用普通 netem 入队丢弃分支累计 qdisc drops，旧 qdisc 的计数也随删除不可继续读取。这说明本案零 drop counter 不能证明删除无损；源码机制说明仍不是本案确实丢包的证明。这里核对的是上游 v5.4 实现，环境声明为 Ubuntu 5.4.0-216-generic，未宣称已审计该发行版内核全部补丁。[Linux sch_netem.c](https://raw.githubusercontent.com/torvalds/linux/v5.4/net/sched/sch_netem.c)

不暂停发生器、不降载、不放宽时限的可行方向需要改变注入器的恢复机制，而不是再试 rate：例如从实验开始就使用可持久化的测试队列，在同一队列锁下于旧 backlog 完全排空时切换至透明模式，保留以后入队与旧队列的顺序，测量结束后才销毁。它必须明确证明透明模式无剩余延迟/速率限制，并重新冻结、独立验收；不能仍声称中途恢复为原 noqueue。现有 Linux 5.4 netem/tc 用户态接口没有这种 empty-and-switch 事务。保持 2 Gbit 到 recovery 会改变恢复条件；直接 change 为无速率又可能让新包越过旧时间队列；直接 replace 为另一 qdisc 同样会 reset 旧队列。这些都不能作为本次严格合同下已经成立的最小修复。若必须在持续流量下恢复原 noqueue，则需具备内核侧原子排空/切换能力的专门测试注入器，并先验证该能力；当前证据不足以声称已有安全实现。

本次没有增加测试或实验；原始负载、guard、请求超时和 SLO 均保持冻结值。
