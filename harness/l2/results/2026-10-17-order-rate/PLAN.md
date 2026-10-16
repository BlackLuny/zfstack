# Receiver-order rate: netem-fifo against plain netem — registered before execution, not yet authorized

Follows [Canada attempt 02](../2026-10-17-netem-fifo-canada192-r2/REPORT.md). Its gate failed on one
receiver overtake: UDP 8836 was overtaken by 8837. Both were sent about 2 ms apart, left eth0 in
order, and 8836 then took 2.64 ms from egress to the socket. The three plain-netem controls showed
none.

Pooling every committed-code run that checked receiver order on a physical host gives:

- netem-fifo: 2 of 10 full and cancel runs had any overtake. The two were the full runs on remote23
  and in Canada attempt 02; every other full or cancel run on remote23, OVH (two gates) and Canada
  had none;
- plain netem: 0 of 6 runs.

On physical hosts, a one-sided exact test cannot separate the two (p ≈ 0.19 even counting all 12
plain-netem runs, emulated ones included). The emulated runs (netem-fifo 4 of 8, plain netem 0 of 6)
suggest a difference, but emulation has frequent pauses. The per-run `/proc/stat` and
`/proc/softirqs` deltas of attempt 02 do not separate the full run from the controls: softirq time
was 0.63% against 0.09–0.48%, and NET_RX counts were similar.

The open question this plan answers: **does netem-fifo make receiver overtakes more frequent than
plain netem under the same load on the same physical host?**

- If yes, the next step is a component ablation of the mirror, the capture rings and the injector
  process.
- If not, and plain netem also overtakes, the overtake is shared by netem on veth. Only then can
  maintainers consider re-registering where the order obligation applies; that would be a new
  pre-registration, not a regrade of any earlier run.

This is not a qualification run. C16 case v3, strict receiver order in the self-test, both 20 ms
limits, the workload and known-BLOCKED are unchanged. There is no gate, build or formal C16 here.
**It needs the user's explicit authorization before it runs.**

## Frozen design

- **Driver:** [`order_rate.py`](order_rate.py), run from a clean checkout of the commit containing
  this plan. It refuses to start if `harness/l2/*.py`, `harness/l2/*.sh`, `qa/isolate-linux.sh` or
  the control script differ from the attempt 02 gate commit `ca9b597`. Test files are excluded from
  that check.
- **Arms:**
  - **F:** `fifo_selftest.py --kind netem-fifo --mode full`, the gate's full self-test.
  - **P:** `netem_order_control.py`, the gate's plain-netem control, with the same veth pair, sender,
    receiver and load.
- **Order:** 30 runs per arm, strictly alternating F, P, F, P, … (60 runs, about 45–50 minutes).
- **Execution:** every run executes once inside `qa/isolate-linux.sh`, under the public QA lease held
  for the whole sequence. Per-run `/proc/stat` and `/proc/softirqs` are saved before and after.
- **Stopping:** there is no early stop and no peeking. A failing, timed-out or invalid run is
  retained and not replaced. Only loss of the lease stops the sequence.
- **No other changes:** no tracing, affinity, IRQ, priority, power or service changes beyond the
  host's existing, authorized background-service procedure.
- **Valid run:** the receiver has all 18475 datagrams exactly once. Runs that drop datagrams, for
  example plain netem's historical delete-race loss, are reported but excluded from the endpoint.
- **Endpoint:** whether a valid run has any overtaken UDP datagram.
- **Test:** one-sided exact Fisher test for F > P, at α = 0.05. Each arm's rate is reported with a
  95% Clopper–Pearson interval.
  - **F_HIGHER:** p ≤ 0.05.
  - **NOT_SHOWN_SHARED:** p > 0.05 and at least one plain-netem run overtook.
  - **NOT_SHOWN:** p > 0.05 and plain netem never overtook. The interval bounds are reported.
  - **INCONCLUSIVE:** an arm has fewer than 25 valid runs.
- **Power:** with 30 runs per arm, 6 overtaking F runs against 0 P runs gives p ≈ 0.012, and 4
  against 0 gives about 0.056. A true F rate near the observed 20% is likely, but not certain, to be
  detected.

## Host and commands

The proposed host is the Canadian bare-metal host, `root@192.99.148.123:24946`, where attempt 02
reproduced the overtake. Use the same authorized procedure as attempt 02: stop `rc-worker.service`
and `zfc-rig-watch.service` before the sequence, restore them immediately afterwards, and verify the
restoration. Use a fresh task directory and the domain `zfstack-qa-baremetal-192.99.148.123-24946`.
Another host may be used only if the user names it, with its own recorded boundaries. Bundle an
explicit local branch at this plan's commit.

```bash
cd $T/source && PYTHONDONTWRITEBYTECODE=1 python3 harness/l2/results/2026-10-17-order-rate/order_rate.py \
  --expected-sha <full SHA of this plan's commit> --output $T/order-rate \
  --domain zfstack-qa-baremetal-192.99.148.123-24946 --run-id order-rate-$(date -u +%Y%m%d)
```

Bring back `plan.json`, `execution.json`, `summary.json`, every run's summary JSON and log, and each
run directory's receiver, sender, ledger and report files, with hashes. Keep host restoration
evidence and report every failure as it happened.

## Local smoke

[`smoke-local/`](smoke-local/) holds one round under the repository's TCG VM: one run per arm,
`--runs 1`, which the driver records as a smoke run. It is not this measurement. Both runs exited 0
with all 18475 datagrams and no overtakes, and the verdict was INCONCLUSIVE, as designed for too
few runs.
