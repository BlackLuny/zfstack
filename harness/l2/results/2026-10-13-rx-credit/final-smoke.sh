#!/usr/bin/env bash
set -euo pipefail
TASK=/root/zfc-rig/local/pr25-rx-credit-2d060d87
[[ "$(cat "$TASK/final/build.exit")" == 0 ]]
[[ "$(cat "$TASK/final/candidate-core.sha256")" == dab72bf076ac94e9fd89f36539df0743c7fb28fd7ef082f3c142eb2049ad56c4 ]]
set +e
env L2_MEMORY_MB=8 L2_UPLINK_DELAY_MS=0 L2_LOADED_SECS=16 L2_HEAVY_CONNS=16 L2_REQUEST_TIMEOUT_SECS=3 L2_CAPTURE=1 L2_NAMESPACE_WAIT_SECS=150 timeout 360 bash "$TASK/l2/run-rig.sh" /root/zfc-rig "$TASK/final/zf-worker-candidate" "$TASK/final/small" > "$TASK/final/small.log" 2>&1
rc=$?
printf '%s\n' "$rc" > "$TASK/final/small.rc"
exit "$rc"
