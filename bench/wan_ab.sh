#!/usr/bin/env bash
# WAN A/B through the WG link mode, driven from any machine that can SSH to
# both nodes (e.g. your laptop with this repo checked out).
#
#   bench/wan_ab.sh <server ssh> <server port> <client ssh> <client port> [run_matrix args...]
#   bench/wan_ab.sh root@192.99.148.123 24946 root@23.136.204.72 23826 --reps 3
#
# Steps: copy this source tree to both nodes (rsync, no git access needed),
# install Rust if missing, build zfbench, start `zfbench --serve-wg` on the
# server (allowing only the client's public IP), run run_matrix.py on the
# client, stop the server and copy the results back to bench/results/.
#
# The server must accept UDP 51820 and TCP 5202 from the client.
set -euo pipefail

SRV=$1 SRV_PORT=$2 CLI=$3 CLI_PORT=$4
shift 4
MATRIX_ARGS=("$@")
[ ${#MATRIX_ARGS[@]} -eq 0 ] && MATRIX_ARGS=(--stacks kernel-cubic,smoltcp-cubic,zfstack-cubic,zfstack-bbr --tests down,up,mixed,connect --flows 1,8 --reps 3)
NAME=${NAME:-wan-$(date +%Y%m%d-%H%M)}
REMOTE_DIR=/root/zfstack-bench
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TOKEN=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')

ssh_s() { ssh -p "$SRV_PORT" -o StrictHostKeyChecking=accept-new "$SRV" "$@"; }
ssh_c() { ssh -p "$CLI_PORT" -o StrictHostKeyChecking=accept-new "$CLI" "$@"; }

prepare() { # $1 = ssh function, $2 = port, $3 = host
    echo "== $3: sync + build" >&2
    "$1" "command -v rsync >/dev/null && command -v cc >/dev/null && command -v wg >/dev/null ||
        (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq rsync build-essential pkg-config wireguard-tools >/dev/null)"
    rsync -az --delete -e "ssh -p $2" --exclude target --exclude 'bench/results' "$ROOT/" "$3:$REMOTE_DIR/"
    "$1" "set -e
        [ -x ~/.cargo/bin/cargo ] || curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null
        cd $REMOTE_DIR && ~/.cargo/bin/cargo build -q --release -p zfbench
        uname -r; nproc; command -v wg >/dev/null && echo 'wg tools: yes' || echo 'wg tools: no (userspace WG will be used)'"
}

prepare ssh_s "$SRV_PORT" "$SRV" &
prepare ssh_c "$CLI_PORT" "$CLI" &
wait

SRV_IP=$(ssh_s "hostname -I | awk '{print \$1}'")
CLI_PUB=$(ssh_c "curl -s -4 https://ifconfig.me || hostname -I | awk '{print \$1}'")
SRV_PUB=${SRV#*@}
echo "== server $SRV_PUB (local $SRV_IP), client public IP $CLI_PUB" >&2

# Start the server as a transient systemd unit: a backgrounded process under
# `ssh host "... &"` can keep the SSH session open and hang this script.
echo "== starting server on $SRV_PUB" >&2
ssh_s "systemctl stop zfbench-serve 2>/dev/null; systemctl reset-failed zfbench-serve 2>/dev/null; pkill -x zfbench;
    systemd-run --quiet --unit zfbench-serve --setenv=ZFBENCH_TOKEN=$TOKEN \
        $REMOTE_DIR/target/release/zfbench --serve-wg --allow $CLI_PUB" </dev/null
trap 'ssh_s "systemctl stop zfbench-serve; '"$REMOTE_DIR"'/target/release/zfbench --cleanup" </dev/null || true' EXIT
sleep 1
if ! ssh_s "ss -ltn | grep -q ':5202 '" </dev/null; then
    echo "server is not listening on 5202:" >&2
    ssh_s "journalctl -u zfbench-serve --no-pager -n 30" </dev/null >&2
    exit 1
fi
if ! ssh_c "timeout 5 bash -c '</dev/tcp/$SRV_PUB/5202'" </dev/null; then
    echo "client cannot reach $SRV_PUB:5202 (TCP). Open TCP 5202 and UDP 51820 on the server to $CLI_PUB." >&2
    exit 1
fi

echo "== matrix $NAME" >&2
ssh_c "cd $REMOTE_DIR && ZFBENCH_TOKEN=$TOKEN python3 -u bench/run_matrix.py --wg-server $SRV_PUB --name $NAME ${MATRIX_ARGS[*]}" || echo "matrix exited non-zero" >&2

mkdir -p "$ROOT/bench/results"
rsync -az -e "ssh -p $CLI_PORT" "$CLI:$REMOTE_DIR/bench/results/*-$NAME" "$ROOT/bench/results/"
ssh_s "journalctl -u zfbench-serve --no-pager" </dev/null > "$(ls -d "$ROOT/bench/results/"*"-$NAME" | head -1)/serve.log" 2>/dev/null || true
echo "== results in bench/results/*-$NAME" >&2
