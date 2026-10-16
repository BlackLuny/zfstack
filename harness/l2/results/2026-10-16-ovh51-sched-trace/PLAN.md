# OVH scheduling/IRQ diagnostic and physical-core isolation control — registered before execution

Follows [the OVH report](../2026-10-16-netem-fifo-ovh51/REPORT.md). The one formal C16 v3 attempt is
**NOT_RUN / UNKNOWN**: in the baseline arm, udp-3 seq 186 was 23.053 ms late against the unchanged
20 ms generator tolerance, so the fault arm never started. The other three UDP generators kept sending
inside that 30.4 ms window, so it was not a whole-host stop. eno1 IRQ 130 is bound to CPU 0, whose SMT
sibling is CPU 4. No scheduling record exists for the miss, so cause is unconfirmed. Every physical
core also carries an eno1 vector (128→6, 129→7, 130→0, 131→1, 132→2), so avoiding CPU 0, or core 0,
does not avoid NIC interrupts.

Nothing here is a qualification run. The formal attempt stays NOT_RUN, C16 case v3, the 20 ms limits
and strict order are unchanged, and known-BLOCKED stays. No formal invocation is part of this plan; a
later one needs its own pre-registration. Every run below executes exactly once, in the order given,
and every outcome is retained; nothing is repeated, replaced or chosen.

## Host boundaries

- Target `root@51.75.146.55:2280` (Debian, kernel 6.12.57, Xeon E3-1270 v6, 4 cores / 8 threads).
  The user confirmed its services are test-only and permitted tracing.
- Do not stop, restart or reconfigure any service.
- Hold the public QA lease `/var/lock/zfstack-qa`, domain
  `zfstack-qa-baremetal-51.75.146.55-2280`, for every arm.
- No system packages are installed or upgraded. Every network operation stays inside
  `qa/isolate-linux.sh`, which the formal command already uses.
- Tracing uses only a private tracefs instance, `qa44sched`, created and removed by `arm.py`.
  If that instance already exists, `arm.py` refuses to start. It never mounts tracefs and never
  touches global tracing state.
- Only part 2, arm C, changes host state: eno1 IRQ affinity. It does so only with
  `--allow-irq-affinity` and only while irqbalance is inactive. It restores and verifies the original
  `smp_affinity_list` after every run, and a failed restore stops the sequence.
- Reuse task directory `/root/zfstack-qa-pr75-20261016-ovh51-01`: its `node.json`, `qa-venv`, the
  frozen rig and the frozen worker (SHA-256 `d921130672b975fb48f646e7ff159301ea5089d51a8fdd06b16cd0a08477a551`).
  Before each arm, `arm.py` checks that the checkout is clean at the expected SHA, that the files the
  baseline arm runs are byte-identical to formal source `604a618` (`git diff` over `harness/l2/*.py`,
  `harness/l2/*.sh`, `qa/*.py`, `qa/*.sh` and `qa/policy/cases.json`), that the worker hash matches,
  that the interpreter is the task venv, and that no cargo or rustc is running.

## Part 1: synchronized scheduler/IRQ trace, three traced baseline arms

`arm.py --trace` runs the formal baseline command, `qa.run.commands(...)[0]`, under the runner's own
environment and subreaper. Runs are labelled `trace-1`, `trace-2` and `trace-3`, in that order.

Recording:

- Events: sched_switch, sched_wakeup, sched_wakeup_new, sched_waking, sched_migrate_task,
  sched_process_fork, sched_process_exit, irq_handler_entry/exit, softirq_entry/exit, hrtimer_start
  and hrtimer_expire_entry/exit.
- Monotonic clock, the same clock as the generator timestamps. 256 MiB per CPU, overwrite off.
- Snapshots before and after: `/proc/interrupts`, softirqs, stat, softnet_stat, every IRQ's affinity,
  CPU topology, governors and irqbalance state.
- The driver's own descendants mapped to host PIDs through `/proc`, including command lines; no
  other process's command line is read.

`analyze_trace.py` classifies every UDP sample at least 5 ms late; samples over 20 ms are misses. It
splits the generator thread's window from scheduled time to observed time into five components, and
the largest one labels the window:

- **sleeping (late wakeup):** also reports how late the waking select/poll hrtimer fired;
- **runnable, CPU idle;**
- **runnable behind other tasks:** names those tasks;
- **IRQ/softirq on generator CPU:** IRQ and softirq time on that CPU while the generator was runnable
  or running;
- **generator on CPU:** Python/runtime or system-call time.

