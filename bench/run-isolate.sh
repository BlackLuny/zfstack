#!/bin/sh
# L1 isolate: kernel TCP + one TUN + zfstack logical ports. No WireGuard.
# Deletes the TUN on the way out, including after Ctrl-C.
set -eu
cd "$(dirname "$0")/.."
if [ "$(id -u)" -ne 0 ]; then
    echo "isolate needs root (CAP_NET_ADMIN and /dev/net/tun)" >&2
    exit 3
fi
bin=${BIN:-target/release/zfbench}
if [ ! -x "$bin" ]; then
    echo "missing $bin; build with: cargo build --release -p zfbench" >&2
    exit 3
fi
trap '"$bin" --cleanup >/dev/null 2>&1 || true' EXIT INT TERM
exec "$bin" --stack zfstack-cubic --test isolate "$@"
