# BBR with unknown capacity: prospective adaptive-pacing validation

This is a new opt-in prototype, not the fixed-capacity V5 study. Preserve PR 94,
its frozen executables, failures, verdicts and all sealed reports. Work starts
from `52642445dfae1becbf21d68ee8e2276ca38c139d`, in a separate worktree/branch.
No default change, merge, deployment, remote gateway or credential changes.

## Candidate A0

No link capacity or configured rate enters the core estimator. The benchmark
passes fixed iface rate **zero in both arms**. The candidate selects adaptive
mode through the production core/Tokio API; the control leaves it disabled.
The policy receives only newly ACKed/SACKed delivery and bulk RTT information.
ACKed payload is converted to approximate IP bytes using each connection's
actual headers and MSS; it is an estimate, not exact wire delivery accounting.
Never sum individual BBR bandwidth maxima. No extra packet queue or thread.

- A never-contended peer bypasses without allocating the controller; the hot
  iface wrapper stays 24 bytes. Distinct ready peers activate observation with
  the existing one-second contention hold. Expired contention restarts learning.
- Initial observation is unpaced. Sum delivery over wall-clock windows at least
  max(100 ms, two largest observed bulk minimum RTTs). Require four MTUs and
  bulk RTT samples. Discard gaps longer than four such windows. Small-request
  delayed ACKs do not provide the queue signal.
- First rate = 99% of observed aggregate delivery. Queue signal is delivery-
  weighted bulk smoothed RTT minus each connection's own minimum RTT. The
  target is clamp(base RTT/64, 1 ms, 5 ms), not a common absolute RTT ceiling.
- Subsequent windows qualify as demand-limited by pacing only when accepted
  wire output reaches 75% of the current target's window budget. Otherwise
  hold the estimate instead of mistaking sparse demand for low capacity.
- With qualified demand, queue above target and delivery below 75% of target:
  immediately reprice to 99% delivery. Otherwise every fourth qualified window
  probes upward by 8% from max(target, delivery). Other congested windows use
  min(98% target, 99% delivery); uncongested windows use max(102% target,
  101% delivery). These explicit probes avoid passive repeated 0.99 feedback.
- Existing 2-ms batches, bounded catch-up credit, accepted IP-byte charging,
  TSO headers/debt, sink Full handling and DRR order remain. Learning and
  proportional probing do not prove true capacity identification or universal
  queue stability. In particular, large capacity increases may recover slowly.

Unit/model checks cover app-limited holds, upward exploration under persistent
delay, capacity drops, two-RTT windows, idle gaps, mode coalescing/idempotence,
single-peer packet/time/step trace identity, capacity-change progress and exit
after a late peer leaves. Simulator evidence is reported as such, separately
from native kernel TCP -> production Tokio/splice -> FIFO observations.

## Environment and observations

Same existing Docker image, network none, CPUs 0-4, existing parent four-CPU
quota and sysctl ceilings. No host governor/IRQ/priority/quota changes. Native
driver 0, link directions 1/2, ordinary threads 3, RR sender/readers and observer
4. Required roles by two seconds and zero driver migrations after placement.
Retain original observer comparisons. Allow only the already specified RR
self-assignment/startup race certificates plus independently correct actual
masks; no new certificate designed after effect estimates. Arrival generator
gate remains 20 ms, request SLO 3 s. Missing arrivals invalidate full latency.

Actual native BBR/Tokio/splice; MTU 1420, FIFO 250,000 bytes, zero random loss,
16-MiB stack caps, seed 23, fixed-arrival 25 Hz interactive requests per user.
Warmup max(8 seconds, 40 RTT), useful-work window 20 seconds unless below.
Record CPU/s and CPU/useful GB for driver, driver+link and full process, RSS,
all arrivals, per-flow 250-ms read progress, actual masks, cgroup throttling,
steal/softirq and observer CPU. Process/driver CPU is not full-system CPU;
RSS excludes kernel socket memory. Same-host repetition is not host independence.

## Bounded stages

All directories refuse overwrite. Freeze source, build settings, candidate and
baseline hashes and complete per-phase sequence before its first attempt.
No build/test/analysis/plot/compression during measurement; no outlier deletion,
replacement, extension for significance or favorable subset selection.

1. **12 engineering pilot attempts**: one off/on pair for 100-Mbps BBR at
   1/500 ms, single/8 bulk+3 interactive (8); one 50-ms capacity-change pair
   and one 50-ms producer-pause pair (4). Pilot effects are exploratory only.
   Repair a functional/instrumentation defect before confirmation if needed,
   retaining every failed pilot, old binary and change rationale; at most one
   corrected candidate and a separate complete pilot. Performance tuning is
   not permitted after the confirmation binary/sequence freezes.
2. **192 primary attempts**: 100 Mbps, six RTTs 1/10/50/100/250/500 ms,
   single or 8 bulk+3 interactive; 12 cells, eight balanced off/on pairs each.
   Shuffle all cells within each of eight rounds; four AB/four BA per cell.
