# Bounded concurrency model

Run `cargo +1.94.1 test --manifest-path qa-models/Cargo.toml --locked -- --nocapture`.
This is a **copied model**, not an instrumented execution of `GlobalBudget`.
It models the logical waiter's publication in `src/budget.rs` at baseline
`df432fd`: release epoch, published waiter flag, mutex-protected presence, and
one delivered wake. The fields and both threads use Loom synchronization. Each access to the
epoch/published flag is a separate mutex transition modeling the SC total order;
the slot uses a different mutex. Loom 0.7.2 treats SeqCst loads/stores as AcqRel
([upstream limitation](https://github.com/tokio-rs/loom/blob/v0.7.2/README.md#unsupported-features)),
so direct atomic substitution raises a false alarm for this SC-dependent
handshake. This model tests SC transition ordering, not a weak-memory proof.
No model-only fences or locks are copied into production.
The production implementation continues using std, with no new cfg or branch.

The model fixes a stale epoch from one failed allocation, one registration and
one release, with preemption bound 2, at most 100,000 executions and 1,000
branches per execution. The passing test prints the executed model count.
The required negative control checks the epoch before publication and must panic
with `lost unique release`. An unexpected other panic does not satisfy that test.

Not modeled: real task scheduling or Wakers, physical byte/port/peer fit scans,
HashMap/VecDeque capacity, cache-holder callbacks, cancellation, allocator failure,
multiple waiters or repeated epochs. This supplies model-only evidence for one
handshake; real production code is exercised separately by A0 and A1 tests.
It is not a claim of exhaustive concurrency correctness.
