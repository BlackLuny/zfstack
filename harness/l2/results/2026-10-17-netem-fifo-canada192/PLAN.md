# Canada final attempt — registered before measurements

User explicitly requested temporarily stopping the Canadian host's remote compile worker
and zfc-rig tasks, then one final test. Target verified from existing deployment documentation
and SSH: root@192.99.148.123:24946 (ns512147), bare-metal Xeon E5-1630 v4, 4C/8T,
62 GiB RAM, Debian kernel 6.1.0-41-amd64 / 6.1.158, 286 GiB disk available.

## Fixed sequence and stop rules

1. Record the original rc-worker.service and zfc-rig-watch.service states and owned rig-watch
   scopes. Stop those two active services and their current rig-watch scopes using systemd;
   retain the interrupted rig outputs and allow their normal cleanup. Do not touch product
   workers, proxy services, other containers or host networking. Restore the originally active
   services on every success/failure exit; record restoration and service/namespace state.
2. New task directory: /root/zfstack-qa-pr75-20261017-canada192-01. Transfer an explicit-branch
   git bundle pinned to the commit containing this plan. Runtime L2/QA code and policy remain
   identical to cc1091a / the tested 604a618 baseline; no workload, case or limit changes.
3. On that clean commit run remote_selftests.py exactly once under the public lease
   /var/lock/zfstack-qa, domain zfstack-qa-baremetal-192.99.148.123-24946. Its frozen order is
   capabilities, full, cancel, overflow; only receiver overtakes trigger its three registered
   plain-netem controls. All network changes stay inside qa/isolate-linux.sh. No tracing,
   affinity changes or real-time priorities. Recompute raw ledgers and receiver ordering.
4. Gate FAIL or prerequisite failure stops this final attempt. No repeat, no substitute run,
   no parameter tuning, and no formal invocation. Preserve every failure and restore services.
5. Only gate PASS permits one frozen native head-head worker/fakectl build, Rust 1.98.1,
   fast-release, mmsg/jemalloc, jobs=2, the same other four repository SHAs:
   zfc 975c677d2920c86a14dcd1e08ae743abf7b7be1c;
   private_tun ca922c518992426bfaea4a2d60abf653c5c694de;
   shadow-tls-tokio 787b8e261a919689f90d9d89ee9362b344da56df;
   rig 8c9e737ed505306b97c9f2971635c75808fbc04d.
   Use task-local tools only, no system package installation/upgrades. Record artifact hashes,
   make the frozen QA plan, pre-register one formal invocation and settle 5 seconds. The sole
   formal run-plan baseline/fault invocation uses the runner's existing stop-on-baseline-failure
   behavior. There is no extra pilot baseline. Build failure is retained and is not retried.
6. Evaluate on the remote, retrieve safe evidence (no WireGuard keys/configuration), independently
   recompute locally, publish report and PR comment. Keep full failed artifacts on the remote.

## Boundaries

The user's authorization is a narrow exception to previous no-service-stop guidance, for
rc-worker and zfc-rig only. Their original enabled state/configuration is preserved. Existing
other services remain running; this is not a claim of zero background activity or performance
certification. Record competing work and refuse to begin measurement with active compilers,
a live rig-watch scope, an occupied public QA lease, or substantial competing workload.
The original 500 ms / 100000 packets / 8-16-8 seconds / 250 ms guard / 3 second deadline /
250 ms service contract, strict order and both 20 ms limits are unchanged. C16 stays case v3
netem-fifo, known-BLOCKED remains even on PASS, PR stays draft, #59 stays open. This is the
last authorized qualification attempt in this session, not an invitation to repeat until PASS.
