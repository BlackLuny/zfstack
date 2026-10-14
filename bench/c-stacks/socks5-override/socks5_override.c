/* Minimal SOCKS5 server that ignores the requested destination and always
 * connects to --override HOST:PORT. Used so hev-socks5-tunnel (C tun2socks)
 * can be measured on the same 198.18.0.2:5201 → 127.0.0.1:5201 topology.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/epoll.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <unistd.h>

static struct sockaddr_in override_sa;
static volatile sig_atomic_t stop_flag;

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

struct sess {
    int cfd, ufd;
    uint8_t in[512];
    size_t in_n;
    int stage; /* 0 greet, 1 req, 2 connecting, 3 relay */
    uint8_t *c2u, *u2c;
    size_t c2u_n, u2c_n, c2u_cap, u2c_cap;
};

static void
sess_free(struct sess *s)
{
    if (!s) {
        return;
    }
    if (s->cfd >= 0) {
        close(s->cfd);
    }
    if (s->ufd >= 0) {
        close(s->ufd);
    }
    free(s->c2u);
    free(s->u2c);
    free(s);
}

static ssize_t
full_read_some(int fd, uint8_t *buf, size_t cap, size_t *n)
{
    ssize_t r = read(fd, buf + *n, cap - *n);
    if (r > 0) {
        *n += (size_t)r;
    }
    return r;
}

static int
ensure_buf(uint8_t **q, size_t *cap, size_t need)
{
    if (*cap >= need) {
        return 0;
    }
    size_t ncap = need < 16384 ? 16384 : need;
    uint8_t *n = realloc(*q, ncap);
    if (!n) {
        return -1;
    }
    *q = n;
    *cap = ncap;
    return 0;
}

static int
relay_copy(int from, int to, uint8_t **q, size_t *qn, size_t *cap)
{
    if (*qn) {
        ssize_t w = write(to, *q, *qn);
        if (w < 0) {
            return (errno == EAGAIN || errno == EINTR) ? 0 : -1;
        }
        memmove(*q, *q + w, *qn - (size_t)w);
        *qn -= (size_t)w;
        if (*qn) {
            return 0;
        }
    }
    uint8_t buf[16384];
    ssize_t r = read(from, buf, sizeof buf);
    if (r < 0) {
        return (errno == EAGAIN || errno == EINTR) ? 0 : -1;
    }
    if (r == 0) {
        shutdown(to, SHUT_WR);
        return 1;
    }
    ssize_t w = write(to, buf, (size_t)r);
    if (w < 0) {
        if (errno == EAGAIN || errno == EINTR) {
            if (ensure_buf(q, cap, (size_t)r) != 0) {
                return -1;
            }
            memcpy(*q, buf, (size_t)r);
            *qn = (size_t)r;
            return 0;
        }
        return -1;
    }
    if ((size_t)w < (size_t)r) {
        size_t left = (size_t)r - (size_t)w;
        if (ensure_buf(q, cap, left) != 0) {
            return -1;
        }
        memcpy(*q, buf + w, left);
        *qn = left;
    }
    return 0;
}

static int
parse_override(const char *s)
{
    char host[128];
    unsigned port = 0;
    if (sscanf(s, "%127[^:]:%u", host, &port) != 2 || port == 0 || port > 65535) {
        return -1;
    }
    memset(&override_sa, 0, sizeof override_sa);
    override_sa.sin_family = AF_INET;
    override_sa.sin_port = htons((uint16_t)port);
    return inet_pton(AF_INET, host, &override_sa.sin_addr) == 1 ? 0 : -1;
}

