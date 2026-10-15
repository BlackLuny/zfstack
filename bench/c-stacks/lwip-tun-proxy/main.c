/* Direct TUN → lwIP TCP → kernel TCP upstream, same topology as zfbench --tun-proxy.
 *
 *   kernel TCP client ──TUN──▶ lwIP (this process) ──▶ 127.0.0.1:5201
 *
 * Usage:
 *   lwip-tun-proxy --tun zfcT --addr 172.19.0.1/30 --listen 198.18.0.2 \
 *                  --port 5201 --upstream 127.0.0.1:5201 --mtu 65535
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/if.h>
#include <linux/if_tun.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#include "lwip/init.h"
#include "lwip/ip.h"
#include "lwip/ip4_addr.h"
#include "lwip/netif.h"
#include "lwip/pbuf.h"
#include "lwip/tcp.h"
#include "lwip/timeouts.h"

#define IFF_TUN 0x0001
#define IFF_NO_PI 0x1000
#ifndef TUNSETIFF
#define TUNSETIFF _IOW('T', 202, int)
#endif
#ifndef TCP_NODELAY
#define TCP_NODELAY 1
#endif

#define FD_MAX 65536
#define PENDING_CAP (256 * 1024)
#define READ_CHUNK (256 * 1024)
#define TUN_BATCH_PKTS 64
#define TUN_BATCH_BYTES (512 * 1024)

static int tun_fd = -1;
static int epfd = -1;
static struct netif tun_netif;
static char tun_name[IFNAMSIZ] = "zfcT";
static char tun_cidr[64] = "172.19.0.1/30";
static char listen_ip[64] = "198.18.0.2";
static uint16_t listen_port = 5201;
static struct sockaddr_in upstream_sa;
static int mtu = 65535;
static volatile sig_atomic_t stop_flag;

struct conn {
    struct tcp_pcb *pcb;
    int fd;
    uint32_t ep_events;
    uint8_t *to_fd;
    size_t to_fd_len;
    size_t to_fd_off;
    uint8_t *to_pcb;
    size_t to_pcb_len;
    size_t to_pcb_off;
    int connecting;
    int pcb_fin;
    int pcb_fin_sent;
    int fd_eof;
};

static struct conn *by_fd[FD_MAX];

unsigned int
lwip_port_rand(void)
{
    unsigned int x = 0;
    if (getentropy(&x, sizeof x) != 0) {
        x = (unsigned int)time(NULL) ^ (unsigned int)getpid();
    }
    return x;
}

u32_t
sys_now(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (u32_t)((uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u);
}

static void
die(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    exit(1);
}

static void
sh(const char *fmt, ...)
{
    char cmd[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(cmd, sizeof cmd, fmt, ap);
    va_end(ap);
    int rc = system(cmd);
    if (rc != 0) {
        fprintf(stderr, "lwip-tun-proxy: command failed (%d): %s\n", rc, cmd);
    }
}

static void
on_sig(int sig)
{
    (void)sig;
    stop_flag = 1;
}

static int
set_nonblock(int fd)
{
    int fl = fcntl(fd, F_GETFL, 0);
    return fcntl(fd, F_SETFL, fl | O_NONBLOCK);
}

static err_t
tun_output(struct netif *netif, struct pbuf *p, const ip4_addr_t *ipaddr)
{
    (void)netif;
    (void)ipaddr;
    struct iovec iov[16];
    int n = 0;
    for (struct pbuf *q = p; q && n < 16; q = q->next) {
        iov[n].iov_base = q->payload;
        iov[n].iov_len = q->len;
        n++;
    }
    for (;;) {
        ssize_t w = writev(tun_fd, iov, n);
        if (w >= 0) {
            return ERR_OK;
        }
        if (errno == EINTR) {
            continue;
        }
        return ERR_OK;
    }
}

static err_t
tun_init(struct netif *netif)
{
    netif->name[0] = 't';
    netif->name[1] = 'u';
    netif->output = tun_output;
    netif->mtu = (u16_t)mtu;
    netif->flags = NETIF_FLAG_UP | NETIF_FLAG_LINK_UP;
    return ERR_OK;
}

static int
tun_pump(void)
{
    uint8_t buf[65535];
    int pkts = 0;
    size_t bytes = 0;
    while (pkts < TUN_BATCH_PKTS && bytes < TUN_BATCH_BYTES) {
        ssize_t n = read(tun_fd, buf, sizeof buf);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                return 0;
            }
            if (errno == EINTR) {
                continue;
            }
            return 0;
        }
        if (n == 0) {
            return 0;
        }
        pkts++;
        bytes += (size_t)n;
        struct pbuf *p = pbuf_alloc(PBUF_RAW, (u16_t)n, PBUF_RAM);
        if (!p) {
            continue;
        }
        pbuf_take(p, buf, (u16_t)n);
        if (tun_netif.input(p, &tun_netif) != ERR_OK) {
            pbuf_free(p);
        }
    }
    return 1;
}

static void
sync_epoll(struct conn *c)
{
    if (!c || c->fd < 0) {
        return;
    }
    uint32_t want = 0;
    if (c->connecting || c->to_fd_off < c->to_fd_len) {
        want |= EPOLLOUT;
    }
    if (!c->connecting && !c->fd_eof && c->to_pcb_off >= c->to_pcb_len) {
        want |= EPOLLIN | EPOLLRDHUP;
    }
    if (want == c->ep_events) {
        return;
    }
    struct epoll_event ev = { .events = want, .data.fd = c->fd };
    if (epoll_ctl(epfd, EPOLL_CTL_MOD, c->fd, &ev) == 0) {
        c->ep_events = want;
    }
}

static void
conn_free(struct conn *c, int abort_pcb)
{
    if (!c) {
        return;
    }
    if (c->fd >= 0) {
        epoll_ctl(epfd, EPOLL_CTL_DEL, c->fd, NULL);
        if (c->fd < FD_MAX) {
            by_fd[c->fd] = NULL;
        }
        close(c->fd);
        c->fd = -1;
    }
    if (c->pcb) {
        tcp_arg(c->pcb, NULL);
        tcp_recv(c->pcb, NULL);
        tcp_sent(c->pcb, NULL);
        tcp_err(c->pcb, NULL);
        tcp_poll(c->pcb, NULL, 0);
        struct tcp_pcb *p = c->pcb;
        c->pcb = NULL;
        if (abort_pcb) {
            tcp_abort(p);
        } else if (tcp_close(p) != ERR_OK) {
            tcp_abort(p);
        }
    }
    free(c->to_fd);
    free(c->to_pcb);
    free(c);
}

static int
conn_done(const struct conn *c)
{
    return c->pcb_fin && c->to_fd_off >= c->to_fd_len && c->fd_eof && c->to_pcb_off >= c->to_pcb_len && c->pcb_fin_sent;
}

static int
queue_to_fd(struct conn *c, const uint8_t *data, size_t n)
{
    if (n == 0) {
        return 0;
    }
    size_t used = c->to_fd_len - c->to_fd_off;
    if (c->to_fd_off && used) {
        memmove(c->to_fd, c->to_fd + c->to_fd_off, used);
    }
    c->to_fd_len = used;
    c->to_fd_off = 0;
    if (used + n > PENDING_CAP) {
        return -1;
    }
    if (!c->to_fd) {
        c->to_fd = malloc(PENDING_CAP);
        if (!c->to_fd) {
            return -1;
        }
    }
    memcpy(c->to_fd + c->to_fd_len, data, n);
    c->to_fd_len += n;
    return 0;
}

/* Returns 0 if `c` is still live, 1 if `c` was closed cleanly, -1 if aborted. */
static int
flush_to_fd(struct conn *c)
{
    while (c->to_fd_off < c->to_fd_len && c->fd >= 0) {
        ssize_t w = write(c->fd, c->to_fd + c->to_fd_off, c->to_fd_len - c->to_fd_off);
        if (w < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                sync_epoll(c);
                return 0;
            }
            if (errno == EINTR) {
                continue;
            }
            conn_free(c, 1);
            return -1;
        }
        if (w == 0) {
            break;
        }
        c->to_fd_off += (size_t)w;
        if (c->pcb) {
            tcp_recved(c->pcb, (u16_t)w);
        }
    }
    if (c->to_fd_off >= c->to_fd_len) {
        c->to_fd_off = c->to_fd_len = 0;
        if (c->pcb_fin && c->fd >= 0) {
            shutdown(c->fd, SHUT_WR);
        }
        if (conn_done(c)) {
            conn_free(c, 0);
            return 1;
        }
    }
    sync_epoll(c);
    return 0;
}

