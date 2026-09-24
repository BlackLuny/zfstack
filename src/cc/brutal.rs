//! Brutal: fixed-rate, loss-insensitive sender (product requirement, §9.2).
//! cwnd = rate × srtt × 2, pacing = configured rate.

use super::{AckCtx, CongestionControl};
use crate::time::Instant;

pub struct Brutal {
    rate: u64,
    cwnd: u64,
}

impl Brutal {
    pub fn new(rate: u64, init_cwnd: u64) -> Self {
        Brutal { rate, cwnd: init_cwnd }
    }
}

impl CongestionControl for Brutal {
    fn name(&self) -> &'static str {
        "brutal"
    }
    fn on_ack(&mut self, ctx: &AckCtx) {
        let rtt = if ctx.srtt.is_zero() { ctx.min_rtt } else { ctx.srtt };
        let c = (self.rate as u128 * rtt.as_nanos() * 2 / 1_000_000_000) as u64;
        self.cwnd = c.max(4 * ctx.mss as u64);
    }
    fn on_congestion_event(&mut self, _: Instant, _: u64, _: u32) {}
    fn on_rto(&mut self, _: Instant, _: u32) {}
    fn cwnd(&self) -> u64 {
        self.cwnd
    }
    fn ssthresh(&self) -> u64 {
        0
    }
    fn pacing_rate(&self) -> Option<u64> {
        Some(self.rate)
    }
    fn uses_prr(&self) -> bool {
        false
    }
}
