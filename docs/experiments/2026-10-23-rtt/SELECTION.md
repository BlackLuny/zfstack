# Candidate selection, frozen before guards/WAN/confirmation

Selected: **q1s0p0**, peer byte DRR + pushout at the controlled bottleneck;
keep default quantum, round cap, prefetch and budget hierarchy. Same selection
for CUBIC and BBR. Control is q0s0p0, FIFO with the same serializer and limit.
This is a conditional frontier candidate, not a qualified production default.

Evidence used for selection: 32 factorial attempts (all valid), 16 wider-budget
attempts (14 valid), 16 original-budget/wider-prefetch attempts (all valid).
No validation or confirmation results existed at selection time.

- Smaller prefetch raised processing cost. Smaller scheduling batches did not
  establish an independent stable benefit across the repeated CC strata.
- Queue-only screening reduced equal-user mean latency about 62–66% and
  worst-user P99 about 89–90%, at roughly unchanged goodput. Stack+link CPU/GB
  changes were +1.9% CUBIC / +2.6% BBR in two exploratory blocks; equivalence
  is not established by these small samples.
- Wider prefetch plus a peer ceiling equal to the port ceiling caused a real
  request failure: 251 of 1,200 loaded requests unfinished/deadline-exceeded.
  A separate run had one missed generator arrival (34.8 ms lag); retain it too.
- Wider prefetch under original budgets lowered CPU but left roughly half
  the heavy connections without measured progress, versus far fewer in the
  CUBIC controls. Jain fairness within that peer fell from roughly 0.92–0.97
  to 0.48–0.49. This is not accepted as a CPU optimization. The observations
  do not isolate whether the bare-Shard benchmark adapter, budget admission,
  loss recovery, or their interaction causes the starvation; do not present
  this as a confirmed production root cause or fix.

Use the predeclared independent guard, 96-run WAN, 24-run confirmation and
16-run AA phases. No candidate substitution based on favorable individual RTT
cells. If the candidate fails a range/CPU/starvation guard, report the boundary
and retain default core settings; do not silently narrow the headline claim.