static err_t
write_to_pcb(struct tcp_pcb *pcb, const uint8_t *data, size_t len, size_t *written)
{
    *written = 0;
    while (len > 0) {
        u32_t space = tcp_sndbuf(pcb);
        if (space == 0) {
            return ERR_OK;
        }
        size_t chunk = len < (size_t)space ? len : (size_t)space;
        if (chunk > (size_t)TCP_MSS) {
            chunk = (size_t)TCP_MSS;
        }
        err_t e = tcp_write(pcb, data, (u16_t)chunk, TCP_WRITE_FLAG_COPY);
        if (e == ERR_MEM) {
            if (chunk > (size_t)pcb->mss && tcp_sndbuf(pcb) >= pcb->mss) {
                e = tcp_write(pcb, data, pcb->mss, TCP_WRITE_FLAG_COPY);
                if (e == ERR_OK) {
                    data += pcb->mss;
                    len -= pcb->mss;
                    *written += pcb->mss;
                    continue;
                }
            }
            return ERR_OK;
        }
        if (e != ERR_OK) {
            return e;
        }
        data += chunk;
        len -= chunk;
        *written += chunk;
    }
    return ERR_OK;
}

/* Returns 0 if `c` is still live, 1 if `c` was closed cleanly, -1 if aborted. */
static int
flush_to_pcb(struct conn *c)
{
    if (!c->pcb || c->connecting) {
        return 0;
    }
    if (c->to_pcb_off < c->to_pcb_len) {
        size_t w = 0;
        err_t e = write_to_pcb(c->pcb, c->to_pcb + c->to_pcb_off, c->to_pcb_len - c->to_pcb_off, &w);
        if (e != ERR_OK) {
            conn_free(c, 1);
            return -1;
        }
        c->to_pcb_off += w;
        if (w > 0) {
            tcp_output(c->pcb);
        }
    }
    if (c->to_pcb_off >= c->to_pcb_len) {
        c->to_pcb_off = c->to_pcb_len = 0;
        if (c->fd_eof && c->pcb && !c->pcb_fin_sent) {
            if (c->pcb_fin && c->to_fd_off >= c->to_fd_len) {
                conn_free(c, 0);
                return 1;
            }
            if (tcp_shutdown(c->pcb, 0, 1) == ERR_OK) {
                c->pcb_fin_sent = 1;
                tcp_output(c->pcb);
            }
        }
        if (conn_done(c)) {
            conn_free(c, 0);
            return 1;
        }
    }
    sync_epoll(c);
    return 0;
}

