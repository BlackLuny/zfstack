//! Congestion control framework (§9.1).
//!
//! The connection owns loss detection (RACK-TLP), recovery (PRR) and delivery-rate
//! sampling; algorithms only see the resulting signals.

use crate::time::Instant;
use core::time::Duration;

pub mod bbr;
pub mod brutal;
pub mod cubic;

/// Delivery rate sample (draft-cheng-iccrg-delivery-rate-estimation).
#[derive(Clone, Copy, Debug, Default)]
pub struct RateSample {
    /// Bytes delivered over `interval`.
    pub delivered: u64,
    pub interval: Duration,
    /// `delivered` counter at the time the sampled packet was sent.
    pub prior_delivered: u64,
    pub is_app_limited: bool,
    /// RTT of the most recently sent packet acknowledged by this ACK (None if only retransmits).
    pub rtt: Option<Duration>,
    /// Bytes newly acked or sacked by this ACK.
    pub newly_acked: u64,
    /// Bytes newly marked lost while processing this ACK.
    pub newly_lost: u64,
    /// In-flight bytes before this ACK was processed.
    pub prior_in_flight: u64,
    /// Lost bytes counted by the sampled packet's lifetime (BBR's `rs.lost`).
    pub lost: u64,
    /// `tx_in_flight` recorded when the sampled packet was sent.
    pub tx_in_flight: u64,
}

impl RateSample {
    /// Bytes per second, if the sample is valid.
    pub fn rate(&self) -> Option<u64> {
        if self.interval.is_zero() || self.delivered == 0 {
            return None;
        }
        Some((self.delivered as u128 * 1_000_000_000 / self.interval.as_nanos()) as u64)
    }
}

/// Context passed with every ACK that acknowledged or sacked data.
#[derive(Clone, Copy, Debug)]
pub struct AckCtx {
    pub now: Instant,
    pub mss: u32,
    pub rs: RateSample,
    /// Total delivered bytes so far (connection counter).
    pub delivered: u64,
    pub in_flight: u64,
    pub srtt: Duration,
    pub min_rtt: Duration,
    pub in_recovery: bool,
    /// True at the start of a new delivery round (one RTT of data).
    pub round_start: bool,
    pub round_count: u64,
}

pub trait CongestionControl: Send {
    fn name(&self) -> &'static str;
    fn on_ack(&mut self, ctx: &AckCtx);
    /// A new loss episode starts (first loss detected outside recovery).
    fn on_congestion_event(&mut self, now: Instant, in_flight: u64, mss: u32);
    /// Recovery ended (snd_una passed the recovery point).
    fn on_recovery_exit(&mut self, _now: Instant) {}
    fn on_rto(&mut self, now: Instant, mss: u32);
    /// Detected a spurious retransmission episode; restore prior state if possible.
    fn on_spurious(&mut self) {}
    fn cwnd(&self) -> u64;
    fn ssthresh(&self) -> u64;
    fn in_slow_start(&self) -> bool {
        self.cwnd() < self.ssthresh()
    }
    /// Pacing rate in bytes/second; `None` means "derive from cwnd/srtt".
    fn pacing_rate(&self) -> Option<u64> {
        None
    }
    /// Whether the connection should apply PRR during loss recovery.
    fn uses_prr(&self) -> bool {
        true
    }
    /// Application became idle/restarted after idle.
    fn on_app_restart(&mut self, _now: Instant) {}
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CcAlgo {
    Cubic,
    Bbr,
    /// Fixed rate in bytes/second.
    Brutal(u64),
}

pub fn new_cc(algo: CcAlgo, mss: u32, init_cwnd_segs: u32) -> Box<dyn CongestionControl> {
    let init = mss as u64 * init_cwnd_segs as u64;
    match algo {
        CcAlgo::Cubic => Box::new(cubic::Cubic::new(init, mss)),
        CcAlgo::Bbr => Box::new(bbr::Bbr::new(init, mss)),
        CcAlgo::Brutal(rate) => Box::new(brutal::Brutal::new(rate, init)),
    }
}
