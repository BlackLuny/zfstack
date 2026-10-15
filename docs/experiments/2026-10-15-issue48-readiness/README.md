# Issue 48: independent offline environment verification

## Scope

The relay now polls/registers socket read readiness once per pump before TX
preparation. The ready bulk read loop and all budget/release handling remain.
A shared side-effect-free write-acceptance predicate preserves close progress
when an upstream socket is silent.

Source base: 88a2e3f7a2e86fb6948223054a377e8a7f831af1.
Candidate production code: d00e339f367277fd91cd618536466a8c9ddb2cba.

## Functional evidence

All local execution used the existing independent offline development service.
No Mac or another development computer was used.
208 selected source files were verified against upstream Git blob identities.

- Baseline core/integration: 283 passed, 0 failed, 7 existing ignored.
- Four new regression tests against baseline: 2 failed, 2 passed.
- Candidate: 287 passed, 0 failed, 7 existing ignored.
- Candidate no-default-features check passed.
- The idle regression held 12,952 ledger bytes before pumping. Baseline
  retained 79,064 afterward (+66,112); candidate retained 12,952.
  These are ledger bytes, not process RSS.
- Socket-wake arrival, stale readiness/EOF, obsolete quota waiter cleanup,
  and silent socket/core shutdown are covered. Existing RST, half-close,
  delayed-ACK, payload and release-epoch regressions also passed.

Exact-lock GitHub CI on Rust 1.94.1 passed lint, test, bench, L0, tools and
qa/gate: https://github.com/BlackLuny/zfstack/actions/runs/38789840635 .

## Local measurement limitations

The offline image has Rust 1.98.1/Python 3.11.2, no rustfmt/rustup, and lacks
the main lockfile's mio 1.2.4 and benchmark smoltcp Git commit. The local
diagnostic copy excludes benchmark/harness workspace members and resolves
cached dependencies (including mio 1.2.3 and tokio 1.53.1). Repository
Cargo.toml/Cargo.lock remain unchanged. This is supplemental evidence, not
an exact-lock local qualification.

Measurements use release-profile lib-test binaries, not the production
fat-LTO/panic-abort profile. The host is shared; no physical-domain exclusive
lease, fixed core pinning, cgroup noise certification, true-network mixed-RR
tail-latency or cross-platform performance qualification was obtained.
Existing ignored heavy tests remain unexecuted locally. Do not generalize
the micro-workload improvement to production throughput.

## Paired measurements

Two cases: 250,000 idle relay pumps, and the existing bidirectional
splice/half-close test. Each case first ran four same-binary A/A pairs,
then eight alternating ABBA/BAAB blocks (16 samples per arm). No failed
measurement slot was discarded or replaced. All measured tests passed.

The first experimental attempt shared a Cargo target directory across
different checkouts and accidentally reused the baseline test binary.
That attempt is INVALID and excluded entirely. The valid run uses separate
target directories and verifies candidate idle ledger invariance and baseline
ledger growth on every idle sample, plus distinct binary hashes.

CPU is child-process user+system time, including test setup/teardown;
wall time is subprocess elapsed time. Intervals below bootstrap the eight
paired block ratios (5,000 samples, seed 48); they are descriptive and do not
establish a calibrated non-regression gate.

### tokio_adapter::tests::qa::issue48_measure_idle_pump

A/A CPU second/first ratios: [0.9402888453893178, 0.9693426249305405, 0.9183064633037519, 0.9305266380893201]

- cpu: candidate/baseline geometric mean 0.3417; descriptive 95% interval [0.3331, 0.3510].
- wall: candidate/baseline geometric mean 0.3434; descriptive 95% interval [0.3342, 0.3535].

### tokio_adapter::tests::splice_relays_both_directions_and_half_closes

A/A CPU second/first ratios: [0.8352715245920721, 1.0566534687557296, 0.8214897196538853, 1.0076300316026916]

- cpu: candidate/baseline geometric mean 0.9322; descriptive 95% interval [0.8670, 1.0014].
- wall: candidate/baseline geometric mean 0.9757; descriptive 95% interval [0.9545, 0.9989].

## Artifacts

Raw samples and binary hashes: [paired-perf.json](paired-perf.json).
The measurement-only test snippet is in [measurement-snippet.txt](measurement-snippet.txt); it is not a new ignored test or a CI gate.
