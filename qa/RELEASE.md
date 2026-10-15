# Release qualification and baseline review

The release entry point is **QA release evidence** (`qa-release.yml`). It runs the
native portable suite on Linux x86_64, Linux aarch64, macOS aarch64 and Windows
x86_64, then combines those raw results with explicitly selected nightly, safety
and performance runs. It does not deploy a worker, publish a release, write a PR,
merge a change, or activate a baseline.

## Establish the trusted reporter first

Merge the QA control plane through normal code review. Protect `qa/`, its policy
and schemas, `.github/`, test harnesses and lockfiles through CODEOWNERS **and** a
repository ruleset requiring the relevant reviews. CODEOWNERS alone does not
make reviews mandatory. Configure `QA_TRUSTED_REF` to a reviewed full 40-character
commit SHA containing the required reporter version and approved policy. Update
that SHA deliberately as the reporter evolves. A branch name or moving tag is
not a trusted immutable reference.

The initial PR can execute candidate tests before a reviewed reporter exists,
but its gate remains BLOCKED. Use the reviewed control-plane commit as the
bootstrap reference; do not bypass the gate by accepting a candidate's own
summary or deleting required cases. A reporter version missing `qa/release.py`
likewise blocks release qualification.

Require the exact **`qa/gate`** check in branch protection for ordinary PRs and
merge queue candidates. Confirm with a trial PR that a skipped job, missing
artifact and wrong tested SHA all prevent merging. The workflow evaluates the
actual PR merge or merge-group SHA. A manually dispatched native, performance or
release workflow is additional evidence; its green check does not replace the
PR's required check. Preserve the execution/reporter separation and keep
credentials out of candidate execution jobs.

## Collect release evidence for one exact SHA

1. Select a reviewed commit on the protected default branch. Record its full
   tested SHA. The nightly and safety workflows must have completed successfully
   at that exact SHA. Their workflow run IDs are explicit inputs to the release
   workflow, so advancing the branch cannot silently substitute newer results.
2. Provision the designated disposable or resettable integration node and its
   independently built worker through the existing trusted process. Run the
   nightly L1/L2 coverage. Missing permission, path evidence, worker identity,
   full configuration, cleanup or a disabled L2 job blocks release. The release
   workflow itself dispatches no candidate onto a self-hosted integration node.
3. Run the performance workflow using the same reviewed tested SHA and a frozen
   experiment. Keep raw ABBA/BAAB rows, the frozen experiment, analysis, calibration
   report and all calibration controls. Choose its `qa-performance` artifact's
   successful run ID.
4. Dispatch **QA release evidence from the protected default branch** with the
   tested SHA, three evidence run IDs, generation, immutable release anchor and
   baseline expiry. Four hosted native jobs execute the trusted portable plan;
   Linux-specific benchmark coverage comes from the Linux/nightly scopes.
5. Review `qa-release-review`. A passing qualification may contain
   `baseline-candidate.json` with `status: REVIEW_REQUIRED`. It is a proposed
   metadata change, never an activated baseline. Independently review it before
   installing it into whatever baseline registry the deployment process uses.

The reporter obtains workflow provenance directly from the GitHub API, checks
repository, protected branch, workflow identity and successful completion, and
checks every raw result's tested SHA and trusted plan hash. It ignores uploaded
`summary.json` and other PASS summaries. It reconstructs portable, nightly,
safety and performance plans, checks actual native OS/architecture and complete
build manifests, re-runs integration evidence validation, and rejects missing,
duplicate, corrupt, failed or inconclusive required evidence.

## Calibrate before enforcement

Default shadow performance is useful for observation but **cannot qualify a
release or promote a baseline**. Enable `mode: enforce` only for an independently
reviewed case/runner/profile/environment configuration with measured calibration
controls. Freeze the primary metric family, product budgets, block schedule,
statistical method and stopping rule before execution. Do not widen a threshold
or switch methods to make the candidate pass.

The reviewed `qa/policy/required.json` contains `release_calibrations`. Initially
it is empty. Register only real measured calibrations, with entries of this form:

