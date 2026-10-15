# 持续验证与证据门禁

这套入口把源码、测试范围、原生平台、配置和实际执行记录绑定到不可变计划。
测试代码和工具位于 `qa/`、`harness/`、`qa-models/`、`fuzz/` 及 `cfg(test)` 模块中；
生产默认参数、发送调度、预算算法和运行时依赖保持原样。

## 本地执行

使用 Python 3.12、Rust 1.94.1。证据应在干净的 Git 提交上生成；未提交源码会被门禁拒绝。
先安装一次工具，再冻结计划。以下输出目录必须不存在，重跑应使用新目录，不能覆盖失败证据。

```sh
python3.12 -m venv .qa-venv
.qa-venv/bin/python -m pip install -r qa/requirements.lock
rustup toolchain install 1.94.1 --profile minimal --component rustfmt --component clippy
.qa-venv/bin/python -m qa make-plan --suite portable --output qa/results/plan.json
.qa-venv/bin/python -m qa run-plan --plan qa/results/plan.json --output qa/results/local
.qa-venv/bin/python -m qa evaluate --plan qa/results/plan.json --results qa/results/local \
  --expected-sha "$(git rev-parse HEAD)" --output qa/results/summary.json --report qa/results/report.md
```

Linux 上可用 `--suite pr` 跑完整 PR 范围（包括 Linux Bench）。`portable` 按原生平台选择可用层；
Windows 保留核心和 Tokio Adapter，不把 Unix 台架的交叉编译冒充运行证据。
`--case CASE_ID` 可选择执行子集，但聚合器仍按照原计划检查完整必需集合，局部测试不能变成完整通过。

其他独立入口：

```sh
cargo +1.94.1 test --locked --release -p zfstack --features tokio,test-peer
cargo +1.94.1 test --locked --release --manifest-path qa-models/Cargo.toml
cargo +1.94.1 run --locked --release -p zfharness -- l0 --scenario matrix --app-drive event-only --seed 1 --full
cargo +1.94.1 run --locked --release -p zfharness -- replay --script harness/regressions/sink-recovery-v1.json
python3.12 -m qa audit-coverage
```

## 结果含义

| 字段 | 含义 |
|---|---|
| `validity.functional / performance` | 分别记录功能和性能证据是否有效；污染性能不删除功能失败。 |
| `functional` | `PASS / FAIL / NOT_RUN`；必须有实际执行、非空断言和符合配置的场景。 |
| `performance` | `PASS / FAIL / INCONCLUSIVE / NOT_APPLICABLE`；未校准只供观察。 |
| 最终 `PASS` | 本计划全部必需证据满足策略；不代表计划外平台或性能资格。 |
| 最终 `FAIL` | 实际观察到未豁免的契约失败；基线同样失败也不会豁免。 |
| 最终 `BLOCKED` | 缺测、取消、未知环境、身份错误、清理未完成或必要统计证据不足。 |

`evaluate` 的退出码分别是 0、1、2。数据损坏、重复结果、零测试、错误 seed、错用 toolchain/profile、
外平台结果、过期豁免、失租约和缺少子进程记录都会阻断。报告重新检查原始证据，不能上传一个 `ok: true`
或 `validated: true` 就获得资格。安全性断言不允许豁免。`known-failures.json` 默认没有有效豁免。

`QA-coverage` 将 Rust 手写测试入口、Python 测试方法及相关条件/跳过属性与受审查的
`policy/test-inventory.json` 比对。纯新增测试自动接受：受审查条目必须逐字保留，新增条目以 `additions`
列入覆盖证据，由可信 reporter 复核；新增 `#[ignore]`（含 `cfg_attr(..., ignore)`）、带 Python
skip/skipIf/skipUnless/xfail/expectedFailure 装饰器（含所在类的装饰器）或在 `if`/`try` 下条件定义的测试仍需登记。文件只能新增 `cfg(test)` 或其新增测试自带的
cfg。删除、改名、改动既有测试的属性/跳过条件，或新增可能隐藏测试的 cfg，仍需显式更新清单并审查；
`python -m qa.coverage --output /tmp/test-inventory-candidate.json` 只生成待审查版本，audit 不自动接受这类漂移。
合入此类清单变更后，维护者将 `QA_TRUSTED_REF` 更新为新的 main 提交：`gh variable set QA_TRUSTED_REF --body "$(git rev-parse origin/main)"`。
清单是源码范围检查，不能替代实际执行；Rust 宏生成的测试仍由运行记录验证。
`QA-features` 同时检查无默认 feature 的核心依赖图及启用 Tokio 的生产依赖图，确保 dev-only `test-util` 不泄漏。

每个 case 保存构建清单、环境清单、命令日志、完整生命周期、原始场景输出和 v2 结果。
日志、JSON 大小、子进程时限和归档量均有上限。Linux 使用独立 subreaper 清理被收养的 detached 后代；
Windows 使用 Job Object；macOS 的 process-group 范围及 detached 限制写进结果。

