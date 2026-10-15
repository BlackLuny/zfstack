# 固定工作量 CPU A/B 复现

这里保存本轮三个候选的复现工具：临时 `MemoryHandle` 转移所有权、RX 首个预留 chunk 内联、heap 相同 deadline 提前返回。DRR / `PeerReady` 实验不在交付候选中。

基线为 `71feaedafca7ce53e1823490a59b37bd8afcd028`。候选由待测源码的提交 ID 加精确补丁定位；不要用可移动的分支名称代替实验记录。本文描述方法和结果口径，不预填测量结论。

| 文件 | 用途 |
|---|---|
| [../../examples/pareto_probe.rs](../../examples/pareto_probe.rs) | 运行实际库代码的固定工作量 probe |
| [run_ab.py](run_ab.py) | 对已构建二进制做串行、绑核的 AB / BA 运行，保留逐次原始记录 |
| [cases.json](cases.json) | 本轮完整用例配置；预算与 RX 微基准、heap、上下行、双向、多 peer、短流等 |
| [analyze.py](analyze.py) | 只读取原始证据，校验成对记录并生成统计，不启动基准 |

脚本保留原实验的文件组织，分析器另有 TX 账本与校验方法检查。`analyze.py` 保留原文件名 `pareto_cases.json` 的 fallback；以下命令始终显式传 `--cases`。

## 测量范围

需要 Linux、Python 3 标准库、`taskset` 和同一套固定版本的 Rust/Cargo。probe 不需要 root 或 TUN 权限。probe 本身也可在 macOS 上运行（进程 CPU 时钟用 `CLOCK_PROCESS_CPUTIME_ID`），但 `run_ab.py` 依赖 `taskset` 与 `/proc`，只适用于 Linux；macOS 无法绑核。

`pump-*` 在一台真实机器上运行两份 `Shard`，生成并解析原始 IP/TCP 包，逐字节比较应用接收数据，并检查正常关闭和资源释放。合成时钟只驱动 TCP 的协议时间、pacing 与定时器；速率分母使用机器实际 wall time 或 `CLOCK_PROCESS_CPUTIME_ID`。整个 probe 包含两个端点、包 materialization、应用遍历、握手和数据校验的 CPU 成本。

这组结果是固定工作量的 CPU 与包处理能力证据。它没有真实 socket、TUN、WireGuard 加解密、NIC 或网络传输，不能用作这些路径的实际网络吞吐，也不能把两个端点合计成本解释成单端栈成本。`--owned-server` 选择普通 owned ingress；它仍共用受管复制路径，不是 `PacketBuf` 的 leased 接收测试。

## TX 校验函数与测量版本

当前 probe 输出 `verification_method=portable_full_scan_v1`。用 XOR/OR reduction 扫描全部字节做精确比较，随后累加实际数据 checksum，两者均在 work timer 内。没有抽样或把校验移出计时。校验函数不内联；Linux x86_64 用独立页对齐的 ELF section 固定页内布局，避免周边 TX 代码改动移动校验热循环。TX 额外记录 source 和 warmup payload 地址对 64 的余数，帮助识别数据对齐对校验/复制成本的影响；不输出地址。

分析器拒绝一对 baseline/candidate 使用不同 verification_method 的记录；两侧必须使用同一份 probe 构建。计时包括校验器，发现 CPU 差值后应通过分段计时或采样区分 push/consume、比较和 checksum 的成本，再决定生产代码改动。旧记录及不同诊断方法分别统计，不合并绝对耗时。

`tx-alloc` 使用 cache0；`tx-alloc-pooled` 使用 cache32、同一 peer；`tx-alloc-migrate` 使用 cache32，在同一个空 TxBuf 上交替 peer1/2。三者都报告 warmup 和 drop 前的实际 retained 账本，未采样的 TX 峰值继续为 null。可分别用 `--payload 64`、`--payload 65536` 检查小包和大块；跨 peer 负载是迁移压力场景。

## 1. 固定源码并创建两个隔离 worktree

在包含目标基线对象、当前候选源码及本目录的正式 Git 仓库中执行，后续代码块沿用同一个 Bash 会话中的变量。以下命令把本轮三个生产文件相对于基线的当前内容保存为补丁，再应用到单独的 candidate worktree；已提交和未提交的这三个文件修改都会被捕获。源工作区不作构建用的 manifest 改动。

