# Receiver-order rate: revised pre-registration v2 — remote execution not yet authorized

Follows [Canada attempt 02](../2026-10-17-netem-fifo-canada192-r2/REPORT.md). Its gate failed on one
receiver overtake: UDP 8836 was overtaken by 8837. Both left eth0 in order; 8836 took 2.64 ms from
egress to the socket. The three plain-netem controls showed none. No traced kernel path was identified.

The historical physical-host counts are F 2/10 (full and cancel pooled) and P 0/6; the one-sided
Fisher p is **0.375**. Adding six emulated P runs changes it to 0.1948, but mixes environments.
Neither calculation establishes causation. The new comparison uses full runs only, on one host.
The attempt-02 counters do not distinguish the runs either: they cannot localize a millisecond event.

**Revision before any remote comparison:** v1 used fixed F→P order and excluded invalid runs from
an unpaired Fisher test. This revision randomizes within pairs, checks the treatment and evidence,
and keeps missing outcomes in a least-favorable sensitivity analysis. The v1 plan is preserved in
Git (363453b / 6d34a49); its TCG smoke files remain untouched and are not evidence for v2.

The question is whether F has a higher probability of a receiver overtake under these conditions.
This is a screen for a large difference, not an equivalence test or a proof of a shared kernel cause.

- **F_HIGHER:** follow with a separately registered component ablation (mirror, rings, process).
- Plain netem also overtaking establishes that it can happen in P, not that both arms share a cause
  or have equal rates. Any change in the order obligation requires a separate maintainer decision.
- Failure to show a difference is reported with interval bounds and all unknown outcomes.

Diagnostic only: C16 case v3, strict receiver order, both 20 ms limits, workload and known-BLOCKED
are unchanged. No gate, frozen build or formal C16 is part of this experiment. PR #75 remains draft;
#59 stays open. The instruction to continue the local revision does not start remote traffic or stop
services. **Obtain authorization for this revised 60-run window after publishing the reviewed commit.**

## Frozen allocation and analysis

- **Identity:** clean checkout of this plan's commit. The driver rejects changes to measured
  `harness/l2/*.py` / `*.sh` (excluding tests), `qa/isolate-linux.sh`, `qa/process.py`, `qa/lease.py`
  and the historical control relative to gate commit `ca9b597`. Only this diagnostic's files change.
- **F:** `fifo_selftest.py --kind netem-fifo --mode full` with all gate defaults.
- **P:** `netem_order_control.py` with its historical v3 timeline and identical sender/receiver/load.
  P's historical late deletion remains observable; no code or parameter correction is made here.
- **Allocation:** 30 adjacent pairs, one F and one P per pair (60 runs, approximately 50 minutes).
  Each pair independently receives order FP or PF from the low bit of the first byte of
  `SHA256(bytes.fromhex(seed) + pair_number.to_bytes(4, 'big'))`: 1 means FP, 0 means PF.
  Seed, drawn once from the local system RNG before execution:
  `28f8082271b2eafc0070c6e013ac939976b571c094ddf3b0aa28f882c1a6bbc6`.
  There is no forced balance or seed search. This draw happens to give 15 of each first-arm order.
  [`allocation.json`](allocation.json) freezes all 60 positions; the driver writes them again to
  `plan.json` before any child starts. The seed and allocation cannot be overridden by CLI.
- **Endpoint:** any receiver datagram overtaken by a later sequence in a valid run. One binary
  endpoint per arm per pair. A duplicate is not an overtake substitute: it invalidates the run.
- **Primary test:** one-sided exact paired McNemar/sign randomization test, F > P, α = 0.05.
  For discordant pairs, let x = F-only overtakes, y = P-only overtakes. Then
  `p = sum(comb(x+y, k), k=x..x+y) / 2**(x+y)`; no discordance gives p = 1.
  This matches independent within-pair assignment. It assumes no material carryover after owned
  processes and namespaces are destroyed; remaining host/time dependence limits generalization.
