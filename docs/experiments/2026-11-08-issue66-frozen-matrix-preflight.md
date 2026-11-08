# Issue #66 — A/B/C/D/F 冻结性能矩阵：输入预检（仅第一阶段）

## 范围与结论（2026-11-08 UTC）

本变更新增 `bench/pareto/issue66_matrix.py`，为 [#66](https://github.com/BlackLuny/zfstack/issues/66) 将**六份独立构建**（A/B/C/D/F/F2）与七组必要比较固定在同一、可审计的输入计划中。F2 是与 F 源码相同、独立 build directory 的第二份构建，用于先检查编译布局与环境噪声（A/A）。

**本阶段没有运行正式网络性能或长期内存测量；无任何 CPU、吞吐、p99、MTU 或泄漏结论。** 计划把所有比较和验收门标成 `NOT_RUN`，而不是把预检成功误写成性能 PASS。

### 固定源码版本

| 标签 | Git commit |
| --- | --- |
| A（#19 后） | `56aed4ffd92f628d58a8ad773c52baf685fab79e` |
| B（#20 后） | `8b3f418f15f473baf5dce043c981ab753f0a5a11` |
| C（#21 后） | `37c016d3e136426994e4b40198e236d5ffbc0fb6` |
| D（#56 前） | `c2ae50439e50c081940b951bd075a6ce4bb41335` |
| F（#56 后） | `3459562746e689fc5ec78c5b1764b0bba3465363` |
| F2（A/A） | 与 F 相同的 Git SHA，独立编译目录 |

比较顺序：**F2/F（A/A）→ B/A → C/B → C/A → D/C → F/D → F/B**。所有正式用例至少 12 个成对区组，每组顺序预先冻结为 ABBA/BAAB，并保证两种顺序数量至多差 1。预检不会根据实测成绩修改区组、删除失败样本或更换比较定义。

### 如何生成只读的计划

先按历史版本准备六个独立的源码 worktree 和六套独立 target/build 目录。构建时固定相同的 Rust/Cargo、target、profile、RUSTFLAGS、features、实际可互通的 manifest/lock、探针和 harness；对每份二进制记录真实 SHA-256。**源版本不能用可移动的 main/master 代替。** 使用相同 probe、锁文件必须先证明能在各个历史 src 版本上编译与运行，不能为了通过预检伪造共用身份。

创建 `builds.json`：

```json
{
  "common": {
    "rustc": "rustc 1.x.y ...",
    "cargo": "cargo 1.x.y ...",
    "target": "x86_64-unknown-linux-gnu",
    "features": "tokio,test-peer",
    "rustflags": "-C lto=fat -C codegen-units=1",
    "profile": "release-fat-lto",
    "manifest_sha256": "<真实 64 位 SHA256>",
    "lock_sha256": "<真实 64 位 SHA256>",
    "probe_sha256": "<真实 64 位 SHA256>",
    "harness_sha256": "<真实 64 位 SHA256>",
    "case_generator_sha256": "<真实 64 位 SHA256>"
  },
  "common_input_paths": {
    "manifest_sha256": "/frozen/common/Cargo.toml",
    "lock_sha256": "/frozen/common/Cargo.lock",
    "probe_sha256": "/frozen/common/pareto_probe.rs",
    "harness_sha256": "/frozen/common/run_plan.py",
    "case_generator_sha256": "/frozen/common/cases.json"
  },
  "variants": {
    "A": {
      "source_dir": "/frozen/src-A",
      "source_sha": "56aed4ffd92f628d58a8ad773c52baf685fab79e",
      "build_dir": "/frozen/build-A",
      "binary_path": "/frozen/build-A/release/examples/pareto_probe",
      "binary_sha256": "<真实 SHA256>",
      "common_inputs_sha256": "<common 对象规范 JSON 的 SHA256>"
    }
  }
}
```

`variants` 必须同结构补齐 **B/C/D/F/F2**；F2 的 `source_sha` 为 F。这里的占位值不是可运行输入。SHA 采用脚本同目录 `perf_gate.digest(common)` 的 canonical JSON 编码，其他字段采用原始文件的 SHA-256。二进制必须位于各自的 build directory 内。

`cases.json` 必须为非空、名称唯一的用例数组，例如：

```json
[
  {"name": "tx_64", "args": ["tx-alloc", "--iterations", "100000", "--payload", "64"]}
]
```

此示例只对应 CPU probe；不应将其算成 socket/TUN 或 MTU 测试。

```sh
python3 bench/pareto/issue66_matrix.py \
  --manifest /frozen/builds.json \
  --cases /frozen/cases.json \
  --pairs 12 --seed 66 \
  --out /frozen/issue66-preflight.json
sha256sum /frozen/issue66-preflight.json
```

使用 `x` 独占创建，不能覆盖历史计划；会校验真实 Git HEAD、未修改的 `src/tests`、二进制内容哈希、共享输入文件内容哈希、构建目录的隔离性与全部七组比较。缺项、不一致或者少于 12 对时失败；只有预检有效才输出计划。注意预检**不执行测量，不验证运行时环境或网络拓扑**，也无法仅凭不同 build directory 证明编译机物理隔离。正式执行前仍须核实实际构建命令与二进制 provenance。

### 下一阶段的验收门槛

依照现有 `bench/pareto/freeze_plan.py`、`run_plan.py` 与 `qa.lease.Lease` 构建**每个 case** 的真实冻结性能计划并取得物理测量域排他租约，先跑独立 F2/F A/A，再以同源输入、同编译参数运行六组 A/B。需要冻结独立网络 harness（不能把 sans-I/O probe 的 `real_network_io=false` 当 socket/TUN），分开记录代理和负载器的 CPU，保留完整 JSONL/环境采样/失败样本。报告 CPU +2%、吞吐 −5%、P99 +5% 非劣界及 bootstrap/区组置信区间；波动过宽则为证据不足。

在两个实际 MTU（1420 / 1500）下分别测 socket 与 TUN 的单流、8/64/256 流、paced/unpaced、短请求/大流、上下行/双向，固定 offload、CC、qdisc、RTT、预算，至少 1000 次建连。F/F2/C/D 分别补三轮 ≥30 分钟的 idle→burst→idle→burst 内存/账本曲线；区分 live、pool cached 与 allocator RSS 高水位。未取得 PMU 或长期样本时填 **NOT_RUN**，不能填零。

本变更只对冻结输入和矩阵完整性提供可执行预检，不改变 `src/` 性能逻辑，也不修改任何产品默认策略。该 Issue 应保持 OPEN 直到上述真实测量完成。
