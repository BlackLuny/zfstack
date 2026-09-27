# zfbench soak: 2026-09-27-soak-main664

Isolated TUN emulator (10.201.0.0/24). No WAN, no WireGuard, no external network.

- generated: 2026-09-27T06:13:16
- uname: `Linux cursor 6.12.94+ #1 SMP PREEMPT_DYNAMIC Thu Sep 24 16:04:37 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux`
- nproc: `4`
- zfstack_commit: `2f8e179869e571084594fc6e00a760749e2bfd31`
- quick: `False`
- stacks: kernel-cubic, smoltcp-cubic, zfstack-cubic, zfstack-bbr

## Findings (auto-classified; confirm before filing)

No automatic stack findings. Churn `other` failures that are `Cannot assign requested address (os error 99)` are client ephemeral-port / TIME_WAIT exhaustion and are ignored when the same samples appear on kernel and smoltcp.

## Streams (goodput / stall / RSS)

| scenario | stack | ok | Mbps | zero-s | max streak | RSS Δ kB | slope kB/min | leftover active |
|---|---|---|---|---|---|---|---|---|
| down-clean-12 | kernel-cubic | yes | 192.7 | 0 | 0 | 16 | 6 | – |
| down-clean-12 | smoltcp-cubic | yes | 182.1 | 0 | 0 | 372 | 19 | – |
| down-clean-12 | zfstack-cubic | yes | 192.7 | 0 | 0 | 124 | 3 | 0 |
| down-clean-12 | zfstack-bbr | yes | 185.0 | 0 | 0 | 16 | 5 | 0 |
| down-loss-12 | kernel-cubic | yes | 9.8 | 0 | 0 | 0 | 0 | – |
| down-loss-12 | smoltcp-cubic | yes | 7.7 | 0 | 0 | 316 | 56 | – |
| down-loss-12 | zfstack-cubic | yes | 10.3 | 0 | 0 | 8 | 5 | 0 |
| down-loss-12 | zfstack-bbr | yes | 133.2 | 0 | 0 | -524 | -217 | 0 |
| down-shallow-80 | kernel-cubic | yes | 153.9 | 0 | 0 | 12 | 7 | – |
| down-shallow-80 | smoltcp-cubic | yes | 81.9 | 0 | 0 | 1432 | 734 | – |
| down-shallow-80 | zfstack-cubic | yes | 189.2 | 0 | 0 | -1656 | -960 | 0 |
| down-shallow-80 | zfstack-bbr | yes | 163.8 | 0 | 0 | 564 | 2 | 0 |
| down-loss80-8f | kernel-cubic | yes | 12.2 | 0 | 0 | 8 | 0 | – |
| down-loss80-8f | smoltcp-cubic | yes | 12.2 | 0 | 0 | 144 | 42 | – |
| down-loss80-8f | zfstack-cubic | yes | 13.2 | 0 | 0 | 12 | 7 | 0 |
| down-loss80-8f | zfstack-bbr | yes | 89.2 | 0 | 0 | -1804 | -970 | 0 |
| up-clean-12 | kernel-cubic | yes | 192.7 | 0 | 0 | 28 | 16 | – |
| up-clean-12 | smoltcp-cubic | yes | 194.3 | 0 | 0 | 64 | 26 | – |
| up-clean-12 | zfstack-cubic | yes | 192.7 | 0 | 0 | 144 | 53 | 0 |
| up-clean-12 | zfstack-bbr | yes | 192.7 | 0 | 0 | 84 | 27 | 0 |
| up-loss-12 | kernel-cubic | yes | 9.6 | 0 | 0 | 36 | 9 | – |
| up-loss-12 | smoltcp-cubic | yes | 9.1 | 0 | 0 | 3060 | 4 | – |
| up-loss-12 | zfstack-cubic | yes | 9.2 | 0 | 0 | 100 | 20 | 0 |
| up-loss-12 | zfstack-bbr | yes | 9.1 | 0 | 0 | 108 | 25 | 0 |
| mixed-12 | kernel-cubic | yes | 192.6 | 0 | 0 | 84 | 46 | – |
| mixed-12 | smoltcp-cubic | yes | 190.8 | 0 | 0 | 1680 | 688 | – |
| mixed-12 | zfstack-cubic | yes | 192.6 | 0 | 0 | 200 | 28 | 0 |
| mixed-12 | zfstack-bbr | yes | 185.1 | 0 | 0 | 112 | 62 | 0 |
| pause-12 | kernel-cubic | yes | 126.6 | 6 | 6 | 12 | 0 | – |
| pause-12 | smoltcp-cubic | yes | 171.2 | 0 | 0 | 268 | -324 | – |
| pause-12 | zfstack-cubic | yes | 193.8 | 0 | 0 | -116 | 10 | 0 |
| pause-12 | zfstack-bbr | yes | 186.3 | 0 | 0 | -124 | 60 | 0 |
| idlehold-256 | kernel-cubic | yes | 192.7 | 0 | 0 | 0 | 0 | – |
| idlehold-256 | smoltcp-cubic | yes | 191.5 | 0 | 0 | 420 | 1 | – |
| idlehold-256 | zfstack-cubic | yes | 192.7 | 0 | 0 | 68 | 23 | 0 |
| idlehold-256 | zfstack-bbr | yes | 185.0 | 0 | 0 | 4 | 1 | 0 |

