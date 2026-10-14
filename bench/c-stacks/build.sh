#!/usr/bin/env bash
# Fetch pinned C stacks and build lwip-tun-proxy, socks5-override, hev-socks5-tunnel.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TP="$ROOT/third_party"
mkdir -p "$TP/bin"

LWIP_TAG=STABLE-2_2_1_RELEASE
LWIP_REV=77dcd25a72509eb83f72b033d219b1d40cd8eb95
HEV_REV=50d7b41a4f53e72b5f53a5a262ff5e90f1b53cd3
SB_VER=1.15.0-alpha.12

if [[ ! -d "$TP/lwip/src" ]]; then
  git clone --depth 1 --branch "$LWIP_TAG" https://github.com/lwip-tcpip/lwip.git "$TP/lwip"
fi
if [[ ! -d "$TP/hev-socks5-tunnel/.git" ]]; then
  git clone --recursive https://github.com/heiher/hev-socks5-tunnel.git "$TP/hev-socks5-tunnel"
  git -C "$TP/hev-socks5-tunnel" checkout "$HEV_REV"
  git -C "$TP/hev-socks5-tunnel" submodule update --init --recursive
fi

make -C "$ROOT/c-stacks/lwip-tun-proxy" LWIP="$TP/lwip"
make -C "$ROOT/c-stacks/socks5-override"
make -C "$TP/hev-socks5-tunnel" -j"$(nproc)"
cp -f "$ROOT/c-stacks/lwip-tun-proxy/lwip-tun-proxy" "$TP/bin/"
cp -f "$ROOT/c-stacks/socks5-override/socks5-override" "$TP/bin/"
cp -f "$TP/hev-socks5-tunnel/bin/hev-socks5-tunnel" "$TP/bin/"

if [[ ! -x "$TP/bin/sing-box" ]]; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/sb.tgz" \
    "https://github.com/SagerNet/sing-box/releases/download/v${SB_VER}/sing-box-${SB_VER}-linux-amd64.tar.gz"
  tar -xzf "$tmp/sb.tgz" -C "$tmp"
  cp "$tmp"/sing-box-*/sing-box "$TP/bin/sing-box"
  chmod +x "$TP/bin/sing-box"
  rm -rf "$tmp"
fi

{
  echo "lwip $LWIP_TAG $LWIP_REV"
  echo "hev-socks5-tunnel $HEV_REV"
  echo -n "sing-box "; "$TP/bin/sing-box" version | head -1
} | tee "$TP/bin/versions.txt"
echo "built: $TP/bin/{lwip-tun-proxy,socks5-override,hev-socks5-tunnel,sing-box}"
