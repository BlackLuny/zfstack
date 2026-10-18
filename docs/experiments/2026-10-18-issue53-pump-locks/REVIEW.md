# Issue #53 / PR #86 — Independent code review

Reviewer: `independent_code_review` subagent, separate from the implementation and test authors.

## Verdict

**No new blocking correctness issue was found in the reviewed production change or the appended regression tests.** The ordinary pump's queue-lock consolidation preserves the consumption, abort, explicit application notification, and stream-removal boundaries examined below. No production correction is requested by this review.

This is a static code-review conclusion, not a claim that every possible thread interleaving was executed or that the change improves every workload. The longer uninterrupted critical section is an identified contention tradeoff.

## Exact content reviewed

The production baseline is `ed4655e0798efc6b02883967cf933291dba059c8`. The semantic candidate is `e0cd89ab745f025cbc5ccb89c69aba0681a2a13e`; the final production content differs from it only by the formatting change verified below. The regression tests are additions to that candidate's existing QA file.

| File | Final SHA-256 | Final Git blob SHA-1 |
| --- | --- | --- |
| `src/tokio_adapter.rs` | `dadc538c365d9e9bc37fc7219a6856b7d03f3223fd03e898b566563cefada41e` | `2357939ec8c5cd842c6c36239b80aae2a7d22ece` |
| `src/tokio_adapter/tests/qa.rs` | `b59e3b7c644a7375d0f1d34f6ac05d618643ee80a7cd60a0e244ec1945c1c9f5` | `e4ddaaae2ddfcf6a3306589f7fe9cec3577698f8` |

Identity checks performed independently by the reviewer:

- Recomputed the Git blob hashes of all 18 original source snapshots against `SOURCE-IDENTITY.json`; all matched.
- Read the complete baseline-to-candidate production diff.
- Verified that reversing exactly one formatting change in the final production file—the RX `register_physical_waiter` call collapsed to one line—reconstructs candidate blob `315890be1e15063a98319dfeb19022619963a3c1` exactly. There is no additional semantic production edit in the final file.
- Verified that the prefix preceding the new QA tests exactly reconstructs original QA blob `120db0530f4465842987a65ee2c09a4fa0074da9`. The original tests were not modified.

These hashes identify the reviewed final file contents without assuming the identifier of a later publication commit or merge tree.

## Production review scope and findings

Read the changed `Driver::pump` together with queue allocation/consumption, `Shared`, waiter tracking, `DriverWake`, `Ctl::mark`, application read/write/flush/shutdown/drop, the driver's service/event/removal paths, and splice handover and relay paths. Followed the relevant calls through `Shard`, `Conn`, `Budget`, `GlobalBudget`, `MemoryLease`, and RX buffer/index handling.

| Boundary | Finding |
| --- | --- |
| RX consumption | `rx_consumed_pending` is taken once. For nonzero consumption, the queue guard is dropped before `consume_adapter` and reacquired afterward. This matters because `Conn::after_app_read` releases logical budget and can synchronously invoke a registered application waker. |
| RX allocation abort | The guard is dropped before `shard.abort` and reacquired afterward. Connection buffer/budget releases therefore retain the previous queue-unlocked boundary. |
| Explicit read/write/flush notifications | Their wakers are taken under the guard but invoked after the final `drop(q)`. The changed guard does not enclose these callbacks. |
| Dirty queue lock order | `DriverWake` marks the dirty set and notifies the driver. `service` takes the dirty set and releases that mutex before calling `pump`; no new dirty/queue lock cycle was found. |
| Writer progress | TX drain, adapter occupancy publication, send-room calculation, writer watermark checks, and parked-writer registration retain their order. The same queue lock still protects observation and waker registration. |
| Shutdown and drop | Write shutdown remains conditional on an empty adapter TX queue and runs after unlocking. Application drop publishes `closed_by_app` before its dirty mark; stream removal remains outside the pump's queue guard. |
| Splice handover | The initial splice branch unlocks before `start_relay`. A splice request arriving during the consumption unlock window publishes its own dirty mark, leaving a subsequent pump to transfer remaining RX state and ordered TX backlog. No new loss, double consumption, or ordering error was identified. |

### Contention tradeoff

The ordinary pump uses one lock acquisition instead of three when there is neither pending RX consumption nor an RX allocation abort; it uses two when exactly one of those boundaries is needed, and three when both are needed.

The RX and TX work now share one uninterrupted guard in the common case. A runnable application task on another worker can no longer acquire the queue in the former gap between those phases. With work queued in both directions, especially under larger configured queue bounds, this can increase an individual lock wait even while reducing lock-operation overhead.

An already parked reader's explicit wake also occurred after the TX phase in the baseline; the change should not be described as unconditionally adding a TX phase to every reader's notification latency. No performance result is inferred from the source-level reduction in acquisition count. Workload-specific performance evidence is outside this review record.

### Existing synchronous-waker limitation

This review does **not** establish that all budget wakers throughout the adapter execute outside queue locks. Queue descriptor replacement, TX chunk consumption/clearing, and some core buffer/index releases can already drop a `MemoryLease` while a queue guard is held, potentially invoking budget waiters synchronously. Those paths were already guarded in the baseline; the consolidation did not newly place them under a queue lock.

The reviewed safety statement concerns the preserved consumption, abort, explicit notification, and removal boundaries. Arbitrary custom-waker reentrancy across all existing allocation/release paths is not certified here.

## Appended regression-test review

