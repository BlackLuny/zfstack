两处生产修复的可审查回归依据


| 修复 | before 生产来源 | 相同定向回归 before → after | 完整原日志 |

|---|---|---|---|

| 有限小 burst 的及时 pacing 唤醒 | `7b3891149c249842a8563af650f8169b1acbfa1a` 加相同最终测试模块 | 5 FAIL / 1 PASS → 6 PASS | [before](production-regressions/wake-before.log) / [after](production-regressions/wake-after.log) |

| CC 速率变化按字节守恒重定 pacing 债务 | `ddeed62ae34d7f5af3d4ccce6a61ad4b8c7eb11b`，**已包含第一处修复**，加相同自然 BBR 测试 | 自然恢复 1 FAIL / 1 PASS → 2 PASS；共 17 项保护通过 | [before](production-regressions/debt-before.log) / [after 14 项](production-regressions/debt-after.log) |


第一处只在真正待发占用不超过完整 quantum 时提前唤醒；持续 bulk 的原唤醒方式与 batch 保持，原 bulk 控制在 before/after 均通过。第二处保留尚欠的字节量，在速率改变时换算正时间债务并更新已有 heap；没有重置 CC、放宽 RTO、cwnd、发送资格、burst credit 或 workload。两个 before 不属于同一生产 SHA，不能合并写成单一“旧版”。


第二处的 17 项来自 `pacing_` 的 14 项（含两项自然恢复、accounting/heap/bulk/small 等）和独立的 [BBR/Brutal 完成](production-regressions/debt-bbr-brutal.log)、[peer fairness](production-regressions/debt-fairness.log)、[small Cubic](production-regressions/debt-small-cubic.log) 三项。原始日志逐字复制，SHA256/字节/原路径在 production-regressions.json 与 source-map.json。它们是历史确定性回归证据，不能替代实际 CPU 性能认证或新的 native 运行。
