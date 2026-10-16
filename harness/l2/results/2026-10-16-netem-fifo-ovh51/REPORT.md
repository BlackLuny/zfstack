# OVH physical host: stage1 PASS

Tested SHA: `9ba31a7dcedec2fae05efd304831b9d469caf756`. The plan was committed
and posted to PR #75 before execution. The driver ran exactly once; no tracing,
affinity, RT priority, power-policy or workload changes. All thresholds remain
frozen, including 20 ms and strict receiver order.

Host: `51.75.146.55:2280`, Debian kernel 6.12.57+deb13-amd64,
Xeon E3-1270 v6, 4 physical cores / 8 threads, 32 GiB RAM;
systemd-detect-virt returned none. User confirmed existing services are test-only,
no competing jobs during measurements, and permitted later tracing. A remote
compile was observed during preparation; we waited for it to finish naturally
and checked no cargo/rustc remained before starting. See compile-wait.json and
host-ready.json. We did not stop any service or modify host networking.

Remote directory: `/root/zfstack-qa-pr75-20261016-ovh51-01`.
Bundle SHA-256: `8dd8e2147afdec45e6fcaccf3c36d173ea799e158110e888b54fd2f410a57f7d`.
Public lease: `/var/lock/zfstack-qa`, domain
`zfstack-qa-baremetal-51.75.146.55-2280`. Every network step used
`qa/isolate-linux.sh`; per-step proc/stat and proc/softirqs are in execution.json.

| Step | Result | Raw UDP order / delivery |
| --- | --- | --- |
| capabilities | PASS | netem options read back correctly, rings and all required operations succeeded |
| full | PASS | 18475/18475, no duplicates, no overtakes, zero ledger loss |
| cancel | negative PASS | 18475/18475, no duplicates, no overtakes, zero ledger loss; oracle correctly rejects cancellation/incomplete phases |
| overflow | negative PASS | 13561/18475, 4914 missing UDP, no duplicates/overtakes; ledger has 4964 lost frames across protocols; existing overflow accounting passes |

No mode reordered, so the conditional three plain-netem controls were not run.
All 13 full checks passed, including raw-ledger timing, FIFO, kernel-counter
closure, TCP integrity, no sender EAGAIN, zero link drops and restoration.
From raw timestamps, the largest active-phase residence excess over 500 ms
was 0.113 ms in full (this descriptive metric is not a replacement for the
oracle's phase-specific release calculation). The complete oracle passed its
unchanged 20 ms bounds. This one physical-host pass does not identify which
component caused the earlier VPS reorder.

The 43 collected files were verified byte-for-byte against evidence-index.json;
evidence.tar.gz SHA-256 is
`42c13e58686ee71bf690095ef6dab7de975d795994d164e1df81bd81b655612d`.
`recompute.py` independently decoded raw ledgers using frozen expected configs,
checked oracle errors and counted every receiver sequence. Results agree with
the remote gate; see recomputed.json. pause-analysis.json reuses the earlier
read-only analyzer; overflow sample gaps can be packet loss, not CPU pauses.
No WireGuard secrets/runtime configurations are included.

Selected service PIDs and both persistent containers are unchanged. No host
registered namespace or lease remains. act_mirred, cls_matchall, dummy and
sch_ingress auto-loaded; no modules were unloaded. No packages installed.

Preparation failures are retained: two local QA suite invocations and one audit
attempt lacked jsonschema in the system interpreter. The complete second failed
suite log is archived. Using the existing QA venv passed 208 tests (5 platform
skips); L2 passed 128 (2 platform skips), including 6 new gate regression tests;
coverage audit passed. These were local preparation checks, not repeated gates.

Next: separately register case v3/netem-fifo after this evidence commit; gate
that new SHA once. Only its PASS permits frozen worker build and one held-out
formal C16. known-BLOCKED remains even if later formal evidence passes.

## Case v3 preregistration and stage3

Evidence was committed as f285459 before the separate registration commit
`604a61834db76a1b94583932585fb24a0265964b`. cases.json changed exactly version
2→3 and kind fifo-delay-line→netem-fifo; the known-failures note retains BLOCKED.
L2 128 (2 skips), QA 208 (5 skips) and coverage audit passed on that change.

Stage3 ran once at this new SHA: capabilities, full, cancel and overflow all
PASS; no controls triggered. Full/cancel each received 18475 unique ordered UDP,
zero missing. Overflow received 13539 (4936 missing UDP); 4965 total frames lost,
no reorder/duplicates; negative loss accounting and oracle errors agree. The
48 archived files were hash-verified and independently recomputed locally.
Stage3 archive SHA-256:
`d06d0e42f04188c9869dea001df8e3983112a72dd606a4a6ac2f9edca53f5ba7`.

The optional cancel timing mapper declined: it identifies UDP by 1074-byte
frame length and found 18476 matched frames versus 18475 receiver datagrams.
Length alone cannot uniquely identify protocol/flow. All captured frames pair
without loss, the complete oracle and receiver strict-order checks passed;
no per-packet timing attribution is claimed from that optional mapper.

Preparation: three SSH handshakes were closed (two startup attempts and one
read-only check). After recovery we verified source3, driver, plan and output
did not exist, then used a multiplexed SSH connection to start stage3 once.
These are connection/setup failures, not repeated measurements.

Both gates passed without tracing. A separately frozen native build and one
held-out formal C16 are now planned in build-and-formal-plan.json. Four other
repository SHAs and Rust 1.98.1 remain frozen. Rust archives are verified against
the retained distribution manifest; Python wheels match qa/requirements.lock.
All tool installation is task-local; no apt/system-package operation.
