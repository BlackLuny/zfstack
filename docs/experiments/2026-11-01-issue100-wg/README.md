**已在 Linux 真机上复现 #100 的旧实现停滞，并验证修复后双方恢复。** 本轮通过真实 `Shard::run`、TUN 系统调用和内核 WireGuard 完成功能验收；不是 Conn 层模拟，也不提供 WAN、物理网卡性能或 zfc 发布资格。

链路为两端独立进程/网络命名空间中的 `Shard A → TUN A → kernel WG A → veth → kernel WG B → TUN B → Shard B`，反向相同。Xeon E5-1630 v4、Debian Linux 6.1.0-54、Rust 1.94.1；TUN/WG MTU 1420，外层 MTU 1500。主动打开仅在独立验收工具中启用 `test-peer`。生产 `src/` 与修复提交 `da9ad29b02154bb3e23b7d306b51371f18bd9087` 完全一致。

两端先各发送一个满 MSS 首段，再通过屏障发送七个单字节尾段，确保首段发送时尚无 SACK 选项。内核 nft 规则丢弃双方首段并压制 ACK；两端 OOO 都达到 7 字节后，为每端 16 MiB 物理内存账本持有 SendRecord 等级阻塞租约，直至记录扩容确实失败、`record_waiters=1`。这验证配额耗尽，不制造宿主 OOM。两端均达到第一次真实 RTO 且仍有 OOO 后才解除网络丢包；预算阻塞租约继续保留，直到数据完整读取且累计 ACK 清空发送队列。随后释放阻塞租约，在同一连接上双向各传输 256 字节，验证继续使用连接。

| formal-002 固定顺序 | 时间戳 | 旧实现两端 | 修复版两端 |
| --- | --- | --- | --- |
| 正常链路对照 | 开 | — | 完整传输、连接复用，0 RTO |
| 第 1 组 | 开 | 7 秒内各 5 次 RTO，0 字节重传，持续停滞 | 各 2 次 RTO 后恢复并复用 |
| 第 2 组 | 关 | 7 秒内各 5 次 RTO，0 字节重传，持续停滞 | 各 2 次 RTO 后恢复并复用 |
| 第 3 组 | 开 | 7 秒内各 5 次 RTO，0 字节重传，持续停滞 | 各 2 次 RTO 后恢复并复用 |

三组正例的双方均在持有阻塞租约时完成原始传输；满 MSS 重传没有 SACK 块，发送队列、OOO 和 pipe 归零，连接保持 Established。TUN 写入错误及 EMSGSIZE 均为 0；全部发送计划和实际写入检查最大 IP 报文 1420 字节，抓包所见 WG 外层最大 1480 字节。WG 握手、双向加密数据、两端收发计数及内核实际丢包计数均有原始记录。nft 按首条命中：ACK 搭载在满数据段时由第一条规则丢弃，纯 ACK 规则计数允许为零。

抓包日志保留了合计 **199 个采集丢包**，不能当作完整逐包轨迹。完整 TUN 写入错误统计、全量发送事件与应用逐字节校验独立于抓包；核查器同时检查这些证据。原始业务数据全部正确，不能把抓包丢失解释为应用丢失或声称抓包零丢包。

测量冻结于 `ddc5435d7b9046430e19273224c903c7aac7f525`；旧版生产源码为 `a0fb2a3`（#99），修复版为 `da9ad29`。两臂使用相同验收工具、独立锁文件及相同构建命令。`protocol.json` 在构建/运行前冻结组数、顺序和 7 秒恢复观察期限。较早的 pilot 与 formal-001 中的 nft 语法、准备屏障和抓包问题全部保留；未替换失败记录或延长最终观察期限。第一版核对器错误要求两条 nft 规则均有非零计数，第二版按首条命中语义更正；两版核对器、旧失败日志及原始计数均保留，未修改运行数据。

[verification.json](verification.json) 是独立核查结果，[provenance.json](provenance.json) 包含构建输入哈希、主机和清理核对；[evidence.tar.xz](evidence.tar.xz) 保存全部四轮原始结果、pcap、日志、冻结输入及索引，SHA256 为 `242c69085698afd3699a0d21e00f24b81a26ae16504c33f8aec0e6dd5f111ca8`。本地在新目录解包后，243 个文件哈希全部匹配，核对报告逐字节一致。所有子进程已回收，命名空间和临时接口已清理，预算租约与物理域排他租约均释放。

复核归档时，在新目录解包，然后运行：

```sh
python3 analyze-v2.py formal-002 --output replay.json
cmp replay.json formal-002-verification-v2.json
```

重新执行真实链路需要 Linux root、TUN、WireGuard、nft、tcpdump 和 unshare，使用独立输出目录及实际物理域/boot generation：

```sh
python3 bench/pareto/issue100_wg_case.py \
  --fixed-root /absolute/frozen-fixed --base-root /absolute/frozen-base \
  --output /absolute/new-results --physical-domain ACTUAL_DOMAIN \
  --generation ACTUAL_BOOT_ID --lease-dir /var/lock/zfstack-qa --rounds 3
```

入口持有物理域租约覆盖构建和运行，通过 `qa/isolate-linux.sh` 创建私有 mount/network/PID 域与 `/run`，不修改宿主业务网络。独立工具位于 `bench/issue100-wg`，不加入可移植 workspace 默认构建，也不改变生产默认或运行时依赖。
