# OVH scheduler trace and physical-core isolation diagnostic — all planned runs retained

**Registered IRQ 130 hypothesis: not supported by either traced miss. Neither pre-registered isolation comparison shows a reduction at α=0.05.** All 3 traced arms and all 15 rotated A/B/C arms ran exactly once. There was no gate, worker rebuild, fault arm or new formal C16 invocation. The earlier formal result remains **NOT_RUN / UNKNOWN**, C16 remains **case v3 / netem-fifo**, both 20 ms limits and strict order are unchanged, and **known-BLOCKED** remains. PR stays draft; #59 stays open.

## Registration, executable identity and boundaries

- Original registration: `eafde3e`, [PLAN.md](PLAN.md). Before any measurement, correction `85ac164a2b762a87f00ceac4f56727c5cbc91128` fixed the diagnostic's venv check (the real task venv symlinks to `/usr/bin/python3.13`), restored/verified effective as well as configured IRQ affinity, and rejected incomplete generator-state coverage. These did not change baseline code, schedule, hypotheses, metrics or thresholds. Three regression tests were added; L2 142 tests (2 macOS skips), QA 208 (5 macOS skips), coverage audit passed. Logs are in `preflight/`.
- Every arm checked a clean `85ac164` checkout, executable baseline files byte-identical to `604a618`, the task venv identity, and frozen worker SHA-256 `d921130672b975fb48f646e7ff159301ea5089d51a8fdd06b16cd0a08477a551`. The original four other repository versions and worker build were reused. No compiler ran in any of the 36 before/after snapshots.
- Host: `root@51.75.146.55:2280`, bare-metal Xeon E3-1270 v6, 4C/8T, Debian kernel 6.12.57. User authorized tracing and separately answered **“允许执行 C 臂并恢复 IRQ 设置”**. irqbalance was inactive, with no process, before isolation; all per-arm snapshots also say inactive.
- Public QA lease held for each traced arm and the entire isolation sequence. Baselines used the runner's own command builder, environment, subreaper and `qa/isolate-linux.sh`. Analysis completed between traced arms. No simultaneous analysis or evidence transfer ran during the isolation workload. Small status reads continued during orchestration; the host was not otherwise quiesced by stopping services.
- Only C changed host IRQ affinity, temporarily. No service, package, qdisc, firewall, priority or qualification policy was changed. Tracing used owned private instance `qa44sched`, removed after each traced arm.

## Part 1: three traced baseline arms

| Arm | Maximum UDP generator lag (ms) | Misses >20 ms | UDP timeouts | Baseline exit | Trace validity |
| --- | ---: | ---: | ---: | ---: | --- |
| trace-1 | 4.259 | 0 | 0 | 0 | valid |
| trace-2 | 20.699 | 1 | 0 | 1 | valid |
| trace-3 | 22.020 | 1 | 2 | 1 | valid |

All four flows in every arm retain all 3,200 scheduled samples. All eight CPUs report zero overruns, commit overruns and dropped events. Generator PID mappings are one-to-one and every ≥5 ms window has complete scheduler-state coverage. Local reparsing of raw event excerpts reproduces **all seven late-window classifications and component values exactly**, including both misses; see [trace-excerpts-recomputed.json](trace-excerpts-recomputed.json). A no-miss traced run neither supports nor refutes the hypothesis.

| Miss | Sleeping / late wakeup | Runnable on idle CPU | Generator on CPU | Associated timer lateness | CPUs after wakeup |
| --- | ---: | ---: | ---: | ---: | --- |
| trace-2, flow 1, seq 366 | 18.636 ms | 0.005 ms | 2.058 ms | 21.779 ms | 4 |
| trace-3, flow 2, seq 770 | 21.524 ms | 0.003 ms | 0.494 ms | 22.051 ms | 1 |

Both registered dominant classes are **sleeping (late wakeup)**, not IRQ/softirq while the generator was runnable/running. Neither meets the frozen IRQ 130 support rule; core-0 NET_RX time being nonzero is not enough. This does not prove IRQ 130 harmless in general, nor retroactively attribute the earlier untraced formal miss. Trace overhead remains a limitation.

### Post-hoc read-only context: the sleeping CPU is CPU 2

The registered analyzer's CPU list describes runnable/running placement; it does not include the CPU on which the generator previously slept. [analyze_sleep_context.py](analyze_sleep_context.py) separately inspects that sleeping interval, without changing any frozen verdict. Both generators slept on **CPU 2**, and their delayed hrtimer expiry/waking events occurred on CPU 2 before migration to CPU 4 or CPU 1.

| Trace | Preceding host IRQ | CPU 2 NET_RX entry → exit (monotonic seconds) | Full NET_RX elapsed interval | Portion overlapping the miss's sleeping interval |
| --- | --- | --- | ---: | ---: |
| trace-2 | IRQ 132, eno1-TxRx-3 | 27053324.086706 → 27053324.121870 | 35.164 ms | 18.636 ms |
| trace-3 | IRQ 132, eno1-TxRx-3 | 27053549.272649 → 27053549.307829 | 35.180 ms | 21.524 ms |

In each case IRQ 132 returned immediately before NET_RX entered. A roughly 2 ms select/poll hrtimer was already armed; its observed expiry was ~21.8/22.1 ms late, inside the long NET_RX interval. Each other UDP flow started two requests inside the corresponding miss window, so these windows are not whole-host stops. Full timing/pointer evidence is in [sleep-context.json](sleep-context.json), backed by the retained raw excerpts. Read-only post-run device identification found **Intel I210, `igb`, PCI 0000:04:00.0**, firmware 3.25.

