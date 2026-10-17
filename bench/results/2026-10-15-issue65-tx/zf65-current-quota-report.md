# QA PASS

Tested SHA: `2694c1406b7ebb5d1eef381a0c1a69e27f1ea743`
Coverage: 17/17 required instances.
Plan: `682ab4f5860ba1d89faf2f3274b2914318eb267750193bf7526bcfda8eac913a`

| Case | Layer | Functional | Validity | Performance |
|---|---|---|---|---|
| QA-no-default | CORE | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-core | CORE | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-adapter | A1 | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-adapter-directed | A0 | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-budget-directed | A1 | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-harness | L0-C | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-tools | META | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-l2-tools | META | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-perf-tools | META | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-coverage | META | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-features | CORE | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-l0-eager | L0-C | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-l0-event-only | L0-E | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-replay-external-owner | L0-E | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-replay-network-recovery | L0-E | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-replay-scripted-arrivals | L0-E | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-replay-sink-recovery | L0-E | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |

## Replay and input identities

Use the original frozen plan and tested checkout. Each attempt needs a new output directory; integration/performance also require the same reviewed node inputs and physical-domain lease.

```sh
python -m qa run-plan --plan plan.json --output qa/results/new-attempt
```

| Case | Build manifest | Environment | Configuration |
|---|---|---|---|
| QA-no-default | d3a0bf9b8892a5bb95528b1aad35b11cc99499fa2232b57a3277b466fb49094b | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-core | d3a0bf9b8892a5bb95528b1aad35b11cc99499fa2232b57a3277b466fb49094b | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-adapter | 0844fc16d5b12d01818ce582208680ccc343166a8338efc191e19a48b751e12c | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-adapter-directed | 0844fc16d5b12d01818ce582208680ccc343166a8338efc191e19a48b751e12c | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-budget-directed | d3a0bf9b8892a5bb95528b1aad35b11cc99499fa2232b57a3277b466fb49094b | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-harness | b61a27c48029c147a379521d311763b833754e94462e7e29aaf36fac71ea228f | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-tools | d3a0bf9b8892a5bb95528b1aad35b11cc99499fa2232b57a3277b466fb49094b | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-l2-tools | d3a0bf9b8892a5bb95528b1aad35b11cc99499fa2232b57a3277b466fb49094b | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-perf-tools | d3a0bf9b8892a5bb95528b1aad35b11cc99499fa2232b57a3277b466fb49094b | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-coverage | d3a0bf9b8892a5bb95528b1aad35b11cc99499fa2232b57a3277b466fb49094b | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-features | d3a0bf9b8892a5bb95528b1aad35b11cc99499fa2232b57a3277b466fb49094b | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-l0-eager | 6d96c89b67852d7827ca9c727e447a81bec9743b88ceed437200093b35517a25 | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | c95caec0358a961e290345d155343ea659f02d5accc50f807dbedd472ed7a1b6 |
| QA-l0-event-only | 6d96c89b67852d7827ca9c727e447a81bec9743b88ceed437200093b35517a25 | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 85a9e192a062d81f5794eb0c321dfdad31f30f86fb3a2f5b82d586a3b28f6b6e |
| QA-replay-external-owner | 6d96c89b67852d7827ca9c727e447a81bec9743b88ceed437200093b35517a25 | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 372b35d433ff998f50a5023a19043c031fe584e42569c9e3073045d2abb542de |
| QA-replay-network-recovery | 6d96c89b67852d7827ca9c727e447a81bec9743b88ceed437200093b35517a25 | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 12b10e38992ba6c698d030f9786f1529a5ca8eca8cb1d23ea779c229d7a5b5a9 |
| QA-replay-scripted-arrivals | 6d96c89b67852d7827ca9c727e447a81bec9743b88ceed437200093b35517a25 | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | eac473363b6f3391f47be06a918bae4ee65cc91f170a1897a20e9259ce1a0151 |
| QA-replay-sink-recovery | 6d96c89b67852d7827ca9c727e447a81bec9743b88ceed437200093b35517a25 | 321328e4d487c727a7abf0998aa17f697521209d181062dea2cf4bbba76c654c | 24af525fda5035c2c0edd7462bd67142f5b7c7ea860b34b80610620b6cf6a073 |

Only the declared case instances and native platform; shadow metrics are not performance certification.
