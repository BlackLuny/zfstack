# zfc 接入前置：生产适配器与资源所有权

状态：接入设计定稿 v3，实现进行中。共享连接许可、轻量 TIME_WAIT、RFC 6191 复用、主要 payload allocation 和发送记录增长的物理预留已接入；其余动态容器的容量对账、持续短连接及并发大流验收仍待完成。2026-09-25。保留完整单流 allowance；在线限速/窗口更新撤出本期。

基线：库源码 `be334ee8e0c46e21b7d3303f64a250dd92ddef7d`；远端 `b67043a` 只新增测试报告。本文是 [0001](0001-architecture.md) §4.4–6、S4 在 zfc 接入阶段的修订来源；冲突处以本文为准。zfc 文档维护 env、部署和业务契约，本文维护库内部实现与测试，不复制环境变量默认值。

## 1. 范围与入口

现有 sans-IO Shard 保留。扩展 Tokio Driver 构造 API，使调用方可注入进程资源管理器、端口/peer 子额度、连接许可、有界 accept、StreamConfig 和取消信号，并拿到可 join 的 task/future。库不依赖 zfc、boringtun 或其 CancellationToken。

生产入口不得隐式为每个 shard 调用 `GlobalBudget::from_system()`。一个进程可有多个 shard，每端口一个 shard 时用显式 PortInstanceId 隔离资源身份；PeerId 只在所属端口实例内有效。测试/bench 现有简便 spawn 可作为 wrapper 保留。

Driver 可在一个任务里处理外部有界 ingress 与批处理，避免库外 bridge 再投一层 channel；同步 ingress 回调允许宿主先分流 UDP。公开 API 名称在实现时依库惯例确定，下面描述的是接口合同。

## 2. RX：借用入包，保存紧凑 payload

zfc 的 WG 包池全进程共享，只有 512 个初始 2KiB buffer。把这些 buffer 的所有权长期留在 TCP RX/OOO 会耗尽池并拖累其他端口及 UDP。因此 zfc 生产适配默认使用同步借用入包入口，例如 `ingress_borrowed(now, iface, peer, &[u8])`：

1. 在调用期间解析、校验 checksum、序号和窗口；不保存对输入切片的借用。
2. ACK/重复/拒绝包不复制整包；只对需要保留的新 payload 申请受管 chunk 空间并复制。
3. 有序 RX 追加到 chunk，乱序 RX 使用同一受管存储方式，按序号保存范围；不能只改有序队列而把整包藏在 OOO 中。
4. 调用返回后宿主可以立即归还 PooledBuf。read_chunk 返回栈拥有的 Bytes/owner，无法继续占住 WG 包池。

当前借用入口按 payload 大小选择 64–8192B 的紧凑 chunk，MSS 约 1400B 时用 2KiB backing；最终分档仍按测量调整。容量与小段合并必须有界。小切片钉住大块时按 backing capacity 收费，通过受压 collapse 回收；不得改成每包 `to_vec`，也不能无上限维护每 peer 的半空 slab。ACK-only 不触发 payload 分配。

原有 owned Bytes ingress 可供模拟器和其他调用方保留，但与借用入口共用解析后的 TCP 状态机，避免两套校验逻辑。两入口共享同一物理内存契约；owned 模式不能仅用 Bytes.len() 推断外部 backing capacity，需要可核验的 owner/lease 接口，否则保守复制进受管块。

这不是上行零拷贝承诺。上行有一次入库 payload 复制和一次 AsyncRead 到应用缓冲的复制；zfc 解密后原有搬运另计。下行生产适配器仍有 app→queue→TX block→连续 IP 组包，先删 queue→scratch 的额外中转，不声称 sans-IO bench 的两次拷贝等于完整 Tokio 路径。

## 3. 逻辑字节与物理账本

