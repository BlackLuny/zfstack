# Adapter and ownership QA

The implementation at `df432fd` already includes ordinary/relay memory, quota,
share, watermark, ACK and sender-exit regressions. This addition keeps those
production paths intact and puts new code inside existing `cfg(test)` modules.
Only the dev Tokio dependency gains `test-util`. Loom and libFuzzer have separate
workspaces and committed lockfiles; they are not production dependencies.

## Commands and contracts

```sh
cargo +1.94.1 test -p zfstack --release --locked --features tokio,test-peer tokio_adapter::tests::qa::
cargo +1.94.1 test -p zfstack --release --locked budget::tests::qa:: -- --nocapture
cargo +1.94.1 test --manifest-path qa-models/Cargo.toml --locked -- --nocapture
cargo +1.94.1 test --manifest-path fuzz/Cargo.toml --locked --lib
```

| Test group | Evidence | Boundary |
| --- | --- | --- |
| `a0_only_release_before_and_after_registration` | Real failed AsyncWrite allocation; the sole release meets descriptor + payload charge; no in-flight data; repoll only after delivered task notification | Completion into the adapter TX queue, not network delivery |
| `a0_negative_control_dropped_wake_cannot_pass_by_rescue_poll` | Test-side Wake gate drops the only notification; identical oracle must reject progress | Does not modify the production implementation |
| `a0_memory_waiter_migration_and_cancel_remove_old_notification` | Re-poll replaces the pending physical waiter; cancel removes it before a late release | A terminal close wake is permitted; a later stale memory wake is not |
| `a0_driver_deadline_reclaims_idle_cache_on_virtual_time_only` | Real driver timer reclaims without ingress, snapshot, manual pump or OS I/O; paused current-thread time | Explicit opt-in cache trim policy; no production default changed |
| `a1_loopback_*_shutdown_and_last_owner` | Real loopback splice, salted bidirectional bytes and EOF, four shards sharing global budget with repeated PeerId; current/2/4 runtime workers | Linux checks owned socket identities disappear from `/proc/self/fd`; other OSes prove joined tasks/socket EOF, not process FD counts |
| `a1_pending_read_write_cancel_*` | Pending ordinary reader/writer on 2/4 workers; app cancellation and driver shutdown; terminal errors, no surviving stream, late lease release | Outer wall-clock watchdog is independent of the tested runtime |
| `a1_shared_global_*_reserve_rollback_release` | Real std threads, 64 synchronized rounds, same peer IDs on independent shard budgets, hard-limit/rollback/release checks | Finite schedule coverage, not exhaustive ordering |
| `c13_waiter_burst_one_zero_burst_ledger_and_capacity` | Three 2048→1→0 waiter cycles, exact notification owners and ledger, capacity snapshots | Capacity while one remains is observed; no RSS or mandatory sparse trimming claim |

Existing selectors remain essential: `ordinary_writer_watermark_is_attainable_with_small_queue_or_share`,
`ordinary_writer_with_empty_queue_recovers_on_ack_sender_exit_or_close`,
`tx_wait_state_switches_without_stale_queue_entries_and_clears_on_success`,
`share_bound_relay_ignores_unrelated_releases_and_recovers_on_ack_or_sender_exit`,
`quota_bound_relay_with_send_space_wakes_on_logical_release`,
`issue37_relay_does_not_miss_release_during_failed_allocation`.
They are included by the full `--features tokio,test-peer` suite; real socket tests
are not marked `start_paused`.

A1 scenarios have a 15-second runtime deadline, two-second runtime shutdown bound,
and an independent 20-second wall watchdog. Every successful path joins its driver,
application and upstream tasks. A worker that cannot be joined after the external
watchdog is a test failure; CI must also keep its process deadline. These tests
introduce no sleeps to make a missing notification pass.

## Safety and implementation validation

Use pinned `nightly-2026-07-11` for the safety lane, separately from performance:

```sh
cargo +nightly-2026-07-11 miri test -p zfstack --locked --lib pktpool::tests::buffers_stay_charged_while_cached_and_release_beyond_the_cap -- --exact
cargo +nightly-2026-07-11 miri test -p zfstack --locked --lib buf::tests::write_with_never_offers_uninitialized_bytes -- --exact
cargo +nightly-2026-07-11 miri test -p zfstack --locked --lib buf::tests::borrowed_ingress_chunks_outlive_source_and_only_stores_ooo_gaps -- --exact
```

The early development run used native aarch64 macOS, Rust 1.94.1, and the
`nightly` alias whose actual manifest date was 2026-07-11 (rustc
`375b1431b7d89d1c2e2bc168c011848ae12b7d14`). It ran against an uncommitted
working tree. Its stdout/stderr were visible in tool sessions, but complete raw
log files were not retained, so these checks are not complete v2 safety evidence.
That run reported 262 release library tests passed and five pre-existing tests
ignored (not executed). It also passed two Loom tests including an expected failing mutation and 33 positive model
executions, two stable fuzz smoke tests, three Miri buffer checks, and 512 iterations
per ASan fuzz target with seed 43105. These finite checks do not certify long fuzzing,
Linux FD cleanup, Windows, throughput, or RSS.

The reviewed source inventory contains six ignored library entries and one ignored
integration entry; feature selection affects which entries appear in a given run.
The 12 added adapter/budget tests contain no ignored cases. Clean frozen-plan runs,
commands, raw artifact locations and remaining qualification gaps are recorded in
[the implementation validation record](../qa/VALIDATION.md).

The clean `854303599729d88bd7677118a79ff55ef5c5bf39` safety plan passed the exact
Miri selector `buf::tests::write_with_never_offers_uninitialized_bytes`, the two
Loom tests and three native ASan fuzz targets (30 seconds each, seed 1, dev profile).
It did not rerun the other two historical Miri selectors above. Its aggregate is
5/6 and `BLOCKED`: the separate ASan+LSan case is `NOT_RUN` because the native
Darwin target lacks LeakSanitizer support. All executed child cleanup checks passed.

`cargo tree -p zfstack --features tokio -e normal,build,features` has no `test-util`
entry. No production socket, clock, allocation, synchronization or hot-path code
was changed. The SC model's explicit limitations are in `qa-models/README.md`.
