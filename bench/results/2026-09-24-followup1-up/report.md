# zfbench report: 2026-09-24-followup1-up

- generated: 2026-09-24T09:08:19
- matrix: `{"name": "followup1-up", "bin": "/tmp/claude-0/-home-user-zfstack/9625fbf2-2b1b-5027-a7c7-4a9537e57900/scratchpad/zfbench-f1", "stacks": ["smoltcp-cubic", "zfstack-cubic", "zfstack-bbr"], "baseline": "smoltcp-cubic", "tests": ["up"], "rate": 200.0, "rtts": [12.0], "losses": [0.0], "queues": [2.0], "flows": [1, 8], "mixed_rtts": [12.0, 80.0], "mixed_queues": [2.0, 0.25], "connect_rtt": 12.0, "conns": 2000, "concurrency": 64, "secs": 15, "warmup": 3, "reps": 3, "seed": 1, "client_cc": "cubic", "quick": false, "resume": false, "outdir": null, "report_only": null, "dry_run": false, "extra": []}`
- uname: `Linux vm 6.18.44-fc-v37 #1 SMP PREEMPT_DYNAMIC @0 x86_64 x86_64 x86_64 GNU/Linux`
- nproc: `4`
- zfstack_commit: `db18784f1f5ae7b3782b4dd36586bb8ef971576f`
- smoltcp_rev: `8014f8b21e12faf89b3b453ceea32027344721af`
- net.ipv4.tcp_rmem: `4096 131072 33554432`
- net.ipv4.tcp_wmem: `4096 16384 4194304`
- net.ipv4.tcp_congestion_control: `bbr`

Cells show **median [min–max]** over repetitions. ⚠n = n runs crashed/timed out or failed a correctness check (see the .log / JSON `errors`). Ratio columns = median(stack) / median(smoltcp-cubic).

Goodput = bytes delivered to the receiving application, mean of the per-second samples after warmup. Rates are IP-level on the emulated bottleneck (MTU 1420, so the TCP payload ceiling at 200 Mbps is ~192-194 Mbps).

## up

### up: goodput Mbps

| RTT ms | loss | queue k | flows | smoltcp-cubic | zfstack-cubic | zfstack-bbr | zfstack-cubic/smoltcp-cubic | zfstack-bbr/smoltcp-cubic |
|---|---|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 194 [194–194] | 193 [192–193] | 193 [193–193] | 0.99 | 0.99 |
| 12 | 0% | 2 | 8 | 194 [194–194] | 193 [193–193] | 193 [193–193] | 0.99 | 0.99 |

### up: zero-tput s

| RTT ms | loss | queue k | flows | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 0 [0–0] | 0 [0–0] | 0 [0–0] |
| 12 | 0% | 2 | 8 | 0 [0–0] | 0 [0–0] | 0 [0–0] |

### up: bottleneck drops

| RTT ms | loss | queue k | flows | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 34 [32–40] | 36 [31–50] | 44 [38–44] |
| 12 | 0% | 2 | 8 | 222 [173–233] | 195 [192–201] | 170 [159–192] |

### up: stack CPU s/GB

| RTT ms | loss | queue k | flows | smoltcp-cubic | zfstack-cubic | zfstack-bbr |
|---|---|---|---|---|---|---|
| 12 | 0% | 2 | 1 | 8.90 [8.72–9.05] | 9.82 [9.75–9.87] | 9.89 [9.79–10.4] |
| 12 | 0% | 2 | 8 | 10.1 [10.0–12.2] | 9.81 [9.77–9.87] | 9.71 [9.62–9.77] |

## Emulator health

- down-link delivery lateness: mean of means 29.9 µs, worst max 21937 µs
- drops outside the emulator (TUN qdisc/tx_dropped): 0; softnet backlog drops: 0