保留 TCP payload 数字用于序号、窗口与发送空间；另用 allocation ledger 表示受管物理驻留。当前 TX 64KiB 块、池缓存、RX/OOO chunk、发送记录、适配器 RX/TX 队列 payload 与描述符容量、活跃连接固定状态和可跨 core 连接生命周期的适配器句柄已在分配前取得 RAII 容量许可；空闲池同时受跨 shard 的全局缓存上限约束。队列描述符扩容先预留新 backing，保留旧 backing 的许可直到搬迁结束；额度不足时 RX 不先从 core 出队，TX 不接受未保存的数据。下面的容器与端到端对账仍是交付要求，不能把已覆盖的 payload backing 当作全部驻留：

生产适配器提供 shard 配额生效后创建 egress sink 的入口，供 zfc 获取每个已配置 peer 的 `MemoryHandle`。zfc 的有界 WG 回程队列按包实际 `Vec::capacity()` 持有物理 lease，TCP 在额度不足时注册释放唤醒并返回 `Full`，UDP 回包可丢；两者沿用同一 global→port→peer 配额。队列槽位和其他动态容器容量仍需单独对账。

- 分配前申请物理容量，失败不分配、不确认未保存的接收数据。
- 每个 allocation 由一个 RAII lease 计费，slice/clone 共享，不重复收费；最后一份引用释放才归还。跨 core/adapter 的移交不会提前释放物理额度。
- TX 按实际 64KiB 块容量计费；只有 1 字节的首块也不能只记 1 字节。BytesMut 扩容、chunk 尾部、OOO/记录容器、空块和池内缓存同样计入。
- core、adapter RX/TX、accept pending、TCB/slot、计时器与发送记录、ingress/egress 都有容量限制；可用保守有界估算覆盖容器容量，但须与实测 capacity 对账。
- allocator 元数据/RSS 与内核 socket 不属于精确 allocation ledger，独立测量；不宣称全进程 RSS 被该数字硬限制。

为减少原子竞争可分批取额度，但批额度在 global、port、peer 层都必须有归属。一个 peer 的本地余量不能变成另一个 peer 未计费的用量。跨层预留全部成功才提交，部分失败回滚；回收时防双放和代际 ID 复用。

当前 global→port→peer 的物理额度申请与 active/TIME_WAIT 准入使用短事务锁；子额度失败后撤销的临时 global 预留不会被另一 shard 当成已提交占用而进入无通知等待。受压重试的容量提示也在同一锁内读取。单流原始 `zfbench` 档位未见吞吐/CPU 回退，但多核多端口争用成本仍须 P4 验证，不能把单流结果外推。

Tokio Driver 的 `streams` 索引在短连接突发退潮后按稀疏阈值重建，完全空闲时释放桶容量；已结束流的 orphan 定时节点立即移除，不再占到超时。它们降低高水位驻留，但活跃期的索引桶、控制集合和其他动态容器仍需在 P4 测实际 capacity/RSS 并补足账本；不能因回收路径存在就视为全部已计费。

宿主仅在启用诊断 env 时定时请求快照；当前快照只有当适配器已接收流不超过 4 条时，才读取这些流的 core TCP 状态和适配器 TX/RX 待处理量，不保存报文，也不在每包路径遍历连接。它用于区分尾部慢流的拥塞/重传、核心待发送、适配器积压与应用供数空档，不能替代 per-flow 逐包 trace 或 24 小时 RSS 验收。

Core 的 RX `VecDeque<Bytes>` 与 TX `VecDeque<Block>` 在排空后若曾超过 8 个槽位，释放描述符高水位 backing；TX 分配失败回滚时也执行同样清理。归还 payload lease 不代表描述符已经释放，两者分别检查。活跃期描述符、乱序树、计时器等动态容器仍须按 P4 的物理容量账本与 RSS 对账。

## 4. 端口与 peer 份额

宿主单独注入共享 `GlobalBudget`，并通过 `ResourceLimits` 注入 `port_bytes`、`peer_bytes` 和 peer 连接数；库的生产入口不使用默认的 `peer = global / 4`。zfc 当前方案使用端口硬上限，peer 上限取基础份额与完整单流 allowance 的较大者并钳在端口上限内；以 global→port→peer→connection 的顺序执行上限。库提供物理容量估算接口，覆盖 RX/OOO+adapter、TX inflight/预取+adapter、块取整、元数据及进展空间；不可只返回 payload 字节。比例及内存探测属于 zfc，不在库重复维护。

