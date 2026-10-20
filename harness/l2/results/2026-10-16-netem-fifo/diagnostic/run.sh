#!/bin/bash
cd /workspace
export PATH=$HOME/.local/bin:$PATH
vng --disable-kvm --cpus 4 --memory 4G --rwdir /tmp/vmout --cwd /workspace --run /boot/vmlinuz-6.8.0-147-generic --user root \
  --exec "modprobe sch_netem; modprobe dummy; bash harness/l2/results/2026-10-16-netem-fifo/veth_order_trace.sh /tmp/vmout/diag/order-trace --mode full" \
  > /tmp/vmout/diag/order-trace.log 2>&1
echo "exit=$?" > /tmp/vmout/diag/done
