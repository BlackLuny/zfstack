# zfbench report: 2026-09-24-smoke1

- generated: 2026-09-24T03:36:51
- matrix: `{"name": "smoke1", "bin": "/home/user/zfstack/target/release/zfbench", "stacks": ["kernel-cubic", "smoltcp-cubic", "smoltcp-bbr", "zfstack-cubic"], "baseline": "smoltcp-cubic", "tests": ["down", "up", "mixed", "connect"], "rate": 200.0, "rtts": [12.0, 80.0], "losses": [0.0, 0.01], "queues": [2.0, 0.25], "flows": [1], "mixed_rtts": [12.0, 80.0], "mixed_queues": [2.0, 0.25], "connect_rtt": 12.0, "conns": 2000, "concurrency": 64, "secs": 10, "warmup": 2, "reps": 1, "seed": 1, "client_cc": "cubic", "quick": true, "resume": false, "outdir": null, "report_only": null, "dry_run": false, "extra": []}`
- uname: `Linux vm 6.18.44-fc-v37 #1 SMP PREEMPT_DYNAMIC @0 x86_64 x86_64 x86_64 GNU/Linux`
- nproc: `4`
- zfstack_commit: `642461c7ccaddbb8f04b3a52778f9484dcd52e36`
- smoltcp_rev: `8014f8b21e12faf89b3b453ceea32027344721af`
- net.ipv4.tcp_rmem: `4096 131072 33554432`
- net.ipv4.tcp_wmem: `4096 16384 4194304`
- net.ipv4.tcp_congestion_control: `bbr`

Cells show **median [min–max]** over repetitions. ⚠n = n runs crashed/timed out or failed a correctness check (see the .log / JSON `errors`). Ratio columns = median(stack) / median(smoltcp-cubic).

Goodput = bytes delivered to the receiving application, mean of the per-second samples after warmup. Rates are IP-level on the emulated bottleneck (MTU 1420, so the TCP payload ceiling at 200 Mbps is ~192-194 Mbps).

## down

### down: goodput Mbps

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic | kernel-cubic/smoltcp-cubic | smoltcp-bbr/smoltcp-cubic | zfstack-cubic/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 193 | 192 | 194 | 193 | 1.00 | 1.01 | 1.00 |
| 12 | 0% | 0.25 | 1 | 168 | 172 | 1.57 | 182 | 0.97 | 0.01 | 1.05 |
| 12 | 1% | 2 | 1 | 9.92 | 9.84 | 4.85 | 9.94 | 1.01 | 0.49 | 1.01 |
| 12 | 1% | 0.25 | 1 | 9.88 | 9.28 | 1.37 | 10.4 | 1.06 | 0.15 | 1.12 |
| 80 | 0% | 2 | 1 | 193 | 194 | 194 | 192 | 0.99 | 1.00 | 0.99 |
| 80 | 0% | 0.25 | 1 | 155 | 177 | 27.8 | 177 | 0.88 | 0.16 | 1.00 |
| 80 | 1% | 2 | 1 | 1.79 | 1.76 | 91.7 | 1.89 | 1.02 | 51.99 | 1.07 |
| 80 | 1% | 0.25 | 1 | 1.84 | 1.75 | 26.0 | 1.87 | 1.06 | 14.89 | 1.07 |

### down: zero-tput s

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 0 | 0 | 0 |
| 12 | 0% | 0.25 | 1 | 0 | 0 | 6 | 0 |
| 12 | 1% | 2 | 1 | 0 | 0 | 5 | 0 |
| 12 | 1% | 0.25 | 1 | 0 | 0 | 6 | 0 |
| 80 | 0% | 2 | 1 | 0 | 0 | 0 | 0 |
| 80 | 0% | 0.25 | 1 | 0 | 0 | 1 | 0 |
| 80 | 1% | 2 | 1 | 0 | 0 | 0 | 0 |
| 80 | 1% | 0.25 | 1 | 0 | 0 | 1 | 0 |

