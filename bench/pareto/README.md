# 固定工作量 CPU A/B 复现

这里保存本轮三个候选的复现工具：临时 `MemoryHandle` 转移所有权、RX 首个预留 chunk 内联、heap 相同 deadline 提前返回。DRR / `PeerReady` 实验不在交付候选中。

基线为 `71feaedafca7ce53e1823490a59b37bd8afcd028`。候选由待测源码的提交 ID 加精确补丁定位；不要用可移动的分支名称代替实验记录。本文描述方法和结果口径，不预填测量结论。

| 文件 | 用途 |
|---|---|
| [../../examples/pareto_probe.rs](../../examples/pareto_probe.rs) | 运行实际库代码的固定工作量 probe |
| [run_ab.py](run_ab.py) | 对已构建二进制做串行、绑核的 AB / BA 运行，保留逐次原始记录 |
| [cases.json](cases.json) | 本轮完整用例配置；预算与 RX 微基准、heap、上下行、双向、多 peer、短流等 |
| [analyze.py](analyze.py) | 只读取原始证据，校验成对记录并生成统计，不启动基准 |

三个脚本/配置文件是原实验文件的原样副本。`analyze.py` 保留了原文件名 `pareto_cases.json` 的 fallback，因此以下命令始终显式传 `--cases`，不依赖该 fallback。

## 测量范围

需要 Linux、Python 3 标准库、`taskset` 和同一套固定版本的 Rust/Cargo。probe 不需要 root 或 TUN 权限。

`pump-*` 在一台真实机器上运行两份 `Shard`，生成并解析原始 IP/TCP 包，逐字节比较应用接收数据，并检查正常关闭和资源释放。合成时钟只驱动 TCP 的协议时间、pacing 与定时器；速率分母使用机器实际 wall time 或 `CLOCK_PROCESS_CPUTIME_ID`。整个 probe 包含两个端点、包 materialization、应用遍历、握手和数据校验的 CPU 成本。

这组结果是固定工作量的 CPU 与包处理能力证据。它没有真实 socket、TUN、WireGuard 加解密、NIC 或网络传输，不能用作这些路径的实际网络吞吐，也不能把两个端点合计成本解释成单端栈成本。`--owned-server` 选择普通 owned ingress；它仍共用受管复制路径，不是 `PacketBuf` 的 leased 接收测试。

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

两份 release 二进制使用同一 Rust 版本、target、feature、profile 和编译环境；构建期间不要切换 toolchain 或 flags。不要为其中一边单独使用 `target-cpu=native`、LTO 或不同 allocator。`bin/` 中的副本固定后，测量期间不再覆盖它们。runner 也会在 `metadata.json` 和逐次记录中保存二进制 SHA256。

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
| `sampled_peak_reserved_bytes` | 受管额度占用的采样峰值，是下界，不是完整 allocator/RSS 峰值 |
| `retained_reserved_bytes_before_drop` | 队列或 Shard 析构前仍正常持有的 index/cache/metadata 等额度；是否残留要结合最终释放检查 |
| `process_lifetime_peak_rss_bytes` | 进程生命周期 VmHWM，包含初始化与校验/清理，不能视为稳态每连接内存 |
| `final_reserved_bytes == 0` | probe 持有的预算 lease 在对象销毁后已全部归还，不等于所有动态容器的账本已完整覆盖 |
| cgroup 限流增量 | 子进程执行期间整个 cgroup 的计数，可能包含其他进程，不直接归因于被测循环 |

先看每对 `100 * (candidate / baseline - 1)`，再报告这些变化的中位数与范围。CPU 每单位工作变化为负表示省 CPU；wall 处理速率变化为正表示处理更快。`ratio_of_medians_change_pct` 是另一种汇总，不能与“成对变化中位数”混称。

六对及以上可生成固定 seed 的 descriptive bootstrap 区间。它描述当前机器和固定工作负载的这些成对观测，不是对其他机器、真实网络或协议正确性的概率保证。检查 `consistency`：若包数、round 数或 virtual work 等发生变化，CPU 差异可能同时包含协议工作量变化，应解释后再归因。

### v1 已知描述差异

原 v1 probe 对所有 microprobe 使用 `microprobe_work_loop_includes_validation_excludes_setup_and_cleanup`。实际 heap 的完整 pop/order/key/uniqueness 校验在 `timer.stop()` 后执行，**heap 更新循环计时不包含最终校验**；RX 的逐字节验证则包含在 work 时间内。旧 JSON 的这项描述不能按字面外推到 heap。若以后修正 probe 元数据或代码，必须将同一新 probe 复制到 A/B 两侧，记录新的 probe 和二进制 hash，不混合新旧实验。

heap-mixed 为 60% 等 key、20% 减小、20% 增大的配置。等 key 早退在实际 key 变化时增加一次等值判断，因此该组结果不能证明 100% key 变化的负载也严格改善。RX 首项内联只增加短期栈上暂存和判断，不增加连接或队列的常驻字段；多 chunk 输入仍有 overflow 临时容器。
