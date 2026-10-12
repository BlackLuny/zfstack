#!/usr/bin/env bash
# Optional L2, on an explicitly designated Linux zfc-rig host. Caller must hold
# that host's zfctest node lease. Does not touch the installed zf service/panel.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RIG_SELF_DIR="${1:?usage: run-rig.sh /path/to/zfc-rig /path/to/worker /path/to/new-output}"
WORKER_SOURCE="${2:?worker binary required}"
OUT="${3:?fresh output directory required}"
[[ "$(id -u)" == 0 && "$(uname -s)" == Linux ]] || { echo 'Linux root required' >&2; exit 2; }
[[ -x "$WORKER_SOURCE" && -x "$RIG_SELF_DIR/local/build/tools/fakectl" ]] || exit 2
[[ ! -e "$OUT" ]] || { echo 'Output must be a fresh directory' >&2; exit 2; }
umask 077
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
export RIG_SELF_DIR
source "$RIG_SELF_DIR/lib/common.sh"; rig_load_env
source "$RIG_SELF_DIR/lib/topo.sh"
source "$RIG_SELF_DIR/lib/nftpath.sh"
PIDS=(); WATCH_ACTIVE=0
systemctl is-active --quiet zfc-rig-watch.service && WATCH_ACTIVE=1
cleanup() {
  rc=$?
  trap - EXIT
  set +e
  for pid in "${PIDS[@]}"; do kill -TERM "$pid" 2>/dev/null; done
  sleep 1
  for pid in "${PIDS[@]}"; do kill -KILL "$pid" 2>/dev/null; done
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null; done
  topo_release
  ip netns list > "$OUT/namespaces-after.txt"
  (( WATCH_ACTIVE == 0 )) || systemctl start zfc-rig-watch.service
  systemctl is-active zfc-rig-watch.service > "$OUT/watcher-after.txt"
  printf '%s\n' "$rc" > "$OUT/exit-code"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
systemctl stop zfc-rig-watch.service
# The watcher starts ci.sh in a separate systemd scope. Stopping its service
# alone leaves that batch alive and able to create the next topology.
mapfile -t watch_scopes < <(systemctl list-units --type scope --state active --no-legend --plain \
  | awk '$1 ~ /^rig-watch-[a-f0-9]+\.scope$/ {print $1}')
printf '%s\n' "${watch_scopes[@]}" > "$OUT/paused-watch-scopes.txt"
for scope in "${watch_scopes[@]}"; do systemctl stop "$scope"; done
exec 9>"$RIG_ROOT/state/watch.lock"
flock -n 9 || { echo 'Another rig owner holds watch.lock' >&2; exit 3; }
# The stopped watcher may still be finishing a case's EXIT cleanup. Wait for
# those owned resources to drain; never delete an unknown namespace here.
for ((i=0;i<50;i++)); do
  [[ -n "$(ip netns list)" ]] || break
  sleep .2
done
[[ -z "$(ip netns list)" ]] || { echo 'Existing namespaces: refusing to start' >&2; exit 3; }
topo_up
nftpath_install "$TOPO_ID" "$TOPO_SUBNET"
mkdir -p "$OUT/worker" "$OUT/ctl" "$OUT/bin" "$OUT/keys"
install -m 0755 "$WORKER_SOURCE" "$OUT/bin/zf-worker"
WORKER="$OUT/bin/zf-worker"
sha256sum "$WORKER" > "$OUT/worker.sha256"
"$WORKER" --build-info > "$OUT/build-info.json"
uname -a > "$OUT/uname.txt"
lscpu > "$OUT/lscpu.txt"
ip netns list > "$OUT/namespaces.txt"
for name in server0 server1 client0 client1 client2 client3; do
  wg genkey > "$OUT/keys/$name.key"
  wg pubkey < "$OUT/keys/$name.key" > "$OUT/keys/$name.pub"
done
# Reuse the rig's typed port fixture. Two independent WG listeners/identities.
for port in 0 1; do
  python3 "$RIG_SELF_DIR/tools/wg-portmap.py" --outbound direct \
    --subnet "$TOPO_SUBNET" --port "$((20001+port))" --fwd-port 31001 \
    --echo-port 19001 --password l2-local-only \
    --server-key "$OUT/keys/server$port.key" \
    --client-pub "$OUT/keys/client$((port*2)).pub" \
    --client2-pub "$OUT/keys/client$((port*2+1)).pub" --netstack smoltcp \
    --out "$OUT/port$port.json" --meta "$OUT/path$port-meta.json"
done
python3 - "$OUT" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); ports=[]
for i in range(2):
    port=json.loads((p/f'port{i}.json').read_text())[0]
    cfg=port['inbound_protocol']['Wireguard']['config']
    cfg['service_address_v4']=f'10.99.{i+1}.1/32'
    for j,peer in enumerate(cfg['peers']):
        peer['assigned_ip']=f'10.99.{i+1}.{j+2}/32'
        peer['label']=f'l2-port{i}-peer{j}'
    ports.append(port)
