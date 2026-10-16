#!/bin/bash
# Diagnostic only: run one netem-fifo full self-test while a private tracefs
# instance records, per CPU, when each UDP-sized frame is handed to the
# receiving veth (netif_rx, on the CPU that dequeued it from netem) and when
# that frame is processed (netif_receive_skb). Usage inside the QA VM:
#   veth_order_trace.sh OUTPUT_PREFIX [fifo_selftest.py options...]
set -eu
out=$1; shift
T=/sys/kernel/tracing
[ -d "$T/instances" ] || mount -t tracefs nodev "$T"
I=$T/instances/qa44order
mkdir "$I"
cleanup() { echo 0 > "$I/tracing_on" || true; rmdir "$I" || true; }
trap cleanup EXIT
echo mono > "$I/trace_clock"
echo 65536 > "$I/buffer_size_kb"
# 1074 B UDP frames lose their 14 B Ethernet header in eth_type_trans before netif_rx.
for event in netif_rx netif_receive_skb; do
  echo 'len == 1060' > "$I/events/net/$event/filter"
  echo 1 > "$I/events/net/$event/enable"
done
echo 1 > "$I/tracing_on"
status=0
PYTHONDONTWRITEBYTECODE=1 bash qa/isolate-linux.sh python3 harness/l2/fifo_selftest.py --kind netem-fifo "$@" \
  --output "$out.json" || status=$?
echo 0 > "$I/tracing_on"
gzip -c "$I/trace" > "$out.ftrace.gz"
for cpu in "$I"/per_cpu/cpu*; do echo "== ${cpu##*/}"; cat "$cpu/stats"; done > "$out.trace-stats"
exit $status