```bash
set -euo pipefail
PARETO_REPO="$(git rev-parse --show-toplevel)"
PARETO_BASE=71feaedafca7ce53e1823490a59b37bd8afcd028
PARETO_RUNROOT="$(mktemp -d /tmp/zfstack-pareto.XXXXXX)"
mkdir -p "$PARETO_RUNROOT/manifest" "$PARETO_RUNROOT/bin"

git -C "$PARETO_REPO" rev-parse HEAD > "$PARETO_RUNROOT/manifest/source-head.txt"
git -C "$PARETO_REPO" rev-parse "$PARETO_BASE" > "$PARETO_RUNROOT/manifest/baseline-head.txt"
git -C "$PARETO_REPO" diff --binary "$PARETO_BASE" -- \
  src/budget.rs src/buf.rs src/heap.rs > "$PARETO_RUNROOT/manifest/candidate.patch"

git -C "$PARETO_REPO" worktree add --detach "$PARETO_RUNROOT/baseline" "$PARETO_BASE"
git -C "$PARETO_REPO" worktree add --detach "$PARETO_RUNROOT/candidate" "$PARETO_BASE"
git -C "$PARETO_RUNROOT/candidate" apply --check "$PARETO_RUNROOT/manifest/candidate.patch"
git -C "$PARETO_RUNROOT/candidate" apply "$PARETO_RUNROOT/manifest/candidate.patch"

mkdir -p "$PARETO_RUNROOT/baseline/examples" "$PARETO_RUNROOT/candidate/examples"
cp "$PARETO_REPO/examples/pareto_probe.rs" "$PARETO_RUNROOT/baseline/examples/pareto_probe.rs"
cp "$PARETO_REPO/examples/pareto_probe.rs" "$PARETO_RUNROOT/candidate/examples/pareto_probe.rs"
cmp "$PARETO_RUNROOT/baseline/examples/pareto_probe.rs" "$PARETO_RUNROOT/candidate/examples/pareto_probe.rs"
cp "$PARETO_REPO/examples/pareto_probe.rs" "$PARETO_RUNROOT/manifest/pareto_probe.rs"
cp "$PARETO_REPO/bench/pareto/cases.json" "$PARETO_RUNROOT/manifest/cases.json"
cp "$PARETO_REPO/bench/pareto/run_ab.py" "$PARETO_RUNROOT/manifest/run_ab.py"
cp "$PARETO_REPO/bench/pareto/analyze.py" "$PARETO_RUNROOT/manifest/analyze.py"
```

检查 `manifest/candidate.patch` 确认恰好包含待测修改，且没有 DRR 实验。若候选已经有固定提交，也可以直接以该提交创建 candidate worktree，记录完整提交 ID 和它相对基线的 diff；此时不再重复应用补丁。两个方式都必须使用完全相同的 probe 源码。

probe 内部通过相对路径包含各 worktree 自己的 `src/heap.rs`，因此两边运行各自版本的 heap，不需要为了基准增加公开库 API。

## 2. 锁定依赖、编译器与构建参数

标准有网环境保持原来的 workspace，包括 `members = ["bench"]`。在一份隔离树生成一次 lockfile，再把同一文件复制到另一份；此后所有构建和测试都使用 `--locked`，不要在两边分别自由解析依赖。本次独立机器使用 Rust/Cargo 1.98.1；仓库 CI 另用固定的 1.94.1。

```bash
export CARGO_BUILD_JOBS=4
export CARGO_INCREMENTAL=0
# 与生产（zfc release profile）一致，并消除 codegen-unit 划分带来的构建间差异，见下文。
export CARGO_PROFILE_RELEASE_LTO=fat
export CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1
export CARGO_PROFILE_RELEASE_PANIC=abort
rustc -Vv > "$PARETO_RUNROOT/manifest/rustc.txt"
cargo -V > "$PARETO_RUNROOT/manifest/cargo.txt"
uname -a > "$PARETO_RUNROOT/manifest/uname.txt"
lscpu > "$PARETO_RUNROOT/manifest/lscpu.txt"
printf '%s\n' "${RUSTFLAGS-}" > "$PARETO_RUNROOT/manifest/rustflags.txt"
printf '%s\n' "${CARGO_ENCODED_RUSTFLAGS-}" > "$PARETO_RUNROOT/manifest/cargo-encoded-rustflags.txt"

cargo generate-lockfile --manifest-path "$PARETO_RUNROOT/candidate/Cargo.toml"
cp "$PARETO_RUNROOT/candidate/Cargo.lock" "$PARETO_RUNROOT/baseline/Cargo.lock"
cp "$PARETO_RUNROOT/candidate/Cargo.lock" "$PARETO_RUNROOT/manifest/Cargo.lock"
cmp "$PARETO_RUNROOT/baseline/Cargo.lock" "$PARETO_RUNROOT/candidate/Cargo.lock"

cargo build --locked --release --features tokio,test-peer --example pareto_probe \
  --manifest-path "$PARETO_RUNROOT/baseline/Cargo.toml" \
  --target-dir "$PARETO_RUNROOT/target-baseline" \
  > "$PARETO_RUNROOT/manifest/build-baseline.log" 2>&1
cargo build --locked --release --features tokio,test-peer --example pareto_probe \
  --manifest-path "$PARETO_RUNROOT/candidate/Cargo.toml" \
  --target-dir "$PARETO_RUNROOT/target-candidate" \
  > "$PARETO_RUNROOT/manifest/build-candidate.log" 2>&1

cp "$PARETO_RUNROOT/target-baseline/release/examples/pareto_probe" "$PARETO_RUNROOT/bin/baseline"
cp "$PARETO_RUNROOT/target-candidate/release/examples/pareto_probe" "$PARETO_RUNROOT/bin/candidate"
sha256sum "$PARETO_RUNROOT/bin/baseline" "$PARETO_RUNROOT/bin/candidate" \
  > "$PARETO_RUNROOT/manifest/binary-sha256.txt"
sha256sum "$PARETO_RUNROOT/manifest/candidate.patch" \
  "$PARETO_RUNROOT/manifest/pareto_probe.rs" "$PARETO_RUNROOT/manifest/Cargo.lock" \
  "$PARETO_RUNROOT/manifest/cases.json" "$PARETO_RUNROOT/manifest/run_ab.py" \
  "$PARETO_RUNROOT/manifest/analyze.py" > "$PARETO_RUNROOT/manifest/input-sha256.txt"
```