int
main(int argc, char **argv)
{
    const char *listen_addr = "127.0.0.1:18721";
    const char *override = "127.0.0.1:5201";
    static const struct option opts[] = {
        { "listen", required_argument, NULL, 'l' },
        { "override", required_argument, NULL, 'o' },
        { 0, 0, 0, 0 },
    };
    int opt;
    while ((opt = getopt_long(argc, argv, "l:o:", opts, NULL)) != -1) {
        switch (opt) {
        case 'l':
            listen_addr = optarg;
            break;
        case 'o':
            override = optarg;
            break;
        default:
            fprintf(stderr, "usage: socks5-override --listen HOST:PORT --override HOST:PORT\n");
            return 2;
        }
    }
    if (parse_override(override) != 0) {
        fprintf(stderr, "bad --override\n");
        return 2;
    }
    char lhost[128];
    unsigned lport = 0;
    if (sscanf(listen_addr, "%127[^:]:%u", lhost, &lport) != 2) {
        fprintf(stderr, "bad --listen\n");
        return 2;
    }

    struct rlimit rl = { .rlim_cur = 262144, .rlim_max = 262144 };
    setrlimit(RLIMIT_NOFILE, &rl);
    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, on_sig);
    signal(SIGTERM, on_sig);

    int lfd = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
    int one = 1;
    setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in la = { .sin_family = AF_INET, .sin_port = htons((uint16_t)lport) };
    if (inet_pton(AF_INET, lhost, &la.sin_addr) != 1) {
        perror("listen host");
        return 1;
    }
    if (bind(lfd, (struct sockaddr *)&la, sizeof la) < 0 || listen(lfd, 1024) < 0) {
        perror("bind/listen");
        return 1;
    }
    set_nonblock(lfd);
    int ep = epoll_create1(EPOLL_CLOEXEC);
    struct epoll_event ev = { .events = EPOLLIN, .data.ptr = NULL };
    /* data.fd = lfd, ptr NULL means listener; store lfd in data.u32 */
    ev.data.u64 = (uint64_t)(uint32_t)lfd;
    epoll_ctl(ep, EPOLL_CTL_ADD, lfd, &ev);
    fprintf(stderr, "socks5-override: listen %s override %s\n", listen_addr, override);

    struct epoll_event events[64];
    while (!stop_flag) {
        int n = epoll_wait(ep, events, 64, 200);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        void *dead[64];
        int ndead = 0;
#define DROP(ss)                                                                                   \
    do {                                                                                           \
        if (ndead < 64)                                                                            \
            dead[ndead++] = (ss);                                                                  \
        sess_free(ss);                                                                             \
    } while (0)
        for (int i = 0; i < n; i++) {
            uint64_t tag = events[i].data.u64;
            if ((uint32_t)tag == (uint32_t)lfd && tag < (1ull << 32)) {
                for (;;) {
                    int cfd = accept4(lfd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
                    if (cfd < 0) {
                        break;
                    }
                    setsockopt(cfd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
                    struct sess *s = calloc(1, sizeof *s);
                    s->cfd = cfd;
                    s->ufd = -1;
                    struct epoll_event cev = { .events = EPOLLIN, .data.ptr = s };
                    epoll_ctl(ep, EPOLL_CTL_ADD, cfd, &cev);
                }
                continue;
            }
            struct sess *s = events[i].data.ptr;
            if (!s) {
                continue;
            }
            int skip = 0;
            for (int d = 0; d < ndead; d++) {
                if (dead[d] == s) {
                    skip = 1;
                    break;
                }
            }
            if (skip) {
                continue;
            }
            if (s->stage == 0) {
                ssize_t r = full_read_some(s->cfd, s->in, sizeof s->in, &s->in_n);
                if (r == 0 || (r < 0 && errno != EAGAIN)) {
                    epoll_ctl(ep, EPOLL_CTL_DEL, s->cfd, NULL);
                    DROP(s);
                    continue;
                }
                if (s->in_n >= 2 && s->in[0] == 5) {
                    uint8_t nmethods = s->in[1];
                    if (s->in_n >= 2u + nmethods) {
                        uint8_t rep[2] = { 5, 0 };
                        if (write(s->cfd, rep, 2) != 2) {
                            epoll_ctl(ep, EPOLL_CTL_DEL, s->cfd, NULL);
                            DROP(s);
                            continue;
                        }
                        s->stage = 1;
                        s->in_n = 0;
                    }
                }
            }
            if (s->stage == 1) {
                ssize_t r = full_read_some(s->cfd, s->in, sizeof s->in, &s->in_n);
                if (r == 0 || (r < 0 && errno != EAGAIN && s->in_n == 0)) {
                    epoll_ctl(ep, EPOLL_CTL_DEL, s->cfd, NULL);
                    DROP(s);
                    continue;
                }
                if (s->in_n >= 4 && s->in[0] == 5 && s->in[1] == 1) {
                    uint8_t atyp = s->in[3];
                    size_t need = 4;
                    if (atyp == 1) {
                        need = 10;
                    } else if (atyp == 3 && s->in_n >= 5) {
                        need = 5u + s->in[4] + 2u;
                    } else if (atyp == 4) {
                        need = 22;
                    } else {
                        epoll_ctl(ep, EPOLL_CTL_DEL, s->cfd, NULL);
                        DROP(s);
                        continue;
                    }
                    if (s->in_n < need) {
                        continue;
                    }
                    int ufd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
                    setsockopt(ufd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
                    int rc = connect(ufd, (struct sockaddr *)&override_sa, sizeof override_sa);
                    if (rc < 0 && errno != EINPROGRESS) {
                        close(ufd);
                        epoll_ctl(ep, EPOLL_CTL_DEL, s->cfd, NULL);
                        DROP(s);
                        continue;
                    }
                    s->ufd = ufd;
                    s->stage = 2;
                    struct epoll_event uev = { .events = EPOLLOUT, .data.ptr = s };
                    epoll_ctl(ep, EPOLL_CTL_ADD, ufd, &uev);
                }
            }
            if (s->stage == 2 && s->ufd >= 0 && (events[i].events & EPOLLOUT) && events[i].data.ptr == s) {
                int soerr = 0;
                socklen_t sl = sizeof soerr;
                getsockopt(s->ufd, SOL_SOCKET, SO_ERROR, &soerr, &sl);
                if (soerr != 0) {
                    epoll_ctl(ep, EPOLL_CTL_DEL, s->cfd, NULL);
                    epoll_ctl(ep, EPOLL_CTL_DEL, s->ufd, NULL);
                    DROP(s);
                    continue;
                }
                uint8_t rep[10] = { 5, 0, 0, 1, 0, 0, 0, 0, 0, 0 };
                if (write(s->cfd, rep, 10) != 10) {
                    epoll_ctl(ep, EPOLL_CTL_DEL, s->cfd, NULL);
                    epoll_ctl(ep, EPOLL_CTL_DEL, s->ufd, NULL);
                    DROP(s);
                    continue;
                }
                s->stage = 3;
                struct epoll_event cev = { .events = EPOLLIN | EPOLLOUT | EPOLLRDHUP, .data.ptr = s };
                epoll_ctl(ep, EPOLL_CTL_MOD, s->cfd, &cev);
                epoll_ctl(ep, EPOLL_CTL_MOD, s->ufd, &cev);
            }
            if (s->stage == 3) {
                int a = relay_copy(s->cfd, s->ufd, &s->c2u, &s->c2u_n, &s->c2u_cap);
                int b = relay_copy(s->ufd, s->cfd, &s->u2c, &s->u2c_n, &s->u2c_cap);
                if (a < 0 || b < 0 || (a > 0 && b > 0)) {
                    epoll_ctl(ep, EPOLL_CTL_DEL, s->cfd, NULL);
                    if (s->ufd >= 0) {
                        epoll_ctl(ep, EPOLL_CTL_DEL, s->ufd, NULL);
                    }
                    DROP(s);
                }
            }
        }
#undef DROP
    }
    return 0;
}
