# QA BLOCKED

Tested SHA: `604a61834db76a1b94583932585fb24a0265964b`
Coverage: 0/36 required instances.
Plan: `477043fb6702135b1fa1aeb6cc6d40ae942c5917aaac483a185c90d72aee4797`

| Case | Layer | Functional | Validity | Performance |
|---|---|---|---|---|
| QA-lint | META | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-no-default | CORE | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-core | CORE | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-adapter | A1 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-adapter-directed | A0 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-budget-directed | A1 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-harness | L0-C | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-tools | META | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-l2-tools | META | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-perf-tools | META | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-bench-build | L1 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-bench | L1 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-bench-all | L1 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-coverage | META | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-features | CORE | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-l0-eager | L0-C | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-l0-event-only | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-full-eager-1 | L0-C | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-full-eager-17 | L0-C | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-full-eager-509 | L0-C | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-full-event-only-1 | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-full-event-only-17 | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-full-event-only-509 | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C20-soak | L0-C | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C02-splice-256 | A1 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C18-loom | A0 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C11-l1 | L1 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C16-l2 | L2 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C16-l2-relay | L2 | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C16-l2-queue-control | L2 | NOT_RUN | functional=UNKNOWN, performance=UNKNOWN | NOT_APPLICABLE |
| C13-burst-one | CORE | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-replay-external-owner | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-replay-network-recovery | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-replay-scripted-arrivals | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-replay-sink-recovery | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C01-throughput-256 | L0-C | NOT_RUN | UNKNOWN | INCONCLUSIVE |

## Blocked evidence

- C16-l2-queue-control: independent integration validation: missing L2 workload evidence
- QA-lint: required case NOT_RUN
- QA-no-default: required case NOT_RUN
- QA-core: required case NOT_RUN
- QA-adapter: required case NOT_RUN
- QA-adapter-directed: required case NOT_RUN
- QA-budget-directed: required case NOT_RUN
- QA-harness: required case NOT_RUN
- QA-tools: required case NOT_RUN
- QA-l2-tools: required case NOT_RUN
- QA-perf-tools: required case NOT_RUN
- QA-bench-build: required case NOT_RUN
- QA-bench: required case NOT_RUN
- QA-bench-all: required case NOT_RUN
- QA-coverage: required case NOT_RUN
- QA-features: required case NOT_RUN
- QA-l0-eager: required case NOT_RUN
- QA-l0-event-only: required case NOT_RUN
- QA-full-eager-1: required case NOT_RUN
- QA-full-eager-17: required case NOT_RUN
- QA-full-eager-509: required case NOT_RUN
- QA-full-event-only-1: required case NOT_RUN
- QA-full-event-only-17: required case NOT_RUN
- QA-full-event-only-509: required case NOT_RUN
- C20-soak: required case NOT_RUN
- C02-splice-256: required case NOT_RUN
- C18-loom: required case NOT_RUN
- C11-l1: required case NOT_RUN
- C16-l2: required case NOT_RUN
- C16-l2-relay: required case NOT_RUN
- C16-l2-queue-control: functional evidence NOT_RUN/UNKNOWN
- C16-l2-queue-control: execution/cleanup incomplete (failed, PASS)
- C13-burst-one: required case NOT_RUN
- QA-replay-external-owner: required case NOT_RUN
- QA-replay-network-recovery: required case NOT_RUN
- QA-replay-scripted-arrivals: required case NOT_RUN
- QA-replay-sink-recovery: required case NOT_RUN
- C01-throughput-256: required case NOT_RUN

## C16-l2-queue-control

Attribution: UNATTRIBUTED; run: `412e620d-a448-4dc8-8c94-3ec0225fcdc7`.
- missing L2 workload evidence

## Replay and input identities

Use the original frozen plan and tested checkout. Each attempt needs a new output directory; integration/performance also require the same reviewed node inputs and physical-domain lease.

```sh
python -m qa run-plan --plan plan.json --output qa/results/new-attempt
```

| Case | Build manifest | Environment | Configuration |
|---|---|---|---|
| C16-l2-queue-control | 138a2b28f290b9fff76c571278142ab3bfe6cc75a5412538c780a0dc019ad42c | ca44f7c3ce17741621375389dfabbd5d1a5be5d07ba18a64f869bbe99749627f | 3c3e349f49a08d69eeb24898ea520fcea21ce1a2dbe1f5abeadd167488664aeb |

Only the declared case instances and native platform; shadow metrics are not performance certification.
