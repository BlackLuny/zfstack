# Ingress overflow diagnosis and default-policy assessment

Follow-up to [PR #36](https://github.com/BlackLuny/zfstack/pull/36) and [#23](https://github.com/BlackLuny/zfstack/issues/23). Library main remains `b170affa14e70d73f3d0834b2e06579dde70ca85`; only the diagnostic example, runner and reports change.

The original **9,961** drops count a specific probe operation: its independent TUN reader calls `try_send` on a **256-packet per-port channel**, then discards the packet on `Full`. This is an explicit overload policy in the probe, with timing-dependent frequency. It is not evidence of random corruption, a TX-trim ownership defect, or an unexplained loss inside the core. Further instrumentation discovered a second, independent loss layer: the kernel TUN's default **500-packet** queue can overflow before the reader runs.

## Controlled evidence

All cases use real kernel TCP, two production adapter threads, eight flows per port, the original pressure/release/cancellation checks, and fresh owned namespaces. Each round verifies 16 MiB of natural traffic and a separate 1 MiB release response. The deliberately cancelled transfer is not counted as complete traffic. Each case enables or disables TX trimming explicitly.

1. **Frozen original executable:** 40 rounds, alternating MTU/policy combinations. MTU 512 had application queue overflow in **20/20 rounds**, totaling **10,404 packets**; MTU 1500 had **0/20**. Both trim settings reproduced the loss. This executable did not measure kernel drops; that field is unknown, not zero.
2. **Instrumented discovery grid:** 180 rounds, including queue capacities 256/4096, MTU 512/1500, global budgets 1/2 MiB, trim off/on, and single-CPU controls. At MTU 512 and capacity 256, all 50 rounds dropped packets: **51,107**, of which **51,096 were checksum-valid, payload-free ACK-only packets**. Capacity 4096 removed application queue overflow in all 50 MTU-512 rounds, but kernel drops remained. Even MTU 1500 overflowed the 256-packet application queue once in 40 rounds (three packets), so 1500 is not intrinsically immune.
3. **Final executable, independent queue controls:** configure the kernel queue separately from the application queue. Both remain bounded. The following controls use MTU 512 and 1 MiB global budget, combining trim off/on, five rounds per setting:

| Application queue | Kernel TUN queue | CPU placement | Rounds | Application drops | Kernel drops | Rounds with any overflow |
|---:|---:|---|---:|---:|---:|---:|
| 256 | 4096 | Normal | 10 | 6,708 | 0 | 10/10 |
| 4096 | Default 500 | Normal | 10 | 0 | 221 | 3/10 |
| 4096 | Default 500 | One CPU | 10 | 0 | 5,853 | 10/10 |

All 6,708 application drops in this final control were ACK-only. The 221 normal-placement kernel drops were already present immediately after natural traffic, before cancellation. Single-CPU controls also recorded kernel drops before cancellation; the round totals additionally include the release/cancel phases. Thus teardown alone does not explain the loss.

The loss rate is not a universal random probability: queue capacity, packet rate and thread scheduling change the outcome. A small MTU creates more packets for the same byte workload; fast burst egress and ACK generation can temporarily outpace the TUN reader or driver input consumer. Pinning all threads to one CPU exposes that scheduling sensitivity. No particular scheduler defect or host fault is established.

## Clean acceptance matrix

With **4096 packets per application port and 4096 in the kernel TUN queue**:

- MTU 512/1500 × shared global budget 1/2 MiB × trim off/on × 10 rounds = **80/80** passed.
- MTU 512, 1 MiB, all threads on one CPU, trim off/on × 10 rounds = **20/20** passed.
- **100/100** rounds had zero application `Full` drops and zero kernel TUN `tx_dropped` increments. Maximum sampled application queue occupancy was **982**, below 4096.
- **1700 MiB** verified byte-for-byte. Every round observed natural global allocation pressure, released a parked writer, cancelled/joined the other driver without releasing the surviving port's charge, and returned physical/cache/connection-permit counters to zero.
- Release-to-observed-progress was **313–6,874 µs**, including polling and scheduling; no latency improvement is claimed.

Across all stages, **40 cases / 350 rounds / 5950 MiB** completed their payload and lifecycle checks. Five discovery cases correctly failed the new clean-ingress acceptance because kernel drops remained; their failures are retained, not rerun away or relabeled as clean. Negative controls use the explicit `--allow-ingress-drops` override and retain counters/`clean_ingress=false`.

The result excludes queue-overflow contamination for these measured workloads, **not an unconditional no-loss guarantee**. Arbitrarily larger bursts, slower consumers or different schedulers can exhaust any finite queue. `ingress_closed` separately counts packets arriving after a driver receiver closes during deliberate cancellation; such teardown discards are not represented as successful delivery or conflated with overload.

## Probe changes and tradeoffs

- Add independent `--ingress-capacity` and `--tun-queue-len`, each defaulting to 4096; zero kernel setting retains the platform default for diagnosis.
- Record per-port receives, overflow packets/bytes, ACK-only drops, sampled occupancy, closed-receiver discards and kernel TUN drops. Join the reader before reading final counters, removing the previous late-counter race.
- Fail clean acceptance on either layer's overflow, even when TCP ultimately completes. Missing kernel-drop evidence is also rejected. Diagnostic override is explicit and recorded.
- Keep the shared demultiplexing reader nonblocking: blocking behind one port would also stop the other port and contaminate the cross-port isolation test. A bounded capacity increase absorbs this workload's bursts; it does not implement general backpressure or repair a core defect.

The queue allocation tradeoff belongs to the **test harness**, not production memory tuning. Queued packet vectors and channel metadata remain outside the stack ledger. With the 2048-byte read buffer, the configured application queues can retain at most 8 MiB of raw packet bytes per port, plus allocation/channel metadata; the actual ACK-dominated workload uses less. Kernel socket-buffer memory is separate. This is not a claim that 1 MiB of stack budget accounts for the whole process. Neither the production adapter's queue policy nor zfc's queues were changed.

The existing `bench/src/tunproxy.rs` path uses bounded **batches and blocking_send**, unlike this example's per-port `try_send`. zfc uses its own authenticated ingress and egress queues. Neither path can be declared lossless solely from this probe.

## Default enablement decision

**This loss diagnosis is no longer a reason to reject TX trimming. It is not enough to switch every host's defaults on. Keep library defaults unchanged in this PR.** Following the user's acceptance rule, lack of a statistically clear speedup is not by itself a blocker: releasing unused cached storage is a valid optimization even if throughput/RSS changes cannot be resolved.

| Policy | Evidence of benefit/correctness | Current decision |
|---|---|---|
| TX cache trim | Main contains the low-rate fix. [PR #35](https://github.com/BlackLuny/zfstack/pull/35) measured large-block cache 16→0 in quiet and 16→2 under continuing low traffic at both MTUs, with real mapped peers. Existing trim regressions and this pressure matrix pass. | Ready for an explicitly enabled zfc integration candidate. Do **not** enable globally yet: this probe's short generations do not exercise the 3/10-second warm/cold thresholds, and the real zfc path has not been tested with the intended revision/policy. |
| PacketPool automatic trim | Explicit free-only trim, lock-external release and concurrency/lifetime regressions exist. [PR #33](https://github.com/BlackLuny/zfstack/pull/33) separately supplies continuing-low-rate host maintenance and measured 128→1 cached buffers. | Keep opt-in. That host implementation is not on main, and each actual PacketPool owner still needs a defined idle/low-rate maintenance schedule. zfc's borrowed WG buffers are not automatically covered by the bench PacketPool policy. |

Read-only zfc main inspection at `06bc341f00d48c45d14b1094f579df983baa2354` found its lockfile still pins zfstack `37c016d3e136426994e4b40198e236d5ffbc0fb6`, before the current TX fix, and its adapter does not install `TxCacheTrimPolicy`. The next concrete enablement gate is a zfc candidate pinned to the intended revision, with an explicit policy, tested through real WG peer ingress, egress saturation, cancellation/rebuild and quiet/low-rate/reburst cycles. Hosts must honor `Shard::next_deadline()` even without packets. Do not reinterpret the probe's 1/3/10-second test settings as established production defaults; #23's 5/30/120-second check/warm/cold and 2/2 floors are a conservative candidate for that longer validation, not a measured result here.

Remaining correctness scope includes real zfc lifecycle/integration and the historically unassigned isolated timeout. Neither the backlog finding from PR #34 nor this queue-overflow finding proves every earlier timeout has the same cause. No deployment or zfc modification occurred.

## Validation, environment and reproduction

- **22/22 Python regressions**, including rejection of application/kernel drops and missing drop evidence.
- **17/17 targeted Rust trim regressions**, including concurrent PacketPool trim/get/drop, live owner preservation, low-rate/sustained TX reuse and budget wake semantics. Not a full-suite rerun.
- Example all-features clippy and formatting/diff checks pass; existing library warnings remain.
- All owned child processes were reaped; TUN devices disappeared; namespaces were removed. The dedicated rig was removed afterward.

Rust 1.94.1, release/fat-LTO/codegen-units=1/panic=abort, unchanged Cargo.lock and library source. Linux 6.18.44, CPUs 0–4 exposed, four-CPU quota. Single-CPU controls use CPU 0 for the whole process tree. The final grid ran with no concurrent build and **zero additional cgroup throttling events**; CPU placement still affected loss at the small kernel queue. Final-example compilation overlapped part of the earlier discovery grid, which is not used as a throughput benchmark. No external network path or packet-loss emulator is involved. The privileged diagnostic container used its own network, never host networking.

`build-manifest.json` identifies frozen original, instrumented and final executable/source hashes. `raw-evidence.tar.gz` contains all case outputs, failures, commands, archived validators/source, build/test logs and environment snapshots; no executables. Recheck in a fresh directory:

```sh
mkdir extracted
tar -xzf raw-evidence.tar.gz -C extracted
(cd extracted && sha256sum -c RAW_SHA256SUMS)
python3 analyze.py extracted > reanalyzed.json
cmp summary.json reanalyzed.json
```

For new measurements, build the final checkpoint recorded in the manifest using its command. Use the [existing rig Dockerfile](../2026-10-13-pr31-review/Dockerfile) in a dedicated `zfstack-queue-diagnosis-rig` container with TUN/netns privileges and `--ulimit nofile=65536:65536`. Mount the checkout read-only at `/repo`, a new writable evidence directory at `/evidence`, and `/etc/machine-id` read-only; ensure `/dev/net/tun` exists. Put the executable at `bin/final`, its full source SHA in `source-sha-final`, and run the committed `matrix-final.py` with `ZF_QUEUE_EVIDENCE` pointing to the host evidence directory. Every case owns a new namespace and uses bounded cleanup. Do not reuse output directories or run the example in an existing network namespace.
