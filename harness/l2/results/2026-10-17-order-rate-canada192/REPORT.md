# Canada order-rate comparison v2 — INCONCLUSIVE; services restored

The authorized 60-run comparison completed once, in the exact frozen allocation, on Canadian
bare metal `root@192.99.148.123:24946` (`ns512147`, Debian kernel `6.1.0-41-amd64`, Python 3.11.2).
Tested source: **97e78aa30d705ce8d0ab2d77ddfb4027270d0be2**; clean source verified before and after.
The measured injector/control and isolation/supervision code are unchanged from `ca9b597`.

Measurement: **2026-10-17 03:56:22–04:40:53, Asia/Ho_Chi_Minh (UTC+07)**, 44 min 30.91 s.
User authorization is retained in `authorization.json`. This was the revised [v2 comparison](../2026-10-17-order-rate/PLAN.md),
not a gate or a formal C16 attempt. No run was repeated, replaced, resumed or appended.

**The result does not show netem-fifo overtaking more frequently. Plain netem did overtake in
three valid runs. This is not evidence of equivalence or proof of the same kernel cause.**
The frozen verdict is **INCONCLUSIVE**, because each arm has one unknown outcome and F superiority
does not survive the registered missing-result analysis. No threshold, validity rule or verdict
was changed after observing results.

## Registered comparison

| Arm | Planned/executed | Valid | Unknown | Valid runs with overtakes | Overtaken datagrams |
| --- | --- | --- | --- | --- | --- |
| F: netem-fifo full | 30/30 | 29 | 1 | 2/29 | 6 |
| P: plain netem | 30/30 | 29 | 1 | 3/29 | 10 |

- Complete pairs: 28. Discordance F-only/P-only = **2/3**; one-sided paired p = **0.8125**.
- Least-favorable completion (unknown F clean, unknown P overtaking): discordance **2/4**;
  primary decision uses **p = 0.890625**.
- Secondary descriptive Fisher p on valid runs = 0.8237639553; it does not control the verdict.
- All-slot 95% Clopper–Pearson envelopes allowing unknown outcomes: F **0.82–26.53%**,
  P **2.11–30.72%**. Conditional intervals on valid runs: F 0.85–22.77%, P 2.19–27.35%.
  These binomial intervals do not remove possible serial/background dependence.

Receiver overtakes, recomputed directly from sequence order:

| Run | Overtaken datagrams | Send phase |
| --- | --- | --- |
| F-08 | 1 | loaded |
| F-28 | 5 | 4 loaded, 1 recovery |
| P-10 | 3 | loaded |
| P-20 | 6 | loaded |
| P-25 | 1 | loaded |

“Send phase” is the packet's sender timestamp, not necessarily its egress/receive phase. P has no
capture ledger, so these observations alone do not locate its reordering before/after egress.
Both overtaking F runs passed every non-order validity check, including the raw ledger oracle.
Their self-test exit 1 is retained as an order failure, not relabeled a self-test PASS.

## The two unknown outcomes

**P-18: one missing datagram, sequence 6502.** It received 18474/18475, with no duplicates or observed
overtakes. Sender EAGAIN, socket error counters and link-drop deltas were zero; the control's
last qdisc drop counter was also zero. The exact-sequence rule correctly made the endpoint unknown.

The missing sequence lies between samples 6501 and 6503, whose sender timestamps fall inside the
6.426325 ms root-netem installation command interval. Removal finished about 238.34 ms **before**
recovery, so this run does not exhibit the previously reported remove-after-recovery race.
There is no per-packet P ledger or trace. Association with installation is a timing observation,
not proof of the precise loss mechanism; zero aggregate counters do not establish no loss.

**F-29: the active phase-change request took 257.645639 ms.** It started only 0.203613 ms after
planned loaded start, but completed 257.849252 ms after that boundary. The frozen phase window
and boundary tolerance are **5 ms** (separate from the two unchanged 20 ms limits).
The other 89 F phase changes were at most 0.243281 ms.

The original oracle errors are retained:

- `a netem FIFO phase change exceeded its bounded window`
- `netem FIFO active change missed its planned boundary`

