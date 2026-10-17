#!/bin/bash
cd /workspace
export PATH=$HOME/.local/bin:$PATH
vng --disable-kvm --cpus 4 --memory 4G --rwdir /tmp/vmout --cwd /workspace --run /boot/vmlinuz-6.8.0-147-generic --user root \
  --exec "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0='*' PYTHONDONTWRITEBYTECODE=1; python3 harness/l2/results/2026-10-16-netem-fifo/remote_selftests.py --expected-sha 8fa27dfded25f25ed795b26641fef93f6d232afb --output /tmp/vmout/drv2/out --domain local-tcg-smoke --run-id driver-smoke-2 --lease-dir /tmp/vmout/lease2" \
  > /tmp/vmout/drv2/driver.log 2>&1
echo "exit=$?" > /tmp/vmout/drv2/done
