# zfbench report: 2026-09-24-wg-smoke-veth

- generated: 2026-09-24T11:08:06
- matrix: `{"name": "wgsmoke", "bin": "/home/user/zfstack/target/release/zfbench", "stacks": ["kernel-cubic", "smoltcp-cubic", "zfstack-cubic", "zfstack-bbr"], "baseline": "smoltcp-cubic", "tests": ["down", "up", "mixed", "connect"], "rate": 200.0, "rtts": [1.0, 12.0, 80.0], "losses": [0.0, 0.01], "queues": [2.0, 0.25], "flows": [1, 8], "mixed_rtts": [12.0, 80.0], "mixed_queues": [2.0, 0.25], "connect_rtt": 12.0, "conns": 300, "concurrency": 64, "secs": 6, "warmup": 2, "reps": 1, "seed": 1, "client_cc": "cubic", "quick": false, "resume": false, "outdir": "/tmp/claude-0/-home-user-zfstack/9625fbf2-2b1b-5027-a7c7-4a9537e57900/scratchpad/wgsmoke", "report_only": null, "dry_run": false, "wg_server": "10.210.0.2", "client_wg": "auto", "extra": []}`
- uname: `Linux vm 6.18.44-fc-v37 #1 SMP PREEMPT_DYNAMIC @0 x86_64 x86_64 x86_64 GNU/Linux`
- nproc: `4`
- zfstack_commit: `8e012cfa9561bcc8a5b8cbd4de8beb176912704f`
- smoltcp_rev: `8014f8b21e12faf89b3b453ceea32027344721af`
- net.ipv4.tcp_rmem: `4096 131072 33554432`
- net.ipv4.tcp_wmem: `4096 16384 4194304`
- net.ipv4.tcp_congestion_control: `bbr`

Cells show **median [min–max]** over repetitions. ⚠n = n runs crashed/timed out or failed a correctness check (see the .log / JSON `errors`). Ratio columns = median(stack) / median(smoltcp-cubic).

Goodput = bytes delivered to the receiving application, mean of the per-second samples after warmup (inner MTU 1420).

WG link mode: real network through WireGuard, no emulator. The RTT column is the tunnel RTT from TCP connect probes at the start of each run (the row shows the first run's value; the Tunnel section has the spread). `stack+WG thread` = the server thread doing UDP, WireGuard and TCP for the userspace stacks (empty for kernel); `server machine` = /proc/stat busy time on the server, the only fair CPU figure for the kernel baseline.

## down

### down: goodput Mbps

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 0.3 | 1 | 1296 | 1310 | 1277 | 1304 | 0.99 | 0.97 | 1.00 |
| 0.3 | 8 | 1345 | 1030 | 1351 | 1375 | 1.31 | 1.31 | 1.33 |

### down: zero-tput s

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 0 | 0 | 0 | 0 |
| 0.3 | 8 | 0 | 0 | 0 | 0 |

### down: stack+WG thread CPU s/GB

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | – | 3.20 | 3.59 | 3.34 |
| 0.3 | 8 | – | 3.46 | 3.50 | 3.38 |

### down: server machine CPU s/GB

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 13.2 | 11.3 | 11.6 | 11.5 |
| 0.3 | 8 | 12.4 | 14.1 | 11.6 | 11.2 |

## up

### up: goodput Mbps

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 0.4 | 1 | 1262 | 2532 | 2558 | 2551 | 0.50 | 1.01 | 1.01 |
| 0.3 | 8 | 1481 | 1978 | 1828 | 1955 | 0.75 | 0.92 | 0.99 |

### up: zero-tput s

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.4 | 1 | 0 | 0 | 0 | 0 |
| 0.3 | 8 | 0 | 0 | 0 | 0 |

### up: stack+WG thread CPU s/GB

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.4 | 1 | – | 1.97 | 2.24 | 2.23 |
| 0.3 | 8 | – | 2.29 | 2.45 | 2.42 |

### up: server machine CPU s/GB

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.4 | 1 | 13.0 | 5.95 | 6.20 | 6.25 |
| 0.3 | 8 | 11.0 | 7.23 | 7.49 | 7.18 |

## mixed

### mixed: RR P50 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 22.5 | 24.4 | 111 | 9.04 |

### mixed: RR P99 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 0.3 | 1 | 35.7 | 34.8 | 135 | 18.2 | 1.03 | 3.87 | 0.52 |

### mixed: RR P99.9 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 35.7 | 34.8 | 135 | 18.2 |

### mixed: idle RR P50 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 0.43 | 0.35 | 0.36 | 0.36 |

### mixed: goodput Mbps

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 1253 | 1356 | 1280 | 1273 |

## connect

### connect: success

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 0.3 | 1 | 300 | 101 | 300 | 300 | 2.97 | 2.97 | 2.97 |

### connect: refused

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 0 | 199 | 0 | 0 |

### connect: timeout

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 0 | 0 | 0 | 0 |

### connect: reset

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 0 | 0 | 0 | 0 |

### connect: P50 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 8.01 | 66.3 | 2.09 | 3.83 |

### connect: P99 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 0.3 | 1 | 18.7 | 172 | 12.4 | 16.1 |

## Tunnel

- tunnel RTT (connect probes): median 0.3 ms, range 0.2–1.2 ms over 24 runs
- WireGuard implementations: client userspace, server stack-thread; client userspace, server userspace