份额是上限而非预留或最低服务保证；首版不做活跃 peer 动态估额和超额度借用。关联连接/缓存的内存均计入所属端口和 peer，公共小型元数据计全局且有上限。资源空间不足的新 peer 不能通过创建更多连接增加配额。单流 allowance 每 peer 只算一份，不乘连接数；它是准入上限而非预分配或保证同时可得。未限速流按需增长，保持小初始工作集；库不能因上限16MiB而主动预分配到顶。

进展保留在额度内预留并独立封顶，可用于按序填洞、ACK/RST/关闭；不能让 `force` 任意绕过端口/peer 限额。全局额度不足不分配。已确认的字节不能为腾空间给新流而丢弃。

配额等待有明确唤醒：物理额度释放时按队列顺序扫描，只唤醒当前 global、port、peer 剩余额度能够容纳的等待者，并把本轮已选者的额度先从可用量中扣除，避免一次释放广播给所有流；一轮释放足够多个流时也不能只唤醒一个而让剩余流长期 Pending。可复用已计费 TX 缓存块的等待者是例外：global/port 已满时，若共享缓存非空且 peer 有余量，一次最多额外唤醒一个重试；缓存可能属于其他 shard，实际申请失败时会重新等待，不把普通新分配误判为缓存复用。每连接只入等待队列一次，等待者生命周期跟随连接/实例代际，取消和超时移除。

## 5. RX 消费与 BDP 上限

宿主传入每端口派生的 core RX、stream RX、TX inflight、prefetch 和 stream TX cap。未知速率可用部署上限，已知速率按端口 BDP 收敛；算法公式由 zfc 文档维护。

新增区分“core 出队”和“应用消费”的接收状态：read_chunk 交给 adapter 只移动数据，不应立即把这批字节计为应用消费、增长 autotune 或释放 end-to-end RX 可用空间。adapter 报告实际 poll_read 消费量并合并窗口更新；可采用带 consumption credit 的 chunk/单独消费接口，但不能只依赖 owner Drop（块中部分数据已经被读走）。

每个连接的 `core_rx + ooo + adapter_unread` 受逻辑接收总 cap 约束；物理 allocation 另受账本约束。预算缩小时限制后续增长与准入，不回退已通告的 TCP 窗口右边沿、不丢已确认数据。外部原始 read API 可保留立即消费语义，adapter 走延迟消费接口。

TX 预取同时受时间与绝对字节上限约束：`min(prefetch_max, max(min_prefetch, rate × time))`；小端口可把 min_prefetch 降至合法下限，不能让固定 64KiB 最低预取突破其派生 cap。所有乘法 checked。

首版宿主限速变化沿用现有实例重建，新 Driver 构造时接收按新有效限速派生的配置；不要求在线 cap 更新入口、target/applied revision 或窗口 scale 热更新。旧实例按既定关闭契约释放资源，新实例按派生 cap 初始化；这不改变栈内按实际流量自调和内存压力下的正常流控。未来无损限速更新作为独立跨后端能力设计。

## 6. 缓存与连接生命周期

原每 shard 256×64KiB（16MiB）缓存已缩至每 shard 最多 16 块（约 1MiB），每块继续占全局与端口物理额度，跨 shard 空闲缓存合计再受 `min(H/8, 16MiB)` 限制（极小预算有 128KiB 下限且不超过 H）。实测压力下的回收与吞吐仍须验收。连接清空后保留的 TX 首块也需可回收。允许不缓存，不为“池化”强留空闲高水位。

发送记录 `Scoreboard` 在最后一条记录获确认后，若容量超过 8 条则释放 backing；小型请求/响应保留少量复用空间。活跃期增长已在发送规划前申请 global/port/peer 物理许可，替换 backing 时同时保有旧、新两份许可，按目标容量两倍保守记账并核验实际 `VecDeque::capacity()`；申请失败只暂停数据规划，纯 ACK 等控制报文仍可发送。额度释放通过进程共享 epoch 唤醒 driver，core 重排等待连接；FIN/RST、代际销毁和内存释放测试仍要纳入 P4 对账。此处仅覆盖发送记录，不能视为完成 §3 其他动态容器的物理账本。

