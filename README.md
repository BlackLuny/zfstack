# zfstack

面向代理/转发场景的用户态 TCP 栈：输入裸 IP 包（如 WireGuard 解密后），向上层提供类 `TcpStream` 的流接口。
目标是替代 zf-worker WG 入站（`zfw-wireguard`）中的 smoltcp；也可作为代理客户端的 TUN 入站栈（`StackConfig::client()`）。

- 总体设计：[`docs/design/0001-architecture.md`](docs/design/0001-architecture.md)
- S0 基准与证伪：[`docs/design/0002-s0-benchmark-and-falsification.md`](docs/design/0002-s0-benchmark-and-falsification.md)
- 代理客户端场景（TUN 卸载、零拷贝收发、驱动内 splice，与 sing-box 1.15 对比）：[`docs/design/0008-client-profile.md`](docs/design/0008-client-profile.md)
- 状态：设计 v0.3（已过一轮外部评审），S0 未开始
