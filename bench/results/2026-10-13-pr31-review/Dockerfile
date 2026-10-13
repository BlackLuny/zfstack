FROM ubuntu:24.04
RUN --mount=type=secret,id=proxy_ca,required=true \
    apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca update \
    && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::https::CaInfo=/run/secrets/proxy_ca install -y --no-install-recommends python3 iproute2 util-linux tcpdump ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /dev/net && mknod /dev/net/tun c 10 200 && chmod 666 /dev/net/tun
