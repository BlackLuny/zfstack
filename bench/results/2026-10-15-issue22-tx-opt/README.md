# Issue 22 TX 成本优化：第二台机器验证

## PR 中的证据包

汇总、图表和关键验证结果直接保存在本目录；完整 JSONL、冻结输入、脚本、诊断候选及日志压缩在 `raw-evidence.tar.gz` 中。候选源码仅供实验审计，生产改动以仓库 `src/` 为准。

先核对 SHA，再在**新的临时目录**解包。下文文件名和脚本命令相对于解包后的 `evidence/`；其中 README 是打包前的原始索引，历史 SHA 清单的范围保持不变。

```sh
shasum -a 256 -c SHA256SUMS
experiment_evidence=$(mktemp -d)
tar xzf raw-evidence.tar.gz -C "$experiment_evidence"
cd "$experiment_evidence/evidence"
```


[完整报告](../../../docs/experiments/2026-10-15-issue22-tx-optimization.md)。

目标：在保留 #21 的 descriptor 计费、所有者迁移、失败回滚和回收语义的前提下，减少小包 TX 成本。

- 主机：root@38.207.178.131:22100；隔离目录 /root/zfopt22-20261015。
- B：#20，8b3f418f15f473baf5dce043c981ab753f0a5a11；D：main，c2ae50439e50c081940b951bd075a6ce4bb41335。
- P1：不可变 peer 身份检查；p1-identity.patch / budget-p1.rs。
- F：P1 加缓存 payload 记住未计费的 peer 计数器；candidate.patch。
- F2：F 同源码、不同目录独立构建，用于 A/A 对照。
- B/D 使用上一轮未改变的冻结源码和二进制，位于 /root/zfverify22-20261015。
- 统一使用 Rust 1.92.0、C 的 manifest/lock/bench 与扩展 probe，fat LTO、codegen-units=1、panic=abort、incremental=0、build jobs=1。
- 各实验串行运行。微基准 taskset CPU3；真实 socket/TUN CPU0–3，MTU1420、CUBIC、seed1。未修改 governor、IRQ 或线上服务。

## 数据文件

- micro.jsonl / micro-meta.json：27 case，B/D/F/F2，12 正式随机完整区组 + 1 warmup。
- ablation.jsonl / ablation-meta.json：6 个 TX case，D/P1/F，12 正式随机完整区组 + 1 warmup。
- large.jsonl / large-meta.json：针对首轮64KiB迁移的正向 CPU 差值追加12区组，包含P1对照；两轮及合并结果分别保留。
- network-initial.*：首次TUN等待条件过早造成的准备阶段失败，只有两次warmup；正式数据完整重跑，不复用。
- pilot*.jsonl / pilot*-meta.json：选择候选前的 4 区组试测，保留用于审计，不混入正式统计。
- network.jsonl / network-meta.json：D/F 配对的 mixed64、千并发建连、不限速上下行8流。上行两版都使用临时 TUN txqueuelen100000，避免前轮已定位的队列容量限制；pfifo limit仍为10000。
- build*.log、test*.log、zf22*.log、clippy*.log：构建和正确性检查。macOS 的全 workspace clippy 因 benchmark 使用 Linux API 而失败，core clippy1.94.1单独验证；Linux检查使用已安装stable1.96。1.92组件下载取消，原脚本post_checks.sh及日志保留，详见报告。
- source/binary/evidence SHA 文件与 inputs.tar.gz：源码、构建输入和运行证据审计。源码以保存的输入或候选 patch 为准。

`python3 analyze.py > summary.txt` 会检查完整区组、工作量、checksum、最终账本、TX 保留容量、应用数据和建连成功数，然后计算逐对百分比中位数与 5000 次 bootstrap 的 95% 区间（seed22）。百分比不等于两组中位数之比。P99/P99.9 是每次运行的分位数；极限吞吐仅5对，区间可能较宽。

## 重建

使用一个新目录，避免覆盖原始数据。解包 inputs.tar.gz，并将脚本内两个固定实验路径调整到新目录。归档包含B/D/P1/F/F2的实际输入，保持同一probe、manifest、lockfile、bench源码。归档已有F2时，跳过build_measure.sh中复制F2目录的一行。脚本使用独占新建 JSONL，不能直接覆盖重跑。

```bash
export RUSTUP_HOME=/root/zfverify22-20261015/rustup
export CARGO_PROFILE_RELEASE_LTO=fat CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1
export CARGO_PROFILE_RELEASE_PANIC=abort CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=1
cargo +1.92.0 build --locked --release --features tokio,test-peer \
  --example pareto_probe --manifest-path "$RUNROOT/F/Cargo.toml" \
  --target-dir "$RUNROOT/target-F"
cargo +1.92.0 build --locked --release -p zfbench \
  --manifest-path "$RUNROOT/F/Cargo.toml" --target-dir "$RUNROOT/target-F"
```

build_measure.sh 保留完整构建、Linux 正确性测试与正式 micro 命令。随后串行执行 ablation.py、large_followup.py、network.py；全部计时结束后执行post_checks_installed.sh做Linux lint、构建及证据收集。运行网络测试需 root、TUN、iproute2、tc，并先核对临时 zfbA/zfbB/zfbns 与10.201.0.0/24未被占用。network.py 在 finally 中清理临时网络。
