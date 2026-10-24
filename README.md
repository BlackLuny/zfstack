# zfstack

面向代理/转发场景的用户态 TCP 栈：输入裸 IP 包（如 WireGuard 解密后），向上层提供类 `TcpStream` 的流接口。
目标是替代 zf-worker WG 入站（`zfw-wireguard`）中的 smoltcp；也可作为代理客户端的 TUN 入站栈（`StackConfig::client()`）。

- 总体设计：[`docs/design/0001-architecture.md`](docs/design/0001-architecture.md)
- S0 基准与证伪：[`docs/design/0002-s0-benchmark-and-falsification.md`](docs/design/0002-s0-benchmark-and-falsification.md)
- 代理客户端场景（TUN 卸载、零拷贝收发、驱动内 splice，与 sing-box 1.15 对比）：[`docs/design/0008-client-profile.md`](docs/design/0008-client-profile.md)
- 多租户混合负载 harness（sans-IO，不经 WireGuard）：[`harness/README.md`](harness/README.md)
- 多用户竞争时的接口聚合 pacing（core / Tokio / splice）：[配置与边界](docs/design/0011-contention-egress-pacing.md) · [生产路径实验结果](docs/experiments/2026-10-24-production-pacing/README.md)
- 接口热状态与竞争发送循环的后续优化：[V5 实现和独立复测](docs/experiments/2026-10-24-pacing-fast-path/README.md)
- CUBIC/50 ms 单连接 CPU 异常：[128 次独立复测与环境对照](docs/experiments/2026-10-24-cubic50-retest/README.md)
- 持续验证、证据和门禁：[QA 使用指南](qa/README.md)
- 状态：已有核心栈、Tokio adapter、L0 / L1 / L2 台架；性能资格按场景和节点独立校准，未校准结果保持 shadow。

## 独立 TX 容量上限

通过 `StackConfig::max_snd_buf` 设置每连接核心 TX 队列上限，单位为字节：

```rust
let mut cfg = zfstack::StackConfig::client();
cfg.max_snd_buf = 256 * 1024; // 示例：按实际链路测试后选择容量
```

`default()` 和 `client()` 均默认为 `u64::MAX`，不增加额外限制，保持原有行为。
实际容量仍取窗口/预取上限、该配置与预算公平份额的最小值；本项不会扩大已有上限，
也不修改 `max_snd_inflight`、预取参数或全局/端口/peer 物理预算。
它统计已发送未确认和待发送数据，不包含 Tokio adapter 队列；物理块按分配粒度计费，
缓存也可保留，因此它不是物理内存硬上限。

可设置任意字节数，无需按块对齐。小于 `write_low_watermark` 时，Writable 阈值相应降低，
避免小缓冲写满后无法恢复；设为零会阻止向核心 TX 队列写入且不发 Writable，
接收和控制包不受此限制。Tokio adapter 仍可先接收数据至其自身队列上限。
较小容量可能限制高 BDP 链路吞吐，不自动调整生产默认。
