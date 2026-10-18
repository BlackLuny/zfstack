# Issue #87: FIN outside the advertised receive window

Reproduction and fix, 2026-10-18. Base commit: `97c4b285d89ada5cf76ef9def43f9bbf663a5906` (`main` at start of investigation). Rust: 1.94.1, Linux x86_64. Only `src/conn.rs` is changed in production.

## Root cause and impact

The receive segment acceptability check considers a segment acceptable when **some** sequence bytes overlap `[RCV.NXT, RCV.ADV)`. The data path trims data at `RCV.ADV`, but previously the FIN path recorded and consumed FIN even when **the FIN's own sequence byte** was outside that interval. For a full window, data ending at `RCV.ADV - 1` followed by FIN at `RCV.ADV` advanced `RCV.NXT` one past `RCV.ADV`. The SWS avoidance branch in `calc_window_l` could then select `right = RCV.ADV` and perform unsigned `right - RCV.NXT`.

The observed unscaled jumbo case before draining was:

| State | Value |
|---|---:|
| `RCV.NXT` after consuming the invalid FIN | 65537 |
| `RCV.ADV` (exclusive right edge) | 65536 |
| Receive target | 65535 B |
| RX unread | 65535 B |
| Receive MSS, MTU 65535 (IPv4, no TCP timestamps) | 65495 B |
| TCP state | `CloseWait` (premature EOF) |

The original code, run with the added regression test **without either production fix**:

- **Debug**: `cargo +1.94.1 test --locked --lib issue87_fin_outside -- --nocapture` reproduced `src/conn.rs:2263:19: attempt to subtract with overflow`, with backtrace through `Conn::calc_window_l → calc_window → after_app_read → read_chunk` during a 1 KiB application read.
- **Release**: `cargo +1.94.1 test --locked --release --lib issue87_fin_outside -- --nocapture` reported `zero.window = 65535` while the full receive window should be zero. The wrapped subtraction incorrectly reached the maximum unscaled advertised window; the regression failed with `left: 65535; right: 0` (baseline was a separate detached worktree with only the regression tests copied in).

A second pre-fix failure surfaced during validation: refusing the invalid FIN alone is **not sufficient** for progress when a zero receive window reopens under jumbo MSS. The old `after_app_read` required at least **2 MSS** of growth before scheduling an ACK, which a roughly 64 KiB receive buffer cannot satisfy with a roughly 65 KiB MSS. With the original zero-window ACK already sent, this could prevent the receiver from advertising the reopened window and accepting a retransmitted FIN. The second, narrowly scoped fix announces `cur == 0 && new > 0` immediately; `calc_window_l` already enforces SWS avoidance, and the ordinary nonzero-window growth threshold is unchanged.

## Fix

1. Record FIN only if its own sequence byte satisfies `RCV.NXT <= FIN.SEQ < RCV.ADV`. Keep the immediate ACK request for the rejected FIN, so the sender sees the highest valid ACK and current window. Do not advance `RCV.ADV` artificially or clamp the subtraction to hide the invariant violation.
2. After an application read, request a window update when an exactly zero advertised receive window becomes positive after SWS avoidance. Retain the original >=2 MSS/doubling condition for nonzero windows.

No wire-protocol extensions, retries, new timers, heap allocations, locks, or new send RTTs are introduced by these branches.

## Regression coverage

`issue87_fin_outside_full_receive_window_is_deferred_until_retransmitted` and `issue87_fin_at_last_advertised_byte_still_completes_immediately` exercise the actual synchronized TCP input logic, including FIN mixed with the final data segment. Both tests use each combination of:

| IP MTU | Window scale | Advertised receive capacity |
|---:|---|---:|
| 1500 | off | 65535 B |
| 65535 | off | 65535 B |
| 1500 | on | 65536 B |
| 65535 | on | 65536 B |

The first test ensures the out-of-window FIN is not accepted, the full-window ACK advertises **zero**, 1 KiB reads do not cause underflow, a further read releases the SWS threshold and triggers a **new** window-update ACK, the peer's FIN retransmission is then accepted, every byte arrives intact, EOF completes, and logical and physical allocation accounting is released. The second checks that FIN **inside** the advertised window still completes immediately and that repeated 1 KiB application reads do not regress EOF or window invariants. Window calculation is also checked across all four pressure levels.

## Commands / interpretation

```bash
cargo +1.94.1 fmt --all -- --check
cargo +1.94.1 test --locked --all-features --lib issue87_
cargo +1.94.1 test --locked --release --lib issue87_
cargo +1.94.1 test --locked --all-features
```

Local regression tests in both Debug and Release passed after the fix. An initial full `--all-features` run passed (312 library tests, the throughput integration test, RX lifecycle, wire trace and docs; six library tests ignored by their normal annotations), before the final zero-window ACK tweak. Rerun the complete command on the final head and consult CI rather than treating this earlier run as proof of the final head.

Scope limitation: deterministic unit/adapter/integration tests validate the receive state machine, not a live NIC/WireGuard run or hardware CPU/latency A/B. A tiny extra conditional check and occasional **necessary** zero-window update ACK can marginally alter packet/CPU counts for repeated exact-zero-window workloads; bulk throughput/cycle-neutrality are not established by the regression tests alone.
