本目录是 QA 第二个 PR 的小型审查材料；完整证据保存在本机 tar.zst，未上传。


原完整 QA 冻结测试源码为 `8bf5bb86106e5a4f1665f3c1b9c4570d2c80bf72`。Portable 为 **17/17 PASS**；Safety 为 **5/6、BLOCKED**：Miri、三个 ASan fuzz 入口及 Loom 通过，Darwin ASan+LSan 组合因 LSan 能力缺失而 NOT_RUN。原报告与摘要保留，capability_missing 不计通过。


生产分支包含 `ecd91aa29c29c671290fe8468bbcddbbdd19c08b`、`868ce01c4801f9dc2ef582affcc454be270632f8` 两个提交。该独立生产分支在 Linux Rust 1.94.1 的全 workspace release 测试 **300 PASS / 0 FAIL / 7 原有 ignored**，Clippy all-targets/all-features 通过且 warning 保留；单独 sim 49 PASS 和 pacing 14 PASS 与全量测试重叠，不相加。原日志哈希、完整 argv 和独立复核见 [生产原生验证](production-native-summary.md)。macOS whole-workspace 的两次平台编译失败也保留。


QA `609c72aea890ca64e7795448d0b98d1a82ffed94` 的父提交为生产 `868ce01c4801f9dc2ef582affcc454be270632f8`；其 Git tree 与拆分前 QA `5daed2440f97da0fc1d301cc33a95aaadbb8efc1` 相同（`3702eaf843363e118f36d5016ca210fdc7313a84`）。这是树身份比对，不是新执行。**8bf 的完整验证仍归属于 8bf，不能改标为新 QA 提交通过。** 见 split-identities.json；两处生产修复的不同 before 基线、原始失败日志及 after 保护见 [生产回归](production-regressions.md)。


原 final8bf Linux Direct/Relay 功能 PASS/VALID，但性能证据 INVALID，不能声称性能认证。原正式 Queue 与恰好两次追加均严格 NOT_RUN/UNKNOWN，追加 gate 均 BLOCKED：前两轮未证明 delete 时排空，第三轮恢复超出边界。三轮完整 TCP/UDP 原样本均零 timeout/failed/missed/unfinished，所有 baseline 与 fault recovery 的 service/arrival 观测均不超 250 ms；这些业务事实不替代注入器资格。原首轮 loaded TCP 超标 **17/16/16**、约 **1.003 s** 最大 arrival 保留；追加两轮 TCP 各 16 次，UDP 四路各 **1562/1563** 次超 250 ms，见 [三轮完整复算](queue-repetitions.md)。原 peer1 recovery 的 782 个请求来自闭环跨边界，不是遗漏 arrival；90,496 B 快照不等于已证明的删除丢包量。


完整本机包严格覆盖源目录 `/Users/lulin/.codex/worktrees/qa-system/zfstack/qa/results` 的全部文件，含已有 fuzz-build/缓存和重复保留副本。冻结快照共 **12,381 个文件、2,509,219,016 logical bytes**；缓存占 1,726,592,625 B，仅说明体积，不增加覆盖。consolidated validation-20261015 是该树子集，不能再次相加。不按名义 2.2 GB 推断数量，也不声称所有 /tmp 诊断均已收集。


完整包：`/Users/lulin/zfstack-qa-evidence-20261015/qa-results-20261015.tar.zst`，**626,522,907 B**，SHA256 `f5c392ef4339a4f616909b8a8b3d64141610675268865917abd809d9017784ee`。本摘要独立核对了压缩文件大小和 SHA256；完整归档已逐成员与本目录索引核验通过：12,381 个文件及 2,509,219,016 logical bytes 全部相符，包括 1,524 个 tar 硬链接的目标内容；无缺失或额外文件，zstd 校验退出 0。见 [核验记录](archive-verification.json) 与 [校验脚本](archive-verifier.py)。首次脚本因不支持硬链接而失败的记录仍保存在归档旁，路径与哈希见核验记录；归档本身未重写。closure-archive-preflight.json 的 key-marker 粗扫无匹配，不代表穷尽所有秘密类型。


artifact-index.json 逐文件记录完整冻结快照的相对 path、size_bytes 和 SHA256，以 source_root 为基准；归档成员根为 qa/results。source-identities.json 保留已发现 result-v2 的 tested SHA 与 verdict，source-map.json 记录摘要来源。retained-failures.md/JSON 定位旧失败、刻意负例与诊断；所有旧判定保留。审查材料总量 **小于 5,000,000 B**，不包含大型 pcap 或构建缓存正文。
