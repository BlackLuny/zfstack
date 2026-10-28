# Concurrent upload guard, declared before its first attempt

The frozen V3 native-splice RTT/confirmation/peak/A-A sequence continues
unchanged. After it finishes, use the same V3 executable for a separate
bidirectional guard: an additional peer uploads continuously while the existing
200-connection download peer and three fixed-arrival interactive peers run.
The fixture classifies this additional source address as peer 5 on iface 0.

First run one CUBIC/20-ms/four-download pair to validate the external uploader.
Then run two balanced adjacent pairs for each CUBIC/BBR and 1/20/500-ms cell,
24 attempts total. Reuse the selected off/99% settings, 100-Mbps/250-KB FIFO
in both directions, 16-MiB zfstack receive/in-flight ceilings, warmup
max(8 s, 40 RTT), 20-s window. The Linux download clients auto-tune receive
buffers with `tcp_rmem` maximum 32 MiB. The external uploader requests a
16-MiB send buffer and records the kernel's actual buffer and congestion control;
this requested value is not a claim about the negotiated receive window.
No new rate/batch/hold selection is permitted from these results.

The uploader is a separate Python process in the owned container. It waits for
the first heavy download header, so the idle interactive baseline remains idle;
its packet socket closes before the measured interval. It sends the same
modulo-251 payload and existing upload protocol through the production Tokio
driver and real splice upstream. Retain its TCP-acknowledged byte observations,
process CPU, start/end times and termination reason. The existing upstream
application validates all received payload; cancellation at benchmark shutdown
does not establish delivery of every outstanding upload byte.

Require complete interactive arrivals, production/fixture teardown, positive
upload application delivery, uploader lifetime across the loaded interval,
acknowledged progress in its final (measure minus one second) observed suffix and
all download flows observed. Preserve any failed smoke or guard. Report upload
delivery and gaps as additional observations, not a packet scheduling guarantee.

The benchmark's effective-byte denominator counts downloads and does not count
this external uploader. Therefore **do not report CPU/GB or a combined-work CPU
improvement from this guard**. CPU/second for the driver and benchmark process,
plus the separately measured uploader CPU, remain descriptive. The independently
confirmed download workload supplies the equal-work CPU comparison. These two
pairs per cell are regression coverage, not a new confidence interval.

## Fixture correction before the full mixed cohort

The first two smoke attempts did not generate upload traffic and are invalid.
Their protocol-specific AF_PACKET tap missed the outgoing heavy request header,
then reported `ENETDOWN` when the otherwise completed benchmark removed its
TUN. Retain both attempts and their frozen source; they are not fairness/CPU
evidence. Register the setup observer with `ETH_P_ALL`, wait for the interface
UP flag, and run another two-attempt smoke in `mixed-v2-smoke`. Only after that
smoke passes run the unchanged 24-attempt mixed plan. No production Rust or
selected scheduling parameter changes accompany this fixture correction.
