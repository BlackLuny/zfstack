# Issue22 64KiB 交替 peer：原始证据与复现

## PR 中的证据包

汇总、图表和关键验证结果直接保存在本目录；完整 JSONL、冻结输入、脚本、诊断候选及日志压缩在 `raw-evidence.tar.gz` 中。候选源码仅供实验审计，生产改动以仓库 `src/` 为准。

先核对 SHA，再在**新的临时目录**解包。下文文件名和脚本命令相对于解包后的 `evidence/`；其中 README 是打包前的原始索引，历史 SHA 清单的范围保持不变。

```sh
shasum -a 256 -c SHA256SUMS
experiment_evidence=$(mktemp -d)
tar xzf raw-evidence.tar.gz -C "$experiment_evidence"
cd "$experiment_evidence/evidence"
```


主报告：[CPU 回退定位与基准修正](../../../docs/experiments/2026-10-15-issue22-tx-migrate.md)。2026-10-15，`root@38.207.178.131:22100`；远端隔离目录 `/root/zftxmigrate22-20261015`，main `c2ae50439e50c081940b951bd075a6ce4bb41335`。

最终生产库与上一轮 F 完全一致；本轮交付修正的是 probe 的全量比较方法及分析器口径检查。旧 libc 比较仍能复现回退，不能将新旧校验器之间的绝对耗时变化算作生产 TX 提速。

## 最终验收文件

| 文件 | 内容 |
|---|---|
| fullscan-crossover-config.json / *-meta.json / *.jsonl | DP/FP 旧 libc 比较、DV/FV 全字节扫描，同一窗口 24 个完整排列区组，600 次运行 |
| fullscan-followup-config.json / *-meta.json / *.jsonl | DV/FV/FV2 独立复测，12 区组；64KiB 缓存两个 case 工作量提高5倍，351次运行 |
| summary.json / cpu-table.csv | 各轮各方法单独的逐对比值中位数、10000次 bootstrap CI，seed422 |
| validation.json | 工作量、checksum、最终释放与可比较 retained/warmup 账本核对 |
| fullscan-layout.json / DV、FV、FV2-verify-disassembly.txt | 同源码 probe SHA、相同页内地址/热循环指令字节、无 bcmp/memcmp 调用 |
| source-equivalence.json / worktree-source-match.json | DV=main，FV=先前F，FV2=FV；当前工作树 src/ 与 probe 精确匹配 FV |
| inputs.tar.gz / inputs-sha256.json | 30个冻结构建目录的 manifest、lock、src、tests、examples 和统一 bench 输入；包括未采用候选 |
| binary-manifest.json / evidence-sha256.json | 远端二进制、证据 SHA；collect.py 在收尾重新核对每轮二进制未被替换 |
| environment-after.json / main-ref-final.txt | 主机/编译器/进程状态与最终 main 复核 |
| linux-tests.log / linux-probe-tests.log / linux-256-stress.log / linux-no-default.log / python-tests.log | 第二台最终正确性检查 |
| qa/ | 本地检查；包括平台限制和额外严格检查的失败记录，未将其列作通过项 |
| tx-verifier.png / .pdf / plot.py | 可复现的统计图；不混合新旧校验口径 |

DP/DV 的生产源码是 main；FP/FV/FV2 的生产源码是同一个先前小包优化 F。DP/FP 方法为 `outlined_page_verify_v1`，DV/FV/FV2 为 `portable_full_scan_v1`。每次都比较全部字节、累加实际数据 checksum，均在 work timer 内；未采样的 TX 峰值为 null，warmup 与 retained 是账本快照，不能解释为 allocator/RSS 峰值。

Linux 最终结果：253 库测试通过、6 ignored；集成1+2+1通过；probe5通过；显式256流压力测试1通过；Python30通过；无默认特性构建通过。本地 Rust1.94.1 fmt/core clippy/无默认特性/probe检查通过。探索性 `-D warnings` 把已有 style warnings 当作错误；本地全 Python 套件受 Linux/root 前提限制失败，日志保留；Linux正式全套已通过。本轮未新增或重测网络性能结果。

## 定位与未采用实验

这些样本全部保留，不纳入最终两轮的合并统计。