两份 release 二进制使用同一 Rust 版本、target、feature、profile 和编译环境；构建期间不要切换 toolchain 或 flags。不要为其中一边单独使用 `target-cpu=native`、不同 LTO 设置或不同 allocator。

**必须用 fat LTO + `codegen-units=1` 构建两边。** 仓库默认 release profile（16 个 codegen unit、无 LTO）下，函数如何划分到各 unit 取决于 crate 元数据哈希，而该哈希包含源码路径。2026-10-09 在 Apple M1 Max 上的 A/A 实验：同一份基线源码只换一个 worktree 路径重新构建，pump 场景 CPU 在 12/12 对中一致偏差 +5%～+7%，退休指令数偏差最多 +52%（`rx_65535_p1`）、−16%（`heap_equal_4096`）。这与待测改动的量级相同，默认 profile 下的 pump 差异无法归因到代码。改用 `CARGO_PROFILE_RELEASE_LTO=fat`、`CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1` 后，同一 A/A 的退休指令数差异降到 ±0.5% 左右（`down_1_paced` 为 ±0.75%）。

建议每轮额外做两项检查：

- **A/A 对照**：把基线源码放在另一个路径重新构建一份，与基线做同样的 AB/BA，作为本轮"构建噪声"的下界。
- **退休指令数**：用 `perf stat -e instructions`（Linux）或 `/usr/bin/time -l` 的 `instructions retired`（macOS）对两边各跑几次。指令数基本不受负载和频率影响；它与 CPU 时间方向一致时结论更可靠。即使在 LTO 下，CPU 时间仍可能有约 1% 的纯布局差异（同一函数体代码完全相同，CPU 仍差约 1%），小于这个量级的单项结论应结合指令数判断。`bin/` 中的副本固定后，测量期间不再覆盖它们。runner 也会在 `metadata.json` 和逐次记录中保存二进制 SHA256。

### 离线实验环境的特殊处理

本次独立开发作业的离线缓存没有 workspace 中 `zfbench` 所需的 smoltcp Git 依赖。实际作业仅在两个实验副本里将 `members = ["bench"]` 改为 `members = []`，使 Cargo 能使用已有缓存构建库和 probe。**不能将这项修改提交到生产 `Cargo.toml`。**

如需复现同样的离线设置，在生成 lockfile 和构建之前，对上一步创建的两份 worktree 同时执行：

```bash
python3 - "$PARETO_RUNROOT/baseline/Cargo.toml" "$PARETO_RUNROOT/candidate/Cargo.toml" <<'PY'
from pathlib import Path
import sys
for argument in sys.argv[1:]:
    path = Path(argument)
    text = path.read_text()
    old = 'members = ["bench"]'
    if text.count(old) != 1:
        raise SystemExit(f"unexpected workspace manifest: {path}")
    path.write_text(text.replace(old, 'members = []', 1))
PY
git -C "$PARETO_RUNROOT/baseline" diff -- Cargo.toml \
  > "$PARETO_RUNROOT/manifest/baseline-workspace-only.patch"
git -C "$PARETO_RUNROOT/candidate" diff -- Cargo.toml \
  > "$PARETO_RUNROOT/manifest/candidate-workspace-only.patch"
```

随后把上述 `cargo generate-lockfile`、`cargo build` 和下面的 `cargo test` 命令都增加 `--offline`；仍然只生成一份 lockfile、复制到另一树，并以 `--locked` 构建和测试。离线库回归不包括被排除的 `zfbench` workspace 成员。保存实际使用的 manifest patch 和 lockfile，不能把全 workspace 与缩减 workspace 的两次实验混为同一构建条件。

## 3. 功能回归与性能测量分开运行

先完成构建和功能测试，再开始性能串行测量。下面两组库测试应分别保留日志与退出码；候选侧通过不能替代基线侧检查。

