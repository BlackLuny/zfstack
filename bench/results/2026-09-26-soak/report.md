# zfbench soak: 2026-09-26-soak

Isolated TUN emulator (10.201.0.0/24). No WAN, no WireGuard, no external network.

- generated: 2026-09-26T13:02:16
- uname: `Linux cursor 6.12.94+ #1 SMP PREEMPT_DYNAMIC Thu Sep 24 16:04:37 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux`
- nproc: `4`
- zfstack_commit: `3a5c9f636be761a598b7ba5957af64e59f4f5534`
- quick: `False`
- stacks: kernel-cubic, smoltcp-cubic, zfstack-cubic, zfstack-bbr

## Findings (auto-classified; confirm before filing)

No automatic stack findings. Churn `other` failures that are `Cannot assign requested address (os error 99)` are client ephemeral-port / TIME_WAIT exhaustion and are ignored when the same samples appear on kernel and smoltcp.

## Streams (goodput / stall / RSS)

| scenario | stack | ok | Mbps | zero-s | max streak | RSS Δ kB | slope kB/min | leftover active |
|---|---|---|---|---|---|---|---|---|
| down-clean-12 | kernel-cubic | yes | 192.7 | 0 | 0 | 28 | 10 | – |
| down-clean-12 | smoltcp-cubic | yes | 173.0 | 0 | 0 | 108 | -17 | – |
| down-clean-12 | zfstack-cubic | yes | 192.7 | 0 | 0 | 212 | 24 | 0 |
| down-clean-12 | zfstack-bbr | yes | 185.1 | 0 | 0 | 44 | 7 | 0 |
| down-loss-12 | kernel-cubic | yes | 9.8 | 0 | 0 | 0 | 0 | – |
| down-loss-12 | smoltcp-cubic | yes | 7.8 | 0 | 0 | 292 | 46 | – |
| down-loss-12 | zfstack-cubic | yes | 10.4 | 0 | 0 | 0 | 0 | 0 |
| down-loss-12 | zfstack-bbr | yes | 133.7 | 0 | 0 | 848 | 14 | 0 |
| down-shallow-80 | kernel-cubic | yes | 153.4 | 0 | 0 | 24 | 14 | – |
| down-shallow-80 | smoltcp-cubic | yes | 66.9 | 1 | 1 | 1148 | 494 | – |
| down-shallow-80 | zfstack-cubic | yes | 189.1 | 0 | 0 | 144 | 83 | 0 |
| down-shallow-80 | zfstack-bbr | yes | 163.7 | 0 | 0 | -1224 | -316 | 0 |
| down-loss80-8f | kernel-cubic | yes | 12.3 | 0 | 0 | 0 | 0 | – |
| down-loss80-8f | smoltcp-cubic | yes | 12.1 | 0 | 0 | 24 | 14 | – |
| down-loss80-8f | zfstack-cubic | yes | 13.0 | 0 | 0 | -52 | -39 | 0 |
| down-loss80-8f | zfstack-bbr | yes | 101.8 | 0 | 0 | -400 | -1 | 0 |
| up-clean-12 | kernel-cubic | yes | 192.7 | 0 | 0 | 72 | 39 | – |
| up-clean-12 | smoltcp-cubic | yes | 194.4 | 0 | 0 | 288 | 166 | – |
| up-clean-12 | zfstack-cubic | yes | 192.7 | 0 | 0 | 164 | 54 | 0 |
| up-clean-12 | zfstack-bbr | yes | 192.7 | 0 | 0 | 176 | 59 | 0 |
| up-loss-12 | kernel-cubic | yes | 9.5 | 0 | 0 | 36 | 5 | – |
| up-loss-12 | smoltcp-cubic | yes | 9.1 | 0 | 0 | 3072 | 6 | – |
| up-loss-12 | zfstack-cubic | yes | 9.2 | 0 | 0 | 64 | 11 | 0 |
| up-loss-12 | zfstack-bbr | yes | 9.2 | 0 | 0 | 72 | 20 | 0 |
| mixed-12 | kernel-cubic | yes | 192.6 | 0 | 0 | 160 | 88 | – |
| mixed-12 | smoltcp-cubic | yes | 190.0 | 0 | 0 | 1596 | 636 | – |
| mixed-12 | zfstack-cubic | yes | 192.6 | 0 | 0 | 228 | 49 | 0 |
| mixed-12 | zfstack-bbr | yes | 185.0 | 0 | 0 | 136 | 72 | 0 |
| pause-12 | kernel-cubic | yes | 129.2 | 6 | 6 | 76 | 64 | – |
| pause-12 | smoltcp-cubic | yes | 171.3 | 0 | 0 | 264 | -146 | – |
| pause-12 | zfstack-cubic | yes | 193.8 | 0 | 0 | 4 | 176 | 0 |
| pause-12 | zfstack-bbr | yes | 186.1 | 0 | 0 | -132 | 40 | 0 |
| idlehold-256 | kernel-cubic | yes | 192.7 | 0 | 0 | 28 | 38 | – |
| idlehold-256 | smoltcp-cubic | yes | 191.5 | 0 | 0 | 288 | 48 | – |
| idlehold-256 | zfstack-cubic | yes | 192.7 | 0 | 0 | 156 | 27 | 0 |
| idlehold-256 | zfstack-bbr | yes | 185.0 | 0 | 0 | 80 | 102 | 0 |

