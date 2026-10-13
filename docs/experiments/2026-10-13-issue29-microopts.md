# Issue #29：adapter 小优化与汇编验证

本轮验收标准是行为等价，并在真实 release 汇编中确认减少指令或原子操作；不要求吞吐显著提高，也没有重跑吞吐对照。

## 改动

仅修改 `src/tokio_adapter.rs` 的生产代码：

1. `relay_pump` 借用 `Shared.driver_wake` 构造 Context，取消临时 Waker 的 clone/drop。
2. `pump` 取得 Shared 后调用 `relay_pump`，由后者的一次 `get_mut` 同时完成 relay 判定和取值，删除前置 `contains_key`。返回值表示“本轮处理的是 relay”，即使本轮结束并移除了 relay，也不会继续执行普通流的 pump。
3. 五处 Waker 登记共用 `register_waker`：已有值调用 `Waker::clone_from`，为空时 clone。`will_wake` 判定相同时保留已有值；换任务时仍替换。覆盖 read、write 的两个 Pending 分支、flush、shutdown。

队列锁范围、错误返回、FIN/RST、驱动端 take 后 wake 的逻辑不变。Shared 的 Arc 在调用者持有，因此 `finish_relay` 移除 map 条目后，借用的 Waker 仍有效。普通流与 relay 各自仍只读取一次当前时间；新建 relay 后重新读时间。`on_event(Closed)` 中用于判断是否仍需排空尾部的另一个 `contains_key` 保留。

## 构建条件

- 基线：`7d9b924e6eb4bca56468ac1fd5f864f343c4c59b`。
- 两份独立、冻结的源码快照；Cargo.lock SHA-256 均为 `ab2f5085f9a2fbd883e0da21708ba70a47a1ef45c872c16b8dbd56ce4ff1b0c4`。
- rustc **1.96.1 (31fca3adb 2026-06-26)**，LLVM **22.1.2**，**x86_64-unknown-linux-gnu**，相同容器镜像与默认 release 参数。
- 本机可用空间为 **9.3 GiB**，低于仓库的 10 GiB 编译下限，本轮编译与回归经 rc-agent 在远端 Linux 执行；本轮不声称验证了 ARM64 汇编或本机运行结果。
- 基线任务 `t-01M4SKJ2140F2NK54718EDX9DC`；修改版任务 `t-01M4SKMEPETXEZ3WP4AC2ZY777`。

两版使用相同命令，直接生成库与真实 zfbench Driver 实例的汇编，没有为优化证据另写简化模型：

```sh
cargo rustc -p zfstack --release --locked --features tokio -- --emit=asm
cargo rustc -p zfbench --release --locked -- --emit=asm
```

## 汇编证据

### Relay Waker：每轮减少两次原子 RMW

基线 `Driver::relay_pump` 从 Shared 读取 Waker 的 vtable/data，调用 clone 回调；退出时调用其 drop 回调。对应的 `alloc::task::raw_waker` 回调明确包含：

```asm
# clone_waker
lock incq -16(%rdi)
# drop_waker
lock decq -16(%rdi)
```

修改版直接将 Shared 内 Waker 的地址写入 Context，取消了上述 clone/drop 调用。每次处理已建立的 relay，确定减少 **1 次原子递增 + 1 次原子递减**。socket 就绪登记、内存等待者等自身需要的 Waker 克隆仍保留。

注意：Shared 的 Arc clone/drop 从 helper 移到了调用者，不能把它们算成每轮又少一组原子操作。

### Relay 分发：三次查表降为两次

沿“已建立 relay”控制流读取汇编，而非相加整个函数里的所有静态调用点：

| 路径 | 基线 | 修改版 |
| --- | --- | --- |
| `pump` | `relays.contains_key` | `streams.get` |
| `relay_pump` | `streams.get`、`relays.get_mut` | `relays.get_mut` |
| 合计 | 3 次 hash/查桶 | 2 次 hash/查桶 |

真实汇编对应 `BuildHasher::hash_one` 调用从 **3 → 2**，同时删除一次查桶循环。该编译版本的 `hash_one` 函数体本身有 **92 条指令**（含一次对 `DefaultHasher::write` 的调用和 ret，不含被调函数体）；这是被省去的一次 hash 的证据，不是整个 pump 净减少指令数的测量。

适用边界：没有 relay 时，普通流仍跳过 helper；混合场景的普通流会进入 helper 查询后返回 false，因此增加一次 helper 调用。此项优化针对已建立 relay，不宣称每种分发路径都减少指令。

### 同一 Waker 重复登记：跳过两个间接调用

以 `poll_flush` 为例，基线每次 Pending 都调用 clone，已有 slot 时再调用 drop。修改版先比较 data 与 vtable，完全相同时跳过两个调用：

```asm
movq %r14, %rdx
xorq %rdi, %rdx       # data
movq %r15, %rsi
xorq %rcx, %rsi       # vtable
movl $1, %eax
orq  %rdx, %rsi
je   .LBB496_32       # 相同：直接进入解锁/返回 Pending
callq *(%rcx)        # 不同：clone
# ...
callq *24(%r15)      # 不同：drop 旧值
```

read、write 的空间不足分支、shutdown 也有相同比较；write 的 quota 分支调用 `register_waker`，其内部有相同的跳过分支。五个登记点均已核对。

因此，在已登记且 `will_wake` 为真的路径，确定少两个 Waker 回调；对于 Arc 型 Waker，少一组原子引用计数加减。空 slot 和不同 Waker 仍 clone/替换。不能对任意自定义 RawWaker 声称固定的原子操作数，也不能把总汇编体积当作动态指令数下降的证据。

## 回归结果

```sh
cargo test -p zfstack --release --locked --features tokio,test-peer
cargo check -p zfstack --locked --no-default-features
```

- 单元测试：**162 passed，0 failed，3 ignored**，19.92 秒。
- 集成测试：**2 passed，0 failed**，0.82 秒；doc tests 为 0。
- 无默认 feature 的核心库 check：通过。
- 两项新测试通过：`pending_polls_keep_the_latest_task_waker`（重复登记、更换任务、take 后重新登记）；`ordinary_stream_progresses_while_a_relay_is_active`（relay 确实已活跃时，普通流仍收发并关闭）。
- 原有双向 splice、三种 MTU/offload 配置、双向 FIN 顺序、关闭后的上传尾部、上游 RST 等回归通过。
- 原有 ignored 测试 `bbr_probe_steady_state`、`bbr_trace`、`soak_paused_reader_lossy_multiflow` 未额外运行。
- `git diff --check` 通过。GitNexus 未收录 zfstack，impact/detect_changes 均不可用；已用源码直接核对两个 helper 调用点、五个登记点及关闭路径，并以针对性运行回归补证。

## 本地证据

原始证据保留在验证机器的 `bench/results/2026-10-13-issue29-microopts/`，不随本次代码和报告提交；以下为本地路径，不是仓库下载链接。本报告已包含验证命令、关键汇编片段和测试结果。

- `provenance.json`：源码、锁文件、汇编产物 SHA-256，构建任务、镜像及编译器版本。
- `candidate.patch`：接受验证的源码 diff。
- `candidate/regression.log`：完整测试输出。
- `baseline-asm/target/release/deps/`、`candidate/target/release/deps/`：原始 `.s`；`evidence/`：对应函数提取，保留原符号与分支标签。

结论：三项改动都有明确的路径级汇编依据，行为回归通过。它们是已验证的小优化；本轮没有新增端到端吞吐提升的证据。
