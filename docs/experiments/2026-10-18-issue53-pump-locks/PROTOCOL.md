# Issue 53 ordinary production pump diagnostic

This package prepares temporary source overlays exported from fixed commits.
The scripts may be published as reproducible documentation evidence. The probe
is enabled only in those separate exports, not in the production branch/build
path or any QA case/threshold. It measures production `Driver::pump` itself,
using the real simulator handshake and the constructor from
`src/tokio_adapter/tests/qa.rs::prepared_stream`.

## Frozen source

- A: `ed4655e0798efc6b02883967cf933291dba059c8`
- B: `e0cd89ab745f025cbc5ccb89c69aba0681a2a13e`
- Rust: 1.94.1, release, fat LTO, one codegen unit, panic abort, incremental off.
- Fixture seed: 40960. Both source versions use identical probe text.
- Only the diagnostic overlays replace the three source-level `q.lock()` sites
  inside `pump` with a counted guard. The formal timing overlays leave the entire
  production `pump` body untouched. Other queue locks are not counted.

Unlike a release lib-test executable, this is a normal example binary, so the
requested panic=abort setting applies without unstable compiler options.

## Preparation and builds

The root agent executes all commands through the SSH gateway. Builds and all runs
must be wrapped in the existing host QA Lease, using `socket.gethostname()` for
the host domain. These scripts do not acquire or replace that lease.

```bash
python3 prepare.py --repo /root/work/zfstack-issue53-20261018 --output /root/work/zfstack-issue53-evidence-20261018/overlays
```

`MANIFEST.json` lists the exact command and build environment for each overlay.
The optional wrapper builds selected labels sequentially, enforces a separate
native target directory for each, and records toolchain, whitelisted build
environment, commands, complete logs, exit status, and binary hashes:

```bash
python3 build_matrix.py --manifest overlays/MANIFEST.json --output build-counts --labels A-counts B-counts
python3 build_matrix.py --manifest overlays/MANIFEST.json --output build-time --labels A1 A2 B
```

The wrapper must also run under the root agent's host QA Lease. To build a single
diagnostic overlay manually, execute the following in that overlay directory:

```bash
env CARGO_INCREMENTAL=0 CARGO_PROFILE_RELEASE_LTO=fat CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 CARGO_PROFILE_RELEASE_PANIC=abort cargo +1.94.1 build --locked --release -p zfstack --example issue53_pump_probe --features tokio,test-peer,issue53-pump-counts
```

Omit `issue53-pump-counts` for A1, A2, and B timing builds. Record actual inherited
RUSTFLAGS, target, wrappers, build command, and toolchain in the parent run log.
A1 and A2 are independent baseline builds with distinct Cargo target directories.

## Diagnostic cases

| Case | Setup | Measured interval | Expected A / B queue locks |
|---|---|---|---|
| idle1 | One established ordinary stream | 1,000 warm-pool pump calls | 3,000 / 1,000 |
| idle64 | One driver, 64 established streams, 64 peers | 64,000 round-robin pump calls | 192,000 / 64,000 |
| consumed | Real peer sends 64 bytes, ordinary app poll_read copies and verifies them | One subsequent pump reporting those 64 consumed bytes | 3 / 2 |
| tx | Ordinary app poll_write queues 64 bytes | One pump moves the bytes into core TX | 3 / 1 |

Idle cases warm 10,000 pumps before measurement. The one-shot consumed and TX
cases do not warm away their target condition. Setup calls to pump are excluded
by resetting counters immediately before the selected interval. Every case
requires correct queue state and final global reserved bytes=0; idle cases also
require unchanged live reserved bytes during repeated pumping.

The consumed case never fabricates `rx_consumed_pending`: `Sim` receives actual
client payload with the server application paused, `on_event(Accepted)` moves it
into the adapter, and `poll_read` consumes and checks the payload before counting.

Counted lock wait/hold times include diagnostic clock and counter effects and
may include preemption. They describe instrumented scope only, not production
lock contention. No formal contended-latency claim is made.

```bash
python3 run_matrix.py --manifest overlays/MANIFEST.json --output counts --mode counts --cpu 2
python3 summarize.py counts/raw.jsonl
```

Choose an actually permitted CPU and record it. `--cpu 2` is an example, not a
claim that CPU 2 was selected. The runner verifies the allowed affinity.

## Timing protocol

Only `idle1` and `idle64` are timed, each with exactly 1,000,000 production pump
calls after 10,000 warmup calls. Each process prepares its own real connections;
setup and destruction are outside the reported timer. The measured wall time
and Linux calling-thread CPU time cover the same loop (plus boundary clocks).
The denominator is pump calls, not requests, packets, or business bytes.

All A/A samples come first: four paired runs for each case, alternating A1/A2 and
A2/A1. Then twelve paired runs per case, alternating A1/B and B/A1. All samples,
orders, source and binary hashes are written to PLAN.json before any run. The
runner rejects any existing output directory and never overwrites evidence.

```bash
python3 run_matrix.py --manifest overlays/MANIFEST.json --output timing --mode time --cpu 2
python3 summarize.py timing/raw.jsonl
```

Before and after every run the runner records compiler presence, loadavg, and
host CPU counters (including steal). It refuses to start with a compiler active
and stops without replacing samples if a compiler appears, a sample fails, the
binary changes, or the 60-second subprocess deadline expires. This checks the
boundaries; it cannot prove that a short unrelated compiler never ran between
them. The root agent should inspect the node before and during the window.

No performance-dependent stopping, outlier removal, retries to obtain favorable
results, or silent augmentation of the predeclared 4/12 pairs. A blocked/incomplete
window is reported as such and all completed/failed rows remain in raw.jsonl.

Summaries provide each paired CPU ratio, their median, and a descriptive 95%
bootstrap interval (10,000 resamples, seed 53). A/A is a repeatability observation,
not calibration or permission to subtract measured bias from A/B.

## Limits

Idle pump calls are artificial wake opportunities on real established production
objects. They isolate the fixed ordinary-pump work, including real queue locking,
budget lookups, core access, time reads, and Arc ownership. They do not measure
the normal frequency of those wake opportunities in a user workload.

These results cannot establish network throughput, P99 request latency, global
fairness, or absence of contention tradeoffs. The code merges critical sections;
fewer lock operations can coexist with longer uninterrupted lock ownership. The
production concurrency/close/reentrant-wake tests and the independent review are
separate correctness evidence. Linux TUN, WireGuard and ZFC performance remain
unmeasured by this package.
