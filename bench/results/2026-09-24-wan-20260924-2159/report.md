# zfbench report: 2026-09-24-wan-20260924-2159

- generated: 2026-09-24T22:41:19
- matrix: `{"name": "wan-20260924-2159", "bin": "/root/zfstack-bench/target/release/zfbench", "stacks": ["kernel-cubic", "kernel-bbr", "smoltcp-cubic", "zfstack-cubic", "zfstack-bbr"], "baseline": "smoltcp-cubic", "tests": ["down", "up", "mixed", "connect"], "rate": 200.0, "rtts": [1.0, 12.0, 80.0], "losses": [0.0, 0.01], "queues": [2.0, 0.25], "flows": [1, 8], "mixed_rtts": [12.0, 80.0], "mixed_queues": [2.0, 0.25], "connect_rtt": 12.0, "conns": 2000, "concurrency": 64, "secs": 30, "warmup": 3, "reps": 3, "seed": 1, "client_cc": "cubic", "quick": false, "resume": false, "outdir": null, "report_only": null, "dry_run": false, "wg_server": "192.99.148.123", "client_wg": "auto", "extra": []}`
- uname: `Linux pa.blestic.com 6.1.0-29-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.1.123-1 (2025-01-02) x86_64 GNU/Linux`
- nproc: `4`
- zfstack_commit: `be334ee`
- smoltcp_rev: `8014f8b21e12faf89b3b453ceea32027344721af`
- net.ipv4.tcp_rmem: `4096 87380 26214400`
- net.ipv4.tcp_wmem: `4096 16384 26214400`
- net.ipv4.tcp_congestion_control: `bbr`

Cells show **median [min–max]** over repetitions. ⚠n = n runs crashed/timed out or failed a correctness check (see the .log / JSON `errors`). Ratio columns = median(stack) / median(smoltcp-cubic).

Goodput = bytes delivered to the receiving application, mean of the per-second samples after warmup (inner MTU 1420).

WG link mode: real network through WireGuard, no emulator. The RTT column is the tunnel RTT from TCP connect probes at the start of each run (the row shows the first run's value; the Tunnel section has the spread). `stack+WG thread` = the server thread doing UDP, WireGuard and TCP for the userspace stacks (empty for kernel); `server machine` = /proc/stat busy time on the server, the only fair CPU figure for the kernel baseline.

## down

### down: goodput Mbps

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | kernel-bbr/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|---|---|
| 12.3 | 1 | 434 [428–438] | 456 [456–456] | 192 [150–193] | 452 [452–454] | 457 [456–457] | 2.27 | 2.38 | 2.36 | 2.38 |
| 12.3 | 8 | 456 [456–457] | 457 [456–457] | 440 [439–441] | 457 [457–457] | 457 [457–457] | 1.04 | 1.04 | 1.04 | 1.04 |

### down: zero-tput s

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.3 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |
| 12.3 | 8 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### down: stack+WG thread CPU s/GB

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.3 | 1 | – | – | 10.5 [10.0–10.7] | 9.00 [8.09–9.30] | 7.04 [6.26–7.89] |
| 12.3 | 8 | – | – | 9.88 [9.35–10.2] | 8.35 [7.79–9.15] | 8.45 [8.29–9.55] |

### down: server machine CPU s/GB

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.3 | 1 | 17.1 [14.5–19.7] | 18.4 [15.2–19.3] | 31.7 [30.2–54.6] | 18.9 [14.8–20.0] | 13.4 [12.6–18.6] |
| 12.3 | 8 | 16.8 [15.5–20.4] | 17.5 [16.6–20.7] | 20.0 [19.9–22.1] | 18.8 [17.9–19.8] | 17.1 [16.4–20.7] |

## up

### up: goodput Mbps

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | kernel-bbr/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|---|---|
| 12.2 | 1 | 864 [700–895] | 863 [586–897] | 906 [848–909] | 900 [820–900] | 897 [894–901] | 0.95 | 0.95 | 0.99 | 0.99 |
| 12.3 | 8 | 901 [872–901] | 901 [901–901] | 909 [900–909] | 901 [889–901] | 890 [881–901] | 0.99 | 0.99 | 0.99 | 0.98 |

### up: zero-tput s

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |
| 12.3 | 8 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### up: stack+WG thread CPU s/GB

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | – | – | 4.75 [4.61–4.90] | 4.76 [4.55–5.49] | 5.09 [4.60–5.38] |
| 12.3 | 8 | – | – | 4.33 [4.14–4.61] | 5.08 [4.66–5.66] | 5.02 [4.84–5.06] |

### up: server machine CPU s/GB

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 14.6 [12.3–15.9] | 14.3 [12.7–15.7] | 10.3 [10.3–11.6] | 10.6 [10.1–10.8] | 11.3 [11.3–11.4] |
| 12.3 | 8 | 15.7 [14.2–16.4] | 14.4 [12.8–14.6] | 11.1 [10.3–13.5] | 13.1 [10.2–15.3] | 11.6 [11.4–12.3] |

## mixed

### mixed: RR P50 ms

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 12.1 [12.1–12.1] | 12.1 [12.1–12.2] | 12.1 [12.1–12.1] | 12.0 [12.0–12.2] | 12.2 [12.1–12.2] |

### mixed: RR P99 ms

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | kernel-bbr/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|---|---|
| 12.2 | 1 | 14.8 [13.5–15.4] | 240 [236–246] | 14.1 [13.4–16.7] | 15.4 [13.6–15.8] | 38.6 [21.7–46.9] | 1.05 | 17.06 | 1.10 | 2.75 |

### mixed: RR P99.9 ms

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 15.9 [15.7–16.5] | 240 [240–464] | 17.2 [16.8–1012] | 25.2 [18.5–27.8] | 39.3 [38.6–239] |

### mixed: idle RR P50 ms

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 12.3 [12.3–12.3] | 12.3 [12.3–12.4] | 12.2 [12.2–12.3] | 12.2 [12.2–12.3] | 12.2 [12.2–12.2] |

### mixed: goodput Mbps

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 449 [436–452] | 456 [455–456] | 174 [170–190] | 454 [450–455] | 456 [456–457] |

## connect

### connect: success

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | kernel-bbr/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|---|---|
| 12.2 | 1 | 2000 [2000–2000] | 2000 [2000–2000] | 838 [821–1063] | 2000 [2000–2000] | 2000 [2000–2000] | 2.39 | 2.39 | 2.39 | 2.39 |

### connect: refused

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 0 [0–0] | 0 [0–0] | 1162 [937–1179] | 0 [0–0] | 0 [0–0] |

### connect: timeout

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### connect: reset

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### connect: P50 ms

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 24.2 [24.2–24.3] | 24.1 [24.0–24.3] | 41.1 [39.1–41.3] | 24.1 [24.1–24.2] | 24.1 [24.1–24.3] |

### connect: P99 ms

| RTT ms | flows | kernel-cubic | kernel-bbr | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12.2 | 1 | 25.5 [25.3–26.8] | 28.3 [26.8–29.1] | 99.3 [96.9–102] | 27.0 [26.6–27.0] | 25.7 [25.7–27.1] |

## Tunnel

- tunnel RTT (connect probes): median 12.2 ms, range 12.1–13.6 ms over 90 runs
- WireGuard implementations: client kernel, server kernel; client kernel, server stack-thread