(p/'portmap.json').write_text(json.dumps(ports,indent=2)+'\n')
PY
ip netns exec "$TOPO_NS_TARGET" python3 "$HERE/load.py" serve --host "$TOPO_IP_TARGET" > "$OUT/target.log" 2>&1 & PIDS+=("$!")
for ((i=0;i<50;i++)); do [[ ! -s "$OUT/target.log" ]] || break; sleep .1; done
grep -q ready "$OUT/target.log" || die 'target did not become ready'
(cd "$OUT/ctl" && exec ip netns exec "$TOPO_NS_CTL" "$RIG_BUILD/tools/fakectl" \
  --listen "$TOPO_IP_CTL:18080" --portmap "$OUT/portmap.json" \
  --pubkey-out "$OUT/ctl.pub" --events "$OUT/fakectl.jsonl" --iface eth0 \
) > "$OUT/fakectl.log" 2>&1 & PIDS+=("$!")
for ((i=0;i<50;i++)); do [[ ! -s "$OUT/ctl.pub" ]] || break; sleep .1; done
python3 - "$OUT" "$TOPO_IP_CTL" <<'PY'
import json, os, pathlib, sys
p=pathlib.Path(sys.argv[1])
config={'mgmt_arranger_public_key':(p/'ctl.pub').read_text().strip(),
        'mgmt_ws_endpoint':f'ws://{sys.argv[2]}:18080',
        'mgmt_upgrade_url':'http://127.0.0.1:19998',
        'backends':[{'iface':'eth0','mgmt_priv_key':os.urandom(32).hex(),'port_start':20000,'port_end':20100}]}
(p/'worker/config.json').write_text(json.dumps(config)+'\n')
PY
(cd "$OUT/worker" && exec env ZFW_LOG_TO_FILE=false RUST_LOG=info \
  ZFW_WG_USERSPACE_STACK=zfstack ZFW_WG_ZFSTACK_DIAG_INTERVAL_SECS=5 \
  ZFW_WG_ZFSTACK_MEMORY_MB="${L2_MEMORY_MB:-64}" ZF_OUTBOUND_FIREWALL=off \
  GEOIP_DB_PATH="$RIG_BUILD/geo/Country.mmdb" ASN_DB_PATH="$RIG_BUILD/geo/Asn.mmdb" \
  ZFW_EGRESS_PROBE_URLS_V4=http://127.0.0.1:19998 ZFW_EGRESS_PROBE_URLS_V6=http://127.0.0.1:19998 \
  ip netns exec "$TOPO_NS_WORKER" "$WORKER" --config-path "$OUT/worker/config.json" \
) > "$OUT/worker.log" 2>&1 & WPID="$!"; PIDS+=("$WPID")
for ((i=0;i<60;i++)); do
  count="$(ip netns exec "$TOPO_NS_WORKER" ss -lunH | awk '{print $4}' | grep -Ec ':2000[12]$' || true)"
  [[ "$count" != 2 ]] || break
  kill -0 "$WPID" || die 'worker exited before WG readiness'
  sleep 1
done
[[ "$count" == 2 ]] || die 'two WG listeners did not become ready'
clients=("$TOPO_NS_CLIENT" "$TOPO_NS_FWD1" "$TOPO_NS_FWD2" "$TOPO_NS_FWD3")
for i in 0 1 2 3; do
  port=$((i/2)); peer=$((i%2)); ns="${clients[$i]}"
  ip netns exec "$ns" ip link add wg0 type wireguard
  ip netns exec "$ns" ip addr add "10.99.$((port+1)).$((peer+2))/32" dev wg0
  ip netns exec "$ns" wg set wg0 private-key "$OUT/keys/client$i.key" \
    peer "$(cat "$OUT/keys/server$port.pub")" endpoint "$TOPO_IP_WORKER:$((20001+port))" \
    allowed-ips "$TOPO_IP_TARGET/32" persistent-keepalive 5
  ip netns exec "$ns" ip link set wg0 mtu 1420 up
  ip netns exec "$ns" ip route replace "$TOPO_IP_TARGET/32" dev wg0
  ip netns exec "$ns" ip route get "$TOPO_IP_TARGET" > "$OUT/route$i.txt"
  grep -q 'dev wg0' "$OUT/route$i.txt" || die 'WG target route is missing'
done
nftpath_snapshot "$TOPO_ID" "$OUT/path-pre.json"
set +e
python3 "$HERE/load.py" run --host "$TOPO_IP_TARGET" --clients "${clients[@]}" \
  --worker-pid "$WPID" --out "$OUT/load" --conns "${L2_HEAVY_CONNS:-4}" \
  --request-timeout "${L2_REQUEST_TIMEOUT_SECS:-3}"
load_rc=$?
set -e
nftpath_snapshot "$TOPO_ID" "$OUT/path-post.json"
python3 - "$OUT" "$load_rc" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); pre=json.loads((p/'path-pre.json').read_text()); post=json.loads((p/'path-post.json').read_text())
r=json.loads((p/'load/result.json').read_text()); errors=r['errors']
if int(sys.argv[2]): errors.append('load generator failed')
edges={}
for dst,counters in post.items():
    for name,c in counters.items():
        edges[f'{dst}/{name}']=c['bytes']-pre.get(dst,{}).get(name,{}).get('bytes',0)
for client in ('client','fwd1','fwd2','fwd3'):
    if edges.get(f'target/from_{client}',-1) != 0: errors.append(f'{client} bypassed worker or path counter missing')
    if edges.get(f'worker/from_{client}',0) <= 1000: errors.append(f'{client} WG outer path had no traffic')
if edges.get('worker/from_target',0) < r['heavy_bytes']: errors.append('target-to-worker edge did not carry heavy payload')
log=(p/'worker.log').read_text()
if log.count('WG zfstack: tuning_fp=') < 2: errors.append('missing ZFStack startup evidence for both ports')
r.update(ok=not errors,path_bytes=edges,topology={'wg_ports':2,'peers':4,'heavy_peer':0,'light_peers':[1,2,3]},memory_mb=int(__import__('os').environ.get('L2_MEMORY_MB','64')))
(p/'result.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({'ok':r['ok'],'errors':errors,'heavy_mbps':r['heavy_mbps']}))
sys.exit(bool(errors))
PY