Each window also records the generator's CPUs, whether it was on core 0, IRQ 130 handler time and
NET_RX softirq time on core 0, IRQ/softirq time on the SMT sibling, and the top IRQ and softirq labels.

Validity is fail-closed. An arm is INCONCLUSIVE if any CPU reports overrun or dropped events, if a
late window is not covered by the trace, or if the four generators cannot be mapped one to one.

Interpretation, fixed now:

- Misses (over 20 ms) in a valid trace are decisive for that miss. Windows of 5–20 ms are descriptive.
- The IRQ 130 hypothesis is supported only if misses are labelled "IRQ/softirq on generator CPU" with
  non-zero core-0 IRQ 130 or NET_RX time while the generator ran or waited on CPU 0 or CPU 4.
- Any other dominant class points elsewhere.
- Tracing perturbs timing. A run without misses neither supports nor refutes any hypothesis; its
  late windows are only described.

## Part 2: physical-core isolation control, untraced

Run this after part 1, whatever part 1 shows, with `isolation.py`. The arms:

| Arm | Placement | NIC IRQs |
| --- | --- | --- |
| A | default, no affinity (as formal) | unchanged |
| B | whole arm under `taskset -c 1-3,5-7`: physical cores 1–3 with both SMT siblings; core 0 (CPUs 0 and 4) excluded | unchanged |
| C | as B | every eno1 IRQ set to CPUs 0,4 for the run, then restored |

- **Order:** five rounds, each running every arm once, with the order rotated by round. With C:
  A,B,C / B,C,A / C,A,B / A,B,C / B,C,A. Without C: A,B / B,A / A,B / B,A / A,B. That is 15 or 10
  arms, all retained.
- **Arm C condition:** C runs only with `--allow-irq-affinity` (the user's explicit permission) and an
  inactive irqbalance. Otherwise C is recorded as NOT_RUN.
- **Placement rule:** this is the first, explicit, diagnostic-only revision of the earlier rule against
  setting affinity. It does not change any qualification node policy.
- **Primary metric:** the maximum UDP generator lag per run, over all four flows and all phases of the
  baseline arm.
- **Test:** one-sided exact Mann–Whitney for A > B and for B > C, alpha 0.05. Each result is LOWER or
  NOT SHOWN. An arm with fewer than four valid runs makes the comparison INCONCLUSIVE.
- **Secondary, descriptive:** runs with any miss, the 99.9th percentile of lag, and the count of
  samples over 5 ms.
- **Meaning of the outcomes:**
  - A > B shown: excluding core 0 lowers the generator's worst lag.
  - B > C shown: removing NIC vectors from the test cores lowers it further.
  - Neither shown: placement is not shown to matter at this sample size.
- No outcome changes C16. Adopting any CPU or IRQ placement for qualification needs a maintainer
  decision, a node-policy change and a new pre-registered formal attempt.

## Commands

The checkout lives at `$T/source-trace`, a clean clone of the commit containing this plan, made from a
bundle of an explicit local branch. Output goes outside the checkout. The interpreter is the task venv.

```bash
T=/root/zfstack-qa-pr75-20261016-ovh51-01; PY=$T/qa-venv/bin/python; SHA=<full SHA of this plan's commit>
D=zfstack-qa-baremetal-51.75.146.55-2280; O=$T/sched-trace-$(date -u +%Y%m%d)
cd $T/source-trace
for n in 1 2 3; do
  PYTHONDONTWRITEBYTECODE=1 $PY harness/l2/results/2026-10-16-ovh51-sched-trace/arm.py --expected-sha $SHA \
    --task-dir $T --output $O/trace-$n --label trace-$n --domain $D --run-id sched-trace-$n --trace
  PYTHONDONTWRITEBYTECODE=1 $PY harness/l2/results/2026-10-16-ovh51-sched-trace/analyze_trace.py $O/trace-$n \
    --output $O/trace-$n/analysis.json
done
# Part 2; add --allow-irq-affinity only with the user's explicit permission for arm C
PYTHONDONTWRITEBYTECODE=1 $PY harness/l2/results/2026-10-16-ovh51-sched-trace/isolation.py --expected-sha $SHA \
  --task-dir $T --output $O/isolation --domain $D --run-id sched-isolation
```

## Retention

Keep everything below `$O` on the host. Bring back into this directory:

- every plan, execution and analysis JSON, the trace stats, process maps, host snapshots, logs and
  isolation summary;
- each `trace.txt.gz` if it is at most 50 MB, otherwise only its size and SHA-256.

Bulky rig outputs stay on the host with their hashes. Failures and preparation problems are reported
as they happened.
