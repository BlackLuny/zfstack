生产独立分支 868ce01c4801f9dc2ef582affcc454be270632f8 在 Linux x86_64、Rust 1.94.1 的全 workspace release 测试为 **300 PASS / 0 FAIL / 7 原有 ignored**。单独复跑 sim 为 49 PASS / 3 ignored，pacing 为 14 PASS / 0 ignored；后两组与全量测试重叠，不相加作为独立覆盖数量。全 workspace、all-targets、all-features Clippy 通过；原日志保留 57 条 warning 标题，没有传入 -D warnings。

原导出 18 项和补充 2 项的大小/哈希独立核验通过；9 条实际测试启动记录与对应原日志绑定一致。8 个命令均 exit 0、Linux subreaper cleanup PASS；源码执行前后 clean 且相同，公共租约释放、postflight 无任务进程/租约。忽略的 Cargo.lock 以 SHA256 f16d0a552ae1785ce8ea646ebb66d52ac2155336e732b38c0760f606f147046d 绑定，构建/测试/依赖解析命令均使用 --locked。远端 binary 哈希是保留的观测身份，因本导出没有 executable 原件，本地复核不声称重新 hash 远端 binary。

macOS 的 Linux 专用 API 导致的两次 whole-workspace 101 失败保留在 pacing-split-macos，Linux 通过不覆盖它们。辅助只读监视命令的 SyntaxError 也完整保留，独立于 8 个生产验证命令。具体 argv、逐组统计、ignored 名称、原路径/哈希/字节见 production-native-summary.json；实际执行身份归属于生产 868，而 8bf 的 QA 结果仍归属于 8bf。
