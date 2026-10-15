# Bounded coverage-guided targets

This workspace adds libFuzzer to the existing deterministic packet mutation
coverage. It does not replace the simulator or the event-driven L0 harness.
Use `cargo-fuzz` **0.13.2**, Rust **nightly-2026-07-11**, and the committed
`Cargo.lock` (`libfuzzer-sys` **0.4.10**). Production builds do not enable any
fuzz feature or link libFuzzer.

```sh
cargo +1.94.1 install cargo-fuzz --version 0.13.2 --locked
cargo +nightly-2026-07-11 fetch --manifest-path fuzz/Cargo.toml --locked
cargo +nightly-2026-07-11 fuzz run wire_parser -- -runs=10000 -seed=43105 -timeout=5 -max_len=4096
cargo +nightly-2026-07-11 fuzz run sequence_sack -- -runs=10000 -seed=43105 -timeout=5 -max_len=1024
cargo +nightly-2026-07-11 fuzz run event_script -- -runs=1000 -seed=43105 -timeout=5 -max_len=128
```

The defaults use ASan; debug assertions/overflow checks remain on. `cargo-fuzz`
0.13.2 has no `--locked` run option, so prefetch with the lock and preserve/hash
both manifest and lock in the run identity. Campaigns should use a **writable copy**
of the checked-in seed directory as the extra corpus argument to avoid silently
turning exploration output into reviewed regressions. Retain crash artifacts and
stderr on failure. These finite run counts are launch examples, not calibrated
coverage or release thresholds.

`cargo +1.94.1 test --manifest-path fuzz/Cargo.toml --locked --lib` runs all checked-in
seed files and 64 deterministic bounded inputs without requiring nightly. It is
seed smoke, not coverage-guided evidence. Empty required seed directories fail.

## Input grammar and oracles (version 1)

- `wire_parser`: at most 4096 bytes. Parse raw IP/TCP, plus the same bytes as a TCP
  segment inside a checksummed IPv4 envelope with trusted TCP checksum. This
  reaches malformed TCP header/options paths without requiring every mutation to
  solve a checksum. Rejecting malformed packets is valid; parser bounds and
  consistency remain checked.
- `sequence_sack`: first 16 bytes define ISN/reference/signed displacement; at most
  128 five-byte commands mutate a real, charged scoreboard (SACK, partial ACK,
  mark-lost, retransmit, reneging). Valid method preconditions are enforced by the
  grammar. Incremental counters, sequence wrapping and full final release are
  checked after the bounded run.
- `event_script`: byte 0 modulo 3 selects legal, finite-fault or malformed input;
  byte 1 sets 64–16384 bytes each way. At most 32 two-byte operations pause reads,
  set finite loss/egress Full, inject malformed packets or advance bounded virtual
  time. Full recovery explicitly calls `egress_released`; it is not implicit in
  removing a sink limit. Legal/fault classes require correct full bytes and EOF
  after faults end; malformed input requires invariants and owner release, not
  successful transfer. The simulator's same-time guard and libFuzzer per-input
  wall timeout bound hangs. The existing Sim polls applications, so this target
  cannot certify event-only liveness.

Dynamic `Sim.gap_a` changes are excluded from v1. During generator construction,
changing quantization between partial `run_until` calls exposed a simulator
same-time spin (a pending B→A delivery could quantize before `now`). It is a
scheduler/generator contract issue, not evidence of a TCP production regression.
The existing fixed-gap tests continue running. Initial generator drafts also
omitted `egress_released` after Full; requiring that real release signal fixed
that invalid recovery precondition. No production code was altered for either.

The checked-in seeds are hand-authored boundary inputs, with identity and provenance
in `corpus/manifest.json`. A newly discovered crash must be triaged with its
source/tester/toolchain hashes, target, original bytes, seed, failure assertion and
classification. Replay a single input with `cargo +nightly-2026-07-11 fuzz run
TARGET PATH`. Use `cargo +nightly-2026-07-11 fuzz tmin TARGET PATH` for reduction;
verify the minimized input retains the same input class, preconditions and
assertion before promoting it. Never overwrite an older seed when changing the
grammar version.

The QA safety runner freezes the nightly above and executes each 30-second,
seed-1 campaign with explicit native `--target`, ASan and `--dev` (the registered
QA build profile). It copies the reviewed target's seeds into a fresh per-case
`fuzz-corpus/`; the original corpus is checked before and after execution.
`fuzz-seeds.json` records the initial seed hashes/sizes and actual copy path.
LibFuzzer receives that writable directory and an explicit per-case
`fuzz-artifacts/` prefix. Each case directory is created exclusively, so reruns
cannot overwrite prior crashes. Raw generated corpus/crash bytes remain local;
archives include only the bounded seed/failure indexes and command output.

Before native ASan execution, the runner checks the selected compiler's actual
host and target-spec sanitizer support. Cross targets and unsupported tools are
reported as capability limitations (`NOT_RUN`), never native qualification. The
separate sanitizer test requires `detect_leaks=1`; targets without declared
LeakSanitizer support (including current Apple Darwin targets) report `NOT_RUN`
for that case. Native ASan fuzzing still runs there, with its evidence explicitly
scoped to ASan rather than a leak-detection guarantee.
