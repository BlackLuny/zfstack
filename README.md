# zfstack

面向代理/转发场景的用户态 TCP 栈：输入裸 IP 包（如 WireGuard 解密后），向上层提供类 `TcpStream` 的流接口。
目标是替代 zfc 中的 smoltcp（WG 入站 `zfw-wireguard` 与 `zf-client-core`）。

- 设计稿：[`docs/design/0001-architecture.md`](docs/design/0001-architecture.md)
- 状态：设计阶段（S0 未开始）
