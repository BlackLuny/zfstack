#!/usr/bin/env bash
set -euo pipefail
TASK=/root/zfc-rig/local/pr25-rx-credit-2d060d87
BASE=/root/zfc-rig/local/pr25-l2-f55ea791/build/out/func/zf-worker
CAND="$TASK/zf-worker-candidate"
was_active=0
systemctl is-active --quiet zfc-rig-watch.service && was_active=1
restore() { rc=$?; trap - EXIT; printf '%s\n' "$rc" > "$TASK/compare.exit"; (( was_active == 0 )) || systemctl start zfc-rig-watch.service; exit "$rc"; }
trap restore EXIT
trap 'exit 130' INT TERM
[[ -x "$CAND" ]]
[[ "$(cut -d' ' -f1 "$TASK/core-restored.sha256")" == bedf3b672b40ebe40db72bbac22943065e11efb7339ad9deb978083b22ba0742 ]]
[[ "$(sha256sum "$BASE" | cut -d' ' -f1)" == fb32f8b37862d364d7e6abd4cf4bb4d39e3776e5f2f8ddb3ac89d1aebdbd07eb ]]
systemctl stop zfc-rig-watch.service
run() {
 local name="$1" binary="$2" memory="$3" delay="$4" loaded="$5"
 printf '%s\n' "$name" > "$TASK/current-case"
 set +e
 env L2_MEMORY_MB="$memory" L2_UPLINK_DELAY_MS="$delay" L2_LOADED_SECS="$loaded" L2_HEAVY_CONNS=16 L2_REQUEST_TIMEOUT_SECS=3 L2_CAPTURE=1 L2_NAMESPACE_WAIT_SECS=150 timeout 280 bash "$TASK/l2/run-rig.sh" /root/zfc-rig "$binary" "$TASK/$name" > "$TASK/$name.log" 2>&1
 rc=$?
 set -e
 printf '%s\n' "$rc" > "$TASK/$name.rc"
 [[ -f "$TASK/$name/result.json" ]] || return "$rc"
}
for profile in small normal delayed; do
 case "$profile" in small) memory=8; delay=0; loaded=16;; normal) memory=64; delay=0; loaded=16;; delayed) memory=8; delay=50; loaded=64;; esac
 run "$profile-base-a" "$BASE" "$memory" "$delay" "$loaded"
 run "$profile-candidate-a" "$CAND" "$memory" "$delay" "$loaded"
 run "$profile-candidate-b" "$CAND" "$memory" "$delay" "$loaded"
 run "$profile-base-b" "$BASE" "$memory" "$delay" "$loaded"
done
printf 'done\n' > "$TASK/current-case"
