#!/bin/bash
cd /workspace
export PATH=$HOME/.local/bin:$PATH
for n in 1 2 3; do
  start=$(date -u +%FT%TZ)
  vng --disable-kvm --cpus 4 --memory 4G --rwdir /tmp/vmout --cwd /workspace --run /boot/vmlinuz-6.8.0-147-generic --user root \
    --exec "modprobe sch_netem; PYTHONDONTWRITEBYTECODE=1 bash qa/isolate-linux.sh python3 harness/l2/results/2026-10-16-netem-fifo/netem_order_control.py --output /tmp/vmout/ctl/control-$n.json" \
    > /tmp/vmout/ctl/control-$n.log 2>&1
  echo "control-$n exit=$? start=$start end=$(date -u +%FT%TZ)" >> /tmp/vmout/ctl/runs.log
done
echo done >> /tmp/vmout/ctl/runs.log
