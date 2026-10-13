# 已建立连接的 RX 物理信用

## 问题与范围

PR #25 的真实 WG L2 实验确认：8 MiB global / 4 MiB port 下，重 peer 的发送缓冲使同端口轻连接的 320 B RX backing 被 Bulk 拒绝。旧 ACK 保持开放窗口，客户端重传退避将等待放大到 3–7 秒。证据见 `harness/l2/results/2026-10-13-cause/REPORT.md`。

0005 保证排空链条，0007 只约束发送份额；两者都不保证每条已建立连接可以接收小请求。本变更增加独立、预付的连接级 RX 信用，不将新 RX 提到 Drain，不放宽 global/port/peer 硬上限，不改变产品缓冲默认值。

## 合同

- 普通 SYN、TIME_WAIT 替换及 cookie 准入必须准备 core 状态和 host stream 状态；状态失败全部回滚，准入欠账只包含状态。随后尝试预付可选 RX 信用，信用失败仍然准入并使用普通 Bulk RX。测试 active-open 路径采用同一信用账本。这是 issue #37 对原先「每连接强制预付」的修订。
- 信用按接口准入 MTU 计算，至少 8 KiB，覆盖两个 MTU 取整后的 payload owner、各自元数据、至多 8 槽的 RX descriptor replacement 峰值和信用 owner 元数据。只有信用 owner 立即分配；payload 按需分配。整个承诺从准入起计入三层物理账本，TX/OOO 无法借用。
- 正常 RX 继续使用 Bulk。只有顺序数据且 core + adapter 未读量小于两个已观察 MSS 时，在正常 backing 准备失败后可借自己的信用。借用必须同时准备完整 payload 和 descriptor；失败不移动 ACK，不保留半份输入。超过信用或系统 allocator 失败仍拒收，不承诺所有 RX 无丢包。
- 信用在 payload 最后一个 `Bytes` owner 释放后才可复用。部分读、adapter 转移、连接关闭、跨线程持有切片都不能提前归还。连接关闭后外部切片继续持有整份根租约，直到最后 owner 释放。
- 信用总量分别限制为 global / port / peer 硬额度的 1/8；全局计数跨 shard 共享，三层申请失败均回滚。信用总量与物理租约有相同生命周期。先准入的连接可以获得保障；超出信用上限的连接回退到 #21 的 Bulk 行为，不保证受压时首次小请求免重传。1/8 将空闲信用的固定成本限制在 12.5%，在 8 MiB 端口保留 7 MiB 给连接状态及其他分配；不是承诺达到完全取消信用后的理论最大连接数。
- OOO 不使用信用，原有未来 RX 槽与填洞承诺保持；备用描述符扩容限制在 8 槽，已有较大描述符无需重新申请。普通 RX 和信用 RX 按原到达顺序读取。
- 准入预留随接口和 host state 计算，保留 P0/P1/P2 排空空间。已准入连接的信用不因 MTU 变化扩大；MTU 增大后的大包可以走普通 Bulk，信用保障仍限于原承诺大小。
- 小份额的一半若放得下 P0/P1/P2 headroom 与单条状态准入，即使完整 burst headroom 放不下两份，也不关闭推进分级：Bulk headroom 钳制为份额的一半。连这个最低需求都放不下时仍报告推进预留关闭。可选信用不再计入该最低需求。第一版未钳制导致 204800 B peer 的 adapter 队列占满额度，既有多 peer 回归抓到了该死锁；此边界新增独立测试。
- 常规准入 headroom 保留原来的四份状态，再加一份 RX 信用；单条完整准入另记最低需求。预付信用会降低受压时的连续准入 burst，不为四份闲置信用扩大静态 headroom。第一版额外保留四份信用，在 409600 B peer 上使健康 writer 被停读连接阻塞，原断言保留并复测；欠账上限同步取各层实际准入 headroom。
- 最低 64 KiB 发送份额同时约束逻辑长度和物理 backing。单个完整 TX 块按环形缓冲复用 ACK 已释放的前缀，部分 ACK 后可以立即续写，不申请第二块，不再依赖 #39 的 75% 压力阈值。逻辑份额、三层真实分配硬限、准入欠账和推进预留保持生效。
- 2 KiB 首块仍可以与一块 64 KiB backing 共存，避免小写入退化成每 RTT 2 KiB；小首块释放后，剩余完整块进入环形复用。此前较大份额留下的多块 backing 等 ACK 排空到单块，再进入相同路径。
- 普通写入和 relay readv 都直接写入块尾及已 ACK 的前缀。重传、MSS 变化和分段仍只提供至多两个 slice，不复制活跃数据；仅当环形块需要扩展成多块缓冲时重排该块一次。排空/关闭照常释放到有界缓存；新分配失败照常触发现有缓存回收和等待机制。

## 代价

常见 MTU 下每条受保护连接锁住 8 KiB 预算信用，不等于立即驻留 8 KiB payload；jumbo MTU 的信用更大。信用总量有 1/8 上限，耗尽后连接继续准入；已获得信用的连接保留原有保障。共享信用池和无信用连接的后续升级不在本次修复范围。信用仍是非 drain 的准入资源，不能承诺高并发、长 RTT、慢读情况下无吞吐损失。

`Budget::rx_progress_bytes()` 和 Tokio `StackSnapshot::rx_progress_bytes` 导出 global / port / 本 port 所有 peer 合计的信用字节，`MemoryHandle::peer_rx_progress_bytes()` 可取单 peer 数字。`failures_by_kind.rx_progress` 表示跳过可选信用的次数（上限或物理额度失败），不表示拒绝 SYN；信用导致的准入失败已消除。`ConnStats::rx_progress_packets` 保留实际 fallback 次数。

健康 RX 不新增拷贝或信用原子操作；fallback 增加少量额度记账，沿用一次 payload 拷贝。真实 CPU、RSS、goodput 与尾延迟用相同二进制构建参数和 AB/BA 用例比较。

## 验证

- 三层信用上限、跨 shard 并发、可选信用失败仍准入、状态失败回滚与欠账；payload/descriptor 分配失败、hard cap 与非 drain 属性。
- 重 peer 耗尽 Bulk 后轻连接的首次小请求立即接收，ACK 前进，反复读写可复用信用；关闭 fallback 的负对照必须失败。
- 慢读与外部切片耗尽自己的信用，不能用其他连接信用；切片跨线程释放、连接退出及最终账本归零。
- 普通 SYN / cookie / TIME_WAIT 替换与准入压力回归，既有 TX、OOO、adapter、jumbo 测试。
- L2 保持默认缓冲：8 / 64 MiB、16 重连接，基线/候选 ABBA，轻请求默认 3 秒；记录 retransmission、goodput、worker CPU/RSS、路径证据及清理。补充较长 RTT 与压力持续窗口。