This narrows the next investigation to CPU 2 receive/interrupt processing and delayed timer execution. It does **not** identify which device/NAPI callback consumed the NET_RX interval, prove an `igb` defect, or make the 35 ms elapsed span exclusive driver CPU time: intervening interrupts, untraced execution and trace overhead are not separated here. NET_RX may handle multiple devices. A focused, separately registered NAPI/call-path and IRQ-off/timer diagnostic is needed before changing driver/kernel settings. No such additional measurement was run.

## Part 2: fixed untraced isolation control

A = default placement; B = whole arm on CPUs `1-3,5-7`; C = B plus all eno1 IRQs temporarily on `0,4`. Five rounds ran in exactly the registered order: **ABC / BCA / CAB / ABC / BCA**. Every run had all 12,800 UDP lag samples, including misses and timeouts. No outcome was excluded, replaced or rerun.

Primary metric, maximum UDP generator lag per run (ms):

| Round | A | B | C |
| --- | ---: | ---: | ---: |
| 1 | 4.986 | 5.159 | 5.172 |
| 2 | 17.019 | 4.639 | 5.646 |
| 3 | 7.886 | 21.277 | 4.184 |
| 4 | 5.083 | 19.156 | 4.730 |
| 5 | 18.436 | 21.291 | 4.788 |

Frozen one-sided exact Mann–Whitney results:

- **A > B: p = 0.8452, NOT SHOWN.** Excluding core 0 is not shown to lower the maximum lag.
- **B > C: p = 0.1111, NOT SHOWN.** Moving NIC IRQs off the test cores is not shown to lower it further at α=0.05.
- Descriptive only: runs with generator misses A/B/C = **0/2/0**; total missed samples = **0/3/0**. C's five maxima were 4.184–5.646 ms, but this is not statistical proof of a remedy or qualification evidence. B still had 21.277 and 21.291 ms misses with core 0 excluded.

Baseline failures are separate from the primary lag statistic and are all retained:

| Run | Missed arrivals | UDP timeouts | Baseline exit |
| --- | ---: | ---: | ---: |
| r2-B | 0 | 3 | 1 |
| r2-A | 0 | 1 | 1 |
| r3-C | 0 | 3 | 1 |
| r3-B | 1 | 0 | 1 |
| r5-B | 2 | 2 | 1 |
| r5-A | 0 | 1 | 1 |

The other nine isolation baselines exited 0. Timeout totals A/B/C = **2/5/3**. A timeout is not automatically proven packet loss; raw samples preserve any late completion. No injector was installed in these baseline arms. Future work must also locate these baseline timeout failures, rather than treating a low generator-lag metric as a complete baseline pass. All 18 child cleanup records are PASS.

## Restoration and independent verification

All five C runs put every eno1 vector's effective CPU within `{0,4}`. After each C, both configured and effective affinities exactly matched the originals, including IRQ 128's broad `0-7` configured mask and effective CPU 6. Every later A/B host-before snapshot was also checked against those originals. Final state:

| IRQ | Configured CPU list | Effective CPU |
| --- | --- | --- |
| 128 | 0-7 | 6 |
| 129 | 7 | 7 |
| 130 | 0 | 0 |
| 131 | 1 | 1 |
| 132 | 2 | 2 |

The public lease directory, private tracing instances and registered namespace list are empty. Existing service PIDs and container state are unchanged; see [host-restoration-check.json](host-restoration-check.json). No preparation, measurement or independent-verification invocation was repeated after failure. The interpreter/affinity/coverage fixes were made before any measurement, not to salvage an observed result.

[recompute_results.py](recompute_results.py) independently verifies retrieved hashes, complete raw sample sequences, frozen identities, 20 ms environment, cleanup, all IRQ restorations and the exact rank decisions. [verify_trace_excerpts.py](verify_trace_excerpts.py) reparses the raw excerpt events locally and matches the full-trace classifications. Results are in [independent-recomputed.json](independent-recomputed.json). Remote and local metric/statistical results agree.

## Evidence and handoff

- Remote root retained: `/root/zfstack-qa-pr75-20261016-ovh51-01/sched-trace-20261016`.
- Retrieved **256 files**, all size/hash checked, plus local/preflight scripts and read-only checks. Full traces exceed the registered 50 MB transfer limit and remain on the host with their hashes. The two excerpt files contain *every* registered ≥5 ms window with 50 ms context; they are explicitly excerpts, not replacements for complete traces.
- **669** bulk trace/rig files are indexed as retained on the host in [evidence-index.json](evidence-index.json). Runtime configurations and WireGuard keys were not collected.
- Collected archive SHA-256: `393309db67dc30ec0fa92bb4ac613f51ce377bd9cf4164872c34f9cc79d8c292` (13820359 bytes). The archive was verified before extraction and is versioned as `evidence.tar.gz`, preserving all raw bytes without adding 2.8 million JSON lines to the review. `retained_evidence.py` verifies the archive, materializes the ignored `evidence/` directory on first use, and checks all original file hashes. Run the verification scripts with Python 3.12+ from this directory or the repository.
- All failures remain in the report and raw data. No parameter changes, no selected reruns, no new formal C16 and no change to known-BLOCKED. Any qualification placement change requires a maintainer decision and a separately pre-registered attempt.