3. **32 unknown-bandwidth attempts**: 10/300 Mbps, RTT 50/500 ms,
   8 bulk+3 interactive; four balanced pairs in each of four cells. Rates only
   configure the external FIFO, never the controller. Analyze separately.
4. **16 heavy-load attempts**: 100 Mbps/20 ms, 200 bulk of one peer+3 interactive,
   eight balanced pairs, same windows. Analyze separately from 8-bulk data.
5. **16 disabled-code guards**: V5 versus new candidate with adaptive disabled,
   100 Mbps, single, RTT 1/500 ms, four balanced pairs per cell. Detect overhead
   hidden by a same-binary off/on comparison. Keep different estimands separate.
6. **24 local peaks**: V5 disabled versus candidate adaptive enabled, one/two
   local TUN downloads, six balanced pairs each, original process mask 0-3 and
   observer 4, MTU 65535, two proxy workers, warmup 8/window 30 seconds. Actual
   single-peer bypass must hold; 10% extreme margin, also report original 3%.
7. **16 capacity-change attempts**: 100 Mbps ->20 Mbps at useful-window 10 s
   ->100 Mbps at 25 s, RTT 50/500 ms, 8 bulk+3 interactive, window 50 seconds,
   four balanced off/on pairs per RTT. Both external link serializers receive
   identical schedules and record actual transition offsets. A packet already
   in service retains its deadline; new packets use the new rate. No controller
   notification. Opt-in 1-Hz controller traces in both arms, not primary CPU.
8. **8 demand-change attempts**: 100 Mbps/50 ms, 8 bulk+3 interactive, window
   50 seconds, four balanced pairs. Upstream kernel producers pause downloads
   at useful-window 8 s and resume at 28 s; RR echo continues. Existing buffers
   may drain after pause starts. Actual read-progress silence must be observed
   to call this an app-limited validation; no silently assuming demand stopped.
9. **8 A/A attempts**: same binary off/off, 100 Mbps, single RTT 1/500 ms,
   two pairs per cell, descriptive environmental diagnostic only.

Original full plan after the pilot is 312 attempts (192+32+16+16+24+16+8+8).
Pilot is separate. Identity/timeout failures stop the remaining phase and are
reported; no rerunning a failed arm. Preserve incomplete studies and negative
results. Gross functional errors are fixed before resuming any new study,
with a new frozen identity rather than silently changing an in-flight candidate.

## Prespecified reporting and practical guards

Unit is the full independent pair. Geometric mean of paired log(on/off), all
paired effects and pointwise 95% whole-round bootstrap intervals (20,000 draws,
seed 2026102506). The primary 24 CPU/throughput columns also get the previously
used standardized maximum-deviation simultaneous band, resampling all 12 cells
by complete round. All aggregate cells get equal weight. No pooling pilots,
old V5 cohorts, bandwidths, different workloads or diagnostic traces into it.

Primary protection: throughput lower >= -3%, driver CPU/GB upper <= +3%; +3%
is practical protection, not strictly zero CPU. To recommend the tested BBR
scope, require all observations/identities valid, no throttling or missing
arrivals, candidates meet SLO and every bulk flow progresses in each measured
window; >=9/12 pointwise and >=7/12 simultaneously protected; all six single
throughput guards; all/single/contended CPU and throughput aggregate guards,
single/contended driver+link and full-process CPU/GB upper <=+3%; no clear
entire-interval worse-than-margin cell. For contention, both mean/P99 aggregate
upper <=-5%, every cell's latency upper <=+5%, and >=3/6 cells both upper <=-5%.

Additional bandwidth/heavy cells separately require throughput lower >=-5%,
driver CPU/GB upper <=+5%, latency upper <=+5% and complete valid observations.
Disabled-code and local-peak throughput/CPU use 10% guard (also report 3%).
An inconclusive bound stays inconclusive; cannot validate a default from a
positive pooled mean. Unknown capacity is not permission to guess a rate.

For each dynamic run, derive useful bulk throughput from timestamped 250-ms
progress. For drops and rises, look for a two-second window reaching >=80% of
the corresponding off baseline's final plateau (last five seconds of that
capacity phase) within max(5 seconds, 20 base RTTs) after the step. Its final
five-second throughput must reach >=90% of that reference. Retain all values,
including no recovery. Compare per-phase user-equal mean and worst-user P99
from complete planned-arrival ledgers; +5% nonregression guard, no missing
requests/SLO failures. Application pause must produce at least one second of
near-zero bulk progress (<1% of baseline busy throughput), then resume within
the same time bound and reach >=90% baseline final plateau. Intentionally
empty application windows are not counted as scheduler starvation. No claim
of data capacity identification follows merely from a nonzero rate statistic.

This remains one-host, three tested capacities, symmetric no-random-loss FIFO
evidence, not a guarantee for all Internet paths or hidden shared bottlenecks.
Defaults stay off while qualification is incomplete. Publish exact source and
binary identity, failures, raw ledgers and reproducible analysis in a Draft PR.
