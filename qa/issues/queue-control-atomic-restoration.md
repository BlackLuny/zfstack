# Issue draft: QueueControl 原子恢复（known-BLOCKED）

状态：独立后续 issue 文本，未创建远程 issue，本 PR 不实现。

当前 `C16-l2-queue-control` 的业务请求超时与台架资格分别判定。冻结 `8bf5bb8` 的首轮及追加两轮均保留真实判定；请求完成不能把台架 NOT_RUN/BLOCKED 改成 PASS。`known-failures.json` 的标注不是豁免，required case、阈值和聚合规则不变。

## 问题及证据

持续负载下，`tc qdisc show` 读到 backlog=0 后、`tc qdisc del` 执行前可以再次入队。首轮 draining JSON 为零时，同轮稍后的文本已有 46322 B；cleanup JSON 为 90496 B。恢复虽然早于原 24 秒边界，仍不能证明删除没有丢弃当时待发包。零 link/qdisc drop 与 cleanup PASS 也不能覆盖 reset 的不可观测释放；90496 B 同样不能被冒称为确认丢失量。重复读零、缩短查询间隔、调整排空速率或重复跑到通过都不解决原子性。

首轮故障期 TCP 三 peer 有 17/16/16 次超过固定 250 ms，最大请求耗时约 1.003 s；这是保留的负对照事实。完整三轮摘要和原始证据索引见 [review-evidence](../review-evidence/README.md) 与 [VALIDATION](../VALIDATION.md)。

## 后续方案（需隔离宿主，未实现）

采用持续存在的单 FIFO 注入器，让所有包始终经过同一路径；候选为 NFQUEUE 配合有界队列。在同一串行状态机或锁内完成空队列确认与透明转发切换，在整个测量结束后才拆除规则/资源。不得在恢复阶段继续保留 2 Gbit/s 整形来冒充原始无整形网络，也不得暂停、减少业务输入来制造空队列。

应在专用隔离宿主准备内核 NFQUEUE 支持、libnetfilter_queue 与开发依赖，避免影响共享生产宿主。当前节点 `5.4.0-216-generic` 配置为模块，`nfnetlink_queue`/`xt_NFQUEUE` 未加载，库及头文件未发现；本轮没有加载模块、安装依赖或新增规则。

## 验收要求

- 保持原 500 ms 注入、100000 包上限、8/16/8 秒阶段、250 ms guard、3 秒请求 deadline、250 ms 服务合同及原 TCP/UDP/重流负载，不改到达语义。
- 对入队、出队、交付、取消和有界资源占用建立完整原始账本；缺失、重复、乱序、溢出、丢包或未达阶段都要明确失败，不能仅依赖计数器为零。
- 原 24 秒 recovery 边界前原子转为透明路径；证明不改变包内容、负载、生产默认值及恢复期服务语义，保持完整 recovery 观察。
- 实际链路/队列计数、时间与身份闭包、公共 Lease、磁盘/内存守卫、取消/异常/失租约清理及负例必须独立复算。
- 先冻结实现与预注册资格标准，再用独立 held-out 正式运行验证；保留旧三轮失败，不覆盖、不追认、不按结果挑选参数。
- 该 issue 完成前，QueueControl 继续 known-BLOCKED；生产 pacing PR 可单独审查，但不能借此宣称完整 Nightly 或发布资格。
