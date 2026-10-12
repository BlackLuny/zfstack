# RX 物理信用修复：功能与性能对比

本次默认缓冲、3 秒轻请求超时下的 12 组真实 ZFC/WireGuard ABBA 全部完成。候选没有轻请求超时、载荷错误或同端口轻请求重传。8 MiB 无附加延迟时，基线的 TCP 重传退避被消除；代价是锁住每连接 RX 信用，并降低该压力配置的重流吞吐。性能数字是整条单机集成路径，不能当作 core 微基准。

## 修复与边界

根因及可证伪实验见 [前一轮归因报告](../2026-10-13-cause/REPORT.md)。重 peer 占满 4 MiB port backing，轻连接的 320 B 小请求 backing 被 Bulk 拒绝；旧 ACK 的窗口仍开着，客户端等待 TCP 重传。只调整逻辑进展标志不能保住物理 payload/descriptor。

修复合同见 [0010](../../../../docs/design/0010-rx-progress-credit.md)：SYN、cookie、TIME_WAIT 替换准入时预付独立的 RX 信用，常见 MTU 为 8 KiB，始终计入 global/port/peer 硬限。普通 RX 仍用 Bulk；仅顺序且 core + adapter 未读量低于两个观察 MSS 时，可在普通 backing 失败后借自己的信用。TX、OOO 不能借用；逻辑读完或连接关闭不能提前返还外部 Bytes owner 占用的信用。不会把新 RX 升到 Drain，也不修改产品缓冲默认值。

既有测试抓到两个回归并在同一修复中解决：小 peer 的 headroom 需钳制到份额的一半，不能因四份准入 burst 放不下就关闭排空保障；最低 64 KiB 发送份额在部分 ACK 后还需受实际 backing 约束，防止逻辑释放一字节却再钉住第二个整块。原停读/健康 writer 回归的断言保留。精确 TX 缓存边界的夹具直接构造其他 peer 缓存，避免把第四条 live connection 的新 RX 信用混入该边界。

## 口径与出处

指定 Linux worker 218，同一宿主、同一单线程数据面接线；两个真实 WG 端口、四个 peer。peer 0 开 16 条下载，peer 1 同端口每次 256 B 请求，peer 2/3 跨端口请求。每组 8 秒空载、16 秒加载、8 秒恢复；delayed 组加载 64 秒，在所有客户端 wg0 出方向增加 50 ms netem，影响请求及下载 ACK，未给反向加延迟。每种配置按 baseline → candidate → candidate → baseline 运行，每种二进制两次。

使用产品默认 core RX/TX 和 adapter 缓冲。8 MiB 对应 4 MiB port，启动指纹 `1ddaea576182d217`；单靠小预算不能证明撞限，各案卷另保留分层拒绝与 share_blocked 诊断。轻请求仍为 3 秒超时，未上调到上一轮诊断的 30 秒。

两个二进制使用相同 ZFC/private_tun/shadow-tls 源码、Cargo.lock、rustc 1.98.1、Zig 0.16、x86_64-unknown-linux-musl、fast-release（opt-level=1、codegen-units=16、无 LTO、strip=false）、mmsg + jemalloc。ZFC `2cd672c97b18fcd3d9ab01f53190359fa9653014`，private_tun `91bdeab6d2988814dc9c7a21c327f6f609906a78`；唯一 core 行为差异是本次补丁。不是发行 profile，也没有把 Python 端点成本从吞吐中扣掉。

