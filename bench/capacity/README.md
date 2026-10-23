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
