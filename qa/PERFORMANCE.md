# Frozen performance experiments in QA

`external-performance` executes a previously frozen experiment with prebuilt binaries. It does not build during measurement. `qa.perf_run.run_performance_case(plan, case, root, output, node, lease_dir, policy_dir)` returns and saves a complete result-v2 envelope, including when node capabilities or inputs are absent. The normal QA runner invokes this function before creating the case output directory.

The default `QA-performance` policy is a shadow run-AA budget probe:

```json
{
  "args": ["budget", "--iterations", "5000000", "--flows", "1", "--payload", "2112"],
  "timeout_sec": 120,
  "max_output_bytes": 1048576,
  "sample_interval_sec": 0.1,
  "comparison": "AA",
  "blocks": 20
}
```

Its primary metric is `cpu_primary`, which is CPU nanoseconds per operation for this metadata workload. It is not network goodput. The profile, toolchain and requested features must match the original build manifest. The frozen experiment additionally fixes one CPU, the complete reference work counters, ABBA/BAAB order, metric directions/budgets, inference method and sample endpoint. Static policy fields are matched exactly; unknown additional execution settings are rejected. The reference measurement is exploratory and never enters the confirmation rows.

## Prepare inputs on the measurement node

Use an explicitly assigned native Linux node and the common physical-domain lease. Build, reference capture and environment preparation belong to a separate preparation phase. Release that phase's lease before invoking the performance runner, which takes the same domain lease itself. An outer lease must not remain held. A busy or stale lease is an error, and a failed slot is never replaced in the same block.

Build the tested clean checkout with the policy's exact compiler/features/profile, for example:

```sh
cargo +1.94.1 build --locked --release --no-default-features --example pareto_probe
python3 -m qa build-manifest --toolchain 1.94.1 --profile release \
  --binary /absolute/checkout/target/release/examples/pareto_probe \
  --output /absolute/inputs/build-manifest.json
```

Keep the original manifest and binary immutable. The manifest must list the actual candidate binary name, size and SHA-256, and identify the same clean tested SHA/source tree. This runner never regenerates a manifest to relabel an existing binary. A/A may use the same binary in both arms; independently built A/A controls additionally need the build provenance described in `bench/pareto/README.md`.

Capture one reference and the stable performance environment while pinned to the actual permitted CPU, then run `bench/pareto/freeze_plan.py` as described in [the performance tool guide](../bench/pareto/README.md). Use `--case-id QA-performance --case-version 1 --blocks 20 --comparison AA` and the exact policy arguments. The utility prints the canonical experiment hash. Keep `.03` or any initial budget in shadow until the measured calibration procedure validates its resolution and detection power.

The trusted node configuration needs these fields in addition to `isolated`, `kind`, `physical_domain` and `generation`:

| Field | Meaning |
| --- | --- |
| `performance_plan` | Absolute path to the frozen experiment JSON |
| `expected_plan_hash` | Canonical SHA-256 of that exact experiment, supplied by the trusted caller |
| `baseline_binary` | Absolute immutable A binary path |
| `candidate_binary` | Absolute immutable B binary path |
| `performance_environment` | Absolute path to the stable performance fingerprint used when freezing the plan |
| `build_manifest` | Absolute path to the original complete QA build-v1 manifest |
| `calibration_directory` | Enforce only: absolute tree containing calibration report/spec and the spec's unchanged relative trial paths |

Use the normal entry points after preparation:

```sh
python3 -m qa make-plan --suite perf --tested-sha "$TESTED_SHA" \
  --platform linux-x86_64 --output /absolute/inputs/qa-plan.json
python3 -m qa run-plan --plan /absolute/inputs/qa-plan.json \
  --node /absolute/inputs/node.json --lease-dir /absolute/leases \
  --case QA-performance --output /absolute/new-results
python3 -m qa evaluate --plan /absolute/inputs/qa-plan.json \
  --results /absolute/new-results --expected-sha "$TESTED_SHA" \
  --output /absolute/report.json
```

