LWIP ?= ../../third_party/lwip
CC ?= gcc
CFLAGS ?= -O2 -pipe -Wall -Wextra -Wno-unused-parameter
CPPFLAGS += -I. -Iinclude -I$(LWIP)/src/include
LDFLAGS ?=

CORE = \
	$(LWIP)/src/core/init.c \
	$(LWIP)/src/core/def.c \
	$(LWIP)/src/core/inet_chksum.c \
	$(LWIP)/src/core/ip.c \
	$(LWIP)/src/core/mem.c \
	$(LWIP)/src/core/memp.c \
	$(LWIP)/src/core/netif.c \
	$(LWIP)/src/core/pbuf.c \
	$(LWIP)/src/core/stats.c \
	$(LWIP)/src/core/sys.c \
	$(LWIP)/src/core/tcp.c \
	$(LWIP)/src/core/tcp_in.c \
	$(LWIP)/src/core/tcp_out.c \
	$(LWIP)/src/core/timeouts.c \
	$(LWIP)/src/core/ipv4/ip4.c \
	$(LWIP)/src/core/ipv4/ip4_addr.c \
	$(LWIP)/src/core/ipv4/ip4_frag.c

all: lwip-tun-proxy

lwip-tun-proxy: main.c $(CORE)
	$(CC) $(CFLAGS) $(CPPFLAGS) -o $@ main.c $(CORE) $(LDFLAGS)

clean:
	rm -f lwip-tun-proxy
