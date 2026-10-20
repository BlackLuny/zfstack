# Canada final attempt: gate PASS; build UNBUILDABLE; formal NOT_RUN

The final authorized attempt ran on root@192.99.148.123:24946 (ns512147),
bare-metal Xeon E5-1630 v4, Debian kernel 6.1.0-41-amd64. The pre-registration
was committed and pushed as **6ab4c297638000c3b5470274d6fd72ce1fb26252** before
measurements; that exact clean commit was transferred and tested. Its bundle
SHA-256 was dc887dac2d6d71bf5e1dc632027fd35c9d769608644cbb6c164c28f3e3466311.
Later evidence-only commits are not claimed as measured.

## Authorized maintenance window and restoration

Recorded and stopped rc-worker.service, zfc-rig-watch.service, and the active
rig-watch-08c885220c23baa7.scope. The old rig's output was retained; its interrupted
cases were not replayed. Existing product workers, proxies and containers were
left running. There were no cargo/rustc processes or registered rig namespaces
after stopping. A ten-second /proc CPU sample showed the busiest remaining
process at 0.9% of one CPU; this does not establish zero background activity or
performance certification.

Both services were restored automatically after the build failure. Independent
readback confirms **active/running, enabled**, matching their original states.
The owned backup restore timer and both task units are inactive, and the public
QA lease directory is empty. Newly resumed rig namespaces belong to the restored
watcher; all three self-tests reported cleanup PASS. No system packages, host
network settings, IRQ/CPU affinity, priority or tracing settings were changed.
Rust, CMake and pinned Python dependencies were installed only in the task directory.

## Exactly one gate

`remote_selftests.py` ran capabilities → full → cancel → overflow once each,
inside `qa/isolate-linux.sh`, holding the public QA lease. The capability probe
and all modes passed. No receiver had overtakes, so no plain-netem controls ran.

| Mode | Result | UDP received / sent | Overtakes / duplicates | Ledger |
| --- | --- | --- | --- | --- |
| full | PASS | 18475 / 18475 | 0 / 0 | 51474 frames paired, zero lost/extra, oracle accepts |
| cancel | negative PASS | 18475 / 18475 | 0 / 0 | 61613 frames paired, zero lost/extra, oracle rejects cancellation as required |
| overflow | negative PASS | 13681 / 18475 | 0 / 0 | 4814 lost frames across protocols; 4794 UDP losses exactly accounted for |

All modes had zero sender EAGAIN, receiver socket errors and link drop deltas;
qdisc restoration and cleanup passed. Full and cancel TCP byte counts matched.
Full loaded-phase UDP latency was 500.046–500.323 ms, and recovery maximum was
0.310 ms. These are receiver latency measurements, not a replacement definition
of the injector release-lateness bound. The raw-ledger oracle passed the original
20 ms bound. The independent local analysis recomputed **every self-test check**,
raw frame pairing, UDP order and expected negative oracle rejection, and matched
the retained remote results.

## Build precondition failure, not a measured traffic failure

The single authorized frozen build used the five registered repository SHAs,
Rust 1.98.1, fast-release, mmsg/jemalloc and jobs=2. Tool setup, frozen input
verification, rustc/cargo identification and Cargo source resolution succeeded.

The builder stopped **before invoking worker compilation**:

`invalid JSON: command-3.log: Expecting value: line 1 column 2 (char 1)`

The failing step was `cargo metadata --locked --format-version 1`. Its process
returned **0**, execution PASS and cleanup PASS. The fresh task-local Cargo cache
caused download progress and transient HTTP/2 retry warnings on stderr. The QA
process helper merges stderr into stdout, while the cross-repository builder
parses the entire combined log as JSON. There are 1352 non-JSON lines before the
valid metadata payload on line 1353 (1373 packages). Offline diagnostic extraction
confirms that ZFStack resolved to the correct frozen local source. The original
log, build verdict and error are unchanged; extraction is not used to regrade it.
Cargo's own network retries were part of the one metadata process, not repeated
build, gate or formal invocations.

This is a deterministic **QA build-output handling defect**, not evidence of
Canadian-host scheduling failure, injector lateness or packet reordering. There
is no new worker artifact and **zero formal C16 invocations**. Per the registered
stop rule, the build was not retried, no extra gate/traffic experiment ran, and
services were restored. No formal PASS/FAIL or performance conclusion is claimed.

The concrete code fix is to capture structured-command stdout and stderr in
separate retained files, parse stdout only, and apply output budgets and process
containment to both. Regression coverage should include cold-cache progress and
stderr warnings with otherwise valid metadata. Merely stripping leading lines or
silencing output is not a robust parser fix. **This fix is identified, not applied
in this evidence-only update.** Any subsequent qualification requires a separately
authorized, pre-registered attempt; this final attempt is closed.

## Evidence and unchanged qualification status

- Remote task: `/root/zfstack-qa-pr75-20261017-canada192-01`.
- `collected-artifacts.tar.gz`: 85 files, 6042324 bytes; SHA-256
  `6e7d404d77cc00ed5a5aee754ee4eb14fb81cf8d74d6b5a89d671fd50fabd9eb`.
- `collected-artifacts.index.json`: per-file sizes and SHA-256 hashes.
- `gate-recomputed.json`, `build-diagnosis.json`, `post-restoration-check.json`.
- `verify-evidence.py` verifies/extracts the archive and performs offline analyses.
  Run it with Python 3 from this checkout; it launches no remote commands or traffic.
- Full failed build logs and raw gate evidence are retained. No WireGuard keys or
  runtime configurations are collected.

No qualification/runtime code or policy was changed. Existing failures remain
retained. Strict order, both 20 ms limits and all load/phase parameters remain
unchanged. **C16 stays case v3 / netem-fifo, known-BLOCKED remains, PR #75 stays
draft and #59 stays open.** The successful gate is useful evidence, but does not
substitute for the held-out formal run that was never reached.
