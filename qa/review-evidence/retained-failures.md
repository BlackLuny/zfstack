旧失败保留定位

这里不把历史失败覆盖成新版通过。完整逐文件路径、大小与 SHA256 见 artifact-index.json；可按以下原样 prefix 筛选。retained-failures.json 另外列出索引范围内所有非 PASS 的 result-v2、非 PASS gate 摘要及日志中的失败信号；日志信号可能来自刻意负例，不能单独视为生产故障。未收集的 /tmp 不在此完整性声明内。

- `f50f94f-negative-zero-tests`: Intentional negative: zero-test result must not become PASS (98 files).
- `df8d31b-negative-zero-tests`: Intentional negative: zero-test result must not become PASS (99 files).
- `8543035-safety-staged-negative`: Intentional staged-artifact negative evidence (40 files).
- `queue-links-open-loop-retained-failure`: Retained real local generator failure (4 files).
- `validation-20261015/linux-f50`: Cross-repository build output-budget failure and original Linux evidence (154 files).
- `validation-20261015/linux-ae17834`: Original build disk-guard attempts and original Queue failures (165 files).
- `validation-20261015/linux-ae17834-diagnosis`: Original timing/boundary/worker raw closure diagnosis (15 files).
- `validation-20261015/open-loop-retained-failure`: Retained original generator failure raw (2 files).
- `validation-20261015/open-loop-readiness-fix`: Before/after readiness failure and retained explicit stall negative (17 files).
- `validation-20261015/bd05d4f-open-loop-retained-failure`: BD portable real generator failure, unchanged (3 files).
- `validation-20261015/portable-9bb392a-failed`: Portable suite failure retained (106 files).
- `validation-20261015/bd05d4f-portable`: BD portable failure retained (106 files).
- `validation-20261015/pacing-wake-development`: Deterministic pacing before-fix failures and after-fix controls (12 files).
- `validation-20261015/pacing-rate-debt-development`: Natural pacing before-fix failure and accounting regressions (22 files).
- `validation-20261015/bbr-rate-recovery-diagnostic`: Natural BBR recovery diagnostic failures, not formal qualification (9 files).
- `validation-20261015/queue-diagnostic-r1`: Original Queue diagnostic (92 files).
- `validation-20261015/queue-diagnostic-r2`: Original Queue diagnostic (97 files).
- `validation-20261015/queue-diagnostic-r3-rejected`: Rejected drain: link drops and UDP timeouts (96 files).
- `validation-20261015/queue-diagnostic-r4-ddeed62`: DDE Queue recovery failure diagnostic (101 files).
- `validation-20261015/ddeed62-queue-retained-failure`: DDE formal Queue retained failure (75 files).
- `validation-20261015/queue-capacity-slot-rejected`: All three preregistered slot cells rejected; original verdicts unchanged (406 files).
- `validation-20261015/queue-capacity-rate-matrix`: 2G diagnostic candidate; 3G/4G rejected, not a formal PASS (421 files).
- `validation-20261015/rate-capacity-preregistration`: Preregistered scripts, local failed fixtures and final checks (12 files).
- `validation-20261015/8bf5bb8-linux-l2`: Original final formal Queue NOT_RUN retained; Direct/Relay functional PASS (139 files).
- `validation-20261015/8bf5bb8-queue-two`: Exactly two additional unchanged formal Queue runs, both NOT_RUN; complete TCP and UDP populations retained (113 files).
- `validation-20261015/pacing-split-macos`: Retained Darwin whole-workspace compilation failures from Linux-only APIs; successful narrower checks remain separately scoped (12 files).
- `validation-20261015/pacing-split-linux`: Independent production Linux validation and retained auxiliary read-only monitor SyntaxError; no failed production command (22 files).