### down: bottleneck drops

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 34 | 844 | 0 | 172 |
| 12 | 0% | 0.25 | 1 | 138 | 287 | 5907 | 270 |
| 12 | 1% | 2 | 1 | 0 | 0 | 9129 | 0 |
| 12 | 1% | 0.25 | 1 | 0 | 4 | 4231 | 0 |
| 80 | 0% | 2 | 1 | 0 | 0 | 0 | 252 |
| 80 | 0% | 0.25 | 1 | 662 | 902 | 29487 | 2738 |
| 80 | 1% | 2 | 1 | 0 | 0 | 3396 | 0 |
| 80 | 1% | 0.25 | 1 | 0 | 0 | 20667 | 0 |

### down: stack CPU s/GB

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | – | 5.61 | 5.04 | 7.87 |
| 12 | 0% | 0.25 | 1 | – | 5.49 | 13.0 | 7.97 |
| 12 | 1% | 2 | 1 | – | 7.47 | 6.76 | 10.1 |
| 12 | 1% | 0.25 | 1 | – | 7.89 | 9.44 | 9.48 |
| 80 | 0% | 2 | 1 | – | 5.13 | 5.00 | 6.18 |
| 80 | 0% | 0.25 | 1 | – | 6.64 | 6.48 | 8.91 |
| 80 | 1% | 2 | 1 | – | 9.70 | 3.54 | 30.5 |
| 80 | 1% | 0.25 | 1 | – | 9.61 | 5.13 | 29.8 |

## up

### up: goodput Mbps

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic | kernel-cubic/smoltcp-cubic | smoltcp-bbr/smoltcp-cubic | zfstack-cubic/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 192 | 194 | 194 | 193 | 0.99 | 1.00 | 0.99 |
| 12 | 0% | 0.25 | 1 | 173 | 157 | 173 | 171 | 1.10 | 1.10 | 1.09 |
| 12 | 1% | 2 | 1 | 8.92 | 8.91 | 9.17 | 8.64 | 1.00 | 1.03 | 0.97 |
| 12 | 1% | 0.25 | 1 | 8.88 | 8.98 | 8.83 | 8.62 | 0.99 | 0.98 | 0.96 |
| 80 | 0% | 2 | 1 | 193 | 194 | 194 | 193 | 0.99 | 1.00 | 0.99 |
| 80 | 0% | 0.25 | 1 | 192 | 154 | 163 | 138 | 1.25 | 1.06 | 0.90 |
| 80 | 1% | 2 | 1 | 1.37 | 1.43 | 1.42 | 1.40 | 0.96 | 0.99 | 0.98 |
| 80 | 1% | 0.25 | 1 | 1.37 | 1.43 | 1.44 | 1.40 | 0.96 | 1.00 | 0.98 |

### up: zero-tput s

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 0 | 0 | 0 |
| 12 | 0% | 0.25 | 1 | 0 | 0 | 0 | 0 |
| 12 | 1% | 2 | 1 | 0 | 0 | 0 | 0 |
| 12 | 1% | 0.25 | 1 | 0 | 0 | 0 | 0 |
| 80 | 0% | 2 | 1 | 0 | 0 | 0 | 0 |
| 80 | 0% | 0.25 | 1 | 0 | 0 | 0 | 0 |
| 80 | 1% | 2 | 1 | 0 | 0 | 0 | 0 |
| 80 | 1% | 0.25 | 1 | 0 | 0 | 0 | 0 |

### up: bottleneck drops

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 33 | 33 | 47 | 35 |
| 12 | 0% | 0.25 | 1 | 130 | 393 | 198 | 304 |
| 12 | 1% | 2 | 1 | 0 | 0 | 0 | 0 |
| 12 | 1% | 0.25 | 1 | 0 | 0 | 0 | 0 |
| 80 | 0% | 2 | 1 | 0 | 0 | 0 | 0 |
| 80 | 0% | 0.25 | 1 | 1257 | 1134 | 1862 | 1296 |
| 80 | 1% | 2 | 1 | 0 | 0 | 0 | 0 |
| 80 | 1% | 0.25 | 1 | 0 | 0 | 0 | 0 |

