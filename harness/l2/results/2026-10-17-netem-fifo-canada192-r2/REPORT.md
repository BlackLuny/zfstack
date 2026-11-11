# Canada attempt 02: gate FAIL on one UDP overtake; build and formal NOT_RUN

The user explicitly authorized this new attempt after the stderr fixes 2c3682c
and 4cf892e were reviewed. Plan commit **ca9b597621614a371826866f515f6f5f77da4103**
was pushed before measurements and was the exact clean checkout tested. The
previous Canadian gate PASS and build UNBUILDABLE remain unchanged; neither was
reused or regraded. The stderr fix was present, but the build stage was not reached.

Target: root@192.99.148.123:24946, ns512147, bare-metal Xeon E5-1630 v4 (4C/8T),
Debian 6.1.0-41-amd64. Task: `/root/zfstack-qa-pr75-20261017-canada192-02`.

## Execution and stop rule

Temporarily stopped rc-worker.service, zfc-rig-watch.service and its active rig
scope under the user's authorization. No cargo/rustc or rig namespace remained;
the public QA lease was empty. A ten-second CPU sample showed the highest process
at 1.79% of one CPU (writeback), and the busiest product worker at 0.8%. Other
services remained active: no zero-background or performance-certification claim.

One remote_selftests invocation ran, under the public lease and isolated network
wrapper. All modes ran once. Full receiver reordering triggered exactly the three
pre-registered plain-netem controls; these were not replacement qualification runs.

| Step | Exit | UDP received / sent | Overtakes | Missing UDP | Result |
| --- | --- | --- | --- | --- | --- |
| capability probe | 0 | — | — | — | PASS |
| full | 1 | 18475 / 18475 | 1 | 0 | FAIL: strict receiver order only |
| cancel | 0 | 18475 / 18475 | 0 | 0 | expected negative PASS |
| overflow | 0 | 13560 / 18475 | 0 | 4915 | expected negative PASS |
| plain-netem 1 | 0 | 18475 / 18475 | 0 | 0 | retained control |
| plain-netem 2 | 0 | 18475 / 18475 | 0 | 0 | retained control |
| plain-netem 3 | 0 | 18475 / 18475 | 0 | 0 | retained control |

Full paired all 52565 captured frames, with no lost or extra frames. Its raw-ledger
oracle accepted release timing/order, including the unchanged 20 ms bound; 12 of
13 checks passed. TCP bytes matched, UDP payloads were intact, and there was no
sender EAGAIN, receiver socket error or link drop. The separate strict socket-order
check correctly rejected the run. This distinction is intentional: a passing
queue ledger alone is not sufficient qualification evidence.

Cancel paired all 66569 frames without loss and the oracle rejected cancellation
as required. Overflow lost 5012 total frames across protocols, including exactly
4915 UDP frames; receiver loss matched. All three modes restored qdisc and reported
cleanup PASS. The controls' queue removals completed 243.627, 218.342 and 237.473 ms
before recovery, respectively, so this batch did not show the old late-delete race.
These three controls alone do not establish a statistical implementation ranking.

**Gate FAIL stops the attempt.** No native build, pilot baseline or formal C16
invocation occurred, and no measurement was repeated. Both services were restored
immediately to active/running and enabled, matching their original state. The owned
restore timer and gate unit are inactive, the QA lease is released, and build/formal
directories do not exist. Newly resumed rig namespaces belong to the restored watch
service. Interrupted rig output was retained and not manually replayed.

## Offline localization of the single overtake

Raw receiver records show UDP **8836 overtaken by 8837**, at loaded-phase +4.672145 s.
Their send timestamps are 1.920485 ms apart, consistent with the approximately 2 ms
schedule rather than the earlier VPS's long pause/catch-up burst.

The driver's optional old timing mapper declined to attribute packets because it
identifies UDP by 1074-byte length alone: the full ledger also contains ten TCP
frames of that length (cancel has three). Its refusal and original output are
retained. This does not affect the strict receiver-order or raw-ledger verdict.

`analyze-order.py` is a separate offline diagnostic. It selects 1074-byte frames
with a recorded 42-byte IPv4/UDP header, requires exactly 18475 frames from one flow,
and verifies a complete one-to-one mapping into the sender's timestamp intervals.
Every admission lies strictly after that packet's send timestamp and before the
next, even allowing the full observed 760 ns clock-offset spread in either direction.
No captured payload sequence is stored in the ledger; this mapping limitation is
explicit, and no mapping assertion is used to change qualification.

With that checked mapping, all UDP egress timestamps are in send order:

| Quantity | UDP 8836 | UDP 8837 |
| --- | --- | --- |
| Residence from mirror to egress | 500.007842 ms | 500.009262 ms |
| Egress timestamp to socket receive timestamp | 2.638252 ms | 0.085685 ms |

8837's egress is 1.906176 ms later than 8836's, but its socket receive is 0.646391 ms
earlier. During 8836's egress-to-receive interval, the sender also emitted UDP 9087;
therefore a whole-host stop spanning that entire interval cannot explain it. The
largest injector clock-sample interval was 101.169 ms, against nominal 100 ms.

The useful next investigation boundary is **between recorded eth0 egress and socket
receipt**. This interval includes kernel delivery and receiver scheduling. No trace
or CPU mapping was collected, so this evidence does not identify a specific CPU,
IRQ, kernel function or extra injector component as the cause. It does not justify
an IRQ/affinity change, weakening strict order, raising 20 ms, or another gate retry.
A long VM pause is not necessary for this physical-host reproduction; CPU-local
scheduling or delivery effects remain possible. The injector/self-test/driver and
namespace wrapper are unchanged from attempt 01; the stderr fix did not modify them.

## Retained evidence and verification

- `collected-artifacts.tar.gz`: 67 files, 6447465 bytes; SHA-256
  `4a6388ca8cb3980215b3019a02aa88d5cf07daa7cd533e3c7276137f653cba6e`.
- `collected-artifacts.index.json`: each retained file's size/hash.
- `gate-recomputed.json` and `gate-verification.json`: every self-test check, frame
  pairing, receiver order and the conditional control sequence independently
  recomputed locally, matching the remote **FAIL**.
- `order-analysis.json`: raw timestamps and the diagnostic mapping checks.
- `post-restoration-check.json`: restored services, inactive task/timer, empty lease,
  and absence of build/formal output.
- `verify-evidence.py`: verifies/extracts the archive and runs only local analyses.
  It launches no remote commands or traffic. No WireGuard keys/runtime configs
  are in the collection. Full raw evidence remains in the remote task directory.

No runtime or qualification policy was changed in this evidence update. All earlier
failures remain retained. Strict order, both 20 ms limits, workload/phase parameters,
case v3 / netem-fifo and **known-BLOCKED** remain unchanged. PR #75 remains draft;
#59 remains open. A further diagnostic or qualification attempt is not authorized
by this completed run.
