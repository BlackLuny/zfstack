# Diagnose and remove upstream backlog failures from the 64-flow TUN harness

The confirmed reset source is the **benchmark upstream listener**, not a newly identified zfstack TCP bug. Python's `socketserver.ThreadingTCPServer` defaults to a listen backlog of five, while this harness supports bursts of 64 bulk connections plus light requests. We reproduced upstream SYN retries and resets even with zfstack and TUN completely removed.

The fix sets `Upstream.request_queue_size = 2 * MAX_FLOWS` (128), records it in loader metadata, and tests admission of the entire supported connection wave before any accept/handler is scheduled. It does not change socket timeouts, discard failures, retry failed workloads, relax payload checks, or modify core TCP/budget behavior. Benchmark diagnostics additionally expose close/abort/waiter/tail details when sampling is requested, and report splice errors that were previously ignored.

This follows [#23](https://github.com/BlackLuny/zfstack/issues/23) and the unresolved 64-flow failures in the [PR #31 evidence](../2026-10-13-pr31-review/README.md). It is independent of the PacketPool implementation in [PR #33](https://github.com/BlackLuny/zfstack/pull/33).

## Evidence chain

1. **Latest-main reproduction.** With the original loader, main `b170aff` failed in the 64-flow upload phase at MTU 1500 with an 8 MiB global / 4 MiB port / 4 MiB peer budget. Five other initial cases passed, establishing timing sensitivity rather than a universal failure.
2. **Trace reproduction.** A diagnostic build with unchanged core code also failed. Capture covered both loopback upstream and TUN. Four RSTs originated from `127.0.0.1:5201`, then four corresponding resets appeared from the proxy's TUN address. The first observed upstream reset preceded its TUN propagation by **251 µs**. Each failed upstream connection had already retried its SYN after approximately one second. The proxy logged upstream `Connection reset by peer`, while no upstream Python handler error was reported for those connections.
3. **Remove the stack.** Run the same workload against the Python upstream directly over loopback: four 64-flow downloads and four uploads. With backlog five, **11/512 flows reset** across three upload bursts; with backlog 128, **512/512 flows completed and verified**. This control contains no zfstack, TUN, pool maintenance, or stack budget.
4. **Deterministic regression.** Do not run `serve_forever` or schedule a handler; try to connect the supported 64-client wave. The old backlog admits six and the seventh times out. The corrected listener admits all 64. The test fails under the old setting and passes with the fix.
5. **Unchanged binaries, corrected loader.** Repeat the pressure matrix against the original main executable and the original PR #33 executable. **24/24 cases pass**, with **6 GiB** of bulk payload verified plus RR/idle liveness exchanges. No reset or timeout recurred in this matrix.

The trace capture reports zero kernel drops; all capture counters are preserved. `tcpdump` captured 154,722 packets and reported 176,299 received by the filter at shutdown, so this report does not claim that the capture is an exhaustive record of every packet. The causal conclusion combines the observed reset ordering with the independent direct-kernel control and backlog-only intervention.

## Fixed-loader pressure matrix

Every case uses 64 flows, both upload/download, an initial burst and a reburst, 1 MiB per flow/direction/burst, 100 RR samples for each of 64/512/1400/4096 bytes, concurrent 1-ms light requests, and two seconds of quiet. The original **10-second socket operation timeout** and 240-second case limit are unchanged.

| MTU | Global / port / peer limit | Established idle connections | Main, policies disabled | PR #33, policies enabled |
|---|---|---:|---:|---:|
| 1500 | 8 / 4 / 4 MiB | 0 | 3/3 | 3/3 |
| 512 | 8 / 4 / 4 MiB | 0 | 3/3 | 3/3 |
| 1500 | 64 / 32 / 32 MiB | 1000 | 3/3 | 3/3 |
| 512 | 64 / 32 / 32 MiB | 1000 | 3/3 | 3/3 |

The opt-in side uses TX check/warm/cold 1/3/10 seconds with floors 2/2 and PacketPool observation window 3 seconds, target zero. Each run remains a diagnostic, not a performance measurement. Four earlier backlog-128 TUN probes also passed, for **28/28 fixed-backlog TUN cases** in this archive. Across all 36 TUN cases, the two original-backlog failures are retained; 34 passed. Every case passed process/namespace cleanup.

Both the default and opt-in implementations work under the corrected harness in these scenarios. This is not an isolated comparison of the two pool policies, since the main side deliberately disables them and the PR #33 side enables both. No speedup or formal performance non-inferiority is inferred. Earlier measurements made with the backlog-five loader remain historical evidence; comparisons must use the same corrected loader on both sides.

## Checks

- `bench/pareto` Python regressions: **15/15**, including the new real-listener admission regression.
- `harness/l2` Python regressions: **6/6**.
- zfbench release/all-features unit tests: **16/16**.
- fmt, zfbench all-targets/all-features clippy and `git diff --check` passed; existing warnings remain.
- The old-backlog negative control is recorded separately as an expected failed regression, not counted as a passing test.
- `analyze.py` checks captured RST ordering, direct-control failures, loader backlog, the complete 24-case grid, each case's four 64-flow payload phases, and cleanup. The archived source and raw commands permit independent analysis.

## Provenance and reproduction

Fix checkpoint: `47de359` (later commits add this report/evidence only). Core Rust source remains main `b170affa14e70d73f3d0834b2e06579dde70ca85`.

The matrix reuses independently built, frozen executables from the previous task:

- main: `b170affa14e70d73f3d0834b2e06579dde70ca85`;
- PacketPool: executable checkpoint `db1f6a50a6cb26b5185a0d9ece6c602e2a005e43` from PR #33;
- trace: main plus `trace-proxy.patch`, containing only benchmark diagnostics and error logging. Its declared source SHA in raw runner metadata identifies the base; the patch and binary hash identify the diagnostic build.

All three binaries use Rust 1.94.1, the same Cargo.lock, release/fat-LTO/codegen-units=1/panic=abort, and `packet-pool-idle-trim`. `build-manifest.json` records hashes. The shared VM has a four-CPU cgroup quota and five exposed CPUs; TUN cases pin the proxy to CPUs 0,1 and the loader to 2,3,4. The dedicated Docker rig never uses host networking, and each TUN case owns a fresh netns. Direct controls run only against loopback inside that rig.

`raw-evidence.tar.gz` retains all 36 TUN cases, direct-kernel logs, two diagnostic captures, original/fixed diagnostic loaders, exact execution wrappers, the trace patch, build identity and test logs. Executables are represented by hashes, not bundled. The trace runner is an archived diagnostic copy of the normal runner: capture interface changed from TUN to `any`, snaplen from unlimited to 96, and capture buffer to 4096 KiB; teardown/ownership rules remain intact. The traced loader only adds error/flow diagnostics; the backlog intervention then changes that setting alone.

To reanalyze, extract into a fresh directory and run:

```sh
python3 analyze.py EXTRACTED_DIRECTORY
```

This recomputes `summary.json`. `RAW_SHA256SUMS` covers archive files; the outer `SHA256SUMS` covers committed report artifacts.

To rerun, use the [existing TUN rig Dockerfile](../2026-10-13-pr31-review/Dockerfile) with the managed proxy CA, a dedicated `zfstack-pressure-rig` container with TUN/netns privileges, `--ulimit nofile=65536:65536`, this repository read-only at `/repo`, a fresh evidence directory at `/evidence`, and the host `/etc/machine-id` read-only. Ensure `/dev/net/tun` exists at runtime. Copy the frozen binaries into `bin/main` and `bin/packetpool`; reconstruct the trace executable using the archived patch if repeating tracing. Copy `case_trace.py`, `trace_load.py`, and `direct_upstream.py` from the archive into the fresh evidence directory. Set `ZF_PRESSURE_EVIDENCE` to that directory on the host. Run `run_confirm.py` for the 24-case fixed-loader grid. For direct controls, run `/evidence/direct_upstream.py` and then `/evidence/direct_upstream.py 128` inside the container, capturing stdout/stderr separately. The original wrappers and every actual command used for this report are in the archive; do not reuse their output directories.

## Remaining scope

This resolves a demonstrated **upstream-listener confounder** behind 64-flow resets and restores a valid pressure test. The historical isolated MTU-512 timeout has no comparable two-sided trace, so it is not retroactively assigned the same cause. It did not recur in the corrected matrix; a future recurrence must be investigated separately.

Real multi-peer isolation/fairness and zfc integration are still outstanding. This rig is one shard and `PeerId(0)` even when it has 64 active flows and 1000 idle connections. Neither pool is enabled by default by this change, and #23 is not closed.