## Linux 集成节点

L1/L2 仅接受明确指定的 Linux 测试节点，并持有同一物理域的排他租约。
`qa/isolate-linux.sh` 创建私有 mount、network、PID namespace，私有 `/run` 隐藏宿主 systemd/dbus socket，
所有 TUN、WG、nft、netem 和命名空间注册都在隔离域内。`L2_ISOLATED=1` 经验证后才跳过 watcher/systemd 管理，
`RIG_ROOT` 也使用本次独立目录；原有手动 `run-rig.sh` 模式保持兼容。

节点清单示例（路径和 generation 必须由实际准备结果替换）：

```json
{
  "kind": "resettable-test-node",
  "isolated": true,
  "physical_domain": "qa-linux-node-01",
  "generation": "provisioned-generation-001",
  "rig_lease_held": true,
  "rig_path": "/srv/qa/frozen/zfc-rig",
  "rig_build": "/srv/qa/fixtures/build",
  "worker_binary": "/srv/qa/build/head-head/zf-worker",
  "worker_manifest": "/srv/qa/build/head-head/worker-manifest.json",
  "forwarder_binary": "/srv/qa/build/head-head/forwarder-server"
}
```

```sh
python3.12 -m qa make-plan --suite nightly --platform linux-x86_64 --output plan.json
python3.12 -m qa run-plan --plan plan.json --case C11-l1 --case C16-l2 \
  --case C16-l2-relay --case C16-l2-queue-control \
  --node node.json --lease-dir /var/lock/zfstack-qa --output qa/results/integration-001
```

需要预授予的 root/CAP_NET_ADMIN、TUN、`ip`、`tc`、`wg`、`nft`、`unshare`，以及独立的 rig fixtures。
已安装的生产 worker 不能替代本次源码构建；worker 清单必须证明源码、依赖锁、实际工具链、profile、
features、构建完成状态和二进制哈希。首次功能验证可用固定 `fast-release` profile；它不提供生产性能资格。
不同 profile 或 toolchain 不能伪装成相同构建。L2 仍会检测启动期 UDP 丢失，#43 的历史归因不会使失败变绿。

跨仓构建用 `python3.12 -m qa.crossrepo archive --help` 和 `build --help`。
`build` 和公开 `build_matrix(..., node=..., lease_dir=...)` 均要求固定的节点 JSON 与绝对公共租约目录；
CLI 使用 `--node /absolute/node.json --lease-dir /absolute/shared-leases`。节点至少包含非空
`physical_domain`、`generation`，必须与 `qa.run` / `qa.perf_run` 测量端使用同一个域标识和租约目录。
整个矩阵（包括 source 校验、worker/forwarder/fakectl 构建）持有一次 `qa.lease.Lease`，每个受控子进程
同时轮询租约和磁盘余量。同域测量占锁时不会启动任何编译命令，也不会创建构建输出；失锁会取消本次子树，
保留命令退出/清理记录，将矩阵标记失败并隔离节点，不删除或重写外来锁。不存在隐式无锁或 stale TTL 回收模式。
最终 worker/matrix 清单在校验并释放租约后封存，记录 `build_lease` 的域、generation、owner 与释放状态。
每条命令和每臂结束时另写 `build-progress.json`，保留原始退出及 cleanup 记录；其状态与租约释放状态始终为
`PENDING`，取消后仍可诊断，但不能替代正式 worker/matrix 终态清单或作为已构建资格。
入口从指定 Git 提交导出允许的构建输入，不复制 dirty/untracked 内容、凭据或历史。
四个组合固定同一 private_tun 版本；依赖 path override 和锁文件变化都会保留。
`--cell head-head` 只验证单个组合，另外三臂是 `UNEXECUTED`，不宣称四因子对照通过。
可用 `--cache-dir /absolute/cache` 复用允许的 Cargo 编译缓存；缓存必须位于归档与输出目录之外，
每次仍重新执行受限构建，并独立复制、哈希和核对 worker 的构建身份。
使用 `build --forwarder` 同时构建固定源码的 `forwarder-server`，并在节点清单提供其绝对路径。
`C16-l2` 验证 WG 入站加 Direct 出口；`C16-l2-relay` 单独验证 worker → private_tun forwarder → target，
保留转发器构建身份、真实业务端口路径计数及旁路检查。健康探测使用不同端口，不能充当业务路径证据。
两条路径的执行证据不能互相替代，缺失二进制或路径证据会明确阻断。

**当前状态：known-BLOCKED（台架 `tc` 删除非原子竞态）**。该标注不是豁免，不跳过 required case 或改变阈值；真实 NOT_RUN/BLOCKED 继续保留。后续方案单列为 [issue 草稿](issues/queue-control-atomic-restoration.md)，本 PR 不实现。维护者启用事项见 [手工清单](MAINTAINER-CHECKLIST.md)。

