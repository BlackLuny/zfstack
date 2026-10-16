# Order-rate comparison revision — local work only

Implementation commits: `363bf3a` and `c1ab9ff`.
The measured injector/self-test/control, isolation wrapper, process supervisor and lease code remain
byte-identical to `ca9b597`. Runtime guard verified empty. No remote connection, service change,
privileged traffic, new gate, frozen build or formal run was made for this revision.

## Changes

- Freeze 30 independently randomized FP/PF pairs in `allocation.json` before execution. Primary
  analysis is a one-sided paired exact test, with complete-pair results and unpaired Fisher labeled
  descriptive. The fixed seed is not a CLI option; runtime checks the frozen allocation file.
- Keep every invalid run as an unknown outcome. Complete unknown F as clean and unknown P as
  overtaking; only a result significant under that least-favorable completion can show F higher.
  At least 25 valid outcomes per arm and a completed, unaborted schedule are still required.
- Validate the exact 0..18474 sequence set, sample schema, payload, sender/TCP/socket/link evidence,
  treatment and lifecycle. Recompute the F oracle from raw ledger/counters; summary verdict fields
  do not authorize validity. A receiver-order-only full failure remains an observed overtake.
- Use the existing public-lease-aware Linux subreaper runner, save lifecycle records, check paused
  services/compilers/rig scopes at boundaries, and stop with retained evidence on unsafe cleanup or
  environment changes. Unknown cleanup quarantines the domain. No automatic service changes are
  performed by the driver; the external stop/restore procedure remains separately authorized.
- Handle malformed/truncated evidence without accepting partial counts or losing the failure.
  Add offline `recompute.py`, which rereads raw files instead of cached endpoint/validity fields.
- State the limits explicitly: no equivalence claim, no shared-cause inference from P overtaking,
  physical historical p=0.375, and a 0/30 two-sided 95% CP upper bound of 11.57%.

## Verification on committed source

The following checks ran on **c1ab9ffccdf6530f7b17231ae9dcb297bc71ec33**, after committing the code,
using Python 3.12.3 on macOS 26.5.1 arm64 and the exact `qa/requirements.lock` dependencies in a
new task-local venv. Command, source identity, timestamps and exit code are in `validation/*.json`;
complete logs (including initial environment failures) and SHA-256 hashes are retained there.

| Check | Result |
| --- | --- |
| `unittest discover -s harness/l2 -p 'test_*.py' -v` | exit 0; 157 tests, 2 existing Linux-only skips |
| `unittest discover -s qa/tests -p 'test_*.py' -v` | exit 0; 213 tests, 5 existing Linux-only skips |
| `python -m qa audit-coverage` | exit 0; `ok: true`; no inventory changes needed |
| Frozen runtime source guard vs `ca9b597` | no changed files |
| `git diff --check` | clean |

Order-rate tests grew from 5 to 15. They cover wrong-but-equal-size sequence sets, malformed rows,
raw receiver order, the missing-result selection counterexample, exhaustive completion bounds,
order-only failure acceptance, timeout/treatment/cleanup rejection, truncated ledgers, restored
background activity, interruption/quarantine, frozen allocation, and offline re-evaluation that
ignores tampered cached counts. Sequence tests mock the process runner and emit no traffic.
The existing test names/skip conditions remain registered unchanged.

The retained Canada full and control raw files are used only as offline regression fixtures, with
synthetic supervisor lifecycle records where needed. This does not grant a new gate PASS, regrade
Canada attempt 02, or count historical runs toward the future 60-run experiment. Old records and
v1 TCG smoke are untouched. **No v2 privileged end-to-end smoke has been run.** Linux-only skips
mean this local validation cannot claim that native Linux supervision was exercised here.

Initially the system Python lacked `jsonschema`: QA exited 1 (67 import errors), and coverage audit
exited 1. Those logs are retained separately. Installing the pinned QA dependencies in a new venv
resolved the environment issue; no repository dependency/version change was made. Expected
`QA BLOCKED` / `Release BLOCKED` lines inside successful test logs are negative-fixture output,
not results of a remote qualification attempt.

## Next action

Publish the revised plan and obtain authorization for one approximately 50-minute Canadian-host
window with temporary stop and verified restoration of the compile worker and rig services.
Then execute the new plan once with no replacement measurements. Known-BLOCKED, case v3,
strict receiver order and both 20 ms limits remain unchanged; PR #75 stays draft and #59 open.
