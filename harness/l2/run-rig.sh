#!/usr/bin/env bash
# Optional L2, on an explicitly designated Linux zfc-rig host. Caller must hold
# that host's zfctest node lease. Does not touch the installed zf service/panel.
set -euo pipefail
if [[ "${L2_MODE:-load}" == idle ]]; then
  # Worker and target need descriptors for every idle stream as well as load.
  fd_needed=$((4 * (${L2_IDLE_CONNS:-1500} + ${L2_HEAVY_CONNS:-4} + 16)))
  if [[ "$(ulimit -Sn)" != unlimited ]] && (( $(ulimit -Sn) < fd_needed )); then
    ulimit -Sn "$fd_needed"
  fi
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISOLATED="${L2_ISOLATED:-0}"
OUTBOUND="${L2_OUTBOUND:-direct}"
[[ "$OUTBOUND" == direct || "$OUTBOUND" == relay ]] || { echo 'L2_OUTBOUND must be direct or relay' >&2; exit 2; }
if [[ -n "${L2_QUEUE_DELAY_MS:-}" && "$ISOLATED" != 1 ]]; then
  echo 'Queue negative control requires verified isolated mode' >&2; exit 2
fi
[[ "$ISOLATED" == 0 || "$ISOLATED" == 1 ]] || { echo 'L2_ISOLATED must be 0 or 1' >&2; exit 2; }
if [[ "$ISOLATED" == 1 ]]; then
  python3 "$HERE/isolation.py" || exit 2
