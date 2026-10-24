# TX capacity screening

The frozen question, matrix and decision rules are in
[`PROTOCOL.md`](../../docs/experiments/2026-10-23-capacity/PROTOCOL.md).
These are benchmark-only controls; no library defaults change.

| Environment variable | Unit | Effect in zfstack server/client adapters |
|---|---|---|
| `ZFBENCH_MAX_SND_BUF` | positive bytes, u64 | Override total core TX capacity, including unacknowledged bytes |
| `ZFBENCH_PREFETCH_MAX` | positive bytes, u32 | Override the unsent prefetch cap, preserving the flight cap |

Unset means the existing profile. Other stacks do not implement these controls.
Server/client results record requested values under `tx_tuning`; invalid values fail before
the benchmark creates network interfaces. Client proxy children inherit the
same environment. Do not confuse a low-RTT local client profile with WAN TCP.
The screening and confirmation do not cover remote WG mode.

## Reproduce on Linux

Build before measuring, and use the same binary for all arms:

```sh
export CARGO_PROFILE_RELEASE_LTO=fat
export CARGO_PROFILE_RELEASE_CODEGEN_UNITS=1
export CARGO_PROFILE_RELEASE_PANIC=abort
export CARGO_INCREMENTAL=0
cargo +1.94.1 build --locked --release -p zfbench
cargo +1.94.1 test --locked --release -p zfbench
python3 -m unittest discover -s bench/capacity
```

The included Dockerfile expects the managed executor's CA as a build secret.
Use a dedicated container with its own network, a mounted repository, CPUs 0–3
available, and permission to create/use TUN. The exact measured container
inspection, image identity and build logs are in the evidence archive.

```sh
docker build --secret id=proxy_ca,src="$CODEX_PROXY_CERT" -t zfstack-capacity:20261023 bench/capacity
docker run -d --name zfstack-capacity-20261023 --privileged --network none \
  --cpuset-cpus 0-3 --ulimit nofile=65536:65536 \
  -v "$PWD:/work" -w /work zfstack-capacity:20261023 sleep infinity
python3 bench/capacity/run.py qa/results/capacity-20261023
python3 bench/capacity/analyze.py qa/results/capacity-20261023 --output qa/results/capacity-20261023/summary.json
docker rm -f zfstack-capacity-20261023
```

The runner refuses an existing output directory. Use a fresh destination to
repeat the full plan; never retry a failed arm in place. Every attempt includes
its command, requested controls, binary identity, output, resource usage and
cgroup counters. The separate smoke does not enter comparisons. An outer
timeout stops the remaining plan rather than leaving overlapping workloads.

The analysis uses fixed-arrival ledgers, verifies their raw latency samples,
keeps missing requests in the denominator, and reports invalid blocks without
promoting them. Two pairs per cell are screening, not a confidence interval.
The original 2026-10-23 run's final audit additionally checks every command and
source hash against the frozen plan; see the report and `audit.json`.

## Independent confirmation

After screening, `confirm.py` freezes a separate 12-pair, 20-second CUBIC/200-flow
comparison for the prefetch-only candidate. It requires the screening plan and
the same binary, retains failures, and reports deterministic pair-bootstrap
intervals. The question and endpoints were frozen in
[`CONFIRMATION.md`](../../docs/experiments/2026-10-23-capacity/CONFIRMATION.md).
Run it before removing the owned container:

```sh
python3 bench/capacity/confirm.py qa/results/capacity-20261023-confirm
python3 bench/capacity/confirm.py qa/results/capacity-20261023-confirm --analyze
```

Reanalysis needs only the saved run files. `confirm.py --analyze` expects its
input under the repository root; extract the evidence there. The confirmation
summary intentionally stays separate from screening estimates.

## Few-connection peak guard

The additional [low-concurrency protocol](../../docs/experiments/2026-10-23-capacity/LOW-CONCURRENCY.md)
compares the client-only 256 KiB total TX cap against defaults with one and two
downloads, to check the user's requirement to preserve single-flow peak speed:

```sh
python3 bench/capacity/low_concurrency.py qa/results/capacity-20261023-low
python3 bench/capacity/analyze.py qa/results/capacity-20261023-low
```

This needs the same binary and an owned container running the verified image.
Its 12 attempts are a separate screen, not additional prefetch-confirmation
pairs. CPU or memory improvements cannot compensate for a material peak-speed
regression in this guard.

## RTT, joint factors, and corrected host

The [RTT report](../../docs/experiments/2026-10-23-rtt/README.md) separates the
original fixture, budget-wakeup diagnostics, and corrected-host comparisons.
The bare-Shard host now retries memory/aggregate-budget-blocked writers after
real release and input/read/close progress. `ZFBENCH_RETRY_BUDGET_WRITERS=0`
retains the old diagnostic control; the final runners explicitly set `1`.
This repair does not modify the production Tokio adapter or library defaults.

Additional test controls include `ZFBENCH_MIN_PREFETCH` (bytes),
`ZFBENCH_PREFETCH_MS`, `ZFBENCH_QUANTUM` and `ZFBENCH_ROUND_CAP` (bytes),
`ZFBENCH_CONNECT_PARALLEL` (bounded handshake setup), and
`ZFBENCH_HEAVY_PROGRESS=1` (250-ms per-flow read observations).
`ZFBENCH_QUEUE_DISCIPLINE=fifo|peer` compares the same rate serializer and byte
ceiling with FIFO or peer DRR/pushout at the emulated bottleneck. These are
benchmark controls, not a deployed queue or online RTT/concurrency controller.