### up: stack CPU s/GB

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | – | 7.53 | 7.30 | 7.87 |
| 12 | 0% | 0.25 | 1 | – | 8.07 | 8.09 | 8.36 |
| 12 | 1% | 2 | 1 | – | 14.2 | 13.5 | 14.3 |
| 12 | 1% | 0.25 | 1 | – | 13.5 | 13.7 | 14.2 |
| 80 | 0% | 2 | 1 | – | 8.05 | 8.14 | 9.05 |
| 80 | 0% | 0.25 | 1 | – | 7.83 | 8.14 | 8.59 |
| 80 | 1% | 2 | 1 | – | 22.0 | 24.9 | 19.5 |
| 80 | 1% | 0.25 | 1 | – | 22.7 | 22.3 | 19.7 |

## mixed

### mixed: RR P50 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 30.0 | 12.5 | 25.0 | 30.7 |
| 12 | 0% | 0.25 | 1 | 12.7 | 12.4 | 12.5 | 12.6 |
| 80 | 0% | 2 | 1 | 131 | 173 | 173 | 202 |
| 80 | 0% | 0.25 | 1 | 80.7 | 80.5 | 80.5 | 80.5 |

### mixed: RR P99 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic | kernel-cubic/smoltcp-cubic | smoltcp-bbr/smoltcp-cubic | zfstack-cubic/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 264 | 18.9 | 1017 | 33.9 | 13.97 | 53.86 | 1.80 |
| 12 | 0% | 0.25 | 1 | 14.7 | 1014 | 12.8 | 15.0 | 0.01 | 0.01 | 0.01 |
| 80 | 0% | 2 | 1 | 131 | 173 | 173 | 222 | 0.76 | 1.00 | 1.29 |
| 80 | 0% | 0.25 | 1 | 82.8 | 95.8 | 1081 | 83.8 | 0.86 | 11.28 | 0.87 |

### mixed: RR P99.9 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 264 | 18.9 | 1017 | 33.9 |
| 12 | 0% | 0.25 | 1 | 14.7 | 1014 | 12.8 | 15.0 |
| 80 | 0% | 2 | 1 | 131 | 173 | 173 | 222 |
| 80 | 0% | 0.25 | 1 | 82.8 | 95.8 | 1081 | 83.8 |

### mixed: idle RR P50 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 12.4 | 12.5 | 12.4 | 12.4 |
| 12 | 0% | 0.25 | 1 | 12.5 | 12.5 | 12.5 | 12.5 |
| 80 | 0% | 2 | 1 | 80.5 | 80.2 | 80.2 | 80.6 |
| 80 | 0% | 0.25 | 1 | 80.5 | 80.5 | 80.6 | 80.5 |

### mixed: goodput Mbps

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 193 | 142 | 171 | 193 |
| 12 | 0% | 0.25 | 1 | 168 | 81.6 | 0.60 | 182 |
| 80 | 0% | 2 | 1 | 193 | 194 | 194 | 192 |
| 80 | 0% | 0.25 | 1 | 155 | 48.6 | 52.7 | 178 |

## connect

### connect: success

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic | kernel-cubic/smoltcp-cubic | smoltcp-bbr/smoltcp-cubic | zfstack-cubic/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 2000 | 944 | 889 | 2000 | 2.12 | 0.94 | 2.12 |

### connect: refused

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 1056 | 1111 | 0 |

### connect: timeout

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 0 | 0 | 0 |

### connect: reset

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 0 | 0 | 0 |

### connect: P50 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 24.9 | 47.2 | 45.8 | 24.6 |

### connect: P99 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | zfstack-cubic |
|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 31.0 | 246 | 181 | 25.3 |

## Emulator health

- down-link delivery lateness: mean of means 31.8 µs, worst max 44136 µs
- drops outside the emulator (TUN qdisc/tx_dropped): 0; softnet backlog drops: 0
