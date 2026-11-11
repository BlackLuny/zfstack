#!/bin/bash
cd /workspace
export PATH=$HOME/.local/bin:$PATH
vng --disable-kvm --cpus 4 --memory 4G --rwdir /tmp/vmout --cwd /workspace --run /boot/vmlinuz-6.8.0-147-generic --user root \
  --exec "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' PYTHONDONTWRITEBYTECODE=1; python3 harness/l2/results/2026-10-16-netem-fifo/remote_selftests.py --expected-sha c3abb7826abbfc49dbce7b6eb98b70585a359327 --output /tmp/vmout/drv/out --domain local-tcg-smoke --run-id driver-smoke --lease-dir /tmp/vmout/lease" \
  > /tmp/vmout/drv/driver.log 2>&1
echo "exit=$?" > /tmp/vmout/drv/done