| Tests | What their assertions establish |
| --- | --- |
| `a0_issue53_pump_consumes_refills_drains_and_wakes_outside_queue` | A real core handshake/RX fixture supplies two batches. Application reads and writes prepare pending consumption and a full TX queue. One production pump consumes, refills, drains, and notifies the parked reader/writer/flusher. Byte patterns and queue/core lengths are checked. |
| Its budget-release probe | The waiter is registered after fixture operations and observes the first logical release with `(pending, rx_len, tx_len) = (0, 0, 1024)`. It is consumed before later pre-existing lock-held physical releases, so those releases are not misattributed to the new boundary. |
| `a0_issue53_dropped_unread_stream_releases_budget_outside_queue` | RX begins at its cap and TX is empty, avoiding an earlier transfer/release in the pump. The unread-drop abort invokes the budget callback outside the queue lock; map removal and the unread-drop abort count are checked. |
| `a1_issue53_ordinary_two_workers_bidirectional_half_close` and the four-worker variant | Independent ordinary API reader/writer and driver tasks operate through small queues. Packet exchange with the peer exercises the TCP state machine, ordered data delivery, and continued uploading after the other direction reaches EOF. |

The synchronous queue probe uses `Weak<Shared>` and `try_lock`, so it neither retains the stream nor blocks inside its callback. Its phase observations make the A0 assertions specific to the intended pump boundaries.

The half-close oracle is substantive: the peer withholds the upload tail until download EOF, asserts its own FIN has not yet been sent, then transmits the remaining upload and FIN. The application must receive and verify the complete upload. The test cannot pass merely because all upload bytes arrived before shutdown. This is protocol-peer packet exchange with real Tokio tasks, not a claim of external-network or OS-socket testing by these new ordinary-path cases.

### Test-scope limits

- The A1 weak-reference and physical-budget convergence assertions run **after** `task.shutdown_and_join()`. They prove convergence after driver shutdown, not independent natural-close reclamation while the driver keeps running. The A0 unread-drop case separately checks removal from the live driver's map.
- No new test injects RX descriptor allocation failure to dynamically enter `abort_rx`. That boundary was inspected statically.
- The A0 `try_lock` probe avoids blocking in the waker itself. It is not a general watchdog for a hypothetical omitted unlock followed by `release_if_done` reacquiring the queue. The A1 cases use the existing independent wall-clock watchdog.
- These tests do not establish a latency/throughput/CPU improvement or exhaustively cover scheduler interleavings.

## Execution evidence and remaining publication boundary

The main session reported that all four new tests passed on the remote development host and the full release library run passed with **305 passed, 6 pre-existing ignored**. The reviewer did not execute or independently rerun those commands; the main session's retained logs are the execution evidence.

This agent performed no compilation, performance run, production edit, push, or GitHub comment. An initial SSH discovery request did not return usable source; the completed review was subsequently performed against the independently hash-verified local snapshots described above.

The verdict applies to the exact final file hashes listed here and the baseline/candidate call-chain context inspected. It should not be represented as a full review of unrelated later changes to `main` or an uninspected merge tree. Subject to the main session's final commit/content and CI checks, no further production correction is requested by this independent review.

## Packaging-only follow-up: diagnostic template filename

The main session reported that the repository-wide coverage audit treated the documentation package's `probe.rs` template as Rust inventory and rejected its 14 instrumentation/platform `cfg` attributes. The packaging adjustment names that input `probe.rs.txt` and changes the single template-read path in `prepare.py`; the generated `src/tokio_adapter/issue53_pump_probe.rs` remains Rust source in the separate temporary exports.

The reviewer independently read all 175 lines of the original template, the generator, and the relevant build references. The template's SHA-256 is `f7a039667ca2a03c96c197e9eee9668583f3849ffa46d177034135e05f652063`. Its 14 `cfg` attributes select diagnostic counters and Linux thread-CPU clock handling. It contains no `#[test]`, `#[tokio::test]`, `cfg(test)`, or hidden test module. It provides a diagnostic `main`; the constructor borrows the design of a test fixture but does not register an executable test.

`prepare.py` reads the input as text and creates the module, example entry point, and diagnostic feature only in exported source directories. The inspected production Cargo manifest and source have no reference to this template. Naming the input as a text template accurately describes this role. Keeping its bytes and generated source paths unchanged preserves the diagnostic's executable behavior; no production or regression-test exclusion is needed for this adjustment.

**Static follow-up verdict: no blocking issue found in the specified rename and one-input-path update. It does not hide an executable regression test or change production compilation.** This verdict is based on the inspected template/generator behavior; the main session supplied the applied-change and execution evidence below. The review does not approve changes to audit policy, the scanner, production Cargo/features, or test inventories, and none are part of the specified fix.

The main session reported the following subsequent checks, which this agent did not execute:

- The original `python -m qa audit-coverage` gate passed after the packaging adjustment, with no audit-policy or scanner modification. Its diagnostic had identified zero deleted trusted existing tests.
- All five exports were regenerated from the same frozen commits. Their label, commit, tree, counted flag, original/overlay adapter hashes, and build command were equal field by field.
- For each export, `Cargo.toml`, `Cargo.lock`, `src/tokio_adapter.rs`, `src/tokio_adapter/issue53_pump_probe.rs`, and `examples/issue53_pump_probe.rs` were compared and were byte-identical to the pre-rename outputs.
- The template hash remained `f7a039667ca2a03c96c197e9eee9668583f3849ffa46d177034135e05f652063`; production and regression-test files were unchanged by this follow-up.

Absolute output paths in the generated manifests are not an executable-source equivalence criterion; the reported comparison appropriately uses frozen identities, build commands, and the generated files themselves. No new performance conclusion is added by this packaging review.
