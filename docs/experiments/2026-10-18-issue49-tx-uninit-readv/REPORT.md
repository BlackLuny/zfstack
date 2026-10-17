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
of #83). Candidate is this uncommitted working tree; `git diff --stat`:
`src/buf.rs +332`, `src/conn.rs +42`, `src/shard.rs +17`, `src/tokio_adapter.rs
+34`. The probe is kept as `tx_readv_probe.rs.txt` in this directory so it
stays out of the reviewed Rust cfg inventory; copy it to
`examples/tx_readv_probe.rs` to rebuild it.

## Change

- `Block::window_uninit(start, end)` returns raw `[MaybeUninit<u8>]` capacity
  without an initializing fill.
- `Block::commit_written(start, n)` records exactly the filled bytes. A gap
  before `start` (only reachable when a block is reused with a shorter
  initialized prefix) is zero-filled so the initialized prefix stays
  contiguous; bytes past the reported write stay uninitialized and are never
  exposed through `TxBuf::slices`.
- `TxBuf::write_with_uninit`, `Conn::write_with_uninit` and
  `Shard::write_with_uninit_for_adapter` mirror the safe paths
  (space/share/budget admission, block allocation, index lease, empty-block
  release, `len` bookkeeping). `write_with` is untouched.
- The adapter's `read_into_uninit` uses `TcpStream::try_read_buf`, whose
  `BufMut for &mut [MaybeUninit<u8>]` reads into uninitialized capacity and
  keeps tokio's readiness/`WouldBlock` contract. It fills the first window then,
  only if it is exactly full, the second. If the second attempt returns
  `WouldBlock`, `Interrupted` or any other error, the already-filled prefix is
  kept and the error resurfaces on a later pump, so no accepted byte is
  discarded and no data is lost relative to a single `readv`.

## Correctness and safety

- Full suite, debug profile: `cargo test --features tokio,test-peer` —
  `293 passed, 0 failed, 5 ignored` at the lib level (298 including the
  integration targets), including every splice/relay/half-close test.
- Linux node (Rust 1.96.0, release): `cargo test --locked --release
  --features tokio,test-peer --lib` — `293 passed, 0 failed, 6 ignored`. Exact
  `Cargo.lock`.
- Miri (`nightly-2026-07-11`, `cargo miri test --lib tx_uninit`): 5 passed.
  Miri tracks initialization of every byte and found no exposure of
  uninitialized memory in the new buffer logic.
- New buffer tests: only-written bytes are visible; appends stay contiguous;
  a `readv` crossing a block boundary commits both blocks; a ring-wrapped write
  commits the whole block; an error keeps nothing and returns the fresh block;
  `commit_written` zero-fills only the gap before a write.
- The old safe `write_with_never_offers_uninitialized_bytes` contract still
  holds: fresh blocks offered through `write_with` still read as zeros.

## Assembly evidence

Server build: `CARGO_PROFILE_RELEASE_LTO=fat`,
`CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1`, `CARGO_PROFILE_RELEASE_PANIC=abort`,
`RUSTFLAGS=-C target-cpu=native`, plus `CARGO_PROFILE_RELEASE_DEBUG=1` for
source-line attribution. Binary:
`7afae47fc9b41e31c35b148eb2002d15fa759c6f06b1da85f68118f1da4eca00`.

`objdump -dl -C` over the two probe wrappers (`tx_readv_probe::write_zero`
calls the safe `write_with`; `tx_readv_probe::write_uninit` calls the new
path):

| inlined symbol | `write_zero` | `write_uninit` |
|---|---:|---:|
| `zfstack::buf::Block::spare_mut` | 10 | 0 |
| `zfstack::buf::Block::commit_written` | 0 | 12 |

Every `callq memset@GLIBC` in `write_zero` is inside `Block::spare_mut`
(`src/buf.rs:176`, the eager `data[init..end].fill(0)`). Every `callq memset`
in `write_uninit` is inside `Block::commit_written`, whose fill is guarded by
`if start > self.init` and is skipped on the measured hot path, where a fresh
block has `start == init == 0`. The `readv` path therefore executes zero
`memset`s of the offered window; the safe path executes one per fresh block.
Raw context is in `asm_analysis.txt`.

## Linux A/B measurements

Node: `root@38.207.178.131:22100`, 4 × i7-1265U (KVM), Linux 5.4.0, Rust
1.96.0. `taskset -c 3`; three alternating rounds per payload; process CPU
(`CLOCK_PROCESS_CPUTIME_ID`) reported as `cpu_ns_per_op`. Both arms are the
same binary, so all build and host differences cancel; only the code path
differs. Measured binary:
`f723e338c1043cfe6fca0302f5d07349c73f57b6a381ef75ab6ad5ee55d00af9`.

`cold` = a fresh 64 KiB block is allocated and freed per operation, the
worst case when concurrent short responses exceed the 16-block pool cache.
`warm` = the pooled block is reused, where the one-time zero fill was already
paid.

`final_cold.jsonl` (median of 3):

| payload | zero (ns/op) | uninit (ns/op) | speedup |
|---:|---:|---:|---:|
| 64 B | 2308.4 | 568.4 | 4.06× |
| 512 B | 2282.3 | 576.2 | 3.96× |
| 1500 B | 2292.7 | 607.3 | 3.78× |
| 4096 B | 2405.8 | 626.9 | 3.84× |
| 65535 B | 4591.9 | 2523.2 | 1.82× |
| 65536 B | 4621.3 | 2571.1 | 1.80× |

`final_warm.jsonl` (median of 5) shows the amortized path is unchanged:
64 B 155.8 → 157.1 (1.00×), 512 B 167.1 → 166.5 (1.00×), 4096 B 206.1 →
205.9 (1.00×). An earlier build showed a +1.4–2.3 % warm delta; after removing
two redundant block-index lookups from the new path it is within run-to-run
noise. Warm A/A noise on this shared host is of the same order.

The same probe on the development Mac (arm64, release) reproduced the effect
(cold 512 B: 1225 vs 497 ns/op) and the warm parity.

## Interpretation

The win is bounded by fresh-block allocation, not by steady state:

- Steady, pooled traffic (≤ 16 blocks live) already kept `init == 64 KiB`, so
  the safe path was a no-op there and the new path is a no-op too.
- When short-lived responses exceed the pool, every fresh 64 KiB block was
  zeroed for a possibly tiny read. That is the measured cold case, and it is
  1.8×–4.1× cheaper per operation, saving ≈ 1.7–2.1 µs of process CPU.
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