```bash
cargo test --locked --release -p zfstack --features tokio \
  --manifest-path "$PARETO_RUNROOT/baseline/Cargo.toml" \
  --target-dir "$PARETO_RUNROOT/target-baseline" \
  -- --test-threads=4 > "$PARETO_RUNROOT/manifest/test-baseline.log" 2>&1
cargo test --locked --release -p zfstack --features tokio \
  --manifest-path "$PARETO_RUNROOT/candidate/Cargo.toml" \
  --target-dir "$PARETO_RUNROOT/target-candidate" \
  -- --test-threads=4 > "$PARETO_RUNROOT/manifest/test-candidate.log" 2>&1

cargo build --locked -p zfstack --no-default-features \
  --manifest-path "$PARETO_RUNROOT/candidate/Cargo.toml" \
  --target-dir "$PARETO_RUNROOT/target-candidate" \
  > "$PARETO_RUNROOT/manifest/build-no-default.log" 2>&1
```

probe 的字节模式周期是 251，多个流使用同样的模式。逐字节比较能发现这些运行中接收内容、顺序和长度不符合预期，但不能完整证明跨流 demux 正确，例如交换两条内容完全相同的流未必可见。正常 FIN/EOF、许可释放等 probe 检查也只覆盖当前工作负载；功能无回归需要另行结合库测试及对应的故障、配额、重排、协议互通测试。probe 没有给出真实网络、Tokio adapter 或其他平台的完整功能验收。

## 4. CPU 2 上串行 AB / BA

先确认当前进程允许使用 CPU 2，且没有同时在同一机器上编译、运行其他基准或大规模测试。

```bash
taskset -pc "$$"
taskset -c 2 true
python3 "$PARETO_RUNROOT/manifest/run_ab.py" \
  --baseline "$PARETO_RUNROOT/bin/baseline" \
  --candidate "$PARETO_RUNROOT/bin/candidate" \
  --cases "$PARETO_RUNROOT/manifest/cases.json" \
  --outdir "$PARETO_RUNROOT/results" \
  --cpu 2 --pairs 6 --timeout 120 \
  > "$PARETO_RUNROOT/runner.log" 2>&1
sha256sum --check "$PARETO_RUNROOT/manifest/binary-sha256.txt"
```

`--outdir` 必须尚不存在。每个 case 先运行一个不进入统计的 warmup pair，再运行六个正式 pair；正式顺序为 AB、BA、AB、BA……。`--iterations` 是每个二进制内部的固定工作量，`--pairs` 是独立进程的成对重复次数，两者含义不同。runner 不构建、不并行启动被测程序，失败或超时后停止并保留已有记录。

若 CPU 2 不在允许 affinity 内，选择实际允许的 CPU 并显式改 `--cpu`，在报告中说明与原环境的差异；不要把 affinity 失败当成性能结果。CPU affinity 不能排除其他进程、虚拟机 steal time、频率变化或 cgroup 限流。runner 保存每次运行前后的 CPU/cgroup/load 快照，分析时结合这些证据解释波动，不静默删除不利样本。

主要原始文件为 `results/metadata.json` 和 `results/runs.jsonl`。JSONL 保留每次命令、变体、pair、退出码、stdout/stderr、CPU/wall、字节计数和环境快照。不要只保存终端摘要。

## 5. 提取原始证据后分析

可以将原始结果与复现输入一起打包；以下命令不修改原始记录：

```bash
tar -C "$PARETO_RUNROOT" -czf "$PARETO_RUNROOT/pareto-evidence.tar.gz" \
  manifest results runner.log
```

在分析机器上，把 `PARETO_EVIDENCE_ARCHIVE` 改为实际取得的证据包路径；若包内目录结构不同，将 `PARETO_RAW_RUNS` 指向同时含 `runs.jsonl` 和 `metadata.json` 的目录。

```bash
PARETO_EVIDENCE_ARCHIVE=/path/to/pareto-evidence.tar.gz
PARETO_EXTRACT="$(mktemp -d /tmp/zfstack-pareto-evidence.XXXXXX)"
tar -tf "$PARETO_EVIDENCE_ARCHIVE"
tar -xf "$PARETO_EVIDENCE_ARCHIVE" -C "$PARETO_EXTRACT"
PARETO_RAW_RUNS="$PARETO_EXTRACT/results"

python3 - "$PARETO_RAW_RUNS/metadata.json" "$PARETO_EXTRACT/cases-used.json" <<'PY'
from pathlib import Path
import json
import sys
metadata = json.loads(Path(sys.argv[1]).read_text())
Path(sys.argv[2]).write_text(json.dumps(metadata["cases"], indent=2) + "\n")
PY

python3 "$PARETO_EXTRACT/manifest/analyze.py" "$PARETO_RAW_RUNS" \
  --cases "$PARETO_EXTRACT/cases-used.json" \
  --expected-pairs 6 --bootstrap 20000 --seed 20261009 \
  --output "$PARETO_EXTRACT/summary.json" \
  > "$PARETO_EXTRACT/summary.txt"
```

若取得的旧证据包未附 analyzer，可使用本目录的 `analyze.py` 替换最后一条命令中的脚本路径，并记录所用脚本的 SHA256。用从 `metadata.json` 提取的实际 cases 分析，避免日后仓库配置变化导致错配。若原实验采用不同的 pair 数，`--expected-pairs` 必须与 `metadata.json` 中的 `argv.pairs` 一致。

