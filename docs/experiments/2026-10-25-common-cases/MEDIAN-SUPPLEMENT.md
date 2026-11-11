# Blinded supplemental majority test

Specified from the 2026-10-25 02:01:49 UTC progress snapshot (120/576 ordinary
runs), while enrollment continued and before any
on/off effect calculation or inspection. Only progress, correctness and
placement validity have been inspected. No sample, executable, scenario,
primary estimator, bootstrap band or decision rule is changed. This is a
separate median estimand and sensitivity analysis, not a way to override a
failed primary qualification.

The primary bootstrap band is explicitly approximate at 12 pairs/cell. Add an
exact one-sided sign test for each ordinary cell's two protection endpoints:

- CPU null: median paired CPU/GB increase >= +3%; count the 12 effects < +3%.
- Throughput null: median paired throughput change <= -3%; count effects > -3%.
- For s successes among n=12 independent pairs, the least-favorable binomial
  tail is `sum(comb(n,k), k=s..n)/2**n`. Ties count against protection. No normal
  distribution or symmetric effect distribution is assumed.
- A cell is protected only when both endpoints meet their margins. Its union
  null (at least one endpoint fails) has valid p-value `max(p_cpu,p_throughput)`;
  multiplying endpoint p-values would be unjustified.

Let these 24 cell p-values sorted increasingly be p_(1)..p_(24). The
predeclared supplemental question is whether at least 13/24 cells have both
protected median endpoints. Use the Bonferroni partial-conjunction p-value:

```
p_majority = min(1, (24-13+1) * p_(13)).
```

This controls the at-most-12-protected-cells null at alpha=.05 and permits
arbitrary dependence between cells/endpoints. To see why, consider every
subset of 12 cell hypotheses. Under the null at least one such subset consists
only of true nulls. Bonferroni gives a valid global-null test for each subset;
requiring rejection for every subset is an intersection-union test. The
largest subset p-value is obtained by taking the 12 largest cell p-values,
whose minimum is p_(13), giving the expression above. The component cell test
is itself an intersection-union test by the same maximum-p argument.

The remaining sampling assumption is **independent run-pair signs within a
cell from a common paired-effect population** (or, under the null, success
probability at most one half in every round). Interleaving cells over
six hours and balancing order addresses environmental drift but cannot prove
that assumption, exclusive hardware, absence of every host fluctuation, or a
real-world frequency of these scenarios. This is an exact test conditional on
its model, not a mathematical CPU-cycle bound or a host-independent theorem.
Do not scan unplanned margins, choose another protected-cell count after
seeing results, pool historical pairs or drop a failed pair.

Report the 24 success counts, component and union p-values, p_(13) and
p_majority even when the test fails. A positive result concerns this enumerated
same-host grid's **median** practical endpoints. The primary geometric-mean
bootstrap and all its original correctness/latency/coverage checks remain
separately authoritative. This does not establish strictly zero CPU overhead.
