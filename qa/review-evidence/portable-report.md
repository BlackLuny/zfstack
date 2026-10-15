# QA PASS

Tested SHA: `8bf5bb86106e5a4f1665f3c1b9c4570d2c80bf72`
Coverage: 17/17 required instances.
Plan: `697459d6110f5bbe513d6a2cad03121eb048d39ff18b99b1a703c8c85bdccef8`

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
| QA-no-default | cee8bae1df3e3c1c10d144c1fa5326e9f311539dd6e5229e58d9ab83eaaec8d4 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-core | cee8bae1df3e3c1c10d144c1fa5326e9f311539dd6e5229e58d9ab83eaaec8d4 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-adapter | 81710aa2cc691269373207aa605d66d3e76e5086e557aa6a39c3a473e9f5c276 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-adapter-directed | 81710aa2cc691269373207aa605d66d3e76e5086e557aa6a39c3a473e9f5c276 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-budget-directed | cee8bae1df3e3c1c10d144c1fa5326e9f311539dd6e5229e58d9ab83eaaec8d4 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-harness | 88a53a3bb72480f168bdbb120f03989138f466b6ec10506830c01c90c397df30 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-tools | cee8bae1df3e3c1c10d144c1fa5326e9f311539dd6e5229e58d9ab83eaaec8d4 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-l2-tools | cee8bae1df3e3c1c10d144c1fa5326e9f311539dd6e5229e58d9ab83eaaec8d4 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-perf-tools | cee8bae1df3e3c1c10d144c1fa5326e9f311539dd6e5229e58d9ab83eaaec8d4 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-coverage | cee8bae1df3e3c1c10d144c1fa5326e9f311539dd6e5229e58d9ab83eaaec8d4 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-features | cee8bae1df3e3c1c10d144c1fa5326e9f311539dd6e5229e58d9ab83eaaec8d4 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |
| QA-l0-eager | 6f7de4e6166a9025a1c0ff4b03b746a3aacc0ed828130777d8e5d5c2d42a4fe3 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | c95caec0358a961e290345d155343ea659f02d5accc50f807dbedd472ed7a1b6 |
| QA-l0-event-only | 6f7de4e6166a9025a1c0ff4b03b746a3aacc0ed828130777d8e5d5c2d42a4fe3 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 85a9e192a062d81f5794eb0c321dfdad31f30f86fb3a2f5b82d586a3b28f6b6e |
| QA-replay-external-owner | 6f7de4e6166a9025a1c0ff4b03b746a3aacc0ed828130777d8e5d5c2d42a4fe3 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 372b35d433ff998f50a5023a19043c031fe584e42569c9e3073045d2abb542de |
| QA-replay-network-recovery | 6f7de4e6166a9025a1c0ff4b03b746a3aacc0ed828130777d8e5d5c2d42a4fe3 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 12b10e38992ba6c698d030f9786f1529a5ca8eca8cb1d23ea779c229d7a5b5a9 |
| QA-replay-scripted-arrivals | 6f7de4e6166a9025a1c0ff4b03b746a3aacc0ed828130777d8e5d5c2d42a4fe3 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | eac473363b6f3391f47be06a918bae4ee65cc91f170a1897a20e9259ce1a0151 |
| QA-replay-sink-recovery | 6f7de4e6166a9025a1c0ff4b03b746a3aacc0ed828130777d8e5d5c2d42a4fe3 | 62090bb247724e7bd4300b3f98dc3a7b63225e199b9dbd1321f8a80c2a88cad7 | 24af525fda5035c2c0edd7462bd67142f5b7c7ea860b34b80610620b6cf6a073 |

Only the declared case instances and native platform; shadow metrics are not performance certification.