/* Returns 0 if `c` is still live, 1 if `c` was closed cleanly, -1 if aborted. */
static int
read_from_fd(struct conn *c)
{
    uint8_t buf[READ_CHUNK];
    if (!c->pcb || c->connecting) {
        return 0;
    }
    if (c->to_pcb_len > c->to_pcb_off) {
        int rc = flush_to_pcb(c);
        if (rc != 0 || c->to_pcb_len > c->to_pcb_off) {
            return rc;
        }
    }
    for (int iter = 0; iter < 2; iter++) {
        if (c->fd_eof) {
            sync_epoll(c);
            return 0;
        }
        ssize_t n = read(c->fd, buf, sizeof buf);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                sync_epoll(c);
                return 0;
            }
            if (errno == EINTR) {
                continue;
            }
            conn_free(c, 1);
            return -1;
        }
        if (n == 0) {
            c->fd_eof = 1;
            return flush_to_pcb(c);
        }
        size_t take = (size_t)n;
        size_t written = 0;
        err_t e = write_to_pcb(c->pcb, buf, take, &written);
        if (e != ERR_OK) {
            conn_free(c, 1);
            return -1;
        }
        if (written > 0) {
            tcp_output(c->pcb);
        }
        if (written < take) {
            size_t keep = take - written;
            if (!c->to_pcb) {
                c->to_pcb = malloc(PENDING_CAP);
                if (!c->to_pcb) {
                    conn_free(c, 1);
                    return -1;
                }
            }
            memcpy(c->to_pcb, buf + written, keep);
            c->to_pcb_len = keep;
            c->to_pcb_off = 0;
            sync_epoll(c);
            return 0;
        }
    }
    sync_epoll(c);
    return 0;
}

