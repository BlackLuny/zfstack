# QA BLOCKED

Tested SHA: `8bf5bb86106e5a4f1665f3c1b9c4570d2c80bf72`
Coverage: 5/6 required instances.
Plan: `0305ae17dfacf1b36f0cdb3698fb9d671ae52594a0cd822d9cc397cd6a7ba9ec`

| Case | Layer | Functional | Validity | Performance |
|---|---|---|---|---|
| QA-miri | CORE | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-sanitizer | CORE | NOT_RUN | functional=UNKNOWN, performance=UNKNOWN | NOT_APPLICABLE |
| QA-fuzz-wire_parser | CORE | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-fuzz-sequence_sack | CORE | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| QA-fuzz-event_script | CORE | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |
| C18-loom | A0 | PASS | functional=VALID, performance=UNKNOWN | NOT_APPLICABLE |

## Blocked evidence

- QA-sanitizer: no executed commands
- QA-sanitizer: functional evidence NOT_RUN/UNKNOWN
- QA-sanitizer: execution/cleanup incomplete (capability_missing, PASS)

## Replay and input identities

Use the original frozen plan and tested checkout. Each attempt needs a new output directory; integration/performance also require the same reviewed node inputs and physical-domain lease.

```sh
python -m qa run-plan --plan plan.json --output qa/results/new-attempt
```

| Case | Build manifest | Environment | Configuration |
|---|---|---|---|
| QA-miri | 36cbe03c0c2a7993f0f2c7127b0ef2657cd229a605c43f570fd765856d1ba105 | dd99ce73185c48d440e699f3a371b1888690f04cabe689c0d076d4694020d172 | acb8778a0d4a946ab912a79fc9bee82b725efcfa763c3d183cc9d1c040a5f209 |
| QA-sanitizer | d618041624847194a7e90564721a594083d6165ba269952a2c8761006b2a41c9 | dd99ce73185c48d440e699f3a371b1888690f04cabe689c0d076d4694020d172 | 3fc358057783884d8dd2d47e02368d68349e213477c6d3ac773a79eb8a51e0d5 |
| QA-fuzz-wire_parser | 09f53e7398222c94b7532ea7442479db8afa72e7f4bf363b68a6dff88b32954e | dd99ce73185c48d440e699f3a371b1888690f04cabe689c0d076d4694020d172 | 1f284dd33e20ee0d2208d72ac108e7cdd29d89b0245ccd727ded8ea87751f725 |
| QA-fuzz-sequence_sack | 09f53e7398222c94b7532ea7442479db8afa72e7f4bf363b68a6dff88b32954e | dd99ce73185c48d440e699f3a371b1888690f04cabe689c0d076d4694020d172 | f89f3f73083976a2da355822fd62b892d5e406cfac7bff331840866fbf98ab6b |
| QA-fuzz-event_script | 09f53e7398222c94b7532ea7442479db8afa72e7f4bf363b68a6dff88b32954e | dd99ce73185c48d440e699f3a371b1888690f04cabe689c0d076d4694020d172 | 5ef67206aaddca00efac6e4f65a4f34855bc6f3dcab8787b51946cec31a6d82f |
| C18-loom | 14a305dfd6a3fd2b4eea66444b91b625056f5173a68c60edd7e50b7aac56ab9d | dd99ce73185c48d440e699f3a371b1888690f04cabe689c0d076d4694020d172 | 44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a |

Only the declared case instances and native platform; shadow metrics are not performance certification.
