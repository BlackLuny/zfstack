# zfbench report: 2026-09-24-wan-20260924-2018

- generated: 2026-09-24T20:51:28
- matrix: `{"name": "wan-20260924-2018", "bin": "/root/zfstack-bench/target/release/zfbench", "stacks": ["kernel-cubic", "smoltcp-cubic", "zfstack-cubic", "zfstack-bbr"], "baseline": "smoltcp-cubic", "tests": ["down", "up", "mixed", "connect"], "rate": 200.0, "rtts": [1.0, 12.0, 80.0], "losses": [0.0, 0.01], "queues": [2.0, 0.25], "flows": [1, 8], "mixed_rtts": [12.0, 80.0], "mixed_queues": [2.0, 0.25], "connect_rtt": 12.0, "conns": 2000, "concurrency": 64, "secs": 30, "warmup": 3, "reps": 3, "seed": 1, "client_cc": "cubic", "quick": false, "resume": false, "outdir": null, "report_only": null, "dry_run": false, "wg_server": "192.99.148.123", "client_wg": "auto", "extra": []}`
- uname: `Linux pa.blestic.com 6.1.0-29-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.1.123-1 (2025-01-02) x86_64 GNU/Linux`
- nproc: `4`
- zfstack_commit: ``
- smoltcp_rev: `8014f8b21e12faf89b3b453ceea32027344721af`
- net.ipv4.tcp_rmem: `4096 87380 26214400`
- net.ipv4.tcp_wmem: `4096 16384 26214400`
- net.ipv4.tcp_congestion_control: `bbr`

Cells show **median [min–max]** over repetitions. ⚠n = n runs crashed/timed out or failed a correctness check (see the .log / JSON `errors`). Ratio columns = median(stack) / median(smoltcp-cubic).

Goodput = bytes delivered to the receiving application, mean of the per-second samples after warmup (inner MTU 1420).

WG link mode: real network through WireGuard, no emulator. The RTT column is the tunnel RTT from TCP connect probes at the start of each run (the row shows the first run's value; the Tunnel section has the spread). `stack+WG thread` = the server thread doing UDP, WireGuard and TCP for the userspace stacks (empty for kernel); `server machine` = /proc/stat busy time on the server, the only fair CPU figure for the kernel baseline.

## down

### down: goodput Mbps

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12.2 | 1 | 447 [437–449] | 190 [164–202] | 452 [450–454] | 456 [456–458] | 2.36 | 2.38 | 2.41 |
| 12.3 | 8 | 457 [457–457] | 443 [436–444] | 456 [456–457] | 456 [456–456] | 1.03 | 1.03 | 1.03 |

### down: zero-tput s

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.2 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |
| 12.3 | 8 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### down: stack+WG thread CPU s/GB

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.2 | 1 | – | 9.53 [8.80–9.63] | 7.94 [7.62–8.58] | 7.34 [7.30–7.71] |
| 12.3 | 8 | – | 9.60 [9.38–9.83] | 8.63 [7.82–8.92] | 8.76 [8.05–8.85] |

### down: server machine CPU s/GB

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.2 | 1 | 14.0 [12.2–15.8] | 38.6 [37.3–46.0] | 18.6 [17.4–19.8] | 18.7 [16.7–19.4] |
| 12.3 | 8 | 19.3 [17.5–20.3] | 20.9 [20.0–22.0] | 16.7 [16.1–20.1] | 20.0 [15.8–22.4] |

## up

### up: goodput Mbps

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12.3 | 1 | 848 [612–892] | 904 [902–907] | 853 [643–901] | 899 [899–900] | 0.94 | 0.94 | 1.00 |
| 12.2 | 8 | 881 [879–899] | 909 [908–909] | 901 [901–901] | 896 [895–897] | 0.97 | 0.99 | 0.99 |

### up: zero-tput s

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.3 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |
| 12.2 | 8 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### up: stack+WG thread CPU s/GB

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.3 | 1 | – | 4.86 [4.77–5.52] | 5.25 [4.53–5.71] | 4.84 [4.42–5.47] |
| 12.2 | 8 | – | 4.88 [4.86–5.07] | 4.93 [4.85–5.28] | 4.95 [4.91–5.03] |

### up: server machine CPU s/GB

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.3 | 1 | 14.4 [12.4–18.6] | 10.0 [9.76–13.6] | 12.1 [10.8–13.9] | 10.7 [10.7–14.0] |
| 12.2 | 8 | 16.3 [14.8–16.4] | 10.8 [10.2–12.0] | 11.4 [10.9–12.3] | 11.5 [11.5–11.8] |

## mixed

### mixed: RR P50 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.3 | 1 | 12.1 [12.1–12.2] | 12.1 [12.1–12.1] | 12.1 [12.0–12.1] | 12.3 [12.2–12.3] |

### mixed: RR P99 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12.3 | 1 | 14.5 [14.0–16.7] | 18.9 [15.5–20.3] | 15.6 [15.6–19.1] | 212 [212–213] | 0.77 | 0.83 | 11.25 |

### mixed: RR P99.9 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.3 | 1 | 17.4 [16.8–20.4] | 1012 [23.1–1012] | 27.2 [20.0–212] | 213 [212–216] |

### mixed: idle RR P50 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.3 | 1 | 12.3 [12.3–12.4] | 12.2 [12.2–12.2] | 12.2 [12.2–12.3] | 12.3 [12.2–12.3] |

### mixed: goodput Mbps

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.3 | 1 | 450 [440–451] | 180 [177–198] | 455 [450–456] | 456 [455–457] |

## connect

### connect: success

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr | kernel-cubic/smoltcp-cubic | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12.1 | 1 | 2000 [2000–2000] | 864 [825–1187] | 2000 [2000–2000] | 2000 [2000–2000] | 2.31 | 2.31 | 2.31 |

### connect: refused

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.1 | 1 | 0 [0–0] | 1136 [813–1175] | 0 [0–0] | 0 [0–0] |

### connect: timeout

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.1 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### connect: reset

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.1 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### connect: P50 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.1 | 1 | 24.2 [24.0–24.3] | 39.2 [38.6–40.2] | 24.2 [24.2–24.3] | 24.1 [24.1–24.2] |

### connect: P99 ms

| RTT ms | flows | kernel-cubic | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|
| 12.1 | 1 | 26.2 [25.1–28.4] | 100 [91.0–104] | 28.2 [26.2–33.1] | 25.7 [25.3–30.4] |

## Tunnel

- tunnel RTT (connect probes): median 12.2 ms, range 12.1–13.7 ms over 72 runs
- WireGuard implementations: client kernel, server kernel; client kernel, server stack-thread