无连接回复（RST、SYN cookie）不再为每包另建 `Vec`：≤100B 的 IP/TCP 头内联存入最多 1024 项的待发队列，每项在入队前按 peer 申请 global→port→peer 额度，保守包含队列增长余量。额度不足则不入队、也不虚报已排入的回复数；sink `Full` 时持有额度等待重试，发送或删除 iface 时释放。突发退潮后回收稀疏队列 backing。队列容量与实际 allocator 驻留仍在 P4 统一对账；此处不宣称 RSS 验收完成。

### 6.1 活跃连接与独立 TIME_WAIT

active permit 只涵盖半连接、已建立、FIN关闭中的完整状态，orphan 尚未真正结束也不提前释放。TIME_WAIT 则用独立、进程共享的条目数和字节预算，跨 shard 的元数据不能乘端口数无限放大。具体部署容量由 zfc 传入，库不能硬编码每 shard 65536。

当前 `Conn::enter_time_wait` 只释放 TX 等缓冲，并不意味着已压缩成独立小结构。新增轻量 tombstone，保存 iface/端口代际、认证peer、tuple、最后收发序号（含FIN）、timestamp/协商状态、到期时间及必要回复状态，丢掉完整 CC/发送记录/业务指针等。结构体本身及索引/定时器实际 bytes/entry 必须测量，不能引用理想估计冒充总成本。

当前实现只在应用已关闭、core/adapter 未读量为零且末尾 ACK 已提交后压缩；每条完整连接从准入时预留 640B 的 TIME_WAIT 共享预算（64-bit 固定容器估算约 300B，另计 Vec/HashMap 增长余量），并分别限制条目数与预留字节。tuple 到期后，如 shard 尚有其他连接且 slot/index 容器仍驻留，保留其中 384B 的全局物理额度；slot 复用或整组容器回收时再释放，紧凑代际数组按其实际 capacity 留账。原仅追加、未参与到期或复用判定的 `time_wait` 队列已移除，实际判定仍由 tuple 表、计数和定时器完成。这些额度不等于真机 RSS 或完整 allocation ledger；P4 必须核对 HashMap/Vec/allocator 高水位及多端口驻留，超出估算时调高预留或修改容器布局。

短连接突发退潮而仍有少量长连接/TIME_WAIT 时，tuple `HashMap` 会在容量超过 512 且活跃项不超过容量 1/8 时尝试重建小表；先申请新表，失败保留旧表及全部 tuple。slot 数组仍需按稳定 ConnId 保留高下标，过期 slot 的 384B 驻留额度也继续占用全局预算，不能把 tuple 表缩容视作全部元数据已回收。

当前分支在最后一个 tuple 退场且 slot 容量曾超过 2048 时，释放 slot/free/tuple/计时器等高水位容器；每个旧 slot 的下一代际仍以紧凑 `u32` 保存，防止旧 `ConnId` 命中新连接。该代际表及尚未归零时的动态容器仍须进入 P4 容量对账，不能仅凭空闲回收测试宣称物理账本完整。

准入 active 时预留未来一份 TIME_WAIT 槽位和元数据额度，统一受独立表上限及全局内存约束；转换时不再临时争取可能失败的容量。状态转入顺序：准备受管 tombstone → 原子替换 tuple 路由、定时器与代际 → 释放 active permit/可释放的完整状态。无需 TIME_WAIT 的 RST 等退出归还预留。旧应用仍持有的未读数据/句柄留在有界 adapter 中并继续记账，不能把压缩当成丢尾部数据的理由。

独立容量计 `现存 tombstone + active 预留`，预留字节与使用字节转换不双算；转换失败保持旧状态，不得以无界完整 Conn 兜底。全局满时对新准入可见回压，已经取得转换许可的关闭不被卡住。不因普通容量压力提前删除合法 tombstone。

### 6.2 安全复用与容量验证

