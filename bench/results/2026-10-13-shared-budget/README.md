# Real-TUN shared-global-budget recovery and driver lifecycle

For [#23](https://github.com/BlackLuny/zfstack/issues/23), **8/8 cases and 24/24 driver generations passed** with two production Tokio adapters on separate OS threads sharing one `GlobalBudget`. Real kernel TCP clients verified **408 MiB** across the matrix. Each generation reached natural global-budget pressure, recovered a parked writer after another port released its charge, cancelled another parked writer, and returned all checked physical/cache/connection-permit counters to zero before rebuilding.

This adds a runnable diagnostic and evidence-admission regressions; **no library allocation/TCP policy or default is changed**. It is independent of pending PRs #33–#35 and starts from main `b170affa14e70d73f3d0834b2e06579dde70ca85`.

## Topology and claims

`bench/examples/shared_budget_tun.rs` starts two `spawn_with_source_factory` drivers, each on its own current-thread Tokio runtime and OS thread. Both receive the same `Arc<GlobalBudget>`, with independent port instances created by the production factory. Both deliberately use `IfaceId(0)` and `PeerId(7)`: peer identity must remain scoped to its port instance. Destinations `10.223.0.2` and `10.223.1.2` select the two drivers through a single nonpersistent TUN, `zfmp0`.

The reader has a 256-packet bounded queue per driver and uses borrowed ingress. There is no upstream Python listener, relay socket, WireGuard authentication/encryption, loss emulator, or packet offload. Host ingress queues, client/kernel buffers, application buffers and controller metadata are outside the stack ledger. Ledger zero therefore means all resources accounted by these library counters were released, **not zero RSS or complete accounting of every allocation**.

The previous isolate mode co-schedules sans-IO shards on one thread. The existing library regression uses simulated clients. This probe exercises real kernel TCP plus distinct production-driver threads and lifetime ownership.

## Three checks in every generation

1. **Natural TCP load.** Establish eight application-confirmed connections per port, then send 1 MiB on each. Clients compare every byte against its stream's pattern and require exact EOF length. All sixteen complete. Driver snapshots must show actual global-level allocation refusals, not merely low configured ceilings. Global, port and peer sampled usage must stay within the configured hard limits.
2. **Controlled cross-port release.** Prepare a new port-B stream. Allocate real host buffers under port A's `TxBlock`-tier leases until admission fails; these are deliberately injected buffers, not reported as TCP traffic. Start the port-B response and observe a real physical/core allocation waiter. Require a live client with no byte progress for 300 ms **without snapshots or control requests**. Release A's buffers, then observe B's client progress without manually notifying or sampling either driver. The complete 1 MiB response must verify. This is a targeted release mechanism check, separate from natural pressure.
3. **Cancellation and rebuild.** Repeat the gate, cancel and join B's driver, and join its application tasks. At least one pending application writer must return an error due to shutdown. B's peer usage must become zero while A's charged buffers remain allocated, despite both using peer ID 7. The controller explicitly closes the kernel client socket; this is not a claim that driver cancellation sends a remote RST. Release A, shut down/join its driver and reader, then require `global.reserved`, `global.cached_bytes`, active/TIME_WAIT permits and TIME_WAIT bytes all equal zero. Repeat on the same shared global-budget object with newly created port instances.

Snapshots wake drivers, so they are explicitly absent from the 300 ms held interval and the release-to-progress interval. Ordinary TCP traffic and library timers remain active. Observed delay includes allocator releases, driver/kernel scheduling, network delivery and a 1 ms requested polling interval; it is not a precise waker latency benchmark or an exclusive attribution of every wake source.

## Matrix results

Each case contains three generations. The global ceiling is 1 or 2 MiB; each port and peer ceiling equals the global ceiling, so the shared **global** limit constrains concurrent ports. Two ports are not each granted a separate global allowance.

| MTU | Shared global budget | TX policy | Generations passed | Release → observed progress (µs) |
|---:|---:|---|---:|---|
| 1500 | 1 MiB | Disabled | 3/3 | 2128 / 2120 / 2133 |
| 1500 | 1 MiB | Enabled | 3/3 | 2154 / 2115 / 2110 |
| 1500 | 2 MiB | Disabled | 3/3 | 2144 / 2119 / 2225 |
| 1500 | 2 MiB | Enabled | 3/3 | 2126 / 2152 / 2136 |
| 512 | 1 MiB | Disabled | 3/3 | 2620 / 7435 / 2131 |
| 512 | 1 MiB | Enabled | 3/3 | 2301 / 2509 / 2162 |
| 512 | 2 MiB | Disabled | 3/3 | 2347 / 244 / 2270 |
| 512 | 2 MiB | Enabled | 3/3 | 2283 / 2281 / 2880 |

All generations recorded natural global-level refusals: **15–762 combined refusals per generation**. Natural traffic verified 384 MiB; the release checks verified another 24 MiB. All 48 held intervals (release and cancel) observed zero response bytes and a live, incomplete client. Every cancellation woke one pending application writer, and all 24 post-shutdown ledger/permit checks passed.

The largest sampled global charge, including injected pressure, was 1,039,224 bytes under the 1 MiB ceiling and 2,087,800 under 2 MiB. The progress reserve is intentionally not bypassed by the gate.

MTU-1500 cases had zero reader-queue drops. MTU-512 cases recorded **9,961 bounded reader-queue drops** in total (per-generation counters retained in `summary.json`); TCP retransmission recovered and every required payload verified. These are host-queue drops, distinct from memory-allocation refusals. This is not a lossless or performance comparison.

TX enabled means explicit check/warm/cold 1/3/10 seconds and large/small warm floors 2/2. Each generation lasts roughly one second, so these short rounds **do not prove automatic cache decay**. Multi-peer cache-decay evidence is separately in PR #35. No PacketPool trim policy is used here, and no performance benefit is inferred from the release timings.

## Validation and provenance

- Python evidence/harness regressions: **20/20**, including six new cases rejecting absent waiters, missing pressure/rounds, premature or short recovery, accounting violations, and cancellation that releases the other port or leaves charges.
- Existing `tokio_adapter::tests::ports_sharing_a_small_global_budget_all_make_progress`: **1/1 passed** with `tokio,test-peer`, release mode. This is a targeted regression, not a claim that the full library suite was rerun.
- New example clippy/all-features, formatting and diff checks passed; existing library warnings remain.
- Runner process exit, nonpersistent TUN disappearance, empty owned namespace, child reaping and namespace deletion passed for every case.
- Initial one-generation probe also passed (17 MiB), bringing the archive to nine cases, 25 generations and 425 MiB. It is retained separately from the matrix.

Initial source checkpoint `6c20ee7c37715138b1ce11aa1b251017e5eb63df`; matrix checkpoint is recorded in `build-manifest.json` (`b69bbfb…`). The latter adds explicit checks that clients remain live during the pressure gate. Later commits only add documentation, evidence and evidence-admission tests. Binary hashes distinguish both builds. Rust 1.94.1, release/fat-LTO/codegen-units=1/panic=abort, default zfstack feature, unchanged Cargo.lock.

The shared VM exposes five CPUs with a four-CPU cgroup quota; no affinity partition or isolated performance environment is claimed. The dedicated Docker rig uses its own network, never host networking. The runner creates a fresh namespace for every case and has a 210-second process timeout plus bounded cleanup. The diagnostic rig was removed after verification.

## Reproduction

`raw-evidence.tar.gz` contains the nine cases, commands/results, raw JSON samples, build/test logs, tested sources and manifests; executable bytes are excluded. To recheck:

```sh
mkdir extracted
tar -xzf raw-evidence.tar.gz -C extracted
(cd extracted && sha256sum -c RAW_SHA256SUMS)
python3 analyze.py extracted
```

The analyzer runs the archived admission checker, validates all eight matrix combinations and recomputes `summary.json`. Outer `SHA256SUMS` covers the committed report artifacts.

For a fresh run, build the matrix source checkpoint:

```sh
CARGO_PROFILE_RELEASE_LTO=fat CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1 \
CARGO_PROFILE_RELEASE_PANIC=abort \
cargo build --locked -p zfbench --release --example shared_budget_tun
```

Use the existing [TUN-rig Dockerfile](../2026-10-13-pr31-review/Dockerfile) with the managed proxy CA. Create a dedicated `zfstack-shared-budget-rig` container with TUN/netns privileges, `--ulimit nofile=65536:65536`, the checkout read-only at `/repo`, and a fresh writable evidence directory at `/evidence`; ensure `/dev/net/tun` exists. Copy the built example to `/evidence/bin/probe`. On the host set `ZF_SHARED_BUDGET_EVIDENCE` to that directory and run the committed `matrix.py`. Remove only the owned rig afterwards. Do not reuse output directories or run the example directly in an existing network namespace.

## Remaining scope

This closes a concrete coverage gap for cross-thread, cross-port global-budget release and cancellation/rebuild accounting. It does not close all P4 work: full zfc WireGuard/egress-queue integration, broader multi-port fairness and service guarantees, long-duration leakage/RSS testing, historical timeout attribution, and formal performance acceptance remain outstanding. There is no deployment and no default enablement; #23 remains open.
