# Independent steady-throughput follow-up, before any of its attempts

V4's 48 longer single-flow WAN attempts were strictly valid, with zero aggregate
pacing bytes throughout. CUBIC/100 ms passed: median +3.192%, 95% pair-bootstrap
interval +0.130% to +3.922%. BBR/500 ms remained **inconclusive**: median -0.793%,
interval -4.927% to +2.823%. Four of its 12 pairs varied below -3%; preserve all
of them. The result neither establishes a repeatable >3% regression nor proves
the desired -3% lower bound. Do not describe the 40-s guard as passed.

The user requested repeated measurements that distinguish environmental effects
and preserve steady/single-flow throughput. After the already-running fixed V4
peak cohort finishes, run **one additional, independent 24-attempt cohort**:
BBR, 500 ms, one download, zero light users, 12 balanced adjacent off/on pairs,
20-s warmup and **120-s measured windows**. Use the same frozen V4 executable,
100-Mbps/250-KB FIFO, 16-MiB stack ceilings and Linux receive autotuning maximum,
CPU placement, rate=0/12,375,000 IP bytes/s, payload and progress checks. The
existing 180-s/195-s per-attempt bounds still contain this approximately 141-s
trial. No rate, batch, credit, hold, queue, socket or production-code tuning.

This longer window samples more high-RTT congestion-control cycles; it measures
**sustained steady goodput**, not a guarantee for every 40-s or shorter window.
Keep its own 12-pair bootstrap interval; never pool it with the preceding 40-s
or screening samples. Require all 24 observations valid, bypass counters zero,
and the 95% interval's lower bound >= -3% for this steady-throughput claim.
Record the original 40-s result as an unqualified context guard even if the
longer observation passes. Its median was within the tolerance and its interval
contained zero; this ambiguity, rather than a clear measured regression, is
the eligibility condition for the independent follow-up.

Final qualification can report the steady lower bound together with the
original short-window uncertainty. It must not claim that every original guard
passed, all 40-s windows are protected, or all environments are equivalent.
The intervals are individual descriptive pair-bootstrap intervals, not familywise
error control over the full sequence of experiments. If this final fixed cohort
also fails or is inconclusive, retain that limitation and do not extend the
window/sample count again to obtain a passing result.
