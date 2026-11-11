#!/bin/bash
set -u
cd /workspace
export PATH=$HOME/.local/bin:$PATH
run() {
  name=$1; shift
  start=$(date -u +%FT%TZ)
  vng --disable-kvm --cpus 4 --memory 4G --rwdir /tmp/vmout --cwd /workspace --run /boot/vmlinuz-6.8.0-147-generic --user root \
    --exec "modprobe sch_netem; modprobe dummy; PYTHONDONTWRITEBYTECODE=1 bash qa/isolate-linux.sh python3 harness/l2/fifo_selftest.py --kind netem-fifo $* --output /tmp/vmout/r96/$name.json" \
    > /tmp/vmout/r96/$name.log 2>&1
  echo "$name exit=$? start=$start end=$(date -u +%FT%TZ)" >> /tmp/vmout/r96/runs.log
}
run full --mode full
run cancel --mode cancel
run overflow --mode overflow
run full-heavy --mode full --rate 4000 --tcp-rate 20000000
echo done >> /tmp/vmout/r96/runs.log