分析器的退出码 0 表示通过所配置的证据完整性检查，2 表示记录失败、不完整或不符合约束。`--allow-partial` 只适合查看未完成运行，允许缺失 case/pair，不会放过失败、重复或不匹配记录；部分结果不能当成完整验收。warmup 不进入统计，异常测量不会被混入有效成对比较。

## 6. 阅读结果的口径

| 字段/指标 | 含义和边界 |
|---|---|
| `work_cpu_ns / operations` | budget/heap 的实测循环 CPU ns/op；budget 只申请/释放账本 lease，不传输对应数量的 payload |
| `work_cpu_ns / effective_bytes` | 数值上等于 CPU 秒/十进制 GB；RX 包含字节验证，pump 包含两端及应用/包处理 |
| `effective_bytes * 8 / work_wall_ns` | Gbit/s 形式的真实 wall 包/缓冲处理速率，非网络吞吐；双向数据在各接收端各计一次 |
| `virtual_work_ns` | 合成协议时钟的工作量一致性信息，不是性能分母 |
| `wire_packets` / `wire_bytes` | pump 生成及交付的包计数，包含 cleanup；不能除以排除 cleanup 的 work 时间派生严格窗口 pps |
| `sampled_peak_reserved_bytes` | 受管额度占用的采样峰值，是下界，不是完整 allocator/RSS 峰值；v2 `tx-alloc` 未采样工作峰值，输出 `null` |
| `warmup_reserved_bytes` / `warmup_reserved_scope` | v2 `tx-alloc` 在 warmup push 后、consume 前读取 `global.reserved()`；计时外的已提交账本快照，包含 payload 档位和描述符容量，不含临时预留，不是工作峰值、allocator 或 RSS；其他 case 为 `null` |
| `retained_reserved_bytes_before_drop` | 队列或 Shard 析构前仍正常持有的 index/cache/metadata 等额度；是否残留要结合最终释放检查 |
| `process_lifetime_peak_rss_bytes` | 进程生命周期 VmHWM，包含初始化与校验/清理，不能视为稳态每连接内存 |
| `final_reserved_bytes == 0` | probe 持有的预算 lease 在对象销毁后已全部归还，不等于所有动态容器的账本已完整覆盖 |
| cgroup 限流增量 | 子进程执行期间整个 cgroup 的计数，可能包含其他进程，不直接归因于被测循环 |

先看每对 `100 * (candidate / baseline - 1)`，再报告这些变化的中位数与范围。CPU 每单位工作变化为负表示省 CPU；wall 处理速率变化为正表示处理更快。`ratio_of_medians_change_pct` 是另一种汇总，不能与“成对变化中位数”混称。

六对及以上可生成固定 seed 的 descriptive bootstrap 区间。它描述当前机器和固定工作负载的这些成对观测，不是对其他机器、真实网络或协议正确性的概率保证。检查 `consistency`：若包数、round 数或 virtual work 等发生变化，CPU 差异可能同时包含协议工作量变化，应解释后再归因。

### v1 已知描述差异

原 v1 probe 对所有 microprobe 使用 `microprobe_work_loop_includes_validation_excludes_setup_and_cleanup`。实际 heap 的完整 pop/order/key/uniqueness 校验在 `timer.stop()` 后执行，**heap 更新循环计时不包含最终校验**；RX 的逐字节验证则包含在 work 时间内。旧 JSON 的这项描述不能按字面外推到 heap。若以后修正 probe 元数据或代码，必须将同一新 probe 复制到 A/B 两侧，记录新的 probe 和二进制 hash，不混合新旧实验。

heap-mixed 为 60% 等 key、20% 减小、20% 增大的配置。等 key 早退在实际 key 变化时增加一次等值判断，因此该组结果不能证明 100% key 变化的负载也严格改善。RX 首项内联只增加短期栈上暂存和判断，不增加连接或队列的常驻字段；多 chunk 输入仍有 overflow 临时容器。

### v2 TX 账本快照及消费兼容

v1 `tx-alloc.sampled_peak_reserved_bytes` 使用硬编码 `payload + 64`，没有读取账本；小输入也漏掉 2 KiB payload 档位。v2 改为上述 warmup 快照，将 TX 的旧峰值字段及 `peak_reserved_is_sampled_lower_bound` 设为 `null`，schema_version 升为 2。计时循环保持不变，不在循环中添加账本采样。warmup 快照不证明计时期间的峰值。

`run_ab.py` 原样保留 v1/v2 JSON，无需修改。`analyze.py` 接受旧记录和可空峰值，单独汇总/显示 warmup ledger；旧 TX 估算被标为缺失值并说明原因，不能与快照混算，也不填零。旧记录没有 warmup 样本，不派生相应的成对百分比。历史证据文件保持原样；新实验仍必须向 A/B 两侧复制同一 probe 并记录 hash。

消费回归只使用模拟记录和 mock 子进程，不运行 A/B 测量：`python3 -m unittest discover -s bench/pareto -p 'test_*.py'`。

## 冻结计划与三态性能门禁

