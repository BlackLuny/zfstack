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

The V4 runners expect the retained executable at
`target/release/zfbench-production-v4-20261024`. Start with the isolated-observer
preflight before the three confirmation phases. Its container must be named
`zfstack-production-20261024` (or pass `--container`), permit CPUs 0–4 and retain
the documented four-CPU total quota; only the light reader/writer uses CPU 4.
Use the exact receive-autotuning/sysctl settings retained in the evidence.

```sh
python3 bench/capacity/production_review.py smoke qa/results/production-new-review-smoke
python3 bench/capacity/production_review.py loaded qa/results/production-new-review-loaded
python3 bench/capacity/production_review.py wan qa/results/production-new-review-wan
python3 bench/capacity/production_review.py peak qa/results/production-new-review-peak
python3 bench/capacity/production_long.py qa/results/production-new-review-wan-long
```

The last command is the fixed independent 120-second BBR/500-ms steady test;
read its [scope and stopping rule](../../docs/experiments/2026-10-24-production-pacing/LONG-WINDOW.md).
Do not pool it with the preceding 40-second observations. The final
[production report](../../docs/experiments/2026-10-24-production-pacing/README.md)
retains every unqualified guard. After extracting its archive in a checkout,
reanalysis without a running container is:

```sh
python3 bench/capacity/production_collect.py qa/results/production-reanalysis
python3 bench/capacity/production_qualify.py qa/results/production-reanalysis
```

The qualification JSON separates performance guards from functional CI; an
unqualified physical-peak bound is not a failed Rust test. Never overwrite the
sealed evidence or retained binaries when reanalyzing.

## V5 cold-state continuation

The [V5 report](../../docs/experiments/2026-10-24-pacing-fast-path/README.md) retains
the original V4 study and independently measures a smaller hot iface state and
an outlined aggregate-service loop. `fastpath.py` expects retained old/V4/V5
executables; it never rebuilds or replaces them. Use an owned network-isolated
container named `zfstack-fastpath-20261024` (or `--container`) with the same image,
sysctls, cpuset 0–4 and four-CPU total quota. Follow the prospective protocol,
including the original invalid placement preflight and its PPID-based fix.

`fastpath.py <phase> <fresh-output>` runs `smoke`, `preflight`, `aa`, `peak`,
`loaded`, `wan` or `pin` serially. The `pin` phase is conditional; the retained
study did not meet its eligibility condition and did not run it. The external
placement observer is diagnostic instrumentation; its CPU is separately saved
and it does not replace the unrestricted peak guard.

After extracting the evidence archive at the repository root, reanalysis needs
no running container:

```sh
python3 bench/capacity/fastpath_collect.py qa/results/fastpath-reanalysis
```

The collector independently rechecks per-thread placement ledgers, frozen
commands, sources and binaries, preserving failed attempts and the original
strict versus observation verdicts. Its performance qualification remains
separate from functional CI and makes no all-RTT CPU or universal-starvation
claim. Do not pool this continuation's intervals with preceding V4 results.

## Targeted CUBIC / 50 ms CPU retest

The [targeted report](../../docs/experiments/2026-10-24-cubic50-retest/README.md)
keeps the frozen V5 executable and exact 8-s warmup/20-s window. Original and
fixed native-thread placements each get 16 ABBA/BAAB blocks; same-setting
repeats quantify noise. The observer's scheduling counters cover observed
thread lifetimes, while primary CPU/useful-work measurements come from the
unchanged executable's exact window.

`cubic50_retest.py preflight <fresh-output>` runs the two observer preflights;
only after validating them, run `cubic50_retest.py confirmation <fresh-output>`
in the owned `zfstack-cubic50-20261024` container with the retained image,
sysctls and four-CPU quota. Keep CPUs 0–4 available; the observer runs on 4.
Never rebuild or alter the frozen V5 executable during these measurements.

After extracting the archive at the repository root, saved-data reanalysis
requires no running container:

```sh
python3 bench/capacity/cubic50_collect.py qa/results/cubic50-reanalysis
```

