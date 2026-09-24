# zfbench report: 2026-09-24-validation

- generated: 2026-09-24T03:08:00
- matrix: `{"name": "validation", "bin": "/home/user/zfstack/target/release/zfbench", "stacks": ["kernel-cubic", "smoltcp-cubic", "smoltcp-bbr"], "baseline": "smoltcp-cubic", "tests": ["down", "up", "mixed", "connect"], "rate": 200.0, "rtts": [12.0], "losses": [0.0, 0.01], "queues": [2.0], "flows": [1, 8], "mixed_rtts": [12.0], "mixed_queues": [2.0], "connect_rtt": 12.0, "conns": 2000, "concurrency": 64, "secs": 10, "warmup": 2, "reps": 1, "seed": 1, "client_cc": "cubic", "quick": false, "resume": true, "outdir": "results/2026-09-24-validation", "report_only": null, "dry_run": false, "extra": []}`
- uname: `Linux vm 6.18.44-fc-v37 #1 SMP PREEMPT_DYNAMIC @0 x86_64 x86_64 x86_64 GNU/Linux`
- nproc: `4`
- zfstack_commit: `3cf4bc23b19efef8f754e3310bc87489b9637ece`
- smoltcp_rev: `8014f8b21e12faf89b3b453ceea32027344721af`
- net.ipv4.tcp_rmem: `4096 131072 33554432`
- net.ipv4.tcp_wmem: `4096 16384 4194304`
- net.ipv4.tcp_congestion_control: `bbr`

Cells show **median [min–max]** over repetitions. ⚠n = n runs crashed/timed out or failed a correctness check (see the .log / JSON `errors`). Ratio columns = median(stack) / median(smoltcp-cubic).

Goodput = bytes delivered to the receiving application, mean of the per-second samples after warmup. Rates are IP-level on the emulated bottleneck (MTU 1420, so the TCP payload ceiling at 200 Mbps is ~192-194 Mbps).

## down

### down: goodput Mbps

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | kernel-cubic/smoltcp-cubic | smoltcp-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 193 | 143 | 63.2 | 1.35 | 0.44 |
| 12 | 0% | 2 | 8 | 193 | 192 | 63.0 | 1.00 | 0.33 |
| 12 | 1% | 2 | 1 | 9.73 | 9.22 | 3.94 | 1.06 | 0.43 |
| 12 | 1% | 2 | 8 | 77.5 | 84.3 | 22.3 | 0.92 | 0.26 |

### down: zero-tput s

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 0 | 2 |
| 12 | 0% | 2 | 8 | 0 | 0 | 1 |
| 12 | 1% | 2 | 1 | 0 | 0 | 6 |
| 12 | 1% | 2 | 8 | 0 | 0 | 3 |

### down: bottleneck drops

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 34 | 306 | 88060 |
| 12 | 0% | 2 | 8 | 109 | 989 | 108481 |
| 12 | 1% | 2 | 1 | 0 | 0 | 9130 |
| 12 | 1% | 2 | 8 | 0 | 0 | 53521 |

### down: stack CPU s/GB

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | – | 5.63 | 6.06 |
| 12 | 0% | 2 | 8 | – | 6.61 | 9.97 |
| 12 | 1% | 2 | 1 | – | 7.67 | 10.3 |
| 12 | 1% | 2 | 8 | – | 7.82 | 13.1 |

## up

### up: goodput Mbps

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | kernel-cubic/smoltcp-cubic | smoltcp-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 193 | 194 | 194 | 0.99 | 1.00 |
| 12 | 0% | 2 | 8 | 193 | 194 | 194 | 0.99 | 1.00 |
| 12 | 1% | 2 | 1 | 8.74 | 8.74 | 8.77 | 1.00 | 1.00 |
| 12 | 1% | 2 | 8 | 71.0 | 69.3 | 69.4 | 1.02 | 1.00 |

### up: zero-tput s

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 0 | 0 |
| 12 | 0% | 2 | 8 | 0 | 0 | 0 |
| 12 | 1% | 2 | 1 | 0 | 0 | 0 |
| 12 | 1% | 2 | 8 | 0 | 0 | 0 |

### up: bottleneck drops

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 34 | 36 | 33 |
| 12 | 0% | 2 | 8 | 123 | 171 | 123 |
| 12 | 1% | 2 | 1 | 0 | 0 | 0 |
| 12 | 1% | 2 | 8 | 0 | 0 | 0 |

### up: stack CPU s/GB

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | – | 7.58 | 7.69 |
| 12 | 0% | 2 | 8 | – | 8.86 | 9.15 |
| 12 | 1% | 2 | 1 | – | 14.1 | 13.9 |
| 12 | 1% | 2 | 8 | – | 10.6 | 11.0 |

## mixed

### mixed: RR P50 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 30.0 | 19.0 | 12.5 |

### mixed: RR P99 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | kernel-cubic/smoltcp-cubic | smoltcp-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 35.7 | 23.2 | 36.0 | 1.54 | 1.55 |

### mixed: RR P99.9 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 35.7 | 23.2 | 36.0 |

### mixed: idle RR P50 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 12.2 | 12.5 | 12.5 |

### mixed: goodput Mbps

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 193 | 192 | 38.3 |

## connect

### connect: success

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr | kernel-cubic/smoltcp-cubic | smoltcp-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 2000 | 839 | 909 | 2.38 | 1.08 |

### connect: refused

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 1161 | 1091 |

### connect: timeout

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 0 | 0 |

### connect: reset

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 | 0 | 0 |

### connect: P50 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 25.3 | 46.5 | 47.6 |

### connect: P99 ms

| RTT ms | loss | queue k | flows | kernel-cubic | smoltcp-cubic | smoltcp-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 31.2 | 231 | 235 |

## Emulator health

- down-link delivery lateness: mean of means 33.1 µs, worst max 6604 µs
- drops outside the emulator (TUN qdisc/tx_dropped): 0; softnet backlog drops: 0