static err_t
on_recv(void *arg, struct tcp_pcb *pcb, struct pbuf *p, err_t err)
{
    struct conn *c = arg;
    (void)err;
    if (!c) {
        if (p) {
            pbuf_free(p);
        }
        return ERR_OK;
    }
    if (!p) {
        c->pcb_fin = 1;
        if (c->to_fd_off >= c->to_fd_len && c->fd >= 0) {
            shutdown(c->fd, SHUT_WR);
        }
        if (c->fd_eof && c->to_pcb_off >= c->to_pcb_len) {
            conn_free(c, 0);
            return ERR_OK;
        }
        sync_epoll(c);
        return ERR_OK;
    }
    if (c->fd < 0 || c->connecting || c->to_fd_off < c->to_fd_len) {
        for (struct pbuf *q = p; q; q = q->next) {
            if (queue_to_fd(c, q->payload, q->len) != 0) {
                pbuf_free(p);
                conn_free(c, 1);
                return ERR_ABRT;
            }
        }
        pbuf_free(p);
        sync_epoll(c);
        return ERR_OK;
    }
    for (struct pbuf *q = p; q; q = q->next) {
        const uint8_t *data = q->payload;
        size_t left = q->len;
        while (left) {
            ssize_t w = write(c->fd, data, left);
            if (w < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    if (queue_to_fd(c, data, left) != 0) {
                        pbuf_free(p);
                        conn_free(c, 1);
                        return ERR_ABRT;
                    }
                    for (struct pbuf *r = q->next; r; r = r->next) {
                        if (queue_to_fd(c, r->payload, r->len) != 0) {
                            pbuf_free(p);
                            conn_free(c, 1);
                            return ERR_ABRT;
                        }
                    }
                    pbuf_free(p);
                    sync_epoll(c);
                    return ERR_OK;
                }
                if (errno == EINTR) {
                    continue;
                }
                pbuf_free(p);
                conn_free(c, 1);
                return ERR_ABRT;
            }
            tcp_recved(pcb, (u16_t)w);
            data += w;
            left -= (size_t)w;
        }
    }
    pbuf_free(p);
    return ERR_OK;
}

static err_t
on_sent(void *arg, struct tcp_pcb *pcb, u16_t len)
{
    struct conn *c = arg;
    (void)pcb;
    (void)len;
    if (c) {
        int rc = flush_to_pcb(c);
        if (rc < 0) {
            return ERR_ABRT;
        }
        if (rc == 0 && read_from_fd(c) < 0) {
            return ERR_ABRT;
        }
    }
    return ERR_OK;
}

static err_t
on_poll(void *arg, struct tcp_pcb *pcb)
{
    struct conn *c = arg;
    (void)pcb;
    if (c) {
        int rc = flush_to_fd(c);
        if (rc < 0) {
            return ERR_ABRT;
        }
        if (rc == 0) {
            rc = flush_to_pcb(c);
            if (rc < 0) {
                return ERR_ABRT;
            }
        }
        if (rc == 0 && read_from_fd(c) < 0) {
            return ERR_ABRT;
        }
    }
    return ERR_OK;
}

static void
on_err(void *arg, err_t err)
{
    struct conn *c = arg;
    (void)err;
    if (!c) {
        return;
    }
    c->pcb = NULL;
    conn_free(c, 0);
}

