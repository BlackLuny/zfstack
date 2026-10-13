# PR #31: reclaim unused TX history during low-rate traffic

The previous policy reset one pool-wide idle clock on every successful block get/put. A single 64-byte request per second therefore kept 16 historical 64-KiB TX blocks indefinitely. This follow-up preserves reusable blocks while releasing the unused portion of the historical cache.

## Implementation

- Track large and small classes independently for complete-idle cold reclamation. Small-block traffic cannot postpone an unused large class's cold deadline.
- Within each warm observation window, track the minimum free-list length per class. The free lists are LIFO, so that minimum identifies an old prefix that was never borrowed during the window. With `K` currently free blocks, `U` untouched prefix blocks and warm floor `W`, retain `max(W, K - U)` blocks. Trim from the old end, preserving the recently reused tail.
- Retire unused history even if the same class remains active. Splitting the two class clocks alone would not fix the real-TUN case: the splice path can reserve a large block for a small request.
- Correct the untouched-prefix count when trimming a prefix or evicting a tail, and reset the observation window after warm maintenance. Blocks returned midway through a window can age through the following window. `warm_after=0` observes each maintenance tick; even a zero warm floor preserves blocks reused within that window.
- Preserve free-only destruction, retained nonempty descriptor backing charges, pressure-reclaim priority, complete-idle deadlines and the default-disabled policy. Tracking uses constant-size shard-local counters and flags; no per-block clocks, connection scans, atomics or new locks are added.

This changes warm retention into a floor for recently used caches, not a hard cap: sustained use of all four cached blocks must retain all four even when the configured floor is two. Active blocks and their payloads are never candidates for trimming.

PacketPool's separate benchmark-host policy still requires complete RX idle. This change addresses TX history; it does not claim low-rate PacketPool decay or zero total memory during activity.

## Regression tests

Two new regressions fail on the old implementation and pass with the fix:

1. Periodic small-block writes no longer preserve historical large blocks; the large class eventually reaches zero while the small class stays active.
2. Periodic reuse of one large block releases unused history, while preserving the reused block for both warm floors zero and two. Repeated maintenance does not keep retiring that block.

Additional coverage verifies sustained use above the warm floor and prefix bookkeeping across partial trims and cross-class eviction. Existing active-payload, descriptor-only, pressure-reclaim, disabled-policy and complete-idle tests remain green. The former warm-clock reset test now checks that reuse resets the cold clock without postponing warm decay.

Validation on Rust 1.94.1:

- Release workspace/all-features: **246 passed, 0 failed, 3 ignored** (214 core, 2 integration, 16 bench, 14 harness).
- Full L0 matrix: **20/20 default and 20/20 opt-in**.
- Python runner regressions: **14/14 passed**.
- CI-style fmt, clippy and no-default-features build passed; pre-existing warnings remain.

## Real TUN results

**10/10 diagnostic cases passed** payload verification, bounded execution and namespace/process cleanup. The analyzer additionally checks cache retention and sampled budget bounds.

| Scenario | MTU | Before | After |
|---|---:|---:|---:|
| Burst → one 64-byte request/s for 60 s | 1500 | 16 free large blocks, 1 MiB payload | 2 free large blocks, 128 KiB payload |
| Burst → one 64-byte request/s for 60 s | 512 | 16 free large blocks, 1 MiB payload | 2 free large blocks, 128 KiB payload |

Both new low-rate runs retire 14 blocks and keep that count stable through the remaining periodic requests. TX free payload is **87.5% lower** in this example; nonempty descriptor backing remains charged at 1,152 bytes. PacketPool still retains 128 buffers during the low-rate phase, as expected from its separate complete-idle policy. These are ownership/cache observations, not a general RSS or speedup claim.

The six other new-binary diagnostics are:

- 40-second complete-idle/reburst at both MTUs: after the cold threshold, TX payload/backing and PacketPool free/in-use buffers reach zero; all quiet windows have zero wire traffic.
- Sustained traffic at both MTUs: eight flows, 128 MiB per flow, both directions before and after idle (**4 GiB verified per case**). Warm maintenance runs during the longer load.
- Budget smoke at both MTUs: 8 MiB global, 4 MiB port/peer, eight active flows and 100 verified idle connections.

The previously recorded 64-flow pressure failures are **not claimed fixed** by these smaller budget smokes. This follow-up does not repeat the full performance matrix or establish non-inferiority/Pareto improvement. The historical performance data in the [earlier report](../2026-10-13-pr31-review/README.md) describe the previous implementation.

## Provenance and reproduction

Before source checkpoint: `2e579eb50d079e9c5ae10caaeaf54fa68ef15ecf`; its Rust executable code matches the previously built `594e380` binary (SHA256 in `binary-sha256.txt`). After checkpoint: `c7e5604` (full source SHA is recorded per case). Later report-only commits do not change that executable code.

Both binaries use Rust 1.94.1, the same frozen Cargo.lock, release/fat-LTO/codegen-units=1/panic=abort, and the `packet-pool-idle-trim` feature. Both low-rate controls enable the same policies: TX check/warm/cold 1/5/30 seconds with warm floors 2/2; packet idle 30 seconds, target zero. Sustained/quota exercises use 100/200/1,000 ms TX timings and 1,000 ms packet idle. These remain exercise parameters, not recommended production defaults.

The isolated Docker TUN rig uses proxy CPUs 0,1 and loader CPUs 2,3,4 on the same managed VM as the earlier review. At most two cases run concurrently, all with explicit diagnostic sampling; **none are performance-eligible**. The sampler is aligned approximately by child-start and elapsed time; the analyzer excludes phase boundaries and evaluates the stable low-rate interval after 12 seconds. There are no sampler-based claims of atomic or continuous budget conservation.

`raw-evidence.tar.gz` includes all ten cases, wrapper records, commands/hashes, test logs (including the intentional pre-fix failures), Cargo.lock and source checkpoint. `summary.json` contains the evaluated outcomes. `SHA256SUMS` verifies the artifacts, and the archive's `RAW_SHA256SUMS` verifies individual raw files.

To reanalyze, extract the archive into a fresh directory, copy `analyze.py` there and run `python3 analyze.py`; no TUN privileges are needed. To rerun, build the before/after binaries with the flags above, place them at `/evidence/bin/before` and `/evidence/bin/after`, and use the earlier report's Dockerfile with a dedicated `zfstack-pr31-lowrate-rig` container: repository mounted read-only at `/repo`, fresh evidence directory at `/evidence`, real TUN/netns privileges, no host networking. `run_cases.py` and `run_matrix.py` record the exact CLI, policy, affinity and provenance. Do not reuse an existing case output directory.
