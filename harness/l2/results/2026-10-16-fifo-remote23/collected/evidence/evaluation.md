# QA FAIL

Tested SHA: `16b0d13a0fba59dc4748b530bd854a9cccd5ea70`
Coverage: 1/36 required instances.
Plan: `2072285a8b416e052cd6ba46d35b7fa2683733e325df0d2fd14a779ee89f2720`

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
| C16-l2-queue-control | L2 | FAIL | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| C13-burst-one | CORE | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-replay-external-owner | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-replay-network-recovery | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-replay-scripted-arrivals | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| QA-replay-sink-recovery | L0-E | NOT_RUN | UNKNOWN | INCONCLUSIVE |
| C01-throughput-256 | L0-C | NOT_RUN | UNKNOWN | INCONCLUSIVE |

## Failures

- C16-l2-queue-control: functional FAIL (l2-queue-control.path_workload_contract); attribution=UNATTRIBUTED

## Blocked evidence

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
- C16-l2-queue-control: execution/cleanup incomplete (failed, PASS)
- C13-burst-one: required case NOT_RUN
- QA-replay-external-owner: required case NOT_RUN
- QA-replay-network-recovery: required case NOT_RUN
- QA-replay-scripted-arrivals: required case NOT_RUN
- QA-replay-sink-recovery: required case NOT_RUN
- C01-throughput-256: required case NOT_RUN

## C16-l2-queue-control

Attribution: UNATTRIBUTED; run: `b37bec6a-6837-42b6-ab60-5ade86d44444`.
- positive baseline, payload integrity or recovery contract failed; datagram timeout seq=1987; datagram timeout seq=1988; datagram timeout seq=1989; datagram timeout seq=2301; datagram timeout seq=2302; datagram timeout seq=2303; datagram timeout seq=2304; datagram timeout seq=2305; datagram timeout seq=2306; udp-0.json: request 1987 timed_out; udp-0.json: request 1988 timed_out; udp-0.json: request 1989 timed_out; udp-0.json: request 2301 timed_out; udp-0.json: request 2302 timed_out; udp-0.json: request 2303 timed_out; udp-0.json: request 2304 timed_out; udp-0.json: request 2305 timed_out; udp-0.json: request 2306 timed_out; udp-1.json: request 1987 timed_out; udp-1.json: request 1988 timed_out; udp-1.json: request 1989 timed_out; udp-1.json: request 2301 timed_out; udp-1.json: request 2302 timed_out; udp-1.json: request 2303 timed_out; udp-1.json: request 2304 timed_out; udp-1.json: request 2305 timed_out; udp-1.json: request 2306 timed_out; udp-2.json: request 1988 timed_out; udp-2.json: request 1989 timed_out; udp-2.json: request 2301 timed_out; udp-2.json: request 2302 timed_out; udp-2.json: request 2303 timed_out; udp-2.json: request 2304 timed_out; udp-2.json: request 2305 timed_out; udp-2.json: request 2306 timed_out; udp-3.json: request 1987 timed_out; udp-3.json: request 1988 timed_out; udp-3.json: request 1989 timed_out; udp-3.json: request 2301 timed_out; udp-3.json: request 2302 timed_out; udp-3.json: request 2303 timed_out; udp-3.json: request 2304 timed_out; udp-3.json: request 2305 timed_out; udp-3.json: request 2306 timed_out; service.client-1.loaded.latency: 14 observed completed requests exceed 250.0 ms; censored requests have no imputed latency; service.client-1.loaded.deadline: observed misses/population 14/14; maximum proportion 0.0; service.client-2.loaded.latency: 14 observed completed requests exceed 250.0 ms; censored requests have no imputed latency; service.client-2.loaded.deadline: observed misses/population 14/14; maximum proportion 0.0; service.client-3.loaded.latency: 14 observed completed requests exceed 250.0 ms; censored requests have no imputed latency; service.client-3.loaded.deadline: observed misses/population 14/14; maximum proportion 0.0; FIFO did not switch to the transparent path before the recovery boundary; FIFO frame 106896 was released early or exceeded the lateness bound; queue control lacks the actual loaded phase interval; queue link client0/eth0/rx.dropped increased by 5; latency-only injection is unqualified; queue link worker/eth0/tx.dropped increased by 172; latency-only injection is unqualified

## Replay and input identities

Use the original frozen plan and tested checkout. Each attempt needs a new output directory; integration/performance also require the same reviewed node inputs and physical-domain lease.

```sh
python -m qa run-plan --plan plan.json --output qa/results/new-attempt
```

| Case | Build manifest | Environment | Configuration |
|---|---|---|---|
| C16-l2-queue-control | bf1edb94e4373945531bc57c8d5d4b7852e71bdc7e85ed20a207b3c8315222b5 | 149a77bfda1f86cfaf11d367970c388e1472a3de9089f00390ce5afebd19d438 | 9caa1caca17b42d0e295bd88b866fc5ffdc9c5b9ae8ef9e043651e24ceba31d2 |

Only the declared case instances and native platform; shadow metrics are not performance certification.