static err_t
on_accept(void *arg, struct tcp_pcb *newpcb, err_t err)
{
    (void)arg;
    if (err != ERR_OK || !newpcb) {
        return ERR_VAL;
    }
    struct conn *c = calloc(1, sizeof *c);
    if (!c) {
        tcp_abort(newpcb);
        return ERR_ABRT;
    }
    int fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0 || fd >= FD_MAX) {
        if (fd >= 0) {
            close(fd);
        }
        free(c);
        tcp_abort(newpcb);
        return ERR_ABRT;
    }
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    int rc = connect(fd, (struct sockaddr *)&upstream_sa, sizeof upstream_sa);
    if (rc < 0 && errno != EINPROGRESS) {
        close(fd);
        free(c);
        tcp_abort(newpcb);
        return ERR_ABRT;
    }
    c->pcb = newpcb;
    c->fd = fd;
    c->connecting = (rc < 0);
    c->ep_events = c->connecting ? EPOLLOUT : (EPOLLIN | EPOLLRDHUP);
    tcp_nagle_disable(newpcb);
    tcp_arg(newpcb, c);
    tcp_recv(newpcb, on_recv);
    tcp_sent(newpcb, on_sent);
    tcp_err(newpcb, on_err);
    tcp_poll(newpcb, on_poll, 1);
    struct epoll_event ev = { .events = c->ep_events, .data.fd = fd };
    if (epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &ev) != 0) {
        close(fd);
        c->fd = -1;
        free(c);
        tcp_abort(newpcb);
        return ERR_ABRT;
    }
    by_fd[fd] = c;
    return ERR_OK;
}

static void
on_conn_event(struct conn *c, uint32_t events)
{
    if (!c) {
        return;
    }
    if (events & (EPOLLERR | EPOLLHUP)) {
        if (c->connecting) {
            conn_free(c, 1);
            return;
        }
    }
    if (c->connecting && (events & EPOLLOUT)) {
        int soerr = 0;
        socklen_t sl = sizeof soerr;
        getsockopt(c->fd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
        if (soerr != 0) {
            conn_free(c, 1);
            return;
        }
        c->connecting = 0;
        if (flush_to_fd(c) != 0) {
            return;
        }
    } else if (events & EPOLLOUT) {
        if (flush_to_fd(c) != 0) {
            return;
        }
        if (flush_to_pcb(c) != 0) {
            return;
        }
    }
    if (events & (EPOLLIN | EPOLLRDHUP | EPOLLHUP)) {
        read_from_fd(c);
    }
}

static int
open_tun(const char *name)
{
    int fd = open("/dev/net/tun", O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        die("open /dev/net/tun: %s", strerror(errno));
    }
    struct ifreq ifr;
    memset(&ifr, 0, sizeof ifr);
    ifr.ifr_flags = IFF_TUN | IFF_NO_PI;
    snprintf(ifr.ifr_name, IFNAMSIZ, "%s", name);
    if (ioctl(fd, TUNSETIFF, &ifr) < 0) {
        die("TUNSETIFF: %s", strerror(errno));
    }
    set_nonblock(fd);
    return fd;
}

static void
parse_upstream(const char *s)
{
    char host[128];
    unsigned port = 0;
    if (sscanf(s, "%127[^:]:%u", host, &port) != 2 || port == 0 || port > 65535) {
        die("bad --upstream %s (want HOST:PORT)", s);
    }
    memset(&upstream_sa, 0, sizeof upstream_sa);
    upstream_sa.sin_family = AF_INET;
    upstream_sa.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, host, &upstream_sa.sin_addr) != 1) {
        die("bad upstream host %s", host);
    }
}

static void
usage(void)
{
    fprintf(stderr,
            "usage: lwip-tun-proxy [--tun NAME] [--addr CIDR] [--listen IP] [--port N]\n"
            "                      [--upstream HOST:PORT] [--mtu N]\n");
    exit(2);
}