fi
RIG_SELF_DIR="${1:?usage: run-rig.sh /path/to/zfc-rig /path/to/worker /path/to/new-output}"
WORKER_SOURCE="${2:?worker binary required}"
FORWARDER_SOURCE="${L2_FORWARDER_BINARY:-}"
if [[ "$OUTBOUND" == relay ]]; then
  [[ "$FORWARDER_SOURCE" == /* && -x "$FORWARDER_SOURCE" ]] || { echo 'Relay requires immutable absolute L2_FORWARDER_BINARY' >&2; exit 2; }
fi
OUT="${3:?fresh output directory required}"
NETEM_LIMIT_PACKETS="${L2_NETEM_LIMIT_PACKETS:-100000}"
NETEM_ENABLED="$(python3 "$HERE/netem.py" --check --delay-ms "${L2_UPLINK_DELAY_MS:-0}" --limit-packets "$NETEM_LIMIT_PACKETS")"
[[ "$(id -u)" == 0 && "$(uname -s)" == Linux ]] || { echo 'Linux root required' >&2; exit 2; }
FROZEN_RIG_BUILD="${RIG_BUILD:-$RIG_SELF_DIR/local/build}"
[[ -x "$WORKER_SOURCE" && -x "$FROZEN_RIG_BUILD/tools/fakectl" ]] || exit 2
export RIG_BUILD="$FROZEN_RIG_BUILD"
[[ ! -e "$OUT" ]] || { echo 'Output must be a fresh directory' >&2; exit 2; }
umask 077
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
export RIG_SELF_DIR
source "$RIG_SELF_DIR/lib/common.sh"; rig_load_env
# common.sh derives RIG_BUILD from RIG_ROOT. The caller's immutable fixture
# path takes precedence; isolated topology state gets a different RIG_ROOT.
export RIG_BUILD="$FROZEN_RIG_BUILD"
if [[ "$ISOLATED" == 1 ]]; then
  # Keep the already-resolved read-only tool/build paths, but never write the
  # shared rig's topology state or locks from the isolated invocation.
  export RIG_ROOT="$OUT/isolated-rig-state"
  mkdir -p "$RIG_ROOT/state"
fi
source "$RIG_SELF_DIR/lib/topo.sh"
source "$RIG_SELF_DIR/lib/nftpath.sh"
if [[ "$OUTBOUND" == relay ]]; then source "$RIG_SELF_DIR/lib/fwd.sh"; fi
if [[ "$ISOLATED" == 1 ]]; then
  # The private PID/net/mount namespaces are reaped by the outer QA wrapper.
  # Never schedule or cancel a host systemd undo unit from this mode.
  topo_arm_undo() { :; }
  topo_disarm_undo() { :; }
fi
PIDS=(); WATCH_ACTIVE=0; QDISC_ACTIVE=0; QDISC_CAPTURED=0
snapshot_qdiscs() {
  local phase="$1" i
  for i in "${!clients[@]}"; do
    # Keep failures as evidence, too. The validator rejects missing/invalid
    # JSON instead of silently treating an unavailable counter as zero.
    ip netns exec "${clients[$i]}" tc -j -s qdisc show dev wg0 \
      > "$OUT/qdisc-$phase-$i.json" 2> "$OUT/qdisc-$phase-$i.err" || true
    ip netns exec "${clients[$i]}" tc -s qdisc show dev wg0 \
      > "$OUT/qdisc-$phase-$i.txt" 2>&1 || true
  done
}
summarize_qdiscs() {
  python3 "$HERE/netem.py" "$OUT" --delay-ms "${L2_UPLINK_DELAY_MS:-0}" \
    --limit-packets "$NETEM_LIMIT_PACKETS" > "$OUT/qdisc.json"
}
if [[ "$ISOLATED" == 0 ]]; then
  systemctl is-active --quiet zfc-rig-watch.service && WATCH_ACTIVE=1
fi
cleanup() {
  rc=$?
  trap - EXIT
  set +e
  if (( QDISC_ACTIVE && ! QDISC_CAPTURED )); then
    snapshot_qdiscs post
    summarize_qdiscs
  fi
  for pid in "${PIDS[@]}"; do kill -TERM "$pid" 2>/dev/null; done
  sleep 1
  for pid in "${PIDS[@]}"; do kill -KILL "$pid" 2>/dev/null; done
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null; done
  topo_release
  ip netns list > "$OUT/namespaces-after.txt"
  if [[ "$ISOLATED" == 0 ]]; then
    (( WATCH_ACTIVE == 0 )) || systemctl start zfc-rig-watch.service
    systemctl is-active zfc-rig-watch.service > "$OUT/watcher-after.txt"
  else
    printf '%s\n' 'not_applicable: isolated namespaces; host watcher untouched' > "$OUT/watcher-after.txt"
  fi
  printf '%s\n' "$rc" > "$OUT/exit-code"
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM
if [[ "$ISOLATED" == 0 ]]; then
systemctl stop zfc-rig-watch.service
# The watcher starts ci.sh in a separate systemd scope. Stopping its service
# alone leaves that batch alive and able to create the next topology.
mapfile -t watch_scopes < <(systemctl list-units --type scope --state active --no-legend --plain \
  | awk '$1 ~ /^rig-watch-[a-f0-9]+\.scope$/ {print $1}')
printf '%s\n' "${watch_scopes[@]}" > "$OUT/paused-watch-scopes.txt"
for scope in "${watch_scopes[@]}"; do systemctl stop "$scope"; done
else
  printf '%s\n' 'not_applicable: isolated namespaces' > "$OUT/paused-watch-scopes.txt"
fi
exec 9>"$RIG_ROOT/state/watch.lock"
flock -n 9 || { echo 'Another rig owner holds watch.lock' >&2; exit 3; }
# The stopped watcher may still be finishing a case's EXIT cleanup. Wait for
# those owned resources to drain; never delete an unknown namespace here.
for ((i=0;i<${L2_NAMESPACE_WAIT_SECS:-150}*5;i++)); do
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
if [[ "$OUTBOUND" == relay ]]; then
  install -m 0755 "$FORWARDER_SOURCE" "$OUT/bin/forwarder-server"
  sha256sum "$OUT/bin/forwarder-server" > "$OUT/forwarder.sha256"
  "$OUT/bin/forwarder-server" --build-info > "$OUT/forwarder-build-info.json"
fi
uname -a > "$OUT/uname.txt"
lscpu > "$OUT/lscpu.txt"
ip netns list > "$OUT/namespaces.txt"
for name in server0 server1 client0 client1 client2 client3; do
  wg genkey > "$OUT/keys/$name.key"
  wg pubkey < "$OUT/keys/$name.key" > "$OUT/keys/$name.pub"
done
# Reuse the rig's typed port fixture. Two independent WG listeners/identities.
PROBE_PORT=19001
[[ "$OUTBOUND" != relay ]] || PROBE_PORT=19002
for port in 0 1; do
  python3 "$RIG_SELF_DIR/tools/wg-portmap.py" --outbound "$OUTBOUND" \
    --subnet "$TOPO_SUBNET" --port "$((20001+port))" --fwd-port 31001 \
    --echo-port "$PROBE_PORT" --password l2-local-only \
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
ip netns exec "$TOPO_NS_TARGET" env L2_SERVER_EVENTS="$OUT/server-events.jsonl" python3 "$HERE/load.py" serve --host "$TOPO_IP_TARGET" > "$OUT/target.log" 2>&1 & TARGET_PID="$!"; PIDS+=("$TARGET_PID")
export L2_TARGET_PID="$TARGET_PID"
export L2_PROBE_TARGET_PID='' L2_FORWARDER_PID=''
for ((i=0;i<50;i++)); do [[ ! -s "$OUT/target.log" ]] || break; sleep .1; done
grep -q ready "$OUT/target.log" || die 'target did not become ready'
if [[ "$OUTBOUND" == relay ]]; then
  # Separate health-probe port from business so a worker Direct shortcut cannot
  # be excused as routine probe traffic in the path evidence.
  ip netns exec "$TOPO_NS_TARGET" python3 "$HERE/load.py" serve --host "$TOPO_IP_TARGET" --port 19002 > "$OUT/probe-target.log" 2>&1 & PROBE_TARGET_PID="$!"; PIDS+=("$PROBE_TARGET_PID")
  export L2_PROBE_TARGET_PID="$PROBE_TARGET_PID"
  for ((i=0;i<50;i++)); do grep -q ready "$OUT/probe-target.log" && break; sleep .1; done
  grep -q ready "$OUT/probe-target.log" || die 'relay health-probe target did not become ready'
  FPID="$(fwd_start "$TOPO_NS_FWD1" "$OUT/forwarder" "$OUT/bin/forwarder-server" "$TOPO_IP_FWD1" 31001 l2-local-only "$OUT/forwarder.log")"
  PIDS+=("$FPID")
  export L2_FORWARDER_PID="$FPID"
  fwd_wait_ready "$TOPO_NS_FWD1" 31001 "$FPID" 30 || die 'immutable relay forwarder did not become ready'
  for counter in worker_direct forwarder_target; do
    ip netns exec "$TOPO_NS_TARGET" nft add counter inet "rig$TOPO_ID" "relay_$counter"
  done
  for proto in tcp udp; do
    ip netns exec "$TOPO_NS_TARGET" nft add rule inet "rig$TOPO_ID" obs_in ip saddr "$TOPO_IP_WORKER" "$proto" dport 19001 counter name relay_worker_direct
    ip netns exec "$TOPO_NS_TARGET" nft add rule inet "rig$TOPO_ID" obs_in ip saddr "$TOPO_IP_FWD1" "$proto" dport 19001 counter name relay_forwarder_target
  done
fi
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
[[ "$OUTBOUND" != relay ]] || clients[1]="$TOPO_NS_FWD4"
for i in 0 1 2 3; do
  port=$((i/2)); peer=$((i%2)); ns="${clients[$i]}"
  ip netns exec "$ns" ip link add wg0 type wireguard
  ip netns exec "$ns" ip addr add "10.99.$((port+1)).$((peer+2))/32" dev wg0
  ip netns exec "$ns" wg set wg0 private-key "$OUT/keys/client$i.key" \
    peer "$(cat "$OUT/keys/server$port.pub")" endpoint "$TOPO_IP_WORKER:$((20001+port))" \
    allowed-ips "$TOPO_IP_TARGET/32" persistent-keepalive 5
  ip netns exec "$ns" ip link set wg0 mtu "${L2_MTU:-1420}" up
  if [[ "$NETEM_ENABLED" == 1 ]]; then
    ip netns exec "$ns" tc qdisc add dev wg0 root netem limit "$NETEM_LIMIT_PACKETS" delay "${L2_UPLINK_DELAY_MS}ms"
  fi
  ip netns exec "$ns" ip route replace "$TOPO_IP_TARGET/32" dev wg0
  ip netns exec "$ns" ip route get "$TOPO_IP_TARGET" > "$OUT/route$i.txt"
  grep -q 'dev wg0' "$OUT/route$i.txt" || die 'WG target route is missing'
done
if [[ "${L2_CAPTURE:-0}" == 1 ]]; then
  ip netns exec "${clients[1]}" tcpdump -U -nn -s 512 -i wg0 -w "$OUT/light-inner.pcap" 'tcp port 19001' > "$OUT/light-capture.log" 2>&1 & PIDS+=("$!")
  ip netns exec "$TOPO_NS_TARGET" tcpdump -U -nn -s 384 -i eth0 -w "$OUT/light-target.pcap" 'tcp port 19001 and (tcp[((tcp[12] & 0xf0) >> 2):4] = 1 or tcp[((tcp[12] & 0xf0) >> 2):1] = 0x45)' > "$OUT/target-capture.log" 2>&1 & PIDS+=("$!")
  sleep 1
fi
python3 -c 'import json,time; print(json.dumps({"monotonic":time.monotonic(),"realtime":time.time()}))' > "$OUT/clock.json"
nftpath_snapshot "$TOPO_ID" "$OUT/path-pre.json"
export L2_QUEUE_WORKER_NS="$TOPO_NS_WORKER"
queue_link_args=()
if [[ -n "${L2_QUEUE_DELAY_MS:-}" ]]; then
  queue_link_args=(--node "worker=$TOPO_NS_WORKER" --node "target=$TOPO_NS_TARGET")
  for i in 0 1 2 3; do queue_link_args+=(--node "client$i=${clients[$i]}"); done
  python3 "$HERE/queue_links.py" "${queue_link_args[@]}" --output "$OUT/queue-links-before.json"
fi
QDISC_ACTIVE=1
snapshot_qdiscs pre
load_script="$HERE/load.py"
load_args=()
if [[ "${L2_MODE:-load}" == idle ]]; then
  load_script="$HERE/idle_load.py"
  load_args+=(--idle-conns "${L2_IDLE_CONNS:-1500}" --ramp-budget "${L2_RAMP_SECS:-120}")
fi
set +e
python3 "$load_script" run "${load_args[@]}" --host "$TOPO_IP_TARGET" --clients "${clients[@]}" \
  --worker-pid "$WPID" --out "$OUT/load" --conns "${L2_HEAVY_CONNS:-4}" \
  --request-timeout "${L2_REQUEST_TIMEOUT_SECS:-3}" \
  --baseline "${L2_BASELINE_SECS:-8}" --loaded "${L2_LOADED_SECS:-16}" --recovery "${L2_RECOVERY_SECS:-8}"
load_rc=$?
set -e
if [[ -n "${L2_QUEUE_DELAY_MS:-}" ]]; then
  python3 "$HERE/queue_links.py" "${queue_link_args[@]}" --output "$OUT/queue-links-after.json"
fi
snapshot_qdiscs post
summarize_qdiscs
QDISC_CAPTURED=1
nftpath_snapshot "$TOPO_ID" "$OUT/path-post.json"
RELAY_ALIVE=0
if [[ "$OUTBOUND" == relay ]] && kill -0 "$FPID" 2>/dev/null; then RELAY_ALIVE=1; fi
python3 - "$OUT" "$load_rc" "$RELAY_ALIVE" "$HERE" <<'PY'
import json, pathlib, sys
sys.path.insert(0,sys.argv[4])
from path_contract import inspect_path
p=pathlib.Path(sys.argv[1]); pre=json.loads((p/'path-pre.json').read_text()); post=json.loads((p/'path-post.json').read_text())
r=json.loads((p/'load/result.json').read_text()); errors=r['errors']
r['load_functional_ok']=r['ok']
path_errors=[]
qdisc=json.loads((p/'qdisc.json').read_text())
errors.extend(f'test rig: {error}' for error in qdisc['errors'])
r['qdisc']=qdisc
if int(sys.argv[2]): errors.append('test rig: load generator exited unsuccessfully; inspect retained client failures')
log=(p/'worker.log').read_text()
r['path_evidence']={'before':pre,'after':post,'startup_ports':log.count('WG zfstack: tuning_fp=')}
if r.get('effective_config',{}).get('outbound')=='relay':
    r['path_evidence'].update(outbound='relay',forwarder_alive=sys.argv[3]=='1',relay_business={
        phase:{key:data.get('target',{}).get('relay_'+key,{}).get('packets') for key in ('worker_direct','forwarder_target')}
        for phase,data in (('before',pre),('after',post))})
    r['forwarder_build_info']=json.loads((p/'forwarder-build-info.json').read_text())
path_errors,edges=inspect_path(r)
r['path_evidence']['errors']=path_errors
errors.extend(f'test rig: {error}' for error in path_errors)
if isinstance(r.get('effective_config'),dict):
    import re
    r['effective_config']['worker_tuning_fingerprints']=sorted(re.findall(r'WG zfstack: tuning_fp=([^\s]+)',log))
    r['effective_config']['mode']=__import__('os').environ.get('L2_MODE','load')
    if r['effective_config']['mode']=='idle':
        r['effective_config']['idle_conns']=int(__import__('os').environ.get('L2_IDLE_CONNS','1500'))
        r['effective_config']['ramp_budget_sec']=float(__import__('os').environ.get('L2_RAMP_SECS','120'))
r.update(ok=not errors,path_bytes=edges,topology=r['effective_config']['topology'],memory_mb=int(__import__('os').environ.get('L2_MEMORY_MB','64')))
if r.get('queue_control') is not None:
    links={'schema_version':1,'clock':'CLOCK_MONOTONIC',
           **{phase:json.loads((p/f'queue-links-{phase}.json').read_text()) for phase in ('before','after')}}
    r['queue_control']['link_samples']=links
    with (p/'queue-link-samples.json').open('x') as stream:
        json.dump(links,stream,indent=2); stream.write('\n')
    (p/'queue-control.json').write_text(json.dumps(r['queue_control'],indent=2)+'\n')
    if (p/'load'/'queue-ledger.bin.gz').is_file():
        (p/'load'/'queue-ledger.bin.gz').rename(p/'queue-ledger.bin.gz')
(p/'result.json').write_text(json.dumps(r,indent=2)+'\n')
print(json.dumps({'ok':r['ok'],'errors':errors,'heavy_mbps':r['heavy_mbps'],
                  'qdisc_limit_packets':qdisc['limit_packets'],
                  'qdisc_drops':{i:row.get('delta',{}).get('drops') for i,row in qdisc['clients'].items()}}))
sys.exit(bool(errors))
PY
