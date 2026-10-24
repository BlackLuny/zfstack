# Native validation extension, before any V3 measurements

V2 native-Tokio smoke (two attempts) and 20-ms pilot (eight attempts) all passed,
including zero charged memory after driver shutdown. CUBIC/BBR paired mean user
latency changes were -55.80%/-70.13%; stack CPU/GB -11.06%/-37.30% and
stack+link CPU/GB -0.24%/-22.62%. CUBIC process CPU/GB rose 3.48% in these two
pairs, so the entire-process CPU question remains open; do not call all CPU
scopes equivalent. All bulk connections progressed. These are pilot estimates.

V3 adds production `TcpStream::splice` from real kernel upstream sockets into
the same userspace stack and FIFO downstream fixture. The upstream serves the
identical benchmark protocol on container loopback. Kernel upstream threads and
the fixture bridge are included in process CPU; stack CPU includes the actual
Tokio driver/relay. Neither the bridge CPU nor source-server CPU can be hidden
when reporting whole-process cost. All upstream workers must finish at teardown.
This is not a remote provider-ingress topology.

One production control-update correction accompanies V3: changing capacity
preserves the active contention hold and reprices outstanding credit/debt.
Changing a configured rate must not reset a paused iface into its bypass path
or mint a new burst. Identical updates remain idempotent. Initial off/on
configuration, and the send algorithm during the preceding pilots, are unchanged.
Add a regression for the update contract before running V3.

After all core/Tokio/bench checks, run a splice smoke and a small 20-ms pilot,
then freeze the selected 99% configuration for independent 1–500-ms validation,
12-pair CUBIC and BBR confirmation, matched few-flow peak guards and A/A controls.
The prior binary must also remain available for cross-binary regression checks.
All comparisons keep the physical FIFO and queue capacity identical within the
pair. Preserve all attempts and separate V1/V2 pilots from final estimates.