`analyze.py` 保留现有 AB/BA 描述性报告。正式入口为 `perf_gate.py`，只接受运行前冻结的完整 ABBA/BAAB block；不能将两个时段的半个 block 拼接，不能删去失败或污染记录。候选功能 FAIL 优先于测量 INVALID。每臂执行新进程，所有记录必须与计划中的配置、二进制、构建及环境 hash 一致，单调时钟区间不得重叠。

计划是 schema v1 的 JSON object，包含：

- `runner_id`、`profile`、任意稳定 `case_id` / `case_version`、完整 `config`、`binary_sha256: {A, B}`、`build_manifest_hash`、`environment_hash`。
- `comparison: "AB" | "AA"`、`mode: "shadow" | "enforce"`。只有显式 AA 可使用相同二进制 hash；构建 AA 可使用相同源码独立构建的不同二进制。
- `blocks: [{block_id, order: "ABBA" | "BAAB"}, ...]`，按正式执行顺序保存；`min_blocks >= 2`、`seed`、`alpha`、`max_abs_lag1`，可选 `max_abs_skew`（schema v1 默认 2）。正式 N 和主指标集合必须在候选结果产生前冻结。
- `metrics: [{id, unit, direction: "lower" | "higher", budget_fraction}, ...]`。接近零的指标可事先选择 `scale: "absolute", budget_absolute`；不会在看到结果后自动选较宽松的口径。
- `method: "block_t"`（默认建议）或 `"paired_block_bootstrap"`；后者还需 `bootstrap_iterations >= 1000`，且每个同时区间尾部至少具有 10 个 bootstrap 重复的分辨率。执行预算限制会拒绝过大的分析计划。
- 强制门禁须固定 `calibration_id` 与实际校准报告的 `calibration_hash`。未完成真实节点校准时只使用 shadow；仓库不附带任何已验证节点资格。

每个 block 对每个指标先计算两次 A 与两次 B 的均值 log 差，统一正值表示退化。成本预算为 `log(1 + δ)`，吞吐预算为 `-log(1 - δ)`。默认使用 block 均值的 Student t 区间，K 个主指标采用 Bonferroni `α/K` 同时区间；可选 bootstrap 对整 block 联合重采样，同一组 indices 用于所有指标。`U <= budget` 为 PASS，`L > budget` 为 FAIL，其余 INCONCLUSIVE。明显的 lag-1 依赖、分布偏斜或不足的 bootstrap 尾部分辨率使结论保持 INCONCLUSIVE，不会事后更换方法。统计假设、block effects、区间和诊断均保留。

```sh
python3 bench/pareto/perf_gate.py --plan /absolute/plan.json \
  --rows /absolute/runs.jsonl --calibration /absolute/calibration.json \
  --output /absolute/performance.json
```

行格式包含 `run_id, case_id, block_id, position, arm, plan_hash, config_hash, build_manifest_hash, environment_hash, binary_sha256, metrics`，并包含与 QA v2 同形的 `validity`、`functional`、`execution`。`execution` 必须含真实 `CLOCK_MONOTONIC` 的整数 `start_ns/end_ns`、退出状态与清理结果。缺失、零 log 指标、负计数、NaN/Infinity、身份变化、重复 slot、漏 slot、时间重叠均无法进入正式区间。JSON 拒绝重复键与指数溢出，并有输入大小上限。

## 串行执行与环境

`run_ab.py` 的旧参数继续运行历史探索入口。传入 `--plan` 切换为正式单场景执行；它不会编译，也不会重新采样直到通过。多个 case 的计划顺序由上层可信 QA 计划固定。

```sh
python3 bench/pareto/run_ab.py --plan /absolute/plan.json \
  --expected-plan-hash "$PLAN_HASH" \
  --baseline /absolute/baseline --candidate /absolute/candidate \
  --environment /absolute/environment.json \
  --lease-directory /absolute/leases --physical-domain "$DOMAIN" \
  --generation "$GENERATION" --outdir /absolute/fresh-results
```

正式 runner 的 `config` 另需 `args`（probe 参数数组）、`cpu`、`timeout_sec`、`expected_measurement`（已冻结的工作量字段和值，例如 operations、effective_bytes、iterations、flows、peers、payload_bytes、checksum）。CPU 固定在子进程 exec 前生效。环境 JSON 固定 `kernel/cpu_model/affinity/governors/cgroup_limits`；`cgroup_limits` 为 DUT 所属 cgroup-v2 及所有祖先的 `{path,cpu.max,cpuset.cpus.effective,memory.max}` 数组。`environment.snapshot(pid)` / `environment.cgroup_snapshot(pid)` 可用于节点预检，缺少数据保持 unknown。计划 hash 使用排序 key、紧凑分隔符、UTF-8、拒绝非有限数的 canonical JSON SHA256。

