# Blinded corrective confirmation after a generator miss

Specified before inspecting any treatment effects. Ordinary row
`0487-cubic-r100-n8-fixed-r10-1-p99` has one missing arrival: peer 3, sequence
298, planned 11,925,000,000 ns, started 11,945,422,219 ns. Its 20.422219-ms
generation lag exceeds the unchanged 20-ms gate, so the generator did not send
that frame. The child correctly returned 1; the observer has no mask error.
This is a real observation failure, not the previously certified mask-read
race. No upper bound for the missing response is known.

## Retain uncertainty in the original matrix

Keep all original runs and the missing request. The original full matrix
cannot qualify as completely observed. The affected cell's full user mean
and P99 remain unavailable; do not impute a fast response, silently drop the
request, relax the generator gate, replace its pair, or pool new runs into
its original 12-pair estimate. All numeric CPU/throughput observations remain
visible with their validity flags.

The original collector assumed every latency field existed and would raise
on this incomplete cell. Add missing-field guards only: unavailable latency
fails its gate rather than crashing or passing. Mark a cell protected only
when its complete observation/service flags and numeric bounds both pass.
The original full-matrix verdict stays false. Complete cells' numerical
estimates and the original bootstrap formulas/margins are unchanged.

The blinded majority supplement has a conservative extension for incomplete
cells: keep all 24 hypotheses, set every incomplete/invalid cell's two p-values
and union p-value to 1, and apply the same `12*p_(13)` test. An unknown cell
cannot improve significance. Report that result separately as
`majority-with-unknowns.json`; do not call the original all-observations
qualification passed. Keep the common-population/independent-sign assumptions.
The simultaneous numeric band still includes all 48 numeric CPU/throughput
columns, but protection counts exclude invalid cells. Adding an invalid
nuisance column to the maximum-deviation calibration cannot narrow the band
for the valid columns; it cannot turn the incomplete case into evidence.

## One fresh complete cell, not a replacement pair

After the unchanged original 648-attempt plan finishes, execute exactly one
independent **24-run / 12-pair** CUBIC/100-ms/8-bulk+3-interactive confirmation.
Use the same frozen V5, container, actual CPU policy, FIFO, windows, payload,
20-ms generator gate, 3-s SLO, 8-s warmup/20-s window and off/on rates. No
new candidate, tuning, window or capacity selection. Randomize six AB/six BA
with seed 2026102504. Every pair/run is retained. No further rerun or sample
extension follows, even if this new cohort is invalid or inconclusive.

Use a fresh `qa/results/common-20261025-generator-confirmation` directory and
freeze its entire sequence and source snapshot. Its wrapper supplies the
explicit 24 rows to the unchanged execution function, in a separate process;
the original running observer/runner remain unchanged. Resume no old attempt.
Apply the already specified independent mask audit to every new attempt, and
keep original outer/strict failures as before.

Require all 24 runs independently observed, all candidate service SLOs and
bulk-flow progress, throughput 95% lower >= -3%, driver CPU/GB upper <= +3%,
and both mean/P99 95% upper <= +5%. Use the same log-ratio geometric-mean,
whole-pair bootstrap, 20,000 draws/seed 2026102502. This cohort is reported
separately; it cannot retroactively validate the original matrix or be spliced
into its same-round bootstrap. Do not publish a composite all-cells CI.

Keep all comparative analysis, tests, plots, compression and final collection
until **both** the original plan and this independent cohort finish. Preserve
the original missed arrival, the fresh outcome and every original validity
flag in the archive. A majority-scene engineering recommendation may use the
conservative majority evidence and this separately identified confirmation;
it must disclose the unqualified original full-matrix gate.
