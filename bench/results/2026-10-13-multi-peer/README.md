# Four real peer identities over TUN: pressure isolation and TX cache decay

The TUN harness now supports four actual `PeerId`s in one shard. This advances [#23](https://github.com/BlackLuny/zfstack/issues/23) beyond its previous single-peer coverage: **16/16 pressure-matrix cases passed**, verifying **4 GiB**, followed by **4/4 longer cache-decay cases**. The initial pressure probe and an unchanged single-peer-mode compatibility case also passed. All 22 cases passed owned-process/namespace cleanup.

This is a diagnostic correctness and reclamation result. It does not establish bandwidth fairness, a latency non-regression threshold, multi-port/global-exhaustion recovery, or complete zfc WireGuard integration. Both cache policies remain disabled by default. Only TX trimming is enabled in the treatment cases; PacketPool automatic trimming is disabled, and the unmerged PR #33 implementation is not included.

## What changed

`ZFBENCH_TUN_PEERS=4` explicitly enables diagnostic source mapping: `172.19.1.N` maps to `PeerId(N)` for N=1..4. The owned namespace runner assigns these /32 source addresses to loopback, verifies each route traverses its TUN, and binds every workload socket to its declared source. Unknown IPv4 sources, IPv6 and truncated source headers are rejected by this diagnostic mapping; the stack retains its full packet/checksum validation. This is a local test convention, **not authenticated peer identity or a production ACL**.

Reader packet buffers remain charged to a separate `PeerId(0)`. Multi-peer ingress uses `ingress_borrowed_with`, so retained payload gets allocated against its actual traffic peer, rather than retaining a reader-owned buffer under the wrong identity. This follows the library's external shared-RX-pool contract. Default single-peer mode continues using `ingress_buf`.

Coarse opt-in samples report all five memory handles and egress counts returned by the stack. Admission checks require physical leases and egress from all four traffic peers, zero egress from reader peer 0, no sampled peer ceiling violation, complete bulk payload verification, and completed light requests overlapping every bulk phase. Source labels alone cannot pass. Regression tests reject absent peer egress, reader-peer fallback, excessive sampled usage and light requests that only complete after bulk ends.

This branch starts at current main `b170affa14e70d73f3d0834b2e06579dde70ca85` and includes the source-only upstream-backlog/diagnostic fix from [PR #34](https://github.com/BlackLuny/zfstack/pull/34), needed for valid 64-flow tests. It does not change TCP core or allocation policy.

## Pressure matrix

One shard, 64 bulk flows, 16 established idle connections (four per peer), both upload/download, initial burst plus reburst, 1 MiB per flow/direction/burst. Every case verifies 256 MiB. RR sizes are 64/512/1400/4096 bytes with 50 samples each; three simultaneous 64-byte light probes on peers 2–4 run at a 2 ms requested interval. Socket timeout remains 10 seconds and the case limit 240 seconds.

Budgets are **global 32 MiB / port 16 MiB / each peer 2 MiB**, including the reader's separate peer ceiling. TX treatment uses check/warm/cold 1/3/10 seconds and large/small warm floors 2/2.

| Scenario | MTU | TX policy disabled | TX policy enabled |
|---|---:|---:|---:|
| Fanout: 16 bulk flows per peer | 1500 | 2/2 | 2/2 |
| Fanout: 16 bulk flows per peer | 512 | 2/2 | 2/2 |
| Noisy peer: all 64 bulk flows on peer 1 | 1500 | 2/2 | 2/2 |
| Noisy peer: all 64 bulk flows on peer 1 | 512 | 2/2 | 2/2 |

In noisy-peer downloads, clients advertise a small receive window and pause reads for two seconds after sending the application header. This is deliberate backpressure; no data or timeout validation is relaxed. Uploads are also verified, without the artificial read pause.

All eight noisy cases demonstrably reached peer pressure: peer 1's sampled peak was **2,072,864–2,087,720 bytes**, or 98.8–99.5% of its 2 MiB ceiling. Each recorded **6,992–14,076 peer-level allocation refusals**, with zero global/port refusals. The other three peers peaked at 134,264 bytes each in these cases. Every light peer completed at least **32** requests entirely within each noisy bulk window; the corresponding minimum over the full matrix was 11. Every large flow eventually completed and matched its payload/receipt.

The matrix's largest sampled global/port usage was 7,182,162 / 7,047,122 bytes, below the configured limits. These budgets deliberately leave global/port room to isolate peer contention; this is **not a port/global exhaustion test**. Sampler reads are separate, not an atomic accounting snapshot or a proof of every instantaneous maximum.

The largest recorded mixed request RTT in the matrix was approximately **329.6 ms**. Progress and byte correctness passed; no P99 performance acceptance or minimum service guarantee is claimed. Python endpoints, shared CPU quota and diagnostics also make this unsuitable as a throughput comparison.

## Multi-peer cache decay

Four additional fanout cases use 12 seconds of quiet with all 16 idle connections retained, then a verified reburst, followed by 12 seconds of low-rate traffic on peer 1. Idle peer connections are checked bidirectionally before/after the quiet phase and at final close. Samples confirm all four traffic peers still own physical leases during both decay phases.

| MTU | Policy | Quiet large-cache first → last sample | Low-rate large-cache first → last sample |
|---|---|---|---|
| 1500 | Disabled | 16 → 16 | 16 → 16 |
| 1500 | Enabled | **16 → 0** | **16 → 2** |
| 512 | Disabled | 16 → 16 | 16 → 16 |
| 512 | Enabled | **16 → 0** | **16 → 2** |

Enabled cases each record 16 blocks trimmed during quiet and 14 during low-rate traffic: about **1 MiB** and **896 KiB** of 64 KiB payload backing released respectively, excluding descriptors. This extends the TX reclamation evidence to four real peer identities and the borrowed-ingress adapter path. It is not evidence about the PR #33 PacketPool policy, process RSS reduction, or a speedup.

## Checks and provenance

- `bench/pareto` Python tests: **21 passed**; `harness/l2`: **6 passed**.
- zfbench release/all-features unit tests: **17 passed**, including peer classification and invalid source rejection.
- fmt, zfbench all-targets/all-features clippy and diff checks passed; existing warnings remain.
- Single-peer compatibility: normal `PeerId(0)` mode, 64 flows, 16 idle connections, 256 MiB verified, no peer-mapping env. Global/port/peer budgets 32/16/4 MiB.
- `analyze.py` independently checks the recorded matrix, real identities, concurrent progress, bytes, sampled ceilings, actual peer-level refusals, cache decay and teardown.

Tested executable/source checkpoint: `36058e177fb8966e81c7a9777f75acce36472d38`. Subsequent changes add evidence and evidence-admission regression tests only. Rust 1.94.1, release/fat-LTO/codegen-units=1/panic=abort, `packet-pool-idle-trim` feature compiled but PacketPool trim env unset. Binary and Cargo.lock hashes are in `build-manifest.json`. The same executable was used for both TX-policy settings.

The shared environment has a four-CPU cgroup quota and five exposed CPUs. Proxy affinity was CPUs 0,1; loader affinity 2,3,4. The dedicated Docker rig used its own network and fresh netns per case, never host networking. The rig was removed after verification.

`raw-evidence.tar.gz` includes all 22 runs, exact command/result records, loader and proxy logs, test/build logs, tested harness sources and hashes. It excludes the executable itself. The 21 multi-peer cases verify 5.25 GiB in total; the compatibility case adds 0.25 GiB. No original failed workload has been discarded: there were no failed TUN cases in this task.

To reanalyze the archive:

```sh
mkdir extracted
tar -xzf raw-evidence.tar.gz -C extracted
(cd extracted && sha256sum -c RAW_SHA256SUMS)
python3 analyze.py extracted
```

This regenerates the committed `summary.json`. Outer `SHA256SUMS` covers report artifacts. The raw summary separately identifies the optional single-peer compatibility control.

To rerun the 16-case grid plus four decay cases, build the source checkpoint with the recorded flags, use the [existing rig Dockerfile](../2026-10-13-pr31-review/Dockerfile) with the managed proxy CA, and create a dedicated `zfstack-multipeer-rig` container with TUN/netns privileges and `--ulimit nofile=65536:65536`. Mount this repository read-only at `/repo`, a fresh writable evidence directory at `/evidence`, and `/etc/machine-id` read-only; ensure `/dev/net/tun` exists. Put the compiled executable at `/evidence/bin/proxy`. Set host `ZF_MULTI_PEER_EVIDENCE` to that evidence directory and run `python3 matrix.py`. The supplied wrapper records every command; do not reuse existing output directories. Remove only the rig owned by that invocation afterwards.

## zfc integration inspection and remaining work

Read-only inspection of zfc main `06bc341f00d48c45d14b1094f579df983baa2354` confirms the WireGuard adapter already uses `PeerId(packet.peer_uid)` with `ingress_borrowed`, per-peer egress budgets, and the production source-factory entry. Its Cargo.lock still resolves zfstack to `37c016d3e136426994e4b40198e236d5ffbc0fb6`, an ancestor predating the latest TX-decay fix. The inspected adapter does not wire `TxCacheTrimPolicy`. zfc files were not modified and no worker was deployed or tested in this task.

A full integration result therefore still needs a candidate built against the intended zfstack revision, an explicit opt-in policy in that candidate, and real WG peer traffic through zfc's authenticated ingress, egress queues and lifecycle. This source-address-mapped TUN test cannot substitute for that path. Multi-port/shared-global exhaustion, bandwidth fairness, cancellation/rebuild ledger return, historical isolated timeout attribution and formal performance acceptance also remain open. Process/netns cleanup here is not a graceful driver/ledger-zero proof. #23 remains open.
