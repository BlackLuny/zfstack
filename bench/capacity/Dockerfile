FROM debian:13-slim
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends python3 iproute2 util-linux ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Docker mounts a fresh /dev at runtime, so create the device after startup.
ENTRYPOINT ["/bin/sh", "-c", "mkdir -p /dev/net && { test -c /dev/net/tun || mknod /dev/net/tun c 10 200; } && chmod 666 /dev/net/tun && exec \"$@\"", "--"]
