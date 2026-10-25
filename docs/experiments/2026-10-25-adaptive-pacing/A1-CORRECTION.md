# Prospective A1 correction: excitation and demand observability

Recorded during the unchanged A0 pilot, before inspecting any comparative
CPU/throughput/latency effects. A0 source `7e6b038`, executable
`9801eb32a4d23b5f75f4f87d2e4d634b818bc9f494faff9eee17b5e16c3fc65d`
and all twelve original attempts remain intact. Do not stop an active child,
rebuild or test until the pilot finishes. The two missing generated arrivals
in A0 dynamic row 0006 remain genuine failures; this change does not relax the
20-ms generator gate or replace that run.

## A control-law counterexample, independent of measurement noise

Suppose a saturated sender is paced at R, its ACK delivery follows R, and the
physical capacity rises so queue delay becomes zero. With no independently
visible delivery burst, A0 raises R by 2% in three windows and 8% in the fourth:

```
R_(k+4) = R_k * 1.02^3 * 1.08 = 1.14610464 * R_k.
```

At 500-ms base RTT each control window is at least one second. In ten windows
the maximum such increase is `1.02^8 * 1.08^2 < 1.37`, insufficient to move
from a 20-Mbps cap to even 80 Mbps after a fivefold capacity increase. A0 thus
has a valid feedback trace that cannot meet the frozen ten-second recovery
guard. This is not a finding that every physical run must follow that trace.

Also, actual output below 75% of target is ambiguous: the application may be
idle, or TCP may already have reacted to a lower physical capacity. An output-
only demand gate cannot distinguish these. Existing queued TX/blocked-writer
state and receiver window provide an additional local demand signal.

## One corrected candidate, unchanged acceptance rules

After all twelve A0 pilot runs finish, make and freeze A1 once:

- ACK feedback also carries whether unsent data or a blocked writer exists,
  with receive window at least four MSS. This is queued sender demand, not a
  link-capacity oracle. A window is demand-qualified if emitted bytes reach
  the existing 75% threshold OR >=50% of ACKed IP-byte weight reports this
  queued demand. App-idle or receiver-blocked delivery alone does not qualify.
- In uncongested demand-qualified windows use an elastic increase:
  `gain_percent = 101 + floor(34*(target_queue-queue)/target_queue)`, bounded
  101..135; keep max(gain*R, 1.01*delivery). Near-zero queue excites the path
  promptly, and the increase becomes small near the queue target.
- Preserve A0 learning, high-queue reduction, substantial capacity-drop rule,
  independent every-fourth-window probe, two-RTT windows, hold, wire charging,
  idle reset and single-peer fast path. Do not introduce a fixed capacity,
  sum per-connection bandwidth maxima, or add periodic connection scans.

Run a separate complete twelve-attempt A1 pilot, keeping A0 separate, then
the unchanged 312-attempt confirmation for A1. No further candidate, adaptive
sample extension, changed gate, or replacement of invalid cells follows.
All primary/bandwidth/heavy/disabled/peak/dynamic/demand/A-A cases and margins
in PROTOCOL.md remain. A1 may still fail; no universal stability or capacity
identification theorem is implied by the elastic increase. This correction
repairs a response/observation limitation before the confirmation freeze,
not an inference that A0's observed effects were favorable or unfavorable.

A1's arrival ledger also records each stream's monotonic origin relative to
the useful-work window. This diagnostic field aligns phase boundaries exactly
with link/demand changes; it changes no scheduled arrival, timeout or payload.