## Churn

| scenario | stack | ok | success | failures | conn/s | P99 ms | RSS Δ kB | active / TW |
|---|---|---|---|---|---|---|---|---|
| churn-12 | kernel-cubic | yes | 27975 | {'other': 100540} | 466.1 | 33.5 | -10884 | – / – |
| churn-12 | smoltcp-cubic | yes | 28231 | {'other': 80605, 'refused': 10774} | 470.3 | 64.3 | 151712 | – / – |
| churn-12 | zfstack-cubic | yes | 28473 | {'other': 91834} | 474.4 | 138.6 | 1252 | 0 / 0 |
| churn-12 | zfstack-bbr | yes | 28384 | {'other': 71349} | 472.9 | 116.9 | 5360 | 0 / 0 |

## Mixed RR

| scenario | stack | ok | bulk Mbps | idle P50 | load P50 | load P99 | load P99.9 |
|---|---|---|---|---|---|---|---|
| mixed-12 | kernel-cubic | yes | 192.6 | 12.7 | 33.3 | 36.0 | 36.3 |
| mixed-12 | smoltcp-cubic | yes | 190.0 | 12.7 | 31.0 | 36.0 | 36.3 |
| mixed-12 | zfstack-cubic | yes | 192.6 | 12.8 | 31.7 | 36.0 | 36.0 |
| mixed-12 | zfstack-bbr | yes | 185.0 | 12.7 | 12.7 | 17.4 | 17.7 |

## Confirmation (this run)

40/40 runs completed on the isolated TUN emulator (`10.201.0.0/24`, zfstack `3a5c9f6`, 2026-09-26 11:46–12:58 UTC). No crashes, timeouts, pattern mismatches, unexpected EOFs, or leftover zfstack `active_conns`.

**Not filed as zfstack bugs**

- Auto-classifier first flagged `churn-12` zfstack-cubic/bbr (`other` failures). The same `Cannot assign requested address (os error 99)` samples appear on **kernel-cubic** (100540) and **smoltcp-cubic** (80605). Successful echoes are in the same band (~28k / 60 s). After ~12–22 s the kernel client exhausts ephemeral ports (TIME_WAIT); workers then spin. zfstack `conns_created == conns_freed`, leftover active/TW = 0.
- `pause-12` kernel-cubic had a 6 s zero-throughput streak *after* the pause window (resume 129 Mbps). zfstack-cubic resumed at 194 Mbps with no extra stall; zfstack-bbr at 186 Mbps.
- smoltcp-cubic RSS grew +148 MiB on churn and +3 MiB on lossy upload (listener-pool buffers). zfstack RSS stayed flat after warmup (churn +1.2 / +5.2 MiB then plateau; leftover physical = 1 MiB TX cache).

**Stability comparison (zfstack vs kernel / smoltcp)**

| Concern | zfstack-cubic | zfstack-bbr | Notes |
|---|---|---|---|
| Disconnect / RST of live flows | none | none | all `ok=true` |
| Stall / 断流 on clean or lossy bulk | none | none | max zero streak 0 |
| Resume after 8 s 停读 | 194 Mbps immediately | 186 Mbps immediately | better than kernel |
| 256 idle + 1 bulk | 192.7 Mbps | 185.0 Mbps | leftover active 0 |
| Memory leak (RSS slope) | no | no | cache leftover ~1 MiB by design |
| Echo churn correctness | 28473/28473 accepted also finished | 28384/28384 | P50 ~26–28 ms ≈ kernel |

No GitHub issue opened: nothing here is a confirmed zfstack defect versus the kernel baseline.