runner 复用 `qa.lease.Lease` 与 `qa.process.run_child`，轮询子进程时检查所有权和 generation。Linux 独立 subreaper 清理本次拥有的全部后代，包括新建 session 的子进程，检测到遗留后代仍记录 execution failed；不会删掉失败 slot 或在原 block 补跑。输出文件不覆盖已有目录；取消/超时保留当前记录和未完成 block，清理不能证明时隔离节点。每个 slot 原始 stdout/stderr 合流落盘并记录 hash，exec helper 的 `RLIMIT_FSIZE` 限制继承到后代，默认 1 MiB，最大 64 MiB；触顶会使运行失败。`config.max_output_bytes` 与 `config.sample_interval_sec` 随计划冻结，历史计划缺省分别为 1048576 和 0.1。

环境线程通过 exec 前 PID 握手与 Linux `/proc/PID/exe` 确认采样对象是 DUT，exec 前样本单独标记，不能替代运行中样本。环境观察总量限制为 16 MiB，触顶或采样异常均明确失效。所有共用物理域的构建、测试与性能任务须使用同一租约，额外宿主独占性由编排端确保。采样读取 DUT 自己的 cgroup v2 路径及祖先限制，缺失的 v2 能力或任何祖先限制均为 INVALID；保留 PSI、throttle、频率策略、CPU、内存与编译进程，不会将候选自身引起的 throttling 一概删为噪声。采样安静不等于节点已获性能资格。

## A/A 校准与检测能力

`calibrate.py --spec /absolute/spec.json --output /absolute/calibration.json` 消费已冻结的校准计划和真实 runner 记录。spec 包含 `schema_version:1, calibration_id, target_plan, min_training_blocks, min_null_trials, min_delta_trials, max_false_positive_rate, min_detection_rate, trials`。`trials` 项包含 `trial_id, kind, plan_file, rows_file, provenance_file`，路径相对 spec；kind 为 `run-AA`、`build-AA`、`held-out-null` 或 `held-out-delta`。

每个 A/A 层至少 20 个完整训练 block；这只是起点。构建 AA 需独立同源码构建 manifest 出处，null 与 delta 记录必须与训练记录的 run ID 不相交。已知退化 trial 额外固定 `known_delta: {metric_id: amount}`，退化必须超过对应预算，并提供已测控制产物的 hash 与 `kind: controlled-work | controlled-delay | known-regression-binary, synthetic:false`。不能把候选样本乘一个系数冒充控制实验。每个主指标都需要足够的 held-out delta 检测次数。

报告给出 null family 误报率和每指标检测率的 Wilson 95% 区间；其上/下界必须满足预注册策略，样本不足会拒绝验证。控制出处与独立构建源码仍由可信编排校验。单测中的合成数据只证明统计器会拒绝反例，绝不生成实际节点校准证书。环境、profile、配置、N、指标集合或统计政策变化会使原校准失配。

## 最小真实 shadow A/A

case_version 与主 QA v2 一致，为正整数。`freeze_plan.py` 使用真实 binary / build manifest / 环境与一次探索性 reference measurement，生成固定顺序的 shadow 计划，不执行二进制，也不颁发性能资格。以下调用在已隔离的 Linux 执行目录内进行；构建已完成且停止，`CPU` 来自允许的 cpuset，`PROBE` 是实际不可变的 pareto_probe。

```sh
# 先在节点租约内保存一次探索性输出；此文件不进入正式样本。
taskset -c "$CPU" "$PROBE" budget --iterations 5000000 --flows 1 --payload 2112 > reference.json
# 对实际绑核进程采样，保存稳定的环境指纹。
taskset -c "$CPU" python3 - <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, 'bench/pareto')
from environment import snapshot
s = snapshot()
e = {k: s[k] for k in ('kernel', 'cpu_model', 'affinity', 'governors')}
e['cgroup_limits'] = [{k: row.get(k) for k in ('path', 'cpu.max', 'cpuset.cpus.effective', 'memory.max')}
                     for row in s['cgroup'].get('ancestors', [])]
Path('environment.json').write_text(json.dumps(e, sort_keys=True) + '\n')
PY
PLAN_HASH="$(python3 bench/pareto/freeze_plan.py \
  --baseline "$PROBE" --candidate "$PROBE" --reference reference.json \
  --environment environment.json --build-manifest build-manifest.json \
  --runner-id "$NODE" --profile release --case-id P0-budget --comparison AA \
  --trial-id run-aa-001 --cpu "$CPU" --blocks 20 --budget .03 --output plan.json \
  -- budget --iterations 5000000 --flows 1 --payload 2112)"
# 若编排持有外层租约，先结束该准备阶段租约，再由 runner 接管同一域；不双重申请。
python3 bench/pareto/run_ab.py --plan plan.json --expected-plan-hash "$PLAN_HASH" \
  --baseline "$PROBE" --candidate "$PROBE" --environment environment.json \
  --lease-directory "$LEASES" --physical-domain "$DOMAIN" --generation "$GENERATION" --outdir run-aa-001
python3 bench/pareto/perf_gate.py --plan plan.json --rows run-aa-001/runs.jsonl --output run-aa-001/performance.json
```

