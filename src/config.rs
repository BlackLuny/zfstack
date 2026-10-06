//! Stack configuration. Defaults follow docs/design/0001 (§6, §7, §10).

use crate::cc::CcAlgo;
use core::time::Duration;

#[derive(Clone, Debug)]
pub struct StackConfig {
    pub cc: CcAlgo,
    pub init_cwnd_segs: u32,
    /// Initial advertised receive window (§6.6).
    pub init_rcv_wnd: u32,
    /// Per-connection receive buffer ceiling (autotuning upper bound).
    pub max_rcv_buf: u32,
    /// Per-connection cap on sent-but-unacknowledged bytes (§6.5).
    pub max_snd_inflight: u32,
    /// Unsent prefetch horizon, additionally bounded by `prefetch_max`.
    pub prefetch_time: Duration,
    pub min_prefetch: u32,
    pub prefetch_max: u32,
    /// Writable events fire when at least this much send space is free (§4.4).
    pub write_low_watermark: u32,
    pub min_rto: Duration,
    pub max_rto: Duration,
    pub init_rto: Duration,
    pub delayed_ack: Duration,
    pub pacing: bool,
    /// Window-based CCs (CUBIC) are not paced while cwnd is below this many segments:
    /// a window that small is already spread by the ACK clock, and pacing it only adds
    /// a wakeup per segment (Linux does not pace CUBIC at all without fq).
    /// Rate-based CCs (BBR, Brutal) are always paced.
    pub pacing_min_cwnd_segs: u32,
    /// Pacing credit bounds (§7.2): T_credit = clamp(measured wake P99, min, max).
    pub pacing_credit_min: Duration,
    pub pacing_credit_max: Duration,
    /// Maximum bytes emitted by one `run` call (§8.4).
    pub round_bytes_cap: usize,
    /// Upper bound of the DRR quantum; actual is clamp(rate × 1 ms, 2 MSS, this).
    pub max_quantum: usize,
    pub keepalive_idle: Duration,
    pub keepalive_interval: Duration,
    pub keepalive_probes: u32,
    pub user_timeout: Duration,
    pub orphan_timeout: Duration,
    pub time_wait: Duration,
    pub synack_retries: u32,
    /// Half-open connections before switching to SYN cookies (§10.2).
    pub syn_backlog: usize,
    /// Not-yet-accepted established connections (accept queue).
    pub accept_backlog: usize,
    pub max_time_wait: usize,
    pub sack: bool,
    pub timestamps: bool,
    pub window_scaling: bool,
    pub ttl: u8,
}

impl Default for StackConfig {
    fn default() -> Self {
        StackConfig {
            cc: CcAlgo::Cubic,
            init_cwnd_segs: 10,
            init_rcv_wnd: 64 * 1024,
            max_rcv_buf: 16 << 20,
            max_snd_inflight: 16 << 20,
            prefetch_time: Duration::from_millis(20),
            min_prefetch: 64 * 1024,
            // Preserve the library's original behavior unless the host sets a cap.
            prefetch_max: u32::MAX,
            write_low_watermark: 16 * 1024,
            min_rto: Duration::from_millis(200),
            max_rto: Duration::from_secs(60),
            init_rto: Duration::from_secs(1),
            delayed_ack: Duration::from_millis(40),
            pacing: true,
            pacing_min_cwnd_segs: 32,
            pacing_credit_min: Duration::from_millis(2),
            pacing_credit_max: Duration::from_millis(10),
            round_bytes_cap: 256 * 1024,
            max_quantum: 64 * 1024,
            keepalive_idle: Duration::from_secs(25),
            keepalive_interval: Duration::from_secs(25),
            keepalive_probes: 3,
            user_timeout: Duration::from_secs(120),
            orphan_timeout: Duration::from_secs(60),
            time_wait: Duration::from_secs(60),
            synack_retries: 3,
            syn_backlog: 4096,
            accept_backlog: 4096,
            max_time_wait: 65536,
            sack: true,
            timestamps: true,
            window_scaling: true,
            ttl: 64,
        }
    }
}

impl StackConfig {
    /// Proxy-client profile: terminating the local OS's TCP on a TUN device
    /// (docs/design/0008). The peer is the local kernel, so the path is
    /// lossless with an RTT of tens of microseconds and any MTU up to 65535;
    /// the bottleneck is the upstream proxy connection, not this hop.
    ///
    /// * No pacing: there is no queue to protect, and pacing at sub-ms RTTs
    ///   only adds timers.
    /// * Buffers sized for host scheduling jitter (a few ms at 10 Gbit/s),
    ///   not a WAN BDP. `prefetch_max` must be bounded: with a 50 µs RTT the
    ///   time-based prefetch (`rate × prefetch_time`) is effectively unbounded.
    /// * Delayed ACKs short: a local app with Nagle waits on our ACK.
    /// * Short TIME_WAIT: the peer is the local stack picking fresh ephemeral
    ///   ports; tombstones mostly cost memory on devices with many short flows.
    pub fn client() -> Self {
        StackConfig {
            cc: CcAlgo::Cubic,
            pacing: false,
            init_rcv_wnd: 256 * 1024,
            max_rcv_buf: 4 << 20,
            max_snd_inflight: 2 << 20,
            min_prefetch: 64 * 1024,
            prefetch_max: 512 * 1024,
            delayed_ack: Duration::from_millis(5),
            time_wait: Duration::from_secs(15),
            syn_backlog: 1024,
            accept_backlog: 1024,
            ..StackConfig::default()
        }
    }

    /// Window scale needed so that the max receive buffer can be advertised.
    pub fn rcv_wscale(&self) -> u8 {
        let mut ws = 0u8;
        while (65535u64 << ws) < self.max_rcv_buf as u64 && ws < 14 {
            ws += 1;
        }
        ws
    }
}