`C16-l2-queue-control` 在同一租约内依次运行干净基线和测试侧的 500 ms 深出口队列注入。
它要求基线服务合同通过、注入期间载荷仍正确但延迟合同失败、队列确实积压且没有丢包，并在 recovery 前恢复。
250 ms 是该负对照的固定检测阈值，不是产品 SLO，也不表示复现了 #44 的具体实现缺陷。
每个 peer 的完整请求计数和排队延迟均参与判断；没有样本、发生器漏供给或基线失败不能算负对照成功。

## 性能与发布

完整用法见 [性能入口](PERFORMANCE.md)、[统计与 A/A](../bench/pareto/README.md) 和 [发布治理](RELEASE.md)。
性能实验只执行提前构建的二进制，冻结 ABBA/BAAB blocks、主指标、预算、方法和终点，失败 slot 不补跑。
同一物理域上的构建与测量不能并发。共享宿主、缺失 cgroup 证据、精度不足等均保留原因；安静的一次采样不能证明独占。

默认策略是 shadow，校准注册表为空。正式 enforce 需要同二进制和独立同源构建的 A/A、真实注入负对照、
独立 held-out 会话、误报/检出能力评估，以及经审查的产品预算和 SLO。不能用合成单测代替节点校准，
也不能为了得到绿色结果扩大阈值。发布汇总还要求四个原生平台、L1/L2、安全及完整校准原始证据。

## 工作流与接入

| 工作流 | 范围 |
|---|---|
| `ci.yml` | PR、push、merge_group；完整基础 Rust/Python、显式 eager/event-only、永久 replay；唯一聚合 `qa/gate`。 |
| `qa-nightly.yml` | 完整固定 seeds、已登记重测试、Loom、隔离 L1、指定节点 L2。 |
| `qa-native.yml` | Linux x86_64/aarch64、macOS aarch64、Windows x86_64 原生可用层。 |
| `qa-safety.yml` | 固定 nightly 的 Miri、ASan、覆盖引导 fuzz 与局部 Loom。 |
| `qa-perf.yml` | 受保护默认分支历史上的固定 SHA，指定物理域性能节点，默认 shadow。 |
| `qa-release.yml` | 独立复算上述原始证据，输出待审查 baseline 候选。 |
| `qa-dependency-upgrade.yml` | 显式更新锁文件、验证并产生待审查补丁，不自动合并。 |

仓库首次引入 QA 时，旧基线没有可信 reporter，PR 执行仍可开展，聚合会明确 BLOCKED。
维护者需审查后配置完整提交 SHA `QA_TRUSTED_REF`，并通过仓库规则实际要求 `qa/gate` 和 CODEOWNERS 审查。
L2/perf 自托管工作流还需要节点环境与 `QA_L2_ENABLED` / `QA_PERF_ENABLED`。
这些外部设置不会由本地代码悄悄启用。缺少节点或批准的校准时，发布入口保持阻断。

## 方案对应关系

| 设计步骤 | 实现位置 |
|---|---|
| PR-01 证据与身份 | `schema/`、`plan.py`、`build_manifest.py`、`policy/`、三个独立 Cargo.lock。 |
| PR-02 L2 可信度 | `harness/l2/evidence.py`、`load_contract.py`、`compare.py`、`export_case.py`。 |
| PR-03 严格聚合 | `run.py`、`evaluate.py`、`workflow.py`、PR 工作流和伪通过反例。 |
| PR-04 L0-E | `harness/src/{schedule,event,oracle,world}.rs`，基于完整 ConnId 的就绪集。 |
| PR-05/06 A0/A1 | `src/{budget,tokio_adapter}/tests/qa.rs`、`qa-models/`。 |
| PR-07 探索与语料 | `fuzz/`、事件缩减器、`harness/regressions/`，覆盖引导和 seed 重放分开。 |
| PR-08/09 性能实验 | `bench/pareto/{freeze_plan,run_plan,calibrate,perf_gate}.py`，`perf_run.py`。 |
| PR-10 集成与归因 | `integration.py`、`crossrepo.py`、隔离 wrapper、L1/L2 运行与完整负载身份。 |
| PR-11 恢复与安全 | capacity-recovery、最后 owner 释放、局部 Loom、fuzz/Miri/ASan、原生平台工作流。 |
| PR-12 发布治理 | `release.py`、`baseline.py`、可信 reporter、allowlist 归档、CODEOWNERS。 |

覆盖目录声明需要保护的合同，不等于每层每种规模已经实测。Loom 模型只验证映射出的状态转换，不能称为生产并发的穷尽证明。
最终资格始终由具体提交、具体计划和原始证据决定；实现完成与 G4/G5 的实际节点/产品资格分开记录。

本次落地的实际执行 SHA、原始证据、失败诊断和未取得的资格见 [实现与验证记录](VALIDATION.md)。