- **Missing outcomes:** all 30 slots in each arm remain in the analysis. Invalid runs are unknown,
  never removed from the primary denominator, replaced, or assumed to be clean.
  Report the complete-pair statistic descriptively, then complete every unknown **F as no overtake**
  and every unknown **P as overtake** and compute `worst_case_p`. This is the least favorable
  completion for F superiority. Keep the valid partner of an incomplete pair.
- **Decision:**
  - `INCONCLUSIVE` for an environmental/safety abort, incomplete/wrong schedule, smoke run, or
    fewer than 25 valid outcomes in either arm, regardless of any p-value.
  - Otherwise `F_HIGHER` only if `worst_case_p <= 0.05`.
  - Otherwise any unknown outcome means `INCONCLUSIVE`.
  - With all 60 valid and p > 0.05, `NOT_SHOWN_P_OBSERVED` if P overtook, else `NOT_SHOWN`.
    Neither verdict means equivalence or a shared mechanism.
- **Secondary only:** unpaired one-sided Fisher on valid outcomes is labeled descriptive and never
  controls the verdict. Report valid/unknown counts, every invalid reason, observed conditional
  two-sided 95% Clopper–Pearson intervals, and an all-slot interval envelope: lower endpoint with
  all unknowns clean, upper endpoint with all unknowns overtaking. Intervals assume binomial runs;
  serial dependence can make those bounds optimistic.
- **Resolution:** six F-only discordant pairs against zero P-only gives primary p = 0.015625;
  five gives 0.03125; four gives 0.0625. These are not the former unpaired Fisher thresholds.
  Even 0/30 has a two-sided 95% CP upper bound of 11.57%; this cannot establish a very rare rate.

## Validity from retained evidence

[`order_rate.py`](order_rate.py) recomputes validity; summary `ok` / `checks` are not trusted.
Every failure and its raw evidence remain retained even when its endpoint is unknown.

Common requirements:

- Receiver rows have the exact schema `[integer sequence, integer send_ns, integer recv_ns, bool]`,
  sequence in 0..18474, positive timestamps with receive ≥ send, monotonic receive timestamps,
  **exactly that complete sequence set once**, and intact payloads. Malformed rows become unknown,
  not an uncaught exception or a count-only pass.
- Sender sent exactly 18475, reported zero EAGAIN, and positive TCP byte count matches the receiver
  with no TCP payload errors. Receiver `RcvbufErrors` / `InErrors` and link-drop deltas are zero.
- Actual delay is ≥500 ms in the measured active interior; recovery samples exist with latency
  <125 ms, as in the full self-test. For P the active interior starts after its recorded install
  completed (and at least 50 ms after planned loaded start), avoiding the control's install race.
- `qa.process.run_child` provides a Linux subreaper lifecycle with proven cleanup, no timeout,
  cancellation, budget exhaustion or detached descendants. F may exit 1 **only for order failure**;
  the other raw checks must all pass. P must exit 0. A complete receiver alone cannot override these.

F additionally re-runs the frozen ledger oracle (including its unchanged 20 ms bound), validates
frame conservation/GSO preservation, qdisc restoration, successful injector cleanup, and removal
after sender reap and measurement end. These are recomputed from ledger, kernel counters and
lifecycle records. Missing/unreadable F cleanup evidence is a safety stop, not permission to proceed.

P uses its pinned script's checked `tc` command intervals, planned timeline, qdisc drop count and
before/after link counters. Install, drain and deletion must complete in chronological order.
P has **no option readback, mirror ledger or explicit qdisc-restoration snapshot**; do not claim
those. Proven subreaper cleanup plus destruction of the private PID/mount/net namespace contains
its test resources. Its recorded deletion can still be after the recovery boundary. Loss from that
historical race is retained as an unknown endpoint and enters the sensitivity analysis.

## Execution and stopping

