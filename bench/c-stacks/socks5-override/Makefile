CC ?= gcc
CFLAGS ?= -O2 -pipe -Wall -Wextra
all: socks5-override
socks5-override: socks5_override.c
	$(CC) $(CFLAGS) -o $@ socks5_override.c
clean:
	rm -f socks5-override
