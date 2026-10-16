# OVH physical host: both gates PASS; formal NOT_RUN / UNKNOWN

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

Preparation: three SSH connections were closed (two startup attempts and one
read-only check). After recovery we verified source3, driver, plan and output
did not exist, then used a multiplexed SSH connection to start stage3 once.
These are connection/setup failures, not repeated measurements.

Both gates passed without tracing. A separately frozen native build and one
held-out formal C16 are now planned in build-and-formal-plan.json. Four other
repository SHAs and Rust 1.98.1 remain frozen. Rust archives are verified against
the retained distribution manifest; Python wheels match qa/requirements.lock.
All tool installation is task-local; no apt/system-package operation.


## Frozen build and single formal attempt: NOT_RUN / UNKNOWN

Frozen worker SHA-256:
`d921130672b975fb48f646e7ff159301ea5089d51a8fdd06b16cd0a08477a551`.
Source remains `604a618`; the later evidence-only commits are not claimed as
measured. Rust 1.98.1, fast-release, default features plus mmsg/jemalloc, jobs=2,
the same other four source SHAs, task-local target cache. Native worker/fakectl
build completed on the first attempt. Added a pre-formal compiler check to the
owned orchestration script; no competing cargo/rustc was found. The signed-off
case/workload/threshold values did not change. Worker/plan hashes were recorded
in formal-preregistration.json before the five-second settle and measurement.

**Exactly one formal invocation.** Baseline ran; fault never started. The runner
stopped after the baseline process returned nonzero. Independent remote
verification and local verification agree: functional **NOT_RUN**, functional
validity **UNKNOWN**, aggregate **BLOCKED**, execution failed, cleanup **PASS**.
This is not a measured netem-fifo failure or pass: the injector was absent in
baseline and there is no fault-arm queue ledger.

Raw UDP evidence:

| Flow | Scheduled | Completed | Missed | Max generator lag ms |
| --- | --- | --- | --- | --- |
| udp-0 | 3200 | 3200 | 0 | 3.779 |
| udp-1 | 3200 | 3200 | 0 | 4.428 |
| udp-2 | 3200 | 3200 | 0 | 3.788 |
| udp-3 | 3200 | 3199 | 1 | 23.053 |

There were zero UDP timeouts or send failures. udp-3 sequence 186, at baseline
+1.86 seconds, was 23.052927 ms late to its scheduled arrival. udp_probe.py
checks the unchanged 20 ms **generator scheduling tolerance**, marks it missed
and does not send it. This is a different condition from the injector's 20 ms
release-lateness ceiling; neither threshold was changed. The baseline's observed
service contract and raw payload checks pass, but generator/endpoint/worker
sampling errors remain, and the complete two-arm C16 qualification is missing.
All boundary precision errors are retained in formal-recomputed.json.

`analyze_baseline.py` reads only the archived raw samples. Between udp-3's last
completion and its next send there is a 30.362524 ms interval; each other UDP
process starts three requests within it. Therefore a shared stop of the whole
host across that full interval cannot explain the evidence. The data do not
separate scheduler delay, CPU-local IRQ/softirq delay, and Python/runtime delay.
There was no scheduler tracing in this formal attempt, and the endpoint samples
do not record current CPU IDs. No rerun was performed or substituted.

The 62 collected build/formal evidence files were hash-verified. Collection
archive SHA-256: `d158bce61a066633ce3f6677c89956118295dd838385d344946265c594f20715`.
Artifact, build and command identities verify locally. Integration validation
correctly rejects missing fault workload evidence. The first local analysis
mistakenly passed worker-manifest to the build-v1 validator; its error output is
retained as formal-recomputed-wrong-manifest-type.json. Correcting that analysis
to validate the actual build manifest passes identity checks and does not alter
NOT_RUN/UNKNOWN/BLOCKED. No runtime or qualification code was changed to regrade.

Host service PIDs and persistent containers are unchanged; public lease and
host registered namespaces are empty after cleanup. Rust and pinned QA wheels
were installed only below the task directory. No apt, host network, affinity,
priority, service, or tracing configuration was changed for measurements.

## User-reported CPU 0 NIC interrupt issue: read-only inspection

After the result, the user reported a known CPU 0 NIC interrupt issue. We saved
/proc/interrupts, softirqs, stat and softnet_stat twice over ten seconds, IRQ
bindings and RPS/XPS masks, without changing settings. This observation was
**after formal, under idle conditions**, not synchronized with sequence 186.

- eno1 IRQ 130 / eno1-TxRx-1 effectively binds to CPU 0. Over the ten seconds it
  recorded 513 interrupts on CPU 0; this count alone is not evidence of a storm.
- Other active NIC queues bind to CPU 7, 1 and 2 (432, 579 and 656 increments).
  Thus the NIC's interrupt work is not confined to CPU 0.
- CPU 0 and CPU 4 are SMT siblings on physical core 0; the other sibling pairs
  are 1/5, 2/6 and 3/7. RPS/XPS masks read zero.
- No retained CPU ID links udp-3 to CPU 0 at the missed request. The report is
  a concrete hypothesis for the per-task pause, not a confirmed causal finding.

Next diagnostic should be separately pre-registered: baseline-only synchronized
sched_switch/sched_wakeup plus IRQ/softirq events, identify the generator's host
PID/CPU, and distinguish late wakeup from runnable-but-unscheduled time. Any
CPU placement comparison must account for physical cores/SMT and all active NIC
queues, explicitly revise the previous no-affinity rule, and preserve this
failed formal attempt. Merely excluding logical CPU 0 is not physical-core
isolation. No additional gate, formal invocation or trace was run after the
failure. Case v3 remains registered; **known-BLOCKED, draft PR and open #59 remain**.
