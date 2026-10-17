# Issue 65 TX 记账优化：无缓存小包与跨 peer 迁移实验验证

## PR 中的证据包

汇总、图表和关键验证结果直接保存在本目录；完整 JSONL、冻结输入、脚本、消融候选及日志压缩在 `raw-evidence.tar.gz` 中。候选源码仅供实验审计，生产改动以仓库 `src/` 为准。

先核对 SHA，再在**新的临时目录**解包。下文文件名和脚本命令相对于解包后的 `evidence/`；其中 README 是打包前的原始索引，历史 SHA 清单的范围保持不变。

```sh
shasum -a 256 -c SHA256SUMS
experiment_evidence=$(mktemp -d)
tar xzf raw-evidence.tar.gz -C "$experiment_evidence"
cd "$experiment_evidence/evidence"
```

[完整报告](../../../docs/experiments/2026-10-15-issue65-tx-accounting.md)。

目标：在完整保留 #21/#56 的 descriptor 实际容量计费、global/port/peer 层级检查、非空 TX 拒绝换 peer、失败回滚、精确唤醒和缓存回收语义的前提下，消除 #22/#56 遗留的无缓存小包（`tx-alloc`）和跨 peer 缓存迁移（`tx-alloc-migrate`）记账成本。

- 实验隔离目录：`/tmp/issue65-exp`。
- B：#20，`8b3f418f15f473baf5dce043c981ab753f0a5a11`。
- D：#56 之前的 `main`，`c2ae50439e50c081940b951bd075a6ce4bb41335`。
- F：当前 `main`（含 #56/#64），`ebcd6dd961ecaf4063c5cf0d58e11653fed70c2c`。
- P1：固定槽位 peer 计数器/备用 `Arc` 缓存 + 无缓存 `TxBlock` 租约外壳复用 + 原子读取精简（不含空 `TxBuf` 索引与单块迁移/缓存融合）；`p1-peer-cache-recycle.patch` / `budget-p1.rs` / `buf-p1.rs`。
- G：完整优化（P1 + 空 `TxBuf` 索引与单块配额/缓存融合，热路径消除跨 peer 迁移的 `Arc` 引用计数 RMW 与双重 CAS）；`candidate.patch`。
- G2：G 同源码、不同目录独立构建，用于 A/A 对照。
- 统一使用 Rust 1.92.0、F 的 manifest/lock/bench 与 `portable_full_scan_v1` probe，fat LTO、codegen-units=1、panic=abort、incremental=0、build jobs=1。
- 所有变体（D/F/P1/G/G2）的 `.text.zf_probe_verify`（`0x1d000..0x1d180`）反汇编逐字节一致（SHA256 `a12f72d327d7c778a6aee56163c2a828ce7459a0415f0dc1df46a9b687f721be`）。
- 各实验串行运行，微基准 `taskset -c 3`。

## 数据文件

- `micro.jsonl` / `micro-meta.json`：27 case，B/F/G/G2，12 正式随机完整区组 + 1 warmup（共 1404 次运行）。
- `ablation.jsonl` / `ablation-meta.json`：6 个 TX case（64B / 1400B × 3 模式），F/P1/G，12 正式随机完整区组 + 1 warmup（共 234 次运行）。
- `boundary.jsonl` / `boundary-meta.json`：4 个 64KiB 边界 case（65535B / 65537B × pooled/migrate），B/F/G/G2，12 正式随机完整区组 + 1 warmup（共 208 次运行）。
- `pilot-pre-fmt-*.jsonl` / `pilot-pre-fmt-*-meta.json`：`cargo fmt` 换行与新增单测合入前的首轮完整 1404 + 234 + 208 次运行，完整保留在归档及 `summary.json` 的 `pilot_pre_fmt` 字段中供审计，不与正式区组混算。
- `build-*.log`、`test-*.log`、`debug-*.log`、`no-default-1.92.log`、`clippy-linux.log`：构建与正确性检查日志。
- `inputs.tar.gz`、`inputs-sha256.json`、`binary-manifest.json`、`evidence-sha256.json`、`fullscan-layout.json`：源码、二进制、校验器布局及证据 SHA 审计。

`python3 analyze.py > summary.txt` 会检查完整区组、工作量、checksum、`verification_method=portable_full_scan_v1`、最终账本归零以及 TX warmup/retained 容量一致性，然后计算逐对百分比中位数与 5000 次 bootstrap 的 95% 区间（seed22）。`python3 verify_inputs.py` 校验全部证据 SHA、归档源码与当前工作树一致性、冻结二进制 SHA 及校验器反汇编一致性。

## 重建

使用一个新目录，避免覆盖原始数据。解包 `inputs.tar.gz`，并将脚本内的路径调整到新目录：

```bash
export CARGO_PROFILE_RELEASE_LTO=fat CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1
export CARGO_PROFILE_RELEASE_OPT_LEVEL=3
export CARGO_PROFILE_RELEASE_PANIC=abort CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=1
for v in BV DV FV P1 G G2; do
  src_dir="${v}"
  [ "$v" = "BV" ] && src_dir="B"
  [ "$v" = "DV" ] && src_dir="D"
  [ "$v" = "FV" ] && src_dir="F"
  cargo +1.92.0 build --locked --release --features test-peer \
    --example pareto_probe --manifest-path "$RUNROOT/$src_dir/Cargo.toml" \
    --target-dir "$RUNROOT/target-$v"
done
```