All 18475 UDP packets arrived once and in order, but **104 packets** in the active interior
(sequence 6525–6628, after the self-test's 50 ms offset) failed the required 500 ms delay;
minimum latency was **0.041435 ms**. The run remains invalid even though it had no receiver overtake.

The source timestamps bracket `Netlink.change()` from before `_request(RTM_NEWQDISC, NLM_F_ACK, …)`
through its return. JSON qdisc readback happens afterwards, outside this 257.65 ms interval.
**128 UDP packets were sent and received entirely inside that interval**, so it cannot be explained
as an uninterrupted whole-machine stop lasting the entire interval. Without tracing, this does not
distinguish netlink/kernel waiting from injector-thread scheduling, or establish a particular lock,
IRQ, CPU or component as the cause. This is a separate concrete blocker to investigate; changing
the receiver-order obligation would not repair it.

## Environment, cleanup and restoration

Before measurement, the authorized procedure stopped `rc-worker.service`, `zfc-rig-watch.service`
and their active rig-watch scopes, retaining the original service states and existing output.
No unrelated service, host network, CPU/IRQ affinity, priority or power setting was changed. No tracing.
An owned 6-hour backup restoration timer was installed before service changes; normal restoration
ran immediately after the driver exited, and the timer was then stopped.

The 10-second preflight sample reported total background CPU **8.09% of one logical CPU**;
the largest process was dockerd at 1.99%. Other pre-existing workers remained running at low load.
This was not a zero-background host. Before traffic, the outer launch record fixed a conservative
preflight stop rule (>10% for any surviving process or >25% total); the sample passed. This rule
changed no workload, validity, statistical threshold or measured parameter.

The public QA lease covered the sequence. Each run's host boundary checks and per-CPU `/proc/stat`
and `/proc/softirqs` are retained. Those boundary snapshots cannot rule out all within-run interference.
All 60 owned-process cleanups passed; all F injector cleanups were safe. No environmental abort,
timeout or quarantine occurred. The independent restoration check confirms:

- Both services: **active / running / enabled**, matching their originals.
- Owned backup timer: **inactive**; experiment supervisor completed.
- Public QA lease directory: **empty**.
- Test source: still clean at **97e78aa**.

## Evidence and independent checks

`collected-artifacts.tar.gz` retains **624 files**, including all 60 runs, all nonzero exits and
invalid outcomes, raw receiver/sender/ledger/counters, process lifecycle, service/launch/restoration
records, remote recomputation and helper scripts. Source bundles and unrelated runtime configs/
WireGuard keys are excluded. Historical runs, v1 smoke and their verdicts remain unchanged.

- Archive size: **62,620,664 bytes**.
- SHA-256: `39b7871fc4a6e494815790cad989d827e25bad3e115d1ffaa4d158fc83391c55`.
- Every file's size and SHA-256 verified against `collected-artifacts.index.json` before extraction.
- Frozen `recompute.py` executed remotely and locally after retrieval. Both independently reread
  raw evidence; their full recomputed results match each other and the original summary.
- `analyze-results.py` separately counts raw receiver overtakes and recalculates the paired exact
  tails, verifies the 60-position allocation, and records descriptive timing without changing grades.

Reproduce offline (fresh extraction/output paths; no host access or traffic):

```bash
python3.12 verify-evidence.py --extract /absolute/new-evidence
PYTHONDONTWRITEBYTECODE=1 python3.12 ../2026-10-17-order-rate/recompute.py \
  /absolute/new-evidence/order-rate --output /absolute/new-recomputed.json
python3.12 analyze-results.py /absolute/new-evidence \
  --recomputed /absolute/new-recomputed.json --output /absolute/new-analysis.json
```

## Next direction; no further run authorized here

The physical-host plain-netem overtakes are now observed evidence; the old “P has never overtaken”
pattern no longer holds. The experiment did not establish a higher F rate or a shared mechanism.
Maintainers can consider the meaning of the receiver-order obligation only in a new pre-registration,
without retroactively changing this verdict or past failures.

Prioritize a separately registered, targeted diagnostic of the **F-29 request-to-ACK stall and
missing active delay**, rather than repeating this same 60-run comparison. Do not assume that
changing the order check would make qualification pass. No such diagnostic, component ablation,
gate, frozen worker build or formal C16 was started here.

**C16 remains case v3; strict order, both 20 ms limits and known-BLOCKED are unchanged. PR #75
remains draft, and #59 remains open.**
