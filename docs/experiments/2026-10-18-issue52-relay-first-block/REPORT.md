# Issue 52, bounded stage A: the first relay read

## Scope and design

Refs #52; this does not close the issue or complete its acceptance matrix.

A fresh relay now requests at most the unused portion of its initial 2 KiB
allowance. It reuses the existing cumulative `SpliceStats::downloaded` counter,
so fragmented reads and a fully ACKed/drained TX queue do not restart the
allowance. Once 2 KiB has been read, the same pump immediately resumes its
existing 256 KiB requests. No ACK, timer, readiness probe, or extra per-flow
state is introduced. The actual request is also passed to the physical waiter
charge calculation.

This is an adapter read-sizing policy change, not a claim of universally
equivalent performance. `TxBuf` allocation, byte ordering, unsafe initialization,
ring reuse, logical/physical budget rules, cwnd, prefetch, and `max_snd_buf`
are unchanged. The small-block constant is reused rather than duplicated.

The strongest benefit is a fresh relay with no pre-splice TX data whose first
response is shorter than 2 KiB. Existing TX/backlog can already occupy blocks
and force earlier growth; later responses on a persistent relay are not reset
to the small allowance.

## Tradeoffs and deterministic resource evidence

The first bulk read is smaller and can add an extra initial socket read.
The small prefix adds 2 KiB of backing to responses that grow beyond it.
An exact 2 KiB response still needs a speculative full block to discover EOF.
Existing full-block speculative EOF allocation is not changed by this patch.

The new buffer test models ordered uninitialized reads followed by a normal
zero-length EOF. It reports actual TX/pool payload and physical leases, including
descriptor backing. All committed payload bytes are checked and all leases
return to zero after destruction. These are allocations/leases, not RSS.

| Response | Active payload | Cached payload after EOF | Cold physical | Warm physical |
| --- | ---: | ---: | ---: | ---: |
| 100 B | 2,048 | 0 | 2,336 | 2,624 |
| 2 KiB | 2,048 | 65,536 | 68,160 | 68,448 |
| 4 KiB | 67,584 | 65,536 | 133,696 | 133,984 |
| 64 KiB | 67,584 | 65,536 | 133,696 | 133,984 |

Warm means a second response using the same block pool, with a new TxBuf;
the extra 288 B is retained small-cache descriptor backing. Requests are
`[2048, 1948]` for 100 B; after the initial 2048 B request, grown responses
use 262144 B requests in the same synchronous loop.

The real-socket 100 B regression with old production code charges 131,648
additional bytes in one pump (active large block, speculative EOF block and
indices). Its new-code assertion requires less than 4 KiB and no large cached
block. This negative control and the fragmented-response negative control fail
on old production code; the finite payload/FIN matrix passes both versions.
No change to the #84 uninitialized path is credited as a new zeroing saving;
zero-fill instructions and allocation call counts were not instrumented here.

## Verification

- Five new tests cover normal finite 100 B / 2 KiB / 4 KiB / 64 KiB responses
  at MTU 1500 / 65535, complete payload and FIN, same-pump growth before any ACK,
  cold/warm pools, fragmented readiness wakes, an ACKed/drained initial
  fragment, integer-boundary read sizing, exact small-allocation waiter wake,
  and final physical-credit reclamation.
- The complete zfstack release suite passes: 306 unit tests and four integration
  tests. Six unit and one integration tests remain pre-existing manual ignores.
  Existing #84 unsafe/ring and every-budget-ceiling tests, reset/half-close,
  driver cancellation and delayed-ACK progress tests are included.
- Formatting, diff whitespace check and QA coverage audit pass.
- Workspace/all-target/all-feature Clippy exits successfully with existing
  warnings; this is not a warning-free build.

Commands use the existing Rust 1.95.0 toolchain, `--locked`, at most two Cargo
build jobs, and no installed software or host network changes. CI pins 1.94.1;
that exact toolchain was not installed on this node.

## Bounded bulk comparison

The existing 64-stream delayed-ACK relay regression was run alone on the same
Linux dev node in release mode, features `tokio,test-peer`, after an A/A
sanity pair and then in A-B-B-A order. Each run transfers and checks 64 × 4 MiB
(268,435,456 application bytes), requires every EOF, and verifies complete
server physical-budget release. It uses default MTU 1420, no explicit offload,
a configured 50 ms ACK delay, charged egress, and global/port/peer budgets of
16 / 8 / 8 MiB. This is an in-process simulated peer plus real upstream TCP,
not a production network throughput qualification.

The retained JSON/logs contain individual wall, whole-test-process user/system
CPU and process RSS high-water values. CPU includes the peer/harness and is not
proxy-only CPU. RSS is not TX-object accounting, and its high-water values can
include runner startup. No claim of CPU speedup or RSS savings is made.

All final runs passed with `max_flat_samples=1`. Results overlap baseline
variation and do not establish a performance improvement:

| Order | Arm | Wall seconds | User CPU seconds | System CPU seconds | RSS high-water KiB |
| --- | --- | ---: | ---: | ---: | ---: |
| AA1 | baseline | 5.728 | 2.608 | 0.262 | 15,032 |
| AA2 | baseline | 5.874 | 2.826 | 0.308 | 21,764 |
| AB1A | baseline | 5.324 | 2.596 | 0.279 | 21,860 |
| AB1B | candidate | 5.697 | 2.693 | 0.239 | 21,860 |
| AB2B | candidate | 5.158 | 2.505 | 0.210 | 21,860 |
| AB2A | baseline | 5.440 | 2.731 | 0.311 | 21,860 |

The first interleaved candidate is slower than its adjacent baseline; the second
is faster. The A/A and interleaved variation is why this is diagnostic evidence,
not a throughput non-regression guarantee.

Source base: `ed4655e0798efc6b02883967cf933291dba059c8`.
The baseline executable uses that production code plus the retained
`tests-only.patch.gz`; candidate source identity and both binary hashes are
recorded in `source-identity.json` with the final comparison. The candidate is
`fcbdcd954378f08832ec72b0b22fc39347f924b7`, tree
`0c0159b2ee89688e3d1fc2541a093c4ede5812d7`. The full suite was built from that
same tree before an author-metadata-only amendment. This report is a subsequent
documentation commit and does not change the measured production code.

To reproduce the bulk comparison, build the release library-test binary from
the base plus the decompressed `tests-only.patch.gz` and from the candidate. Copy those executables
beside `compare.py.txt` as `baseline-tests` and `candidate-tests`, then run
`python3 compare.py.txt` in an otherwise idle test environment. The script
writes per-run logs and JSON; it does not change network configuration.

## Limits and remaining issue work

This is a bounded stage-A implementation and regression check. It does not
supply #46 CMD_SHORT L1/L2 trials, high-concurrency short-flow latency percentiles,
isolated proxy/host CPU, an infinite-download cancellation benchmark,
allocation/zeroing counters, or the complete cross-RTT/loss/MTU/budget matrix.
Existing reset/cancellation correctness tests are not relabeled as those
measurements. Performance qualification remains inconclusive. Stage B
(cwnd/prefetch/default-buffer experiments) remains entirely separate.
