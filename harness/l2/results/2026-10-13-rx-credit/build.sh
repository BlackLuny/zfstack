#!/usr/bin/env bash
set -euo pipefail
TASK=/root/zfc-rig/local/pr25-rx-credit-2d060d87
OLD=/root/zfc-rig/local/pr25-l2-f55ea791
CORE="$OLD/repos/zfstack"
was_active=0
patch_applied=0
systemctl is-active --quiet zfc-rig-watch.service && was_active=1
core_hash() { python3 - "$CORE" <<'PY'
import hashlib,pathlib,sys
p=pathlib.Path(sys.argv[1]);h=hashlib.sha256()
for f in sorted([p/'Cargo.toml',*(p/'src').rglob('*.rs')]):h.update(str(f.relative_to(p)).encode()+b'\0'+f.read_bytes())
print(h.hexdigest())
PY
}
restore() {
 rc=$?; trap - EXIT
 if (( patch_applied )); then
  git -C "$CORE" diff -- src > "$TASK/applied-core.patch"
  git -C "$CORE" restore -- src
 fi
 core_hash > "$TASK/core-restored.sha256"
 printf '%s\n' "$rc" > "$TASK/build.exit"
 (( was_active == 0 )) || systemctl start zfc-rig-watch.service
 exit "$rc"
}
trap restore EXIT
trap 'exit 130' TERM INT
systemctl stop zfc-rig-watch.service
mapfile -t scopes < <(systemctl list-units --type scope --state active --no-legend --plain | awk '$1 ~ /^rig-watch-[a-f0-9]+\.scope$/ {print $1}')
printf '%s\n' "${scopes[@]}" > "$TASK/build-paused-scopes.txt"
for scope in "${scopes[@]}"; do systemctl stop "$scope"; done
cd /root/zfc-rig
exec 9>local/state/watch.lock
flock -w 150 9
source local/env.sh
export XDG_CACHE_HOME=/root/zfc-rig/local/cache
export ZIG_GLOBAL_CACHE_DIR=/root/zfc-rig/local/cache/zig
export ZIG_LOCAL_CACHE_DIR=/root/zfc-rig/local/cache/zig-local
export CARGO_TARGET_DIR=/root/zfc-rig/local/build/target
export CARGO_NET_GIT_FETCH_WITH_CLI=true
[[ -z "$(git -C "$CORE" status --porcelain)" ]]
[[ "$(core_hash)" == bedf3b672b40ebe40db72bbac22943065e11efb7339ad9deb978083b22ba0742 ]]
sha256sum "$OLD/repos/zfc/Cargo.lock" > "$TASK/cargo-lock-before.sha256"
git -C "$CORE" apply --check "$TASK/core.patch"
patch_applied=1
git -C "$CORE" apply "$TASK/core.patch"
core_hash > "$TASK/candidate-core.sha256"
git -C "$CORE" rev-parse HEAD > "$TASK/core-base.commit"
cd "$OLD/repos/zfc"
df -h . > "$TASK/build-disk.txt"
cargo zigbuild -p zf-worker --bin zf-worker --profile fast-release --target x86_64-unknown-linux-musl --features mmsg,jemalloc --config 'profile.fast-release.strip=false' > "$TASK/build.log" 2>&1
install -m 0755 "$CARGO_TARGET_DIR/x86_64-unknown-linux-musl/fast-release/zf-worker" "$TASK/zf-worker-candidate"
"$TASK/zf-worker-candidate" --build-info > "$TASK/build-info.json"
sha256sum "$TASK/zf-worker-candidate" > "$TASK/candidate-worker.sha256"
sha256sum "$OLD/repos/zfc/Cargo.lock" > "$TASK/cargo-lock-after.sha256"
cmp "$TASK/cargo-lock-before.sha256" "$TASK/cargo-lock-after.sha256"
