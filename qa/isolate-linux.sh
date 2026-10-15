#!/usr/bin/env bash
# A private network/mount/PID domain for destructive *test* topology commands.
# The host's service manager and network namespace registry are never reachable.
set -euo pipefail
if [[ ${1:-} != --inside ]]; then
  [[ $(id -u) == 0 ]] || { echo 'isolated integration requires pre-granted root' >&2; exit 3; }
  export QA_HOST_NETNS_ID=$(readlink /proc/self/ns/net)
  export QA_HOST_MNTNS_ID=$(readlink /proc/self/ns/mnt)
  export QA_HOST_PIDNS_ID=$(readlink /proc/self/ns/pid)
  exec unshare --mount --net --pid --fork --mount-proc --kill-child=KILL --propagation private \
    bash "$0" --inside "$@"
fi
shift
[[ $# -gt 0 ]] || exit 3
mount --make-rprivate /
# /run is private, including /run/netns, systemd and dbus control sockets.
mount -t tmpfs -o mode=755,size=16m tmpfs /run
mkdir -p /run/netns
ip link set lo up
export QA_NETNS_ISOLATED=1 QA_PRIVATE_PIDNS=1 L2_ISOLATED=1
exec "$@"
