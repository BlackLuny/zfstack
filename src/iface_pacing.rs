//! Aggregate pacing in front of an iface's external FIFO. Packets stay in the
//! existing per-peer DRR queues; a single peer keeps the original fast path.
use crate::Instant;
use core::time::Duration;

const BATCH_NS: i128 = 2_000_000;
const MAX_CREDIT_NS: i128 = 10_000_000;
const CONTENTION_HOLD: Duration = Duration::from_secs(1);

pub(crate) struct IfacePacer {
    rate: u64,
    at: Instant,
    credit: i128,
    batch: i128,
    max_credit: i128,
    until: Instant,
    active: bool,
    deadline: Option<Instant>,
}

impl IfacePacer {
    pub(crate) fn new(rate: u64, mtu: u16) -> Self {
        debug_assert!(rate > 0);
        let mut p =
            Self { rate, at: Instant::ZERO, credit: BATCH_NS, batch: BATCH_NS, max_credit: MAX_CREDIT_NS, until: Instant::ZERO, active: false, deadline: None };
        p.set_mtu(mtu);
        p.credit = p.batch;
        p
    }

    pub(crate) fn set_mtu(&mut self, mtu: u16) {
        let one_packet = (u128::from(mtu) * 1_000_000_000).div_ceil(u128::from(self.rate)) as i128;
        self.batch = BATCH_NS.max(one_packet);
        self.max_credit = MAX_CREDIT_NS.max(self.batch);
    }

    pub(crate) fn allowance(&mut self, now: Instant, competing: bool) -> usize {
        if competing {
            if !self.active || now >= self.until {
                self.at = now;
                self.credit = self.batch;
                self.deadline = None;
            }
            // Keep sparse interactive peers represented between request bursts.
            self.until = now + CONTENTION_HOLD;
        }
        if now >= self.until {
            self.active = false;
            self.deadline = None;
            return usize::MAX;
        }
        self.active = true;
        self.credit = (self.credit + now.saturating_since(self.at).as_nanos() as i128).min(self.max_credit);
        self.at = now.max(self.at);
        if self.deadline.is_some_and(|d| d > now) {
            return 0;
        }
        self.deadline = None;
        let bytes = (self.credit.max(0) as u128 * u128::from(self.rate) / 1_000_000_000).min(usize::MAX as u128) as usize;
        if bytes == 0 {
            self.park();
        }
        bytes
    }

    /// Account accepted IP bytes, including headers. A quantum may overshoot by
    /// one packet; carry that debt rather than refilling on another input event.
    pub(crate) fn charge(&mut self, bytes: usize) -> bool {
        if !self.active || bytes == 0 {
            return false;
        }
        self.credit -= (bytes as u128 * 1_000_000_000).div_ceil(u128::from(self.rate)) as i128;
        if self.credit <= 0 && self.deadline.is_none() {
            self.park();
            return true;
        }
        false
    }

    fn park(&mut self) {
        let delay = (self.batch - self.credit).clamp(1, u64::MAX as i128) as u64;
        self.deadline = Some(self.at + Duration::from_nanos(delay));
    }

    pub(crate) fn deadline(&self) -> Option<Instant> {
        self.deadline
    }

    pub(crate) fn active(&self) -> bool {
        self.active
    }

    pub(crate) fn rate(&self) -> u64 {
        self.rate
    }

    pub(crate) fn set_rate(&mut self, rate: u64, mtu: u16) {
        debug_assert!(rate > 0);
        // Preserve bytes of credit/debt and the contention hold. Reconfiguration
        // must not mint a fresh burst or briefly bypass a paused iface.
        self.credit = self.credit.saturating_mul(self.rate as i128).div_euclid(rate as i128);
        self.rate = rate;
        self.set_mtu(mtu);
        self.credit = self.credit.min(self.max_credit);
        if self.deadline.is_some() {
            self.park();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn single_peer_bypasses_the_clock_and_competition_expires() {
        let mut p = IfacePacer::new(12_500_000, 1420);
        let now = Instant::from_millis(1000);
        assert_eq!(p.allowance(now, false), usize::MAX);
        assert!(!p.charge(10 << 20));
        assert_eq!(p.deadline(), None);
        assert_eq!(p.allowance(now, true), 25_000);
        assert!(p.charge(25_000));
        assert!(p.deadline().unwrap() > now);
        assert_eq!(p.allowance(now + CONTENTION_HOLD, false), usize::MAX);
        assert_eq!(p.deadline(), None);
    }

    #[test]
    fn input_does_not_refill_a_paused_iface_and_late_wakes_catch_up() {
        let mut p = IfacePacer::new(12_500_000, 1420);
        let now = Instant::from_millis(1000);
        assert_eq!(p.allowance(now, true), 25_000);
        assert!(p.charge(26_000));
        let deadline = p.deadline().unwrap();
        for _ in 0..100 {
            assert_eq!(p.allowance(now, true), 0);
            assert_eq!(p.deadline(), Some(deadline));
        }
        // Scheduler lateness can replenish above the nominal 2-ms batch,
        // bounded at 10 ms, so timer granularity does not silently cut rate.
        assert_eq!(p.allowance(now + Duration::from_millis(5), false), 61_500);
        assert_eq!(p.deadline(), None);
        assert_eq!(p.allowance(now + Duration::from_millis(500), false), 125_000);
    }

    #[test]
    fn a_packet_can_progress_at_low_rates_and_accounting_saturates_time() {
        for (rate, mtu) in [(1, 1420), (u64::MAX, u16::MAX)] {
            let mut p = IfacePacer::new(rate, mtu);
            let now = Instant::from_nanos(u64::MAX - 100);
            assert!(p.allowance(now, true) >= usize::from(mtu));
            p.charge(usize::MAX);
            assert!(p.deadline().is_some());
        }
    }

    #[test]
    fn capacity_changes_preserve_competition_and_reprice_outstanding_debt() {
        let mut p = IfacePacer::new(12_500_000, 1420);
        let now = Instant::from_millis(1000);
        p.allowance(now, true);
        assert!(p.charge(27_000));
        let original = p.deadline().unwrap();
        p.set_rate(6_250_000, 1420);
        assert!(p.deadline().unwrap() > original);
        assert_eq!(p.allowance(now, false), 0, "capacity update cannot reset the competition hold");
        p.set_rate(25_000_000, 1420);
        assert!(p.deadline().unwrap() < original);
        let next = p.deadline().unwrap();
        assert!(p.allowance(next, false) > 0);
        assert!(p.active());
    }
}