A missing/invalid node input produces `functional: NOT_RUN` with explicit reasons, rather than an empty case. An actual candidate contract failure is retained even alongside incomplete or invalid companion runs. Compiler activity or unavailable cgroup v2 ancestor limits invalidate performance while independently sound functional evidence may remain valid. A shadow result never establishes calibrated non-inferiority.

## Artifact and identity contract

`environment.json` is the full QA environment-v1 observation. `perf-environment.json` is the separate stable performance fingerprint; they have different schemas and different hashes. The reporter checks the native kernel and CPU model association, then independently checks every DUT environment sample against the frozen performance fingerprint.

The result's top-level `environment_hash` and `config_hash` identify the outer QA environment and static case configuration. Its observations bind `performance_plan_hash`, `performance_environment_hash`, `performance_config_hash` and `qa_config_hash`; the reporter recomputes every binding. The original build manifest is shared by both layers and its canonical hash must be identical in the experiment and result.

Each case preserves `perf-plan.json`, `perf-rows.jsonl`, `perf-analysis.json`, `perf-audit.json`, `execution.json`, the two environment files, the original build manifest, the runner log and all per-slot raw logs under `runs/`. Artifacts are inventoried with content hashes and sizes. Raw output is bounded by the frozen byte limit and process cleanup is recorded by the Linux subreaper. The reporter reparses raw probe output, rechecks work counters/checksums/release assertions, recomputes metrics and then reruns complete-block inference. Rehashing a modified log does not make an old claimed measurement valid. Incomplete blocks, duplicate slots, changed order or overlap cannot become a passing experiment.

## Enforce requires trusted calibration

Optional absolute contracts are functional requirements even in shadow mode. A
reviewed case declares `performance.absolute_slos`, keyed by a registered metric
ID, for example `{"cpu_primary": {"scope": "observed-per-run", "max": 250}}`.
The bound uses that metric's declared unit; this example is not a product default.
The corresponding experiment metric must carry the exact same `absolute_slo`.
The reporter rejects missing, added or changed contracts even if every uploaded
hash has been recomputed. If policy declares `experiment_plan_hash`, it is also
checked in shadow mode. Every observed candidate run is checked separately;
performance contamination, an incomplete block or averaging cannot erase a
known absolute-contract failure.

Enforcement requires policy `performance.mode: enforce`, `calibration_id` and the exact `experiment_plan_hash`. The frozen plan must itself use enforce mode. The caller cannot add or change CPU, counters, thresholds, metric family, block count or any other plan value after that hash is reviewed.

The calibration tree contains `calibration.json`, `calibration-spec.json` and the exact plan/rows/provenance paths listed in the spec. Supported relative trial paths are exactly `calibration/<trial_id>/perf-plan.json`, `calibration/<trial_id>/perf-rows.jsonl` and, when supplied, `calibration/<trial_id>/calibration-provenance.json`. Trial IDs use 1–128 ASCII letters/digits/underscores/dots/hyphens, starting with a letter, digit or underscore. Import and upload share this allowlist; nonstandard spec paths are rejected explicitly. Import also rejects symlinks, more than 512 trials or 4096 files, files over 64 MiB and a tree over 2 GiB. All imported files become result artifacts.

`stage-upload` and `qa.archive.archive` preserve the full performance inventory, including `perf-environment.json`, `perf-audit.json`, `runner.log`, `runs/runs.jsonl` and `runs/probe-<block_index>-<slot>.log`. Probe logs are permitted only for the frozen experiment's block/slot range. Referenced calibration files are included; unreferenced calibration subtrees, private keys, worker configuration and unrelated logs are excluded. If a declared performance artifact is outside the supported allowlist or absent, upload fails instead of silently dropping evidence. The default upload/archive size budget remains 256 MiB; exceeding it is an explicit failure and leaves local evidence intact.

The public `qa.release.verify_calibration` independently recomputes controls and verifies the trusted registry, generation and expiry. During execution it runs against a temporary sealed artifact view before the final result is published; the reporter repeats it against the saved result. A bare `validated: true`, matching scope hash or claimed PASS is insufficient. No synthetic test fixture in this repository certifies a node.
