# Final V3 validation freeze

The real kernel-upstream / production-Tokio-splice pilot completed eight valid
attempts. CUBIC/BBR means improved 55.53%/69.21%, worst-user P99 80.53%/84.60%,
goodput changed -0.80%/+0.22%. Stack CPU/GB changed -22.05%/-44.61%, combined
-10.10%/-24.59%, whole-process -6.57%/-20.91%. All bulk connections progressed,
all planned light requests completed, and core memory/upstream workers returned
to zero. These two-pair estimates select no new factor and do not enter final CIs.

Freeze V3 and the 99% policy. The full core/Tokio/bench suites, Clippy, formatting
and source-inventory checks passed before the V3 binary was built. No code
changes or builds during the following measurement sequence:

1. **96 native splice RTT attempts**: CUBIC/BBR × 1/10/50/100/250/500 ms ×
   one/200 heavy connections × two balanced adjacent pairs. FIFO/250 KB in
   both directions and both arms; windows 16 MiB. Warmup max(8 s, 40 RTT),
   measured window 20 s. Longer high-RTT warmup is declared now because the
   previous study showed loss-recovery sensitivity during short warmup.
2. **48 independent confirmation attempts**: CUBIC and BBR, each 12 balanced
   pairs at 20 ms/200 heavy connections, warmup 8 s/window 20 s. Bootstrap
   whole pairs; require complete request and budget/teardown observations.
3. **48 native few-flow guards**: CUBIC/BBR × one/two connections × six pairs,
   base RTT zero, 10-Gbps FIFO, warmup 4 s/window 12 s. Both arms use the same
   V3 binary; candidate single-peer pacing must remain bypassed.
4. **16 native A/A endpoint attempts**: same off settings on both sides at
   1/500 ms, both CCs, one/200 heavy connections. Descriptive noise context;
   no A/A order is an optimization gain.
5. **60 cross-binary client guards** on the actual local TUN/Tokio/splice path:
   baseline is the frozen pre-change final binary `74a5d0f8…`; candidate is V3
   with 12,375,000 IP bytes/s configured (the low configured rate must be
   bypassed for this single peer). Down/one connection gets 12 balanced pairs;
   down/two, up/one, mixed/one each six. Warmup 4 s/window 12 s, MTU 65535,
   two proxy workers and dedicated driver, identical across binaries.

All FIFO identity, payload, arrival, per-connection progress, binary and source
audits still apply. CPU/second and CPU/GB remain separate; a high-RTT goodput
gain is not evidence that absolute CPU is lower at equal wall time. Primary
service CPU is the production driver/relay; also report combined and complete
process costs, including source and bridge. Any invalid attempt or clearly
failing regime remains in the report and blocks an unqualified universal claim.
The core single-peer deterministic packet/time traces are an additional guard,
not a substitute for the physical throughput comparisons.
