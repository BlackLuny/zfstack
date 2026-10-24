# Review follow-up, declared after V3 WAN and before these attempts

All 96 V3 native-splice WAN attempts were valid. All 12 loaded cells improved
mean/P99 latency, candidate bulk-flow inactivity was zero, and 79,200 planned
light requests completed. However the two-pair single-flow estimates were
-4.57% for BBR/500 ms and -3.62% for CUBIC/100 ms; every single-flow pacing
counter was zero. The latter CUBIC cell included one 94.24-Mbps control and
three 86.87–87.41-Mbps observations. Neither bypass nor a noisy-looking pair
alone establishes a physical throughput guard. Retain these original results.

Code review also identified a separate extreme-capacity boundary: when one
MTU costs more than the one-second contention hold, competing input after the
hold expires can currently reset a still-future paused deadline. After the
fixed V3 sequence and its external-upload guard finish, correct this reset
condition and add a regression. Also bypass the pacer call itself while it is
inactive and a single peer is ready, retaining the original packet/time path
and avoiding unnecessary inactive-state writes. Specialize paced/unpaced
service so disabled and single-peer sends skip per-packet aggregate accounting.
Do not change the selected
99% rate, batch, catch-up credit, hold duration, queueing or memory policy.
Add a native two-peer real-splice bidirectional/half-close/teardown regression.

Run all required suites before building/freeze a new V4 binary. Keep V3 and its
exact source snapshots: its matrix/confirmation are measurements of V3, not
silently relabelled V4 measurements. Then execute these fixed, serial guards:

1. **48 longer single-flow WAN attempts:** CUBIC/100 ms and BBR/500 ms,
   each 12 balanced adjacent off/on pairs; same FIFO and V4 binary in both
   arms. Warmup max(8 s, 40 RTT), **40-s** measured windows. Retain V3's
   original two-pair estimates separately. Report paired bootstrap intervals;
   require useful-throughput lower bound >= -3% to qualify these cells.
2. **48 cross-binary few-download peak attempts:** the frozen old final
   binary against configured V4, 12 balanced pairs each for one/two downloads, same actual local
   TUN/Tokio/splice path and settings as the original 60-attempt peak cohort.
   Require useful-throughput lower bound >= -3%. V3 upload/mixed peak cases remain
   V3 measurements, with no claimed additional repetition.
3. **48 independent loaded splice confirmation attempts:** CUBIC/BBR,
   20 ms/200 downloads, 12 balanced pairs each with the original 8-s warmup/20-s
   window. Keep them distinct from V3's independent 12-pair estimates;
   use V4's own whole-pair intervals for final-code CPU/latency claims. Validate
   request/budget/teardown and finite per-flow progress; report every CPU scope.

If a guard remains inconclusive or fails, do not discard the early samples,
substitute a larger FIFO, or claim that all requirements are established.
No further rate-factor search or optional stopping is planned. These are
targeted review/physical guards, not a second exhaustive RTT experiment.

## Observation audit clarification, still before the first V4 attempt

V3's 48-attempt independent confirmation finished with 46 strict-valid runs.
One BBR control missed a planned request after 48.629-ms generator lag; another
completed every request but one response took 3.089 s, beyond the 3-s service
deadline. All candidate runs passed. Preserve the original strict verdicts and
do not promote V3 BBR's whole-cohort intervals. CUBIC's 12 pairs were valid.

For V4 distinguish **complete, trustworthy observations** from **meeting the
service SLO**. An actual late response that was fully delivered and recorded
has an observed latency: include that full latency, and keep its SLO failure.
Never delete it, truncate it to 3 s, or change its original strict-valid flag.
Only this exact strict-deadline error can receive the additional observation
verdict, after independently rechecking every response's time ordering and the
unchanged 20-ms generator timing gate. Payload, identity, loss of observations,
missing requests, generator misses or other faults still block whole-cohort
mean confirmation; no invalid pair is silently removed.

Final V4 mean/CPU confidence claims require 12 completely observed pairs per
CC; candidate service deadlines and all planned arrivals must pass. A fully
observed control SLO failure may demonstrate a worse service result and does
not erase its actual latency. Report strict-valid count, observation count,
both arms' deadline/missing counts, and every original error separately.

The completed V3 cross-binary single-download guard passed (-2.31% to +15.98%
interval, median +3.30%). Its two-download guard was inconclusive (median
-4.08%, interval -11.14% to +6.05%), as were some CPU-bound native guards.
Therefore V4's prospective cross-binary follow-up includes **two downloads**,
12 pairs as well as the already planned 12 single-download pairs. This extension
is declared before V4 is built or measured; retain all V3 results separately.

For V4's loaded confirmation, reduce source/observer scheduling interference
by pinning the light-request reader/writer threads to CPU 4. Keep the stack,
source server, bridge, bulk clients and link threads on CPUs 0–3; the cgroup's
total CPU quota remains four CPUs. The owned container permits CPUs 0–4 only
after all V3 measurements finish. An opt-in fixture control `ZFBENCH_RR_CPU=4`
sets the reader affinity before spawning its writer; every ledger records the
requested and actual reader/writer CPUs. Audit all six streams against the
frozen command; a failure to pin is an explicit failed attempt. This fixture
control does not affect production code or the actual client-proxy peak path.
Do not pool V3's colocated-client estimates with V4's isolated-observer data.
The 20-ms source-lag gate, 3-s service target, arrival count and all payload
checks remain unchanged; shared-host noise is quantified, not assumed absent.

Execution order is **loaded confirmation, longer WAN guards, cross-binary
peak guards**. This order is declared before the first V4 attempt so primary
latency/CPU failures are found before spending time on the longer single-flow
guards. Sample counts, within-cell balanced orders and endpoints are unchanged.