这里 `.03` 仅是 shadow 分辨率探索参数。没有校准时预期输出 `decision: SHADOW, performance: INCONCLUSIVE`；异常/缺证据仍会阻断。较短的 smoke 可使用 `--blocks 2`，但不能作为校准训练。

构建 A/A 使用同一源码和规范构建路径独立生成的 A1/A2，向 `freeze_plan.py` 传入两个实际产物、`--comparison AA --trial-id build-aa-001`，其余 workload / 环境 / profile / N 与目标校准档一致。provenance 需 `same_source_verified:true`、两项 `independent_build_manifest_hashes` 和两个不同的 `independent_build_invocation_ids`；可信编排必须验证源码与构建调用出处。相同源码的独立可重现构建可能产生相同二进制与 manifest hash，因此不要求这些 hash 不同，不能仅凭 hash 声称已做两次构建。训练之外的 null、已知退化使用独立 trial ID 和新正式记录。全部记录和 provenance 交给前述 `calibrate.py --spec ... --output ...`，通过后将实际报告 hash 绑定到新的 enforce 计划；一次 shadow A/A 不会自动升级为阻断门禁。

## 可选冻结的绝对服务合同

相对非劣（包括 `scale:absolute` 的差值预算）不等于固定的服务上限。`qa.service.validate_contract(contract)` 和 `evaluate_service(contract, peers)` 提供独立、纯 QA 的可选合同；维护者必须先审核并将合同冻结进 case identity，工具不提供任何产品默认阈值。合同禁止未知字段，完整字段为：

```python
contract = {
    "schema_version": 1,
    "peer_ids": reviewed_peer_ids,                  # 非空、唯一 string 数组
    "phases": reviewed_phases,                      # 非空、唯一 string 数组
    "min_samples_per_peer_phase": reviewed_minimum, # 正整数，每 peer/phase 独立满足
    "max_latency_ms": reviewed_latency_bound,        # 正有限数，逐请求上限
    "deadline_ms": reviewed_deadline,                # 正有限数
    "max_deadline_miss_rate": reviewed_miss_bound,   # [0,1]，已观察比例上限
}
report = evaluate_service(contract, peers)
```

`peers` 映射冻结的 peer ID 到 `{samples:[...],counters:{...}}`。每条 sample 需要 `phase,seq,status,scheduled_at,started_at`，成功完成的请求另需有限、非负 `latency_ms`；失败/删失请求可缺少或使用 null latency。时间戳为同一单调时钟的秒数，`latency_ms` 是开始请求至完成的毫秒数，因此合同逐请求比较 `(started_at-scheduled_at)*1000+latency_ms`，包含生成、排队和服务等待。额外原始观察字段可以保留。`seq` 必须在同一 peer 的完整样本中唯一；`counters` 恰含七个非负整数 `scheduled,started,completed,failed,timed_out,unfinished,missed`，须与整个原始样本集合精确复算一致，不能只上传筛选后的成功请求。

检查每个冻结 peer/phase 的最小样本数、每个已完成请求的延迟上限，以及已观察的 deadline miss 比例。`failed/timed_out/unfinished` 均进入业务请求分母并计 miss；已完成但端到端延迟超过 deadline 也计 miss。generator `missed` 不当作业务丢弃、不进入业务分母，而使证据 `NOT_RUN`。没有完整 samples、缺 peer/phase、重复序号、计数不符或没有可观察的成功延迟都不能变绿。不会将 timeout 填成 deadline 或计算仅包含成功样本的服务尾部保证。结果是 `{status:PASS|FAIL|NOT_RUN,assertions:[{id,status,kind:"service",detail}],observations:{contract_hash,scope,peers}}`；已有超限 `FAIL` 优先于其他缺证 `NOT_RUN`，性能环境污染也不能抹掉功能合同失败。此检查描述已观察请求与比例，零 miss 不表示真实失败概率为零，也不承诺总体 p99/p999。

性能计划还可对指定主指标添加 `absolute_slo`，单位沿用该 metric 的 `unit`。`direction:lower` 恰需 `{scope:"observed-per-run",max:reviewed_bound}`，`direction:higher` 恰需 `{scope:"observed-per-run",min:reviewed_bound}`；不能混用 min/max 或使用概率/分位数 scope。计划 hash 和校准 scope 包含这个阈值，修改后不得沿用原校准。

该检查逐个比较身份绑定的候选 B 原始 metric，不能被 block 均值、相对非劣或另一指标抵消。任一明确超限使 `absolute_slo.status`、`functional` 和 `decision` 为 `FAIL`，即使校准缺失、其他 slot 缺失或环境污染；缺少完整 B slot/metric 时不能给绝对合同 PASS。没有可选字段时保持原分析行为。绝对检查只声明 **observed-per-run**，不声称候选总体的尾概率界限。正式 QA reporter 仍需通过 `qa.performance.inspect_rows` 从原始 probe 日志重算 metric，不能只信上传行的数值。产品阈值与深 egress queue 等负对照的合同应由维护者预注册；单测合成阈值不构成发布政策。
