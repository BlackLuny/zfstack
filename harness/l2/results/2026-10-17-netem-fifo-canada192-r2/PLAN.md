# Canada attempt 02 — authorized after the stderr fix; registered before measurements

The user explicitly authorized a new Canadian-host attempt after review of commits
2c3682c and 4cf892e. The prior attempt is closed: its gate PASS and build UNBUILDABLE
remain unchanged and do not qualify this new SHA. This plan's commit is the tested
source identity, containing those fixes with no further qualification/runtime changes.

Target: root@192.99.148.123:24946, ns512147, bare-metal Xeon E5-1630 v4 (4C/8T),
Debian kernel 6.1.0-41-amd64. Fresh task directory:
/root/zfstack-qa-pr75-20261017-canada192-02.

## Frozen sequence and stop rules

1. Transfer an explicit-branch git bundle for this plan's commit; verify its hash,
   full commit and clean checkout. Record read-only host/service/tool state.
2. Record and temporarily stop rc-worker.service, zfc-rig-watch.service and their
   active rig-watch scopes. Preserve interrupted output and original enabled states.
   Confirm no compiler or live rig task remains and sample background CPU for 10 s.
   Do not start measurements with an occupied QA lease or substantial competing work.
   Other existing services remain running: no zero-background/performance claim.
3. Run remote_selftests.py **once**, expected SHA = this plan's commit, public lease
   /var/lock/zfstack-qa, domain zfstack-qa-baremetal-192.99.148.123-24946,
   run ID canada-r2-gate-20261017. Frozen sequence: capabilities, full, cancel,
   overflow, plus exactly three plain-netem controls only if any receiver overtakes.
   All privileged traffic runs inside qa/isolate-linux.sh. Gate FAIL or prerequisite
   failure stops this attempt, with no repeat or replacement measurement.
4. Gate PASS alone permits one native frozen head-head worker/fakectl build:
   ZFStack = this plan's commit;
   ZFC 975c677d2920c86a14dcd1e08ae743abf7b7be1c;
   private_tun ca922c518992426bfaea4a2d60abf653c5c694de;
   shadow-tls-tokio 787b8e261a919689f90d9d89ee9362b344da56df;
   rig 8c9e737ed505306b97c9f2971635c75808fbc04d.
   Rust 1.98.1, fast-release, default features plus mmsg/jemalloc, jobs=2.
   Fresh task-local Cargo cache; reuse only hash-verified tool input archives from
   attempt 01, install under the new task directory. No system package operations.
   Keep parsed stdout and command-N.stderr.log in the evidence archive. Build
   failure is retained and stops this attempt without a retry.
5. After build, verify stopped services, absence of competing compilers/rig scopes,
   source identity and worker identity; freeze the QA plan and worker/manifest hashes.
   Settle 5 s, then make **one** formal C16 run-plan invocation. No pilot baseline.
   Retain the runner's existing baseline-failure stop behavior. No repeats, parameter
   changes, CPU/IRQ affinity changes, tracing or real-time priority changes.
6. Independently evaluate remote and retrieved evidence. Preserve all failures.
   Restore originally active compile/rig services on every completion/failure exit;
   verify active/enabled state and release of the public QA lease. An owned backup
   restoration timer is insurance only; normal restoration happens immediately.
   Collect allowlisted evidence only, excluding WireGuard keys/runtime configs,
   write a report and append the outcome to PR #75.

## Invariants

Strict UDP order, both 20 ms limits, 500 ms delay, 100000 packets, 8/16/8 s phases,
250 ms guard, 3 s deadline, 250 ms service contract and business load are unchanged.
C16 remains case v3 / netem-fifo. known-BLOCKED remains even on PASS, PR stays draft,
and #59 stays open pending maintainer review. QA_TRUSTED_REF is not modified.
This is one newly authorized attempt, not permission to repeat until PASS.