The frozen final executable is `target/release/zfbench-rtt-final-20261023`.
Build once with the release settings above and copy it to that name before
starting a fresh experiment. Full protocol changes and selection decisions are
retained in the RTT report directory. The final continuation is reproducible as:

```sh
python3 bench/capacity/rtt_queue.py loaded qa/results/rtt-new-loaded --multiplier 1
python3 bench/capacity/rtt_queue.py confirm qa/results/rtt-new-confirm --multiplier 1
python3 bench/capacity/rtt_final.py aa qa/results/rtt-new-aa
python3 bench/capacity/rtt_final.py guard qa/results/rtt-new-guard
python3 bench/capacity/rtt_final.py guard qa/results/rtt-new-guard-aa --candidate q0s0p0
```

Run serially in the dedicated container; build before measuring. Every runner
requires a fresh output directory. Do not replace failed attempts. Reanalysis
of retained evidence needs no running container:

```sh
python3 bench/capacity/rtt_queue.py policy qa/results/rtt-20261023-final-policy --analyze
python3 bench/capacity/rtt_queue.py loaded qa/results/rtt-20261023-final-loaded --analyze
python3 bench/capacity/rtt_collect.py qa/results/rtt-reanalysis
python3 bench/capacity/rtt_plot.py qa/results/rtt-reanalysis/final-regimes-summary.json qa/results/rtt-reanalysis/rtvsload
```

The collector requires all frozen cohorts and retained executable identities;
the evidence bundle includes them. Plotting additionally uses Matplotlib.
The combined configured-regime table reuses 64 unchanged attempts and adds 32
new loaded attempts, with explicit source-cohort provenance. It is not another
96 measurements or proof of a runtime transition policy. CPU scopes remain
separate, and plotted ranges from two runs are not confidence intervals.

## Production contention pacing

The [production protocol](../../docs/experiments/2026-10-24-production-pacing/PROTOCOL.md)
tests the real `Shard::set_iface_egress_rate` / `StackHandle::set_iface_egress_rate`
API. `--iface-egress-rate` supplies IP bytes/second; zero disables it. The external
fixture remains FIFO in both arms. `--tokio-stack --splice` uses the production
Tokio driver and `TcpStream::splice` with real kernel TCP upstream sockets.
The extra bridge belongs to this fixture, and process CPU includes its cost.
The library API is opt-in, requires known downstream capacity and preserves
the single-peer path. See the [design](../../docs/design/0011-contention-egress-pacing.md)
for activation and limitations.

After building once with the release settings above, copy the executable to a
new frozen name. Reproduce phases **serially**, using fresh output directories:

```sh
python3 bench/capacity/production_pacing.py screen qa/results/production-new-screen
python3 bench/capacity/production_pacing.py wan qa/results/production-new-wan --candidate p99 --tokio-stack --splice --binary target/release/zfbench-production-v3-20261024
python3 bench/capacity/production_pacing.py confirm qa/results/production-new-confirm --candidate p99 --tokio-stack --splice --binary target/release/zfbench-production-v3-20261024
python3 bench/capacity/production_pacing.py guard qa/results/production-new-guard --candidate p99 --tokio-stack --splice --binary target/release/zfbench-production-v3-20261024
python3 bench/capacity/production_pacing.py aa qa/results/production-new-aa --tokio-stack --splice --binary target/release/zfbench-production-v3-20261024
python3 bench/capacity/production_peaks.py qa/results/production-new-cross-peak
python3 bench/capacity/production_mixed.py qa/results/production-new-mixed-smoke --smoke
python3 bench/capacity/production_mixed.py qa/results/production-new-mixed
```

The peak runner expects the retained old final executable as its A control.
The mixed fixture adds a separate upload peer without changing the frozen Rust
binary; its effective-byte denominator does not include that upload, so CPU/GB
is intentionally omitted. See [its protocol](../../docs/experiments/2026-10-24-production-pacing/MIXED-VALIDATION.md).
The owned container also needs a large TCP receive-window maximum for 500-ms
single-flow coverage. Exact sysctls and requested/actual configuration are in
the saved evidence.

`production_pacing.py <phase> <out> --analyze`, `production_peaks.py <out> --analyze`
and `production_mixed.py <out> --analyze` reanalyze saved attempts. The collector
uses the fixed 2026-10-24 cohort names; the bundle includes raw files, frozen
sources, binaries, build metadata and replay tools. Never build, run suites,
plot or compress archives during measurements, and never replace a failed arm.

The final V4 [review protocol](../../docs/experiments/2026-10-24-production-pacing/REVIEW-VALIDATION.md)
keeps V3 evidence and uses a separate frozen V4 executable. After V3 finishes,
allow CPU 4 in the owned container for the light-request reader/writer only;
service processes remain on CPUs 0–3 and total cgroup quota remains four CPUs.
Then execute, serially, `production_review.py loaded`, `wan`, and `peak` with
fresh output directories. The runner audits requested/actual observer CPUs;
late fully observed responses retain their true latency and SLO failure,
while missing requests or generator faults still block mean confirmation.
