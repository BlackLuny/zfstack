# Issue 49: uninitialized TX path for the relay `readv`

## Scope

`socket readv` writes straight into TX block capacity before the block is
visible. The safe `TxBuf::write_with` offered a zero-filled window because a
generic caller may read bytes it did not write; a `readv` overwrites exactly
the bytes it reports, so the whole 64 KiB offered window was being zeroed even
when the socket returned a few hundred bytes.

This change adds an internal, unsafe, `readv`-specific sibling of
`write_with` that hands out raw capacity and commits only the bytes the read
reported. The safe API and all production defaults are unchanged.

Source base: `987708b4246190f7af88f3508681de8a4235bb9d` (origin/main, the merge
of #83). The probe is kept as `tx_readv_probe.rs.txt` in this directory so it
stays out of the reviewed Rust cfg inventory; copy it to
`examples/tx_readv_probe.rs` to rebuild it.

## Change

- `Block::window_uninit(start, end)` returns raw `[MaybeUninit<u8>]` capacity
  without an initializing fill.
- `Block::commit_written(start, n)` **sets** the initialized prefix end to
  exactly the reported write (zero-filling a gap after the old prefix). Bytes
  past the committed range become uninitialized again, so capacity the fill
  could have touched but did not commit cannot stay claimed as initialized.
- `TxBuf::write_with_uninit`, `Conn::write_with_uninit` and
  `Shard::write_with_uninit_for_adapter` mirror the safe paths
  (space/share/budget admission, block allocation, index lease, empty-block
  release, `len` bookkeeping). `write_with` is untouched.
- A write that fits the single-block ring uses initialized `write_with`
  windows and restores every uncommitted byte to zero after success, error or
  panic. This preserves the live initialized prefix and reuses ACKed capacity
  without allocating another block. Conn admission can reduce the relay's
  256 KiB request below one block, especially at the 64 KiB send-share floor.
  Larger writes retain the raw growth path.
- On the growth path, both offered blocks are committed even when the fill is
  short, errors or panics, which drops any stale prefix they carried. A
  panicking fill is caught with `catch_unwind`, both offered blocks are reset
  to the logical end, and
  the panic is resumed. The error path commits nothing.
- The adapter's `read_into_uninit` uses `TcpStream::try_read_buf`, whose
  `BufMut for &mut [MaybeUninit<u8>]` reads into uninitialized capacity and
  keeps tokio's readiness/`WouldBlock` contract. It fills the first window then,
  only if it is exactly full, the second. A `WouldBlock`/`Interrupted` second
  read keeps the successful prefix; any other error (notably
  `ConnectionReset`/`ConnectionAborted`) is propagated, so the relay resets the
  connection instead of treating it as a clean EOF.

## Correctness and safety

- Ring-fallback follow-up on macOS arm64, Rust 1.94.1:
  `cargo +1.94.1 test --locked --release -p zfstack --features tokio,test-peer`
  — `299 passed, 0 failed, 5 ignored` at the lib level plus all four enabled
  integration tests, including splice/relay/half-close cases.
- Previous Linux-node validation of `9bb825c` (Rust 1.96.0, release):
  `cargo test --locked --release --features tokio,test-peer --lib` —
  `297 passed, 0 failed, 6 ignored`, exact `Cargo.lock`.
- Miri (`nightly-2026-07-11`, `cargo miri test --lib tx_uninit`): 8 passed.
  Miri tracks initialization of every byte and found no exposure of
  uninitialized memory.
- New buffer tests: only-written bytes are visible; appends stay contiguous;
  a `readv` crossing a block boundary commits both blocks; larger writes grow;
  a reused block whose fill uninitializes the uncommitted tail is lowered to
  the committed end so a later safe fill zeroes
  it; a panicking fill is reset the same way; an error keeps nothing and
  returns the fresh block. The ring fallback additionally abandons both
  windows after a short fill, error or panic, preserves live wrapped bytes and
  verifies that a subsequent safe fill sees initialized zeros.
- A Conn-level regression requests the relay's full 256 KiB window, reaches
  the 64 KiB send-share floor, and refills uneven partial ACKs at the global,
  port and peer physical ceilings. It checks byte order, logical accounting,
  Writable arming, one-block backing and unchanged physical charge through
  repeated ring wraps. The test failed with `MemoryBlocked` on `9bb825c` and
  passes with the ring fallback.
- New adapter tests: a hard error on the second window is propagated for
  `ConnectionReset`/`ConnectionAborted`/`BrokenPipe`/`Other`, while
  `WouldBlock`/`Interrupted` keep the prefix.
- The old safe `write_with_never_offers_uninitialized_bytes` contract still
  holds: fresh blocks offered through `write_with` still read as zeros.
- Reverting only `commit_written` to its first (max-based) version reproduces
  the review's Miri failure: `reading memory ... but memory is uninitialized`.
  The committed version passes.

## Measurement revision

The assembly, Linux JSONL and binary hashes below are frozen evidence from
`9bb825cf81b43836a83dd59740324ace67a1c030`, before the ring-fallback follow-up.
They measure fresh-block and whole-operation pool reuse, which release the
block between operations; they do not measure partial-ACK ring refills or the
cost of restoring an uncommitted ring tail. No new end-to-end performance
claim is made for the fallback.

## Assembly evidence

Server build: `CARGO_PROFILE_RELEASE_LTO=fat`,
`CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1`, `CARGO_PROFILE_RELEASE_PANIC=abort`,
`RUSTFLAGS=-C target-cpu=native`, plus `CARGO_PROFILE_RELEASE_DEBUG=1` for
source-line attribution. Assembly binary:
`4135be61d4c2d7a5763999ab50294c7d64a6e3301f2d5c0e92a07a9807d25f7f`.
Measured binary: `5afffe0e41a84f900c421b9f72f8c49f14368afc44ecaecc5e014df6d50d9f85`
(identical source, no debug info).

`objdump -dl -C` over the two probe wrappers (`tx_readv_probe::write_zero`
calls the safe `write_with`; `tx_readv_probe::write_uninit` calls the new
path):

| | `write_zero` | `write_uninit` |
|---|---:|---:|
| `callq memset@GLIBC` | 3 | 1 |
| inlined `Block::spare_mut` | present | absent |
| inlined `Block::commit_written` | absent | present |

Every `memset` in `write_zero` is inside `Block::spare_mut` (`src/buf.rs:176`,
the eager `data[init..end].fill(0)`). The one `memset` in `write_uninit` is
inside `Block::commit_written`, whose fill is guarded by `if start > self.init`
and skipped on the measured hot path where the fill starts at the logical end.
The `readv` path therefore executes zero memsets of the offered window; the
safe path executes one per fresh block. Raw context is in `asm_analysis.txt`.

## Linux A/B measurements

Node: `root@38.207.178.131:22100`, 4 × i7-1265U (KVM), Linux 5.4.0, Rust
1.96.0. `taskset -c 3`; three alternating rounds per payload; process CPU
(`CLOCK_PROCESS_CPUTIME_ID`) reported as `cpu_ns_per_op`. Both arms are the
same binary, so all build and host differences cancel; only the code path
differs.

`cold` = a fresh 64 KiB block is allocated and freed per operation, the
worst case when concurrent short responses exceed the 16-block pool cache.
`warm` = the pooled block is reused, where the one-time zero fill was already
paid.

`final_cold.jsonl` (36 valid JSON lines, median of 3):

| payload | zero (ns/op) | uninit (ns/op) | speedup |
|---:|---:|---:|---:|
| 64 B | 2294.2 | 574.2 | 4.00× |
| 512 B | 2269.3 | 575.7 | 3.94× |
| 1500 B | 2317.0 | 592.9 | 3.91× |
| 4096 B | 2401.5 | 622.6 | 3.86× |
| 65535 B | 4613.3 | 2530.4 | 1.82× |
| 65536 B | 4619.2 | 2517.2 | 1.84× |

`final_warm.jsonl` (30 valid JSON lines, median of 5) shows the amortized
path is unchanged: 64 B 156.4 → 159.3 (0.98×), 512 B 165.3 → 166.0 (1.00×),
4096 B 204.5 → 207.7 (0.98×). This is within the run-to-run noise of the
shared host.

The same probe on the development Mac (arm64, release) reproduced the effect
(cold 512 B: 1225 vs 497 ns/op) and the warm parity.

## Interpretation

The win is bounded by fresh-block allocation, not by steady state:

- Steady, pooled traffic (≤ 16 blocks live) already kept `init == 64 KiB`, so
  the safe path was a no-op there and the new path is a no-op too.
- When short-lived responses exceed the pool, every fresh 64 KiB block was
  zeroed for a possibly tiny read. That is the measured cold case, and it is
  1.8×–4.0× cheaper per operation, saving ≈ 1.7–2.1 µs of process CPU.
- Full-block reads still benefit (≈ 1.8×) because the extra 64 KiB memset was
  never needed.

This is a Pareto change at this primitive: a large cold-path reduction with a
warm-path cost inside the pre-registered non-inferiority bound and no protocol
or default-parameter change.

## Limitations

- No hardware PMU on the node (`perf` reports `<not supported>` for
  instructions/cycles), so instruction counts come from disassembly, not
  counters.
- The comparison is in-process (same binary) rather than two independently
  built binaries; it isolates the primitive but is not a full
  production-profile A/B.
- No end-to-end TUN/WireGuard relay throughput run was performed. The adapter
  read path changed from one `readv` to up to two `try_read_buf` calls in the
  cross-block case; all relay functional tests pass, but a relay-level
  throughput/tail-latency A/B and the full #66 memory matrix remain open. The
  node was shared with an unrelated benchmark during part of the window.
