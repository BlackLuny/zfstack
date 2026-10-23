# Host-peak A/A control, before inspecting the final peak results

The 16 rate-capped A/A runs cannot quantify host-peak noise at several Gbps.
The earlier V1 peak exploration already had large BBR run-to-run variation.
Add **12 same-setting host-peak A/A attempts**, separately retained, after the
12 final A/B peak guards. Do not interpret either A/A order as a candidate gain.

Use the same final binary and exact guard conditions: CUBIC one/two connections,
BBR one, RTT zero, 10-Gbps serializer, warmup 4 s, measured window 12 s, no light
clients. Both arms are FIFO/default (`q0s0p0`), with two adjacent A/A pairs per
case. `rtt_final.py guard --candidate q0s0p0` freezes this order and records all
identities. The analysis recognizes these as A/A pairs, including their order.

No builds, plots or heavy analysis during either peak cohort. Compare observed
A/B changes with this same-load noise context; two pairs per case do not provide
a tight non-inferiority interval or rule out all shared-host interference.
Keep the three CC/flow cases separate. No additional parameter tuning is part
of this control, and no failed A/B run is replaced.
