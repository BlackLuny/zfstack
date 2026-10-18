# Ordinary stream pump queue-lock reuse — issue #53

## Decision and scope

This PR implements the remaining ordinary AsyncRead/AsyncWrite pump cost sub-item of [issue #53](https://github.com/BlackLuny/zfstack/issues/53). [PR #83](https://github.com/BlackLuny/zfstack/pull/83) already delivered the DRR queue reuse and budget lookup changes. The production change here is confined to `Driver::pump` in `src/tokio_adapter.rs`.

The new implementation demonstrably reduces queue-mutex acquisitions on the intended production path and preserves the callback boundaries examined by independent review and regression tests. It is ready for normal PR review, subject to the required checks on the published commit. It does not close the issue's wider end-to-end performance qualification: contended application latency, real-network throughput, peer churn and long-duration performance are not established by the diagnostic below.

## Why the change is small

Previously the ordinary pump acquired `Shared.q` three times: once to check for splice and take reported RX consumption, once to fill the adapter RX queue, and once to drain the adapter TX queue and publish room/waker state. The candidate retains the first guard across normal RX and TX processing.

The operation order, queue capacities, sender shares, memory accounting, partial-write behavior, shutdown decisions and wake publication remain the same. The production diff adds no per-stream fields, allocations, dependencies, timers or wire changes.

Required unlock boundaries remain explicit:

- A pending splice drops the guard before handover and relay pumping.
- Nonzero RX consumption drops the guard before `consume_adapter`, whose logical budget release can synchronously notify an application waker, then reacquires it.
- RX descriptor allocation failure drops the guard before aborting the core, then reacquires it.
- Explicit reader, writer and flush callbacks run after the final guard drop.
- Write shutdown and `release_if_done` also remain after that drop.

This is not a claim that every budget release throughout the existing adapter is unlocked. Some descriptor and TX chunk releases already occurred under their respective queue guards in the baseline. [REVIEW.md](REVIEW.md) documents those pre-existing paths and the precise safety statement.

## Frozen source identities

| Item | Identity |
| --- | --- |
| Baseline A | `ed4655e0798efc6b02883967cf933291dba059c8` |
| Measured semantic candidate B | `e0cd89ab745f025cbc5ccb89c69aba0681a2a13e` |
| B source tree | `210467df9f0f295147992f2d060c1e0368a6af23` |
| Final production file SHA-256 | `dadc538c365d9e9bc37fc7219a6856b7d03f3223fd03e898b566563cefada41e` |
| Final production Git blob | `2357939ec8c5cd842c6c36239b80aae2a7d22ece` |
| Final QA file SHA-256 | `b59e3b7c644a7375d0f1d34f6ac05d618643ee80a7cd60a0e244ec1945c1c9f5` |
| Final QA Git blob | `e4ddaaae2ddfcf6a3306589f7fe9cec3577698f8` |

The final production file differs from B only by rustfmt collapsing one physical-waiter registration call onto one line. The independent reviewer reversed that formatting change and recovered B's exact original blob. The appended tests were not part of the performance overlays. The final source hashes above match the remote functional-test inputs.

The branch started before [PR #85](https://github.com/BlackLuny/zfstack/pull/85) merged. Main subsequently became `ddc2edfff7b49134c247485af0dd72dc44cfea6b`. Its additional relay first-block policy and tests do not change the ordinary pump or this QA file. The frozen A/B baseline is retained to isolate this change; final PR CI validates GitHub's merge with main. This report does not pretend that a later merge tree was the measured B.

## Functional verification

All commands below used Rust 1.94.1 on the SSH development server, with the locked dependency file.

| Verification | Result |
| --- | --- |
| `cargo +1.94.1 check --locked --lib --features tokio,test-peer` | Passed |
| `cargo +1.94.1 fmt --all -- --check` | Passed after the one-line formatting fix |
| `git diff --check` | Passed |
| `cargo +1.94.1 clippy --locked --lib --features tokio,test-peer` | Passed with existing warnings; no lint suppression added |
| Added `issue53` release regressions | 4 passed, 0 failed |
| Full `cargo +1.94.1 test --locked --release --lib -p zfstack --features tokio,test-peer` | 305 passed, 0 failed, 6 existing ignored |

The [new-test log](validation/regressions-first.log.gz), [full library log](validation/lib-suite-final.log.gz), and [clippy log](validation/clippy-production.log.gz) retain the complete command output in lossless gzip form so raw compiler whitespace does not become a source-formatting change. Decompression reproduces each original log hash. Adjacent `.log.json` files record source hashes before/after, lease identity, exit status and log hash. The unit-test binaries use Cargo's test panic behavior; the panic-abort statement below applies specifically to the normal release example probes.

### Added regression coverage

| Test | What it proves |
| --- | --- |
| `a0_issue53_pump_consumes_refills_drains_and_wakes_outside_queue` | A real TCP handshake and 8 KiB receive fixture feed two 4 KiB batches. One pump reports prior consumption, refills RX, drains 1 KiB TX, and wakes the parked reader/writer/flusher after unlocking. Byte patterns, phase observations, continued writer progress and final budget release are checked. |
| `a0_issue53_dropped_unread_stream_releases_budget_outside_queue` | An unread application drop reaches a lock-free budget callback during abort, removes the stream from the live driver map and releases its resources. |
| `a1_issue53_ordinary_two_workers_bidirectional_half_close` | Four concurrent ordinary API lanes on two Tokio workers exchange checked data through 4 KiB queues. |
| `a1_issue53_ordinary_four_workers_bidirectional_half_close` | The same ordinary API contract on four workers. |

Each concurrent lane uploads 64 KiB + 37 bytes and downloads 48 KiB + 29 bytes. The peer sends only a 16 KiB + 7-byte upload prefix until it actually receives download EOF, then sends the remaining upload and its FIN. This makes continued reverse-direction progress after half-close a required condition for success. These are real Tokio application/driver tasks with a protocol peer, not external-network throughput tests.

The A1 resource convergence assertions occur after driver shutdown. They must not be described as a separate proof of natural-close reclamation while the driver continues running. The A0 unread-drop test independently checks live-driver removal. RX descriptor allocation-failure abort is reviewed statically rather than injected by these new tests.

### Error injection confirms the callback oracle

An isolated export of B with the new QA file deliberately moved the final guard drop after the explicit application wakes, while leaving stream removal outside the guard. The named A0 test failed with cargo exit 101 and the expected `callback ran while the stream queue was locked` assertion. The wrapper returned success only after confirming one selected test, that exact failure, and unchanged test/mutated-source hashes.

See [negative-control/RESULT.json](negative-control/RESULT.json), [the exact mutation](negative-control/mutation.diff), [compiler/source manifest](negative-control/MANIFEST.json), and [failure log](negative-control/cargo-test.log). The deliberate failure did not modify the production branch.

The first execution used the host default Rust 1.95.0 and also detected the mutation. The helper was then corrected to pin and record Rust 1.94.1; the retained release-gate evidence above is the successful repeated negative control with that explicit toolchain.

## Actual queue-lock counts

Separate instrumented exports replaced only the three queue-lock expressions inside production `Driver::pump`. Setup and warmup calls are excluded by resetting counters. Normal timing builds contain no such replacement.

| Case | Counted pump calls | A locks | B locks | Locks per pump A → B |
| --- | ---: | ---: | ---: | --- |
| One established idle stream | 1,000 | 3,000 | 1,000 | 3 → 1 |
| One driver, 64 idle streams / peers | 64,000 | 192,000 | 64,000 | 3 → 1 |
| Real peer data read by the ordinary app | 1 | 3 | 2 | 3 → 2 |
| Ordinary app queues 64 TX bytes | 1 | 3 | 1 | 3 → 1 |

All eight instrumented cases passed their exact-count, queue-state and final-zero-budget checks. The consumed case used a real peer payload, verified `poll_read` bytes and the resulting pending consumption; it did not fabricate a counter. Both idle versions retained the same live charged memory: 12,952 bytes for one stream, 828,928 bytes for 64 streams.

See [counts/raw.jsonl](counts/raw.jsonl), [counts/PLAN.json](counts/PLAN.json), and [counts/summary.json](counts/summary.json).

The instrumented average duration per individual guard increased from about 54.8 ns to 117.1 ns in idle1, and from 58.4 ns to 119.4 ns in idle64, while the number of guards fell. These clock/counter-instrumented values are diagnostic scope measurements, not production contention latency. Maximums include preemption and all raw values are retained.

## Uninstrumented CPU diagnostic

A1 and A2 were independently compiled baselines in separate target directories; B was a separate candidate build. All used the identical private production-pump fixture and normal release example binary, Rust 1.94.1, fat LTO, one codegen unit, panic=abort and incremental=0. Source, build, binary and environment details are in [MANIFEST.json](MANIFEST.json), [builds/builds.json](builds/builds.json) and [ENVIRONMENT.json](ENVIRONMENT.json).

The host was an Intel Xeon E5-1630 v4 with Linux 6.1.0-41-amd64. Each sample was pinned to allowed CPU 2. Each process established its own connections, warmed 10,000 pumps, and measured exactly 1,000,000 calls to the unchanged production pump body using calling-thread CPU time. Connection setup and destruction were outside that timer.

The frozen plan ran all four A/A pairs for each case first, then twelve alternating A/B pairs per case: 64 process samples, none discarded or replaced. Build work was complete before the measurement window. Compiler checks at every sample boundary found no active compiler; raw host snapshots and CPU/steal counters are retained. This boundary check does not prove that no short unrelated task ran between observations.

| Diagnostic | Pairs | Median paired CPU change | Descriptive bootstrap 95% interval |
| --- | ---: | ---: | ---: |
| A2/A1, idle1 | 4 | −2.62% | [−7.38%, +4.86%] |
| A2/A1, idle64 | 4 | −0.01% | [−6.22%, +1.02%] |
| B/A1, idle1 | 12 | −7.89% | [−8.10%, −5.93%] |
| B/A1, idle64 | 12 | −5.91% | [−8.30%, −4.25%] |

These are observed reductions in fixed idle-pump CPU cost during a single short window. A/A variation is material, the host is shared, and there was no second independent timing window. The result does not establish a deployment-wide CPU reduction, network goodput increase or P99 non-inferiority. A/A bias was not subtracted from A/B.

The pump calls represent artificial wake opportunities on established production objects. They do not establish how often a real workload invokes this path. Reported denominators are pump calls, not packets, requests or useful business bytes.

See the full [timing plan](timing/PLAN.json), [all raw samples](timing/raw.jsonl), [paired ratios and intervals](timing/summary.json), and the frozen [protocol](PROTOCOL.md).

## Contention and independent review

The merged RX/TX critical section removes an intermediate unlock window. An application already running on another worker can wait longer to acquire the queue when both directions contain work. The baseline also delayed explicit parked-reader wake invocation until after TX, so this tradeoff should not be generalized to every reader notification.

The two-worker/four-worker regressions prove progress and data/close correctness in their specified conditions. They do not measure competing application lock waits or request tail latency. The review found no new deadlock, lost wake, double consumption or splice ordering error, but it explicitly retained this contention limitation.

An agent separate from the production implementation and test author reviewed the complete production delta and relevant core/budget/queue call chains, independently checked 18 source blob identities, reviewed all four new tests, and finally verified the formatting-only delta and final file hashes. No blocking new production or test issue remained. The full, attributed record is [REVIEW.md](REVIEW.md).

## CI and retained preparation corrections

The [first PR workflow](https://github.com/BlackLuny/zfstack/actions/runs/39100233163) passed test, l0, bench and tools. Lint and the aggregate gate failed on rustfmt's new single-line layout preference. That exact formatting issue was reproduced and corrected; no coverage rule, lint policy, test selection or gate was loosened.

An initial overlay preparation attempt used a tarfile API unavailable in Python 3.11.2 and stopped before building. The helper was corrected to validate archive paths and reject links/special files using the supported API; the failed empty export was retained and the measured exports were created under a new path. There were no failed or replaced performance samples.

The final publication's required CI status is recorded on [PR #86](https://github.com/BlackLuny/zfstack/pull/86/checks). This report is committed before that workflow can finish; final commit, merge identity and completed checks are reported in the PR description rather than inventing a self-referential result here.

## Reproduction and evidence index

Run functional verification from the repository root with the commands above. For diagnostic exports, build and execute the frozen sources using [PROTOCOL.md](PROTOCOL.md):

1. `prepare.py` creates five immutable source exports from the two full commit IDs.
2. `build_matrix.py` builds selected exports sequentially with their recorded environment and distinct targets.
3. `run_matrix.py` records the plan and runs counts or uninstrumented A/A then A/B on an allowed CPU.
4. `summarize.py` recomputes every paired ratio and descriptive interval from raw JSONL.
5. `negative_control.py` creates and tests only the isolated deliberate regression.

Builds and runs in this record used the public QA lease rooted at `/var/lock/zfstack-qa` with `socket.gethostname()` as the physical domain. The captured host wrapper is retained as [validation/run_command.py.txt](validation/run_command.py.txt); its fixed repository path records this exact execution environment and must be adjusted for another checkout. The production branch does not import or compile these documentation probes.

[SHA256SUMS](SHA256SUMS) covers the retained evidence package except the checksum file itself. The experiment does not change any existing ignored test, platform exclusion, L1/L2 qualification or production policy.