同 tuple 新 SYN 复用按 [RFC 6191 §2](https://www.rfc-editor.org/rfc/rfc6191.html#section-2) 的 timestamp/序号条件建立测试表，不简化为“序号更大总能复用”。记录旧连接最后 FIN 序号而非最后收到的报文头 seq，使用32位串行比较，覆盖时间戳更大/相等/更小、启用/缺少 timestamps、序号回绕与对端重启。不能按源 IP 缓存跨 peer 的 timestamp 推断安全性。

新 SYN 通过复用检查后仍须通过 peer 身份、admission、active/元数据预留；全部成功再原子替换旧 tombstone。失败保留旧状态；旧定时器/延迟事件靠代际校验不得操作新 Conn。旧 FIN/ACK/RST 不得删除新连接或让旧数据进入新字节流。未满足复用条件时继续保留旧 TIME_WAIT，不提前回收冒充成功。

复用只解决相同 tuple，不能替代不同 tuple 的表容量。宿主的2000 CPS/60s目标至少需要120000条驻留，尚需 active 预留与突发空间；65536条即使独立也不够。不同 tuple 饱和、同 tuple 合法复用和故意非法复用分别测试。上限参数与保留期限可影响可持续CPS，报告必须同时给出条目、字节、active预留和准入拒绝原因。

## 7. Driver 与 Stream 完整性

- accept 真正有界，队列满不继续无限积累 stream；接受不了的状态明确拒绝/清理，不能只以 events.len 近似 backlog。
- 控制状态合并：egress-ready 每 iface 一个可恢复位，MTU 保存最新值，dirty 去重。不能把无界 channel 改成可能丢唯一恢复事件的 try_send。
- ingress、dirty、event、输出都按批次/操作数/字节有界；处理预算耗尽或 run.more 时 yield 后立即续跑，不能等新 IO。空闲只等 deadline/通知，无固定忙轮询。
- 发送记录分配失败后，普通应用写入和 ACK 不应仅因待发送字节仍存在就重试；释放通知要先检查该 peer、端口和进程额度能满足下一次申请，已有记录槽位被 ACK 释放则立即恢复。控制报文仍可发送。TX 块申请失败后的同步逻辑额度回滚不发释放通知；物理额度释放按实际可用的三层份额唤醒可容纳的等待流，实际申请仍做原子容量检查，释放与注册交错不能漏唤醒；同一等待 ID 再登记时更新其端口和 peer 归属。真机 64 并发深队列复测不再有数千万次失败与 CPU 忙轮询，但仍有个别 90 秒长尾；须在独立负载源或多核台架复核，不能视作 P4 已通过。
- sink Full 不提交发送；先获队列容量再构造连续包。Closed 是退出，不是永久 Full。
- writer 部分写和低水位唤醒有竞争测试；零长度读写遵守 Tokio 契约。flush 等已接受字节移交 core，不等待远端 ACK；shutdown 在队列排空后 FIN，保留读半边。
- 取消、channel 关闭、panic 退出均设置 stream 错误并唤醒全部 waiter，释放队列、许可和 shard，宿主可 await 确认退出；本地 close/abort 不发 Closed event 的路径也不能遗漏清理。

## 8. 测试与交付顺序

1. 先建立重现：大 RX/OOO 持有 WG-like 小池、1 字节 TX 的真实容量、adapter 未读保留、无消费者 accept、more=true 无新包、Full 唯一恢复事件和取消唤醒。
2. 引入资源/owner 契约及借用 RX，并与 owned 入口对照字节正确性。测 1 字节段、乱序重叠/填洞、MTU 改变、checksum 拒绝、零窗口。
3. 适配器完成有界事件、消费反馈和生命周期；去 scratch 中转独立验证。
4. 验证多 shard 同预算、四 peer 压单端口、其他端口的配额恢复、跨 shard TIME_WAIT 独立计数/字节/预留转换、RFC复用竞态、重复创建/退出后 ledger/permit 归零。
5. 记录 core bench 与完整 Tokio adapter 的 CPU/GB、allocation、实际 capacity、P99 和大流 RSS，不能复用 sans-IO 成绩冒充适配器实测。

以上完成后向 zfc 提供固定 revision 及测试证据，再开展完整 WG/业务集成。无代码实现、测试或节点实验时，不能将本文的设计条目标为已完成。
