# PR #92: real Linux TUN A/A and A/B follow-up — 2026-10-18

## Verdict

**Functional / correctness: PASS in every exercised TUN case.** **Performance non-inferiority: NOT ESTABLISHED. Do not claim that the VecDeque borrow/return overhead is absent.** This is not a confirmed production regression either: the measurements are not adequately isolated and the independent baseline A/A has substantial noise.

- Baseline source: `f467635a032e0c5ace36536d348263d8f47ee1d0` (main immediately before PR #92).
- Candidate: `6e8333e30e3a4fc7fc1a69dac7e2551e77c38ab0`, PR #92 after P1/P2 hot-tail/LIFO correctness repair. The locally built code has the same production tree hash `d76f9433d2dc0a5d84903246511a18b33f2f144f`.
- Comparison is scoped to this single PR, **not** total issue #23 resolution or an actual zfc/WireGuard production deployment.
- Both source variants build `zfbench` in separate worktrees using Rust 1.95.0, `--locked --release`, `CARGO_PROFILE_RELEASE_LTO=fat`, codegen-units=1, panic=abort and the `packet-pool-idle-trim` feature. The third, independently compiled binary is built from the **same baseline source tree** for A/A.
- Binaries' SHA256: baseline `6562b124f8ee0dd0ec30bd2908ec37a76f78a709271bdfc945a15f8cd586d125`, independent baseline A/A `8f8e74be0bc4a309eaeca97f4a59475f3a16026c9fa77abd2c05affef0f8790a`, candidate `69eeea4c120f46ad02b024e05870144e238d5489ac7869fa3fc2537dfdba5bfc`.
- Linux 6.1.0-41-amd64, Intel Xeon E5-1630 v4, 8 logical CPUs, governor `schedutil`. Proxy bound to CPUs 0,1 and Python load driver to 2,3,4,5; this is **CPU affinity, not exclusive physical-core isolation**. Other build processes still ran on the host.
- Real kernel TCP ↔ Linux TUN ↔ zfstack proxy ↔ kernel TCP upstream. Proxy: client-profile, splice, vnet_hdr, dedicated driver thread, one worker. No WireGuard encryption, WAN, physical NIC performance or netem emulation. Throughput can be limited by Python application endpoints.
- Two formal scenarios × **12 alternating A/B pairs + 4 independent A/A pairs + 1 warmup pair** = **68 independent processes**. Each creates, checks and removes its own network namespace and interfaces. **68/68 passed**. The A/B pairs always preserve source identity and use the same workload. No outcome-based sample exclusions.
- Plus **four** non-performance diagnostic runs: baseline/fixed × fanout/noisy four-peer modes (all passed), for **72/72 valid TUN executions**.
- Source results stored under `/root/work/zfstack-pr92-tun-ab-evidence-20261018` on the owned SSH development host. Full original records, payload-check results, counter snapshots, per-request RTT observations, process CPU and network namespace cleanup logs total ~30 MiB. For reviewability this PR retains a compact **68-row** `real_tun_compact_evidence.json`, both exact comparison scripts and the statistics; see file identity checks in the dataset.

## A/B versus independent A/A

The table shows the **median of 12 per-pair percentages** and descriptive paired-block bootstrap 95% intervals (20,000 fixed-seed resamples). Independent A/A comes from **four** pairs of baseline vs separately compiled baseline. It is a noise control, not a formal proof of hardware isolation or statistical equivalence.

| Load/metric | A/B median [95% CI] | Independent A/A median [95% CI] | Observations |
|---|---:|---:|---|
| MTU1500 mixed, whole proxy CPU seconds / application GB | **+0.20% [-1.45%, +2.46%]** | +7.61% [-1.00%, +15.09%] | No reliable CPU non-inferiority conclusion |
| MTU1500 mixed, download goodput | -0.60% [-6.71%, +5.75%] | -5.29% [-7.74%, +0.34%] | Throughput interval extends beyond -5% |
| MTU1500 mixed, upload goodput | -9.65% [-25.34%, -3.90%] | -15.50% [-18.06%, -3.02%] | Nominal decline, but independent A/A shows comparable decline |
| MTU1500 mixed, loaded download RR P99 | +11.65% [-10.78%, +65.75%] | +14.15% [-2.64%, +273.49%] | Too noisy for +5% P99 qualification |
| MTU1500 mixed, loaded upload RR P99 | +6.20% [-46.84%, +73.39%] | +61.32% [-7.44%, +237.85%] | Too noisy |
| MTU1500, sampled proxy peak RSS | -4.76% [-19.29%, +10.75%] | +22.18% [+11.41%, +84.63%] | RSS samples are not full allocator retention or memory census |
| MTU512 high PPS, whole proxy CPU seconds / application GB | **+3.91% [-1.08%, +6.46%]** | +1.01% [-9.25%, +3.49%] | Neither clear regression nor +2% CPU non-inferiority |
| MTU512 high PPS, download goodput | -1.86% [-5.05%, +0.77%] | +0.58% [-3.06%, +19.40%] | Marginally crosses -5% |
| MTU512 high PPS, upload goodput | -7.37% [-19.84%, +10.51%] | +18.62% [-24.20%, +36.47%] | Both A/B and A/A unstable |
| MTU512 high PPS, loaded download RR P99 | +8.85% [-3.49%, +35.77%] | -2.09% [-35.75%, +269.01%] | Too noisy |
| MTU512 high PPS, loaded upload RR P99 | +0.49% [-28.59%, +6.82%] | +1.76% [-1.88%, +26.30%] | P99 only available in 11/12 A/B pairs |
| MTU512, sampled proxy peak RSS | +2.17% [-7.02%, +8.27%] | +2.47% [-14.37%, +9.99%] | No stable memory change established |

Positive values mean a higher numerical value in the candidate; for CPU/GB, P99 and RSS, this would be worse. Confidence intervals are descriptive with a small number of independent host windows; notably, raw P99 point estimators can be based on too few loaded requests for robust tails. Do **not** treat these as formal hypothesis tests. For context, the open issue requests CPU non-inferiority +2%, goodput -5%, P99 +5%; **these thresholds were not proven**.

### Noise and provenance

- The server is shared with other active zfc builds/tests. For **10/12 A/B pairs in each formal scenario**, process snapshots at one or both run boundaries detected active `cargo`/`rustc` tasks. Compile status is in every row and was *not* used to drop or cherry-pick results.
- Dynamic CPU frequency, SMT co-tenants, background services, independent binary builds and Python endpoint throughput limit detection of small changes. This is why an apparent +3.91% CPU/GB in high PPS is **a concern, not an established production regression**.
- No successful case was excluded, no parameters or thresholds were changed mid-run, and independent baseline A/A observations were retained with their drift.
- The optional PacketPool trim was enabled for MTU1500 with idle 1000ms/target 2; disabled for MTU512 to isolate the common borrow/return path. Both workloads use `idle_secs=2`, 8 flows, mixed 64-byte echo probes while bidirectional bulk traffic is active, 160 RR samples per declared unloaded payload size, and 64 MiB/flow (1500) or 32 MiB/flow (512). Complete arguments are encoded in the frozen plan and row-level metadata.

## Four-peer TUN diagnostics (diagnostic only, no A/B performance claims)

| Peer pattern | Baseline | Corrected PR |
|---|---|---|
| `fanout` (four source-mapped traffic identities) | PASS | PASS |
| `noisy` (mixed activity across four source-mapped identities) | PASS | PASS |

In all four cases, the runner verified traffic and physical quota samples for peer IDs 1–4, no fallback egress to reader peer 0, complete payloads, idle connection progress, **two network-silent quiet windows**, and complete TUN/netns cleanup. All four ended with PacketPool `cached=2`, as configured. The four peer IDs are **diagnostic source identities**, not authenticated users; this does not demonstrate end-to-end zfc fairness or WireGuard interoperability.

## Merge guidance

1. **P1/P2 retention/LIFO bug**: fixed, independently code-reviewed, covered by deterministic tests and 319 Tokio library tests with 6 ignored. The TUN scenarios did not uncover a correctness regression.
2. **Real network CPU/GB, P99 and throughput**: unknown at +1–2% sensitivity; A/A noise and host compiler interference block a strict non-inferiority claim.
3. **Do not merge solely on these measurements.** Re-run the same source/binaries on an exclusive calibrated physical host or with an explicit performance-tradeoff decision. The existing 12-pair results should remain as negative/inconclusive evidence; do not silently replace them with selectively quiet windows.
4. No production code changes were made during this measurement. Issue #23 remains open for the wider automatic idle decay strategy, physical memory accounting and real WireGuard validation.

## Reproduction and data

- `real_tun_run_case_pairs.py`: owner-namespace runner orchestration, immutable binary SHA checks, paired source definitions, alternating A/B with interleaved A/A, environment/compiler snapshots; accepts explicit `--base`, `--fixed`, `--aa`, `--runner`, `--loader` and output paths.
- `real_tun_analyze.py`: rejects invalid cases, inconsistent source/case IDs and missing cleanup, reproduces descriptive block summaries and conditional non-inferiority warnings.
- `real_tun_compact_evidence.json`: all 68 compact case rows (source and binary IDs, stage, sample CPU/GB, goodput, RR P99, RSS, compiler-load evidence) plus the four multi-peer diagnostic summaries and full A/A & A/B statistical results.
- Full per-run files and provenance remain at `/root/work/zfstack-pr92-tun-ab-evidence-20261018/{formal-mixed1500-12pairs,formal-highpps512-12pairs,diagnostic-four-peers-retry}`. The compact evidence preserves hashes to verify the original full output without publishing ~30MB of per-packet/request details in Git.

This report is a **performance evidence follow-up**, not a performance approval or authorization to merge.