- Hold the public QA lease for the whole sequence; `run_child` monitors it during every child.
  All traffic runs inside `qa/isolate-linux.sh` with a 300 s per-run limit, normal output/disk
  budgets, and owned-process cleanup. Save every child lifecycle before assessing results.
- Save per-CPU `/proc/stat` and `/proc/softirqs` before/after every step. Between runs check boot
  identity, stopped services, compiler processes and rig-watch scopes. These boundary checks do
  not prove zero interference throughout a run; no such claim will be made.
- No stopping based on p-values or the number of overtakes; print progress only. Invalid results
  continue at the next frozen position **only when cleanup and environment remain known safe**.
- Stop for lost lease, changed boot, competing compiler/rig activity, restarted paused service,
  unknown/failed cleanup, operator interruption, or an unrecoverable driver/environment error.
  Record `abort.json`, retain the partial execution, mark INCONCLUSIVE, and restore services.
  Cleanup uncertainty quarantines the public lease domain for inspection. Do not bypass quarantine.
  No resume, replacement run, or restart is authorized by this plan.
- No tracing, CPU/IRQ affinity, priority, power changes or additional host-service changes.
  The symmetric subreaper supervision is an explicit diagnostic-wrapper change from v1; the
  measured injector and historical control remain byte-identical to the gate.

## Host, restoration and commands

Proposed target: Canadian bare metal `root@192.99.148.123:24946`, domain
`zfstack-qa-baremetal-192.99.148.123-24946`. Use a new task directory and explicit-branch git bundle
for the new reviewed commit. Verify the bundle, clean source, host identity and available disk first.
Another host requires the user to name it and its boundaries to be recorded before traffic.

After authorization, reuse attempt 02's stop/restore procedure for `rc-worker.service`,
`zfc-rig-watch.service` and owned rig-watch scopes, with **new task paths and unique backup-timer
names**. Save original active/enabled states and interrupted output. Install owned backup restoration
before stopping services. Verify no competing compiler/rig task and sample background CPU before
launch. Do not begin with an occupied/quarantined lease or substantial competing work.

Run through an outer `try/finally` supervisor that restores originally active services on every
exit and verifies active/enabled states immediately. Remove the backup timer only after verified
restoration; retain its evidence. Do not restart interrupted jobs as replacement measurements.
The diagnostic driver itself does not change services.

```bash
cd "$T/source"
PYTHONDONTWRITEBYTECODE=1 python3 harness/l2/results/2026-10-17-order-rate/order_rate.py \
  --expected-sha <full-reviewed-plan-commit> --output "$T/order-rate" \
  --domain zfstack-qa-baremetal-192.99.148.123-24946 --run-id order-rate-v2-20261017
```

Collect `plan.json`, `execution.json`, `summary.json`, `abort.json` if present, all summaries/logs,
child lifecycle and host-boundary records, raw sender/receiver/ledger/kernel/lifecycle files,
service restoration evidence, and file/archive hashes. No WireGuard keys or unrelated runtime configs.
Evaluate again after retrieval with the same diagnostic source, without traffic or editing originals:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 harness/l2/results/2026-10-17-order-rate/recompute.py \
  /absolute/retrieved/order-rate --output /absolute/new-recomputed.json
```

The offline evaluator ignores the execution's cached validity and receiver counts and re-reads raw
files. A mismatch requires investigation, never a re-run to replace unfavorable evidence.

## Local validation and historical smoke

[`smoke-local/`](smoke-local/) is the unchanged **v1** TCG smoke: one run per arm, both clean,
INCONCLUSIVE. It predates randomized pairing and the stricter validity/supervision; it is not a v2
smoke or a physical comparison. New unit regressions replay retained Canada evidence offline and
mock the process runner to test allocation, aborts, quarantine and raw re-evaluation. They emit no
traffic and do not change any old verdict. See `REVISION.md` for the final committed test results.