Historical two-pair data stay separate. The new bounded CPU result does not
resolve the unrelated physical-peak guards or establish the cause of every
historical outlier. All observations and failed verdicts must remain visible.

## Independent 1–500 ms representative-case confirmation

The [672-attempt report](../../docs/experiments/2026-10-25-common-cases/README.md)
keeps frozen V5 and all earlier studies unchanged. Read its prospective
[protocol](../../docs/experiments/2026-10-25-common-cases/PROTOCOL.md),
[actual-mask audit](../../docs/experiments/2026-10-25-common-cases/AUDIT-CLARIFICATION.md)
and [one independent affected-cell confirmation](../../docs/experiments/2026-10-25-common-cases/GENERATOR-CONFIRMATION.md).
It reports 576 ordinary runs (24 cells, 12 balanced pairs), 8 preflights,
16 A/A diagnostics, 48 local-peak runs and one separate 24-run confirmation.
One genuine missing arrival and five observer comparison races remain in
the original records. Neither the original full matrix nor the majority
engineering decision qualifies; both local peaks pass the separately relaxed
10% bound. Defaults remain off.

The runtime tools use fixed `qa/results/common-20261025-*` paths, retained
binary hashes and the owned `zfstack-common-20261025` container. They refuse
existing destinations; do not invoke them to extend or replace sealed runs.
For a new independent study, freeze its own names, protocol and seed first.
The container uses CPUs 0–4, the existing four-CPU quota and the exact recorded
sysctls. `common_cases.py` runs the original plan; only after it finishes does
`common_generator_followup.py run` execute the separately frozen 24 runs.
Do not build, test, plot or compress during either cohort.

Saved evidence needs no running container. Extract its archive at the checkout
root, then use a new replay directory:

```sh
tar -xzf docs/experiments/2026-10-25-common-cases/evidence.tar.gz
python3 bench/capacity/common_verify.py docs/experiments/2026-10-25-common-cases qa/results/common-replay-fresh
(cd docs/experiments/2026-10-25-common-cases && sha256sum -c artifact-index.sha256)
```

## Experimental BBR adaptive aggregate pacing

The [312-attempt A1 report](../../docs/experiments/2026-10-25-adaptive-pacing/README.md)
retains both 12-attempt pilots and all failures separately from fixed-capacity V5.
The original [protocol](../../docs/experiments/2026-10-25-adaptive-pacing/PROTOCOL.md)
and [A1 correction](../../docs/experiments/2026-10-25-adaptive-pacing/A1-CORRECTION.md)
remain frozen. A1 fails the original qualification: repeatable BBR throughput
losses at 50/100 ms and incomplete capacity recovery prevent default enablement.

`Shard::set_iface_adaptive_egress` and `StackHandle::set_iface_adaptive_egress`
select the experimental production policy without supplied capacity. Both
native arms pass fixed rate zero; `--adaptive-egress` rejects a nonzero
`--iface-egress-rate`. Fixed/adaptive settings are exclusive, the last setting
per iface wins, and adaptive false disables either aggregate policy. Defaults
remain off. The local extreme uses the existing client CUBIC/no-pacing profile;
BBR WAN single-flow results and the unmeasured BBR local peak are identified
separately in the report.

`adaptive_artifacts.py collect NEW_DIRECTORY` re-reads all original records;
it refuses overwriting numerical files. The report gives clean extraction and
offline `verify` instructions for the 31 numerical JSON outputs, frozen source
and executable hashes, and every archive member. `adaptive_plot.py` produces
the standalone figures after the completion marker. Descriptive diagnostics
do not replace any original guard. Existing study directories, failed arms,
candidate and sample budget must not be reused or extended for significance.

The verifier checks all archive member hashes/sizes, frozen binary/source
identities and 20 byte-identical JSON replays. Original strict flags and
independently certified actual placement are separate views with identical
numerical estimates. No missing latency is imputed; the independent follow-up
never enters the original same-round intervals or majority count.
