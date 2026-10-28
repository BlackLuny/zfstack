# Independent placement audit after a blinded observer false positive

During ordinary enrollment, row `0196-bbr-r50-n8-fixed-r04-0-p99` completed
with outer return code 1. Keep that original failure, original strict verdict,
all errors and every raw file unchanged. No paired effects had been computed
or inspected when this clarification was specified.

The frozen observer reads a thread's affinity, decides a desired mask, then
reads its affinity again. A normal RR thread can execute `rr_cpu()` between
the two reads: the first mask is [3], the desired mask becomes [3], then the
actual second mask is [4]. The observer reports `actual mask differs`, even
though [4] is precisely the frozen protocol's allowed RR self-assignment.
The same observer already preserves [4] when it sees it on the first read.
This is a comparison race in instrumentation, not a request to allow arbitrary
affinity deviations.

For the observed failure, the benchmark child returned 0, its JSON is `ok:true`
with no errors, all 1,650 arrivals completed, and all six RR ledgers record
requested/reader/sender CPU 4. The error thread's first recorded mask is [3]
and last is [4], with its last observation at 2.010 s, before the 8-s warmup.
All sampled native roles have the required masks and the driver has zero
post-assignment migrations. The original outer code 1 is entirely produced by
the observer's own final error verdict. Exact evidence is in
`qa/results/common-20261025-preparation/observer-investigation.json`.

The controller alone was paused for 128.3 s while the already running child
was allowed to finish. No child was killed, retried or given a longer measured
window. Enrollment resumed with the original runtime observer and workload
unchanged. The affected adjacent pair is rows 0206/0207: retain its longer
between-run gap, without deleting or downweighting it. The pause/resume JSON
records exact times. Statistical variation from that gap remains in its pair.

## A second view, never an overwrite of the frozen view

The frozen `common_cases.py`, `common_env.py` and `common_collect.py` continue
unchanged. Run their original collector first and retain its original counts,
errors and qualification, including any failed strict gate. Add a separate
read-only audit of **every** attempt using `common_reaudit.py`:

- Recheck required role identity, assignment deadlines, every sampled actual
  mask, observer CPU 4, and native driver zero migration. The intended masks
  remain driver 0/link up 1/link down 2, ordinary threads 3, explicit RR
  self-assigned threads 4. The RR arrival ledgers must all report CPU 4.
- Accept an instrumentation-race certificate only for a fixed-placement native
  interactive run with child return 0, benchmark `ok:true` and no benchmark
  errors, outer return 1, and original summary errors consisting solely of the
  outer process failure. No binary/configuration/teardown/throttling/arrival or
  independent mask audit error is allowed.
- Every observer error must exactly identify an `actual mask differs` for a
  non-role thread whose recorded history is a one-way [3] to [4] transition
  (or already [4]), ending at [4]. No critical role, reversal, extra mask,
  late required-role assignment, unknown error or failed child can qualify.
- The executable's only `sched_setaffinity` is `rr_cpu()` in `isolate.rs`.
  That function assigns CPU 4 before setting the RR timing origin; the reader
  and its sender inherit/retain it. The observer never assigns CPU 4 to an
  ordinary thread; it only preserves that self-assignment. Thus the allowed
  transition is the source-defined RR action, not a new placement policy.
- Preserve original strict/SLO flags. A certified run gains a separately named
  independent observation/service verdict. Recompute the unchanged estimator,
  bounds and decision rules in a separate directory using those independently
  audited validity bits. Check that **all numerical paired effects and
  confidence intervals are identical** to the frozen first view. No row or
  pair is dropped; no extra sample is substituted.

Publish both views and their exact difference. The original automatic gate
does not retroactively pass. Any recommendation based on the independently
audited view must explicitly disclose that distinction and the pause. If the
certificate or any independent audit fails, keep that case unqualified; do
not call the issue environmental noise. Additional unrecognized errors remain
failures and require investigation before inference.

## Later blinded startup and missing-data clarifications

The subsequent [generator confirmation](GENERATOR-CONFIRMATION.md) documents
fail-closed missing-field guards added to the analysis collector. The measuring
`common_cases.py` and `common_env.py` remain unchanged. Original strict flags,
all numeric estimators, draws, pairs and margins are retained.

Before any treatment effects were inspected, independent confirmation row
`0003-generator-confirm-r01-1-p99` exposed another early comparison race:
`actual mask differs 34287 taskset`. The wrapper was first observed at
0.000220 s with mask [0,1,2,3], subsequently execed Python and spawned the
benchmark; all three native roles were observed by 0.043347 s, with PPID 34287.
Every post-2-s wrapper sample has name `python3` and mask [3]. The child and
all requests completed normally. This satisfies the existing 2-s assignment
bound; a comparison during taskset's initial assignment is premature.

The additional certificate requires that exact taskset error, fixed native
placement, the wrapper leader's initial/last masks, its actual parentage of
every required role observed by 2 s, and all post-assignment wrapper samples
remaining Python on CPU 3. The existing full-mask, zero-driver-migration,
child-success, payload, arrival and identity checks still apply. A late role,
wrong parent, post-bound mask deviation or other error cannot qualify. Keep
the original startup failure separately identified; no new benchmark retry or
measurement parameter change is introduced.
