//! RTT estimation (RFC 6298) and windowed min-RTT.

use crate::time::Instant;
use core::time::Duration;

const MIN_RTT_WINDOW: Duration = Duration::from_secs(10);

#[derive(Clone, Debug)]
pub struct RttEstimator {
    pub srtt: Duration,
    pub rttvar: Duration,
    pub rto: Duration,
    pub has_sample: bool,
    pub min_rtt: Duration,
    min_rtt_stamp: Instant,
    pub latest: Duration,
    min_rto: Duration,
    max_rto: Duration,
}

impl RttEstimator {
    pub fn new(init_rto: Duration, min_rto: Duration, max_rto: Duration) -> Self {
        RttEstimator {
            srtt: Duration::ZERO,
            rttvar: Duration::ZERO,
            rto: init_rto,
            has_sample: false,
            min_rtt: Duration::MAX,
            min_rtt_stamp: Instant::ZERO,
            latest: Duration::ZERO,
            min_rto,
            max_rto,
        }
    }

    pub fn sample(&mut self, now: Instant, rtt: Duration) {
        let rtt = rtt.max(Duration::from_micros(1));
        self.latest = rtt;
        if rtt <= self.min_rtt || now.saturating_since(self.min_rtt_stamp) > MIN_RTT_WINDOW {
            self.min_rtt = rtt;
            self.min_rtt_stamp = now;
        }
        if !self.has_sample {
            self.srtt = rtt;
            self.rttvar = rtt / 2;
            self.has_sample = true;
        } else {
            let diff = if self.srtt > rtt { self.srtt - rtt } else { rtt - self.srtt };
            self.rttvar = (self.rttvar * 3 + diff) / 4;
            self.srtt = (self.srtt * 7 + rtt) / 8;
        }
        self.rto = (self.srtt + (self.rttvar * 4).max(Duration::from_millis(1))).clamp(self.min_rto, self.max_rto);
    }

    /// Smoothed RTT, or `fallback` before the first sample.
    pub fn srtt_or(&self, fallback: Duration) -> Duration {
        if self.has_sample {
            self.srtt
        } else {
            fallback
        }
    }

    pub fn min_rtt_or(&self, fallback: Duration) -> Duration {
        if self.min_rtt == Duration::MAX {
            fallback
        } else {
            self.min_rtt
        }
    }

    pub fn max_rto(&self) -> Duration {
        self.max_rto
    }
}