```json
{
  "calibration_id": "<reviewed calibration identity>",
  "sha256": "<canonical JSON SHA-256 of calibration.json>",
  "generation": "<reviewed baseline generation>",
  "expires": "<future UTC ISO timestamp>"
}
```

The case's performance policy also binds the exact experiment plan hash and
calibration identity. A registry entry is scoped to one generation and expires;
a baseline candidate cannot outlive its calibration. Hardware, kernel,
compiler, allocator or runner changes require a new generation and appropriate
overlapping A/A measurements. Retain the existing immutable release anchor
across routine baseline promotions so small successive regressions remain
visible. Changing that anchor needs its own review.

Performance evidence must inventory and hash these files:

- `perf-plan.json`, `perf-rows.jsonl`, `perf-analysis.json`;
- `calibration.json` and `calibration-spec.json`;
- every training and held-out trial plan, row file and provenance file named in
  the frozen calibration spec. Use allowlisted names such as
  `calibration/<trial>/perf-plan.json`,
  `calibration/<trial>/perf-rows.jsonl` and
  `calibration/<trial>/calibration-provenance.json`.

The reporter recomputes `calibrate(spec, datasets)` and requires exact equality
with the registered calibration report, then recomputes the candidate analysis.
A fabricated `validated: true`, synthetic rescaling, omitted negative control,
missing row file or copied PASS summary cannot satisfy this path. Synthetic
unit tests exercise rejection and arithmetic; they do not calibrate a node.

## Local entry points

Run the reporter from the reviewed control-plane checkout with the approved
policy. Place downloaded artifacts under this layout; retain artifact nesting
inside nightly/safety/performance directories:

```text
evidence/
  native/qa-native-linux-x86_64/...
  native/qa-native-linux-aarch64/...
  native/qa-native-darwin-aarch64/...
  native/qa-native-windows-x86_64/...
  nightly/...
  safety/...
  performance/...
```

Fetch provenance with a read-only token, rather than importing it from a test
artifact:

```sh
python -m qa.release collect-runs \
  --repository OWNER/REPO \
  --nightly-run NIGHTLY_ID --safety-run SAFETY_ID \
  --performance-run PERFORMANCE_ID --output provenance.json
```

`GITHUB_TOKEN` supplies authentication. The GitHub workflow runs the following
entry point with its trusted native job status and the exact inputs:

```sh
python -m qa.release prepare-baseline \
  --tested-sha FULL_SHA --evidence evidence --policy qa/policy \
  --provenance provenance.json --repository OWNER/REPO \
  --default-branch main --native-status success --generation GENERATION \
  --release-anchor FULL_ANCHOR_SHA --expires FUTURE_UTC_TIMESTAMP \
  --output release-report.json --candidate baseline-candidate.json
```

Use `evaluate` with the same evidence/provenance arguments when no baseline
candidate is requested; omit `--release-anchor`, `--expires` and `--candidate`.
The commands exit 0 for PASS, 1 for observed FAIL and 2 for BLOCKED. Local
provenance and job status are trusted orchestration inputs; the protected GitHub
workflow supplies them directly. A user-edited provenance JSON is not a GitHub
attestation.

## Review dependency changes separately

**QA dependency lock review** runs monthly or manually on the protected default
branch. It updates the chosen Cargo workspace lockfiles, uploads `locks.patch`
and `review.json`, and validates the resulting locked core, adapter, harness,
benchmark, model, fuzz and Python tooling. Inputs select `all`, `root`, `models`
or `fuzz`, plus an optional Cargo package selector. The workflow has read-only
repository permissions and never opens or merges a PR.

Review the patch together with the workflow's final validation status. A patch
artifact alone does not assert that tests passed. Apply approved changes through
the ordinary PR path and required `qa/gate`. Cargo's explicitly pinned git
revisions, Python requirement hashes, toolchain versions and GitHub Action pins
remain separate deliberate updates; this workflow does not silently move those
pins. Dependency changes affecting a calibrated build or runner require the
corresponding calibration/generation review before release enforcement.