- baseline worker SHA256：`fb32f8b37862d364d7e6abd4cf4bb4d39e3776e5f2f8ddb3ac89d1aebdbd07eb`。
- candidate worker SHA256：`12639458cb78ca80c19e9786431ca66bfbcbf678e29a927deec0b2f3cd82cf33`。
- 构建候选 Cargo.toml + src/*.rs 树 SHA256：`d1dba567d9843d4173ba6c1d22fe0220edf04ea229735ea6c7b1f56196ed9f43`。
- 原 core 树 SHA256：`bedf3b672b40ebe40db72bbac22943065e11efb7339ad9deb978083b22ba0742`，与 PR 原头 a951135 的 core 相同；隔离 checkout 的 commit 是 `ee0e7284c24f0162e10238f8805d7cf96901d8b2`。
- [metadata.json](metadata.json)、[build-info.json](build-info.json)、[applied-core.patch](applied-core.patch)、[build.sh](build.sh) 和 [compare.sh](compare.sh) 保存具体出处、精确候选源码及构建/运行命令。worker 自带 build-info 不包含 zfstack path dependency 指纹，因此另记 core 树与补丁，不能只看 ZFC commit。性能案卷构建后，最终自查人工排版并将跨快照欠账减法改为饱和减法，补充欠账钳制断言；12 组没有欠账或准入拒绝，该差异不在本次负载的数据路径内。最终源码另做完整测试与独立 L2 验证，不混入这张性能均值表。

## 性能均值

每格的均值由两次独立运行算出，不混合 L0 虚拟时间。CPU 核数 = 加载期 worker CPU 秒 / 实际采样窗口；CPU 秒/GiB = 核数 × 8 GiB / 有效 Mbps。RSS 是整个 worker 的加载期采样峰值，不是 stack 预算或实时驻留的信用 payload。分位数列是两轮 P99 的范围。

| 配置 | 重流 Mbps：基线 → 候选 | 吞吐变化 | CPU 秒/GiB：基线 → 候选 | CPU/GiB 变化 | RSS MiB：基线 → 候选 | 同端口 P99 ms：基线 → 候选 |
|---|---:|---:|---:|---:|---:|---:|
| 8 MiB，16 s 加载 | 1477.5 → 1414.3 | -4.28% | 11.19 → 11.68 | +4.37% | 96.6 → 97.2 | 878.0–879.0 → 10.3–11.2 |
| 64 MiB，16 s 加载 | 1981.4 → 1971.7 | -0.49% | 7.58 → 7.71 | +1.79% | 167.9 → 162.7 | 28.5–29.0 → 27.0–28.3 |
| 8 MiB，上行 +50 ms，64 s 加载 | 303.7 → 303.9 | +0.06% | 24.19 → 24.03 | -0.63% | 88.3 → 90.0 | 91.1–91.2 → 91.0–91.0 |

| 用例 | Mbps | CPU 核数 | CPU 秒/GiB | RSS MiB | 加载请求数 | P99 ms | 最慢 ms | 轻请求重传数 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| [small-base-a](small-base-a/result.json) | 1487.5 | 1.934 | 11.17 | 97.2 | 120 | 877.99 | 1721.15 | 42 |
| [small-candidate-a](small-candidate-a/result.json) | 1405.5 | 1.919 | 11.73 | 95.8 | 1594 | 10.29 | 24.05 | 0 |
| [small-candidate-b](small-candidate-b/result.json) | 1423.1 | 1.926 | 11.63 | 98.6 | 1594 | 11.25 | 22.27 | 0 |
| [small-base-b](small-base-b/result.json) | 1467.5 | 1.915 | 11.21 | 96.0 | 121 | 879.00 | 1513.64 | 41 |
| [normal-base-a](normal-base-a/result.json) | 1983.2 | 1.752 | 7.59 | 165.2 | 695 | 28.55 | 34.70 | 0 |
| [normal-candidate-a](normal-candidate-a/result.json) | 1970.3 | 1.772 | 7.72 | 159.9 | 789 | 28.35 | 32.56 | 0 |
| [normal-candidate-b](normal-candidate-b/result.json) | 1973.2 | 1.770 | 7.70 | 165.5 | 794 | 26.99 | 33.77 | 0 |
| [normal-base-b](normal-base-b/result.json) | 1979.7 | 1.744 | 7.57 | 170.6 | 688 | 28.99 | 56.28 | 0 |
| [delayed-base-a](delayed-base-a/result.json) | 303.5 | 0.848 | 23.99 | 90.2 | 1182 | 91.21 | 326.59 | 1 |
| [delayed-candidate-a](delayed-candidate-a/result.json) | 303.8 | 0.844 | 23.86 | 90.2 | 1183 | 91.04 | 202.08 | 0 |
| [delayed-candidate-b](delayed-candidate-b/result.json) | 304.0 | 0.857 | 24.21 | 89.8 | 1182 | 91.01 | 94.85 | 0 |
| [delayed-base-b](delayed-base-b/result.json) | 303.9 | 0.862 | 24.38 | 86.3 | 1182 | 91.12 | 309.85 | 1 |

8 MiB 无附加延迟的两轮，基线同端口加载阶段只有 120/121 个请求，最慢 1721/1514 ms，有 42/41 个请求发生重传；候选每轮 1594 个请求、最慢 24.1/22.3 ms、重传 0。这个批次基线没有碰到 3 秒超时，仍复现了拒收和重传；前轮 3–7 秒长尾证据保留，不能把本批次较短的尾部写成原问题消失。

正常预算的吞吐差小于 1%，CPU/GiB 差约 1.8%；小预算吞吐差约 4.3%，CPU/GiB 差约 4.4%，其原始 CPU 核数基本相同。每连接锁住 8 KiB 额度会减少可用发送预算，最低一块发送约束也限制突发；当前组合实验没有将两项成本各自拆分。每配置只有两轮，不能据此做显著性判断或承诺长期性能无回归。

## 用例防护与回归

- core 新增 9 项定向测试：三层 Bulk 撞限后 32 次信用复用、最后外部切片/跨线程释放与连接退出、descriptor 分配拒绝完整回滚和读序、jumbo/OOO 不越信用、部分 ACK 仍约束 TX backing、小 peer headroom 钳制、adapter 未读量不绕过资格、准入三层拒绝与完整欠账、16 次模拟器真实 ACK/重传负对照。关闭信用的同一用例必须失败，释放压力后必须由重传恢复。
- `cargo test -p zfstack --release --features tokio -- --nocapture`：193 library 测试通过、3 ignored，1 integration 通过；不是把 ignored 记为通过。
- `cargo test -p zfstack --release --features tokio,test-peer --test rx_capacity_lifecycle -- --nocapture`：2 通过；取消路径最终 reserved/cached 均为 0。
- `cargo test -p zfharness --release`：13 通过；完整 L0 matrix `--full`：20/20，包括停读、Full、重建、丢包/乱序、64/128 端口。结果见 [l0-matrix.md](l0-matrix.md)。
- 无默认 feature check 与 workspace all-targets clippy 成功；clippy 保留 69 warnings，成功仅表示现有 CI lint 口径通过，没有声明零警告。全部 Cargo 检查通过 remote-compile，task ID 见 [validation.json](validation.json)。
- Python 比较器 6 项测试加入 CI：成功样本不能掩盖超时，3 秒截止时间、恢复进展、抓包丢包、重流校验、负载/二进制一致性、轻请求重传和显式性能门槛均有反例。shell/Python 语法、diff 空白检查通过。
- GitNexus 未索引 zfstack，impact/detect_changes 返回仓库不存在，未宣称图分析通过；源码核查了接收/descriptor、三条被动准入、adapter 与发送份额调用者，并以定向及完整测试补证。

## 代价和未覆盖面

本场景同一 port 有 17 条连接，8 KiB × 17 = 136 KiB，占 4 MiB 的 3.32%；这是预付预算，payload 仍按需分配。不能把 3.32% 用于大量 idle connections：每条空闲连接也锁住信用，实际准入上限会下降；idle 最大容量未另做二进制对比。jumbo MTU 信用更大。OOM、更多碎片、超过八槽的 descriptor 增长，以及大量慢读/长 RTT 并不在无丢包承诺内。

本次仅 2 真 WG 端口和 16 重连接，延迟用例仅增加上行 50 ms、加载 64 秒，没有验证百端口、WAN 丢包/漫游、长时间容量或真实发行构建。基线和候选在同一宿主顺序运行，两轮无法估计可靠置信区间。产品缓冲默认值没有改。

## 案卷、清理与重算

每组保留 result.json、完整 raw.json.gz、analysis.json、诊断、两处轻流 pcap 和 build-info。所有流载荷、WG 路由及路径计数通过；客户端到 target 的物理捷径为 0。两个 tcpdump 每组均为 0 kernel drop，候选同端口 request retransmission 为 0。诊断中的普通 fail_rx_chunk 可以增加后成功使用信用，不能等同于最终输入丢包。

12 组 exit-code=0，自己的 namespaces-after 为空；批次退出为 0，原 zf 与 watcher active/active，core 源码恢复且工作树干净，Cargo.lock 构建前后 hash 相同。同一租约继续用于最终构建的独立验证，完成后释放。完整密钥和 worker/controller 配置只在节点案卷，未导出。共享 rig 缓存由 watcher 管理，没有把其 hash 不变当作本实验清理条件。

```sh
python3 harness/l2/results/2026-10-13-cause/analyze.py harness/l2/results/2026-10-13-rx-credit
python3 harness/l2/compare.py harness/l2/results/2026-10-13-rx-credit
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s harness/l2 -p "test_*.py"
```

[comparison.json](comparison.json) 保存均值、变化率及完整每轮指标。比较器默认只检查候选正确性和证据，不隐含吞吐/CPU 容忍门槛；可用 --max-throughput-drop / --max-cpu-rise 指定本次门槛。