| tag / 文件 | 对照目的 |
|---|---|
| baseline | 原冻结 D/P1/F/F2，重现大块回退和小包收益 |
| crossover | D/F 与仅 outlining 的 DO/FO；outlining 单独未解决 |
| aligned / page | 校验函数64字节/4096字节对齐；对齐单独未解决 |
| phases / breakdown | push/verify/consume 及比较/checksum 的插入时钟定位实验 |
| profile-pairs.* / profile-summary.json | 6个完整排列区组的 cpu-clock:u 配对采样；绝对符号成本估算受频率/采样干扰，不能替代正式未插入时钟的结果 |
| layout.gdb / layout-*.log | 只调试自己的冻结 probe，记录 warmup/首轮指针、长度和布局；不作为正式计时 |
| storage-pilot | G/G2：生产 backing64字节对齐，未改善大块且无缓存小包明显变慢，已撤回 |
| cache / cache-page | H/HC/HP：Budget身份缓存替代扩大的LevelUse，旧比较回退仍在，已撤回 |
| J-pilot / layout-phases | J/JP/JPH：补齐无缓存路径的缓存填充，旧比较回退仍在，已撤回 |
| budget-cache-H.rs / budget-cache-J.rs / buf-aligned-rejected.rs | 上述候选源码快照；实际编译输入以 inputs.tar.gz 为准 |

原始 perf .data 和几个完整程序反汇编留在远端；本地保留全部 JSONL、所需函数反汇编及报告。远端 SHA 清单还包含两个未执行的准备文件，不代表新增测试。当前报告与原始数据之外的本地产物由 `local-evidence-sha256.json` 覆盖。

## 复现统计

在本目录的副本中运行，避免覆盖已有派生统计：

```sh
python3 summarize.py baseline crossover aligned page storage-pilot cache cache-page J-pilot fullscan-crossover fullscan-followup
python3 validate.py baseline crossover aligned page storage-pilot cache cache-page J-pilot fullscan-crossover fullscan-followup
python3 plot.py  # 需要 matplotlib；与基准执行无关
```

逐对 CPU 百分比为 `100 * (candidate_ns_per_op / baseline_ns_per_op - 1)`。每种方法各算 F/main；不把不同方法直接作为一对，不混合长短工作量窗口。4 variants 的24区组包含全部24排列；3 variants 的12区组包含两轮6排列，位置平衡；case顺序另行交错。每 case/variant 一次 warmup 从统计排除。

## 复跑第二台冻结二进制

`run_micro.py` 输出使用独占新建，以下生成新的 tag，不覆盖原始记录。运行期间保持无并行构建、其他 probe 或采样任务。

```sh
ssh -p 22100 root@38.207.178.131
python3 - <<'PY'
from pathlib import Path
import json,time,subprocess
r=Path('/root/zftxmigrate22-20261015')
c=json.loads((r/'fullscan-crossover-config.json').read_text())
c['tag']='repro-fullscan-'+str(int(time.time()))
p=r/(c['tag']+'-config.json');p.write_text(json.dumps(c,indent=2))
subprocess.run(['python3',str(r/'run_micro.py'),str(p)],check=True)
PY
```

`run_micro.py` 固定 CPU3，并在每次运行后验证返回码、ok、checksum 和最终账本。配置里给出冻结二进制完整路径，meta记录SHA、排列、种子和工作量。

## 从冻结输入重新构建

使用全新目录，不覆盖远端黄金二进制。`inputs.tar.gz` 含统一冻结的 Cargo.lock/manifest/bench；这些输入必须保持一致。

```sh
experiment_root=/root/zf22-rebuild-$(date +%s)
mkdir "$experiment_root"
tar xzf /root/zftxmigrate22-20261015/inputs.tar.gz -C "$experiment_root"
export PATH=/root/.cargo/bin:$PATH
export RUSTUP_HOME=/root/zfverify22-20261015/rustup
export CARGO_PROFILE_RELEASE_LTO=fat CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1
export CARGO_PROFILE_RELEASE_PANIC=abort CARGO_INCREMENTAL=0 CARGO_BUILD_JOBS=1
for variant in DV FV FV2; do
  (cd "$experiment_root/$variant" && CARGO_TARGET_DIR="$experiment_root/target-$variant" cargo +1.92.0 build --release --features test-peer --example pareto_probe)
done
```

若使用新构建，复制配置并同时更新所有 binary 路径与新tag；不能把新二进制接到旧metadata下。`checks.sh` 保存最终Linux检查命令，其 test target 与性能 target 分离。没有修改默认 Rust/toolchain、governor、IRQ 或线上服务。
