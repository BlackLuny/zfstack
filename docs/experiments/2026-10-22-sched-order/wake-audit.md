# 发送空间事件审计

所有传输空间门控均与 `plan_data_with` 共用 `new_data_len`，包括 cwnd/PRR、rwnd、TSO 整 MSS 缩段及发送端 SWS；重传、TLP、persist、ACK、SYN/RST 不经新数据门控。仍有未发数据时，待 piggyback 的 FIN 不绕过门控；纯 FIN 维持原语义。

| 事件 | 生产入口与重新调度链 | 回归证据 |
|---|---|---|
| 累积 ACK、CC cwnd 增长 | `ingress_payload -> Conn::input -> process_ack -> reprice_pacing -> after_conn_change -> schedule` | `ack_progress_and_cc_window_growth_reschedule_after_event` |
| 纯窗口更新（无 ACK 推进） | 同上；窗口 WL1/WL2 更新先于 delivered 判断 | `pure_window_update_reschedules_without_ack_progress`；既有 zero-window / FIN-window 测试 |
| SACK、重复 ACK、丢包标记 | 同上；SACK/rack/进入恢复/PRR 更新均在 input 内；RACK timeout 走 timer 链 | `sack_and_loss_marking_reschedule_and_preserve_empty_pipe_exception`；既有 lossy / RACK / PRR 测试 |
| RTO、恢复退出、计时器 CC 变化 | `run -> timers.pop_due -> on_timer -> reprice_pacing -> after_conn_change -> schedule` | `rto_and_cwnd_reset_reenter_service_through_timer_event` |
| PRR quota 变化 | ACK/SACK 的 `process_ack`；发送 commit 只扣 quota，不会打开空间 | `prr_quota_opening_reenters_ready_after_event`；既有 PRR/恢复测试 |
| pacing 速率变化 | CC 只在 ACK/timer 中变化，经上述链；`schedule` 对 armed heap 比较 rate 并移除重定价 | 既有 `rate_change_repositions_armed_heap_without_control_work` 和两项 BBR 恢复测试 |
| pacing 到期 | `run -> pacing.pop_due -> schedule` **直接重新调度**，不经过 after_conn_change；没有连接状态变化 | 既有 pacing_wake_tests 全组 |
| 发送记录预算释放 | `run` 检查 release epoch，预算可分配或已有 spare 时 **直接 schedule**；ACK 释放记录经普通链 | 既有 send-record 预算/Full/原地重传回归和 EventOnly capacity-recovery |
| 应用写入、MTU 变化、份额变化 | 应用 `with_conn -> after_conn_change -> schedule`；MTU 更新循环 `schedule`；share 只通知应用写入，send_share 不改变 cwnd/rwnd | 原有 API/MTU/份额唤醒测试及新 counter 测试 |
| 出口释放 | `egress_released` 解冻原队列，不重新构造 ready；下一 run 从冻结位置恢复 | 原有 sink-full 与跨 iface 测试 |

“全部经过 after_conn_change”不是基线的事实：pacing 到期、预算 epoch 和 MTU 变化直接调用 schedule。这些直接入口同样重新检查门控，保留这一事件驱动设计，不新增扫描轮询。

随机事件测试固定种子、4096 次 ACK/窗口/CC/loss/TLP/写入事件；每次事件后断言不存在 `!wants_tx && plan.is_some()`，并在事件协调后检查可发送连接位于 ready/pacing。随机测试是回归证据，不替代穷尽证明。测试用 `test_conn_event` 调用真正的 `with_conn -> after_conn_change`，只在 cfg(test) 编译。

注意：门控后 `serve_zero` 不统计弹出后已失去就绪条件的 stale entry；它仍需一次出队。这项 CPU 成本由 CPU/GB 测量覆盖，不能仅凭 serve_zero 的下降宣称 CPU 下降。