int
main(int argc, char **argv)
{
    const char *upstream = "127.0.0.1:5201";
    static const struct option opts[] = {
        { "tun", required_argument, NULL, 't' },
        { "addr", required_argument, NULL, 'a' },
        { "listen", required_argument, NULL, 'l' },
        { "port", required_argument, NULL, 'p' },
        { "upstream", required_argument, NULL, 'u' },
        { "mtu", required_argument, NULL, 'm' },
        { "help", no_argument, NULL, 'h' },
        { 0, 0, 0, 0 },
    };
    int c;
    while ((c = getopt_long(argc, argv, "t:a:l:p:u:m:h", opts, NULL)) != -1) {
        switch (c) {
        case 't':
            snprintf(tun_name, sizeof tun_name, "%s", optarg);
            break;
        case 'a':
            snprintf(tun_cidr, sizeof tun_cidr, "%s", optarg);
            break;
        case 'l':
            snprintf(listen_ip, sizeof listen_ip, "%s", optarg);
            break;
        case 'p':
            listen_port = (uint16_t)atoi(optarg);
            break;
        case 'u':
            upstream = optarg;
            break;
        case 'm':
            mtu = atoi(optarg);
            if (mtu < 576) {
                mtu = 576;
            }
            if (mtu > 65535) {
                mtu = 65535;
            }
            break;
        default:
            usage();
        }
    }
    parse_upstream(upstream);

    struct rlimit rl = { .rlim_cur = 262144, .rlim_max = 262144 };
    setrlimit(RLIMIT_NOFILE, &rl);
    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);

    sh("ip link del %s 2>/dev/null", tun_name);
    tun_fd = open_tun(tun_name);
    sh("ip addr add %s dev %s", tun_cidr, tun_name);
    sh("ip link set %s mtu %d txqueuelen 500 up", tun_name, mtu);

    lwip_init();
    ip4_addr_t ip, mask, gw;
    ip4addr_aton(listen_ip, &ip);
    IP4_ADDR(&mask, 255, 254, 0, 0);
    ip4addr_aton("172.19.0.1", &gw);
    netif_add(&tun_netif, &ip, &mask, &gw, NULL, tun_init, ip_input);
    netif_set_default(&tun_netif);
    netif_set_up(&tun_netif);

    struct tcp_pcb *pcb = tcp_new();
    if (!pcb) {
        die("tcp_new failed");
    }
    if (tcp_bind(pcb, IP_ADDR_ANY, listen_port) != ERR_OK) {
        die("tcp_bind %s:%u failed", listen_ip, listen_port);
    }
    pcb = tcp_listen(pcb);
    tcp_accept(pcb, on_accept);

    epfd = epoll_create1(EPOLL_CLOEXEC);
    struct epoll_event ev = { .events = EPOLLIN, .data.fd = tun_fd };
    epoll_ctl(epfd, EPOLL_CTL_ADD, tun_fd, &ev);

    fprintf(stderr, "lwip-tun-proxy: ready on %s mtu %d -> %s (lwIP STABLE-2_2_1)\n", tun_name, mtu, upstream);

    struct epoll_event events[128];
    int tun_more = 0;
    while (!stop_flag) {
        u32_t sleeptime = sys_timeouts_sleeptime();
        int timeout = 10;
        if (tun_more) {
            timeout = 0;
        } else if (sleeptime != 0xffffffffu) {
            timeout = (int)sleeptime;
            if (timeout < 0) {
                timeout = 0;
            }
            if (timeout > 50) {
                timeout = 50;
            }
        }
        int n = epoll_wait(epfd, events, 128, timeout);
        sys_check_timeouts();
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        int saw_tun = 0;
        for (int i = 0; i < n; i++) {
            int fd = events[i].data.fd;
            if (fd == tun_fd) {
                saw_tun = 1;
                tun_more = tun_pump();
            } else if (fd >= 0 && fd < FD_MAX) {
                on_conn_event(by_fd[fd], events[i].events);
            }
        }
        if (!saw_tun && tun_more) {
            tun_more = tun_pump();
        }
    }
    return 0;
}
