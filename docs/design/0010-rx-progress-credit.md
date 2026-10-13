# 已建立连接的 RX 物理信用

## 问题与范围

PR #25 的真实 WG L2 实验确认：8 MiB global / 4 MiB port 下，重 peer 的发送缓冲使同端口轻连接的 320 B RX backing 被 Bulk 拒绝。旧 ACK 保持开放窗口，客户端重传退避将等待放大到 3–7 秒。证据见 `harness/l2/results/2026-10-13-cause/REPORT.md`。

0005 保证排空链条，0007 只约束发送份额；两者都不保证每条已建立连接可以接收小请求。本变更增加独立、预付的连接级 RX 信用，不将新 RX 提到 Drain，不放宽 global/port/peer 硬上限，不改变产品缓冲默认值。

## 合同

- 普通 SYN、TIME_WAIT 替换及 cookie 准入同时准备 core 状态、host stream 状态和 RX 信用。任何一步失败全部回滚；准入欠账包含信用。测试 active-open 路径采用同一信用账本。
- 信用按接口准入 MTU 计算，至少 8 KiB，覆盖两个 MTU 取整后的 payload owner、各自元数据、至多 8 槽的 RX descriptor replacement 峰值和信用 owner 元数据。只有信用 owner 立即分配；payload 按需分配。整个承诺从准入起计入三层物理账本，TX/OOO 无法借用。
- 正常 RX 继续使用 Bulk。只有顺序数据且 core + adapter 未读量小于两个已观察 MSS 时，在正常 backing 准备失败后可借自己的信用。借用必须同时准备完整 payload 和 descriptor；失败不移动 ACK，不保留半份输入。超过信用或系统 allocator 失败仍拒收，不承诺所有 RX 无丢包。
- 信用在 payload 最后一个 `Bytes` owner 释放后才可复用。部分读、adapter 转移、连接关闭、跨线程持有切片都不能提前归还。连接关闭后外部切片继续持有整份根租约，直到最后 owner 释放。
- OOO 不使用信用，原有未来 RX 槽与填洞承诺保持；备用描述符扩容限制在 8 槽，已有较大描述符无需重新申请。普通 RX 和信用 RX 按原到达顺序读取。
- 准入预留随接口和 host state 计算，保留 P0/P1/P2 排空空间。已准入连接的信用不因 MTU 变化扩大；MTU 增大后的大包可以走普通 Bulk，信用保障仍限于原承诺大小。
- 小份额的一半若放得下 P0/P1/P2 headroom 与单条完整准入，即使完整 burst headroom 放不下两份，也不关闭推进分级：Bulk headroom 钳制为份额的一半。连这个最低需求都放不下时仍报告推进预留关闭。第一版未钳制导致 204800 B peer 的 adapter 队列占满额度，既有多 peer 回归抓到了该死锁；此边界新增独立测试。
- 常规准入 headroom 保留原来的四份状态，再加一份 RX 信用；单条完整准入另记最低需求。预付信用会降低受压时的连续准入 burst，不为四份闲置信用扩大静态 headroom。第一版额外保留四份信用，在 409600 B peer 上使健康 writer 被停读连接阻塞，原断言保留并复测；欠账上限同步取各层实际准入 headroom。
- 同一停读回归揭示最低发送份额只数未 ACK 的逻辑长度，部分 ACK 后一块的份额可同时钉住两个 64 KiB backing。最低一块份额下，core 新写入同时受已分配块容量及尾部余量约束，旧块真实释放后才允许下一块；较大的发送份额沿用既有行为。这限制极小份额的发送突发，防止新信用使既有健康 writer 被停读者饿死，需纳入性能对比。

## 代价

常见 MTU 下每连接锁住 8 KiB 预算信用，不等于立即驻留 8 KiB payload。17 条同端口连接约占 136 KiB / 4 MiB。空闲连接降低小预算的准入容量；jumbo MTU 的信用更大。信用仍是非 drain 的准入资源，不能承诺高并发、长 RTT、慢读情况下无吞吐损失。

健康 RX 不新增拷贝或信用原子操作；fallback 增加少量额度记账，沿用一次 payload 拷贝。真实 CPU、RSS、goodput 与尾延迟用相同二进制构建参数和 AB/BA 用例比较。

## 验证

- 三层拒绝、payload/descriptor 分配失败、全部回滚、hard cap 与非 drain 属性。
- 重 peer 耗尽 Bulk 后轻连接的首次小请求立即接收，ACK 前进，反复读写可复用信用；关闭 fallback 的负对照必须失败。
- 慢读与外部切片耗尽自己的信用，不能用其他连接信用；切片跨线程释放、连接退出及最终账本归零。
- 普通 SYN / cookie / TIME_WAIT 替换与准入压力回归，既有 TX、OOO、adapter、jumbo 测试。
- L2 保持默认缓冲：8 / 64 MiB、16 重连接，基线/候选 ABBA，轻请求默认 3 秒；记录 retransmission、goodput、worker CPU/RSS、路径证据及清理。补充较长 RTT 与压力持续窗口。