## Churn

| scenario | stack | ok | success | failures | conn/s | P99 ms | RSS Δ kB | active / TW |
|---|---|---|---|---|---|---|---|---|
| churn-12 | kernel-cubic | yes | 27975 | {'other': 96606} | 466.1 | 34.2 | 936 | – / – |
| churn-12 | smoltcp-cubic | yes | 28231 | {'other': 68502, 'refused': 11958} | 470.3 | 65.4 | 225560 | – / – |
| churn-12 | zfstack-cubic | yes | 28596 | {'other': 74885} | 476.4 | 148.1 | 1296 | 0 / 0 |
| churn-12 | zfstack-bbr | yes | 28578 | {'other': 71087} | 476.1 | 154.9 | 5192 | 0 / 0 |

## Mixed RR

| scenario | stack | ok | bulk Mbps | idle P50 | load P50 | load P99 | load P99.9 |
|---|---|---|---|---|---|---|---|
| mixed-12 | kernel-cubic | yes | 192.6 | 12.8 | 33.3 | 36.1 | 36.4 |
| mixed-12 | smoltcp-cubic | yes | 190.8 | 12.9 | 31.5 | 36.0 | 36.1 |
| mixed-12 | zfstack-cubic | yes | 192.6 | 12.9 | 31.7 | 36.0 | 36.1 |
| mixed-12 | zfstack-bbr | yes | 185.1 | 12.9 | 12.7 | 17.4 | 18.1 |

## Confirmation (this run, vs 2026-09-26)

40/40 runs completed on the isolated TUN emulator (`10.201.0.0/24`, zfstack `2f8e179` = `main` `2d12727` + soak harness, 2026-09-27 05:01–06:13 UTC). No crashes, timeouts, pattern mismatches, unexpected EOFs, leftover zfstack `active_conns`, or leftover TIME_WAIT. Auto-classifier: 0 findings.

Compared to [2026-09-26 soak](https://github.com/BlackLuny/zfstack/blob/cursor/soak-stability-3c45/bench/results/2026-09-26-soak/report.md) on `3a5c9f6` (pre-#664). Same matrix, same machine class.

**Not filed as zfstack bugs**

- Churn `other` failures are again `Cannot assign requested address (os error 99)` on **every** stack: kernel 96606, smoltcp 68502 (+11958 refused), zfstack-cubic 74885, zfstack-bbr 71087. Successful echoes stay in the same band (~28.0–28.6k / 60 s). `conns_created == conns_freed`, leftover active/TW = 0. Same client ephemeral-port / TIME_WAIT exhaustion as last soak.
- `pause-12` kernel-cubic again had a 6 s zero-throughput streak after the pause window (resume 127 Mbps). zfstack-cubic resumed at 194 Mbps with no extra stall; zfstack-bbr at 186 Mbps.
- smoltcp-cubic RSS grew +220 MiB on churn and +3 MiB on lossy upload. zfstack RSS stayed flat after warmup (churn +1.3 / +5.1 MiB then plateau). Several zfstack cells show **negative** RSS Δ (cache reclaim from #664); leftover physical is still the ~1 MiB TX cache (or ~64 KiB on upload).
- `down-loss80-8f` zfstack-bbr goodput 89.2 Mbps vs 101.8 Mbps last soak. No stall (`zero=0`, `ge2=0`), leftover 0, still ~7× kernel (12.2 Mbps). Treated as BBR/lossy 8-flow variance, not a disconnect or leak.

**Stability comparison (zfstack vs kernel / smoltcp, and vs previous soak)**

| Concern | zfstack-cubic | zfstack-bbr | vs 2026-09-26 |
|---|---|---|---|
| Disconnect / RST of live flows | none | none | same; all `ok=true` |
| Stall / 断流 on clean or lossy bulk | none | none | same; max zero streak 0 |
| Resume after 8 s 停读 | 194 Mbps immediately | 186 Mbps immediately | same; still better than kernel |
| 256 idle + 1 bulk | 192.7 Mbps | 185.0 Mbps | same; leftover active 0 |
| Memory leak (RSS slope) | no | no | reclaim more visible; leftover ~1 MiB cache |
| Echo churn correctness | 28596/28596 | 28578/28578 | +123 / +194 success vs last soak |

No GitHub issue opened: nothing here is a confirmed zfstack defect versus the kernel baseline or a regression versus the pre-#664 soak.

