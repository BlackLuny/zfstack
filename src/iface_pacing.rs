//! Aggregate pacing in front of an iface's external FIFO. Packets stay in the
//! existing per-peer DRR queues; a single peer keeps the original fast path.
use crate::adaptive_pacing::{AdaptivePacer, AdaptiveUpdate};
use crate::Instant;
use core::time::Duration;

const BATCH_NS: i128 = 2_000_000;
const MAX_CREDIT_NS: i128 = 10_000_000;
const CONTENTION_HOLD: Duration = Duration::from_secs(1);

/// Keep the configured rate and inactive bit in the iface's hot state. The
/// clock is allocated only when distinct peers first become ready together.
#[derive(Default)]
pub(crate) struct IfacePacing {
    rate: u64,
    active: bool,
    adaptive: bool,
    clock: Option<Box<IfacePacer>>,
}

impl IfacePacing {
    pub(crate) fn set_rate(&mut self, rate: u64, mtu: u16) {
        if self.rate == rate && !self.adaptive {
            return;
        }
        if self.adaptive {
            self.clock = None;
            self.active = false;
        }
        self.adaptive = false;
        self.rate = rate;
        if rate == 0 {
            self.active = false;
            self.clock = None;
        } else if let Some(clock) = self.clock.as_mut() {
            clock.set_rate(rate, mtu);
        }
    }

    pub(crate) fn set_adaptive(&mut self, enabled: bool) {
        if self.adaptive == enabled && (enabled || self.rate == 0) {
            return;
        }
        self.adaptive = enabled;
        self.rate = 0;
        self.active = false;
        self.clock = None;
    }

    pub(crate) fn tracks_delivery(&self) -> bool {
        self.adaptive && self.active
    }

    pub(crate) fn on_delivery(&mut self, now: Instant, wire_bytes: u64, bulk_rtt: Option<(Duration, Duration)>, mtu: u16) -> Option<AdaptiveUpdate> {
        if !self.tracks_delivery() {
            return None;
        }
        let clock = self.clock.as_mut()?;
        let update = clock.adaptive.as_mut()?.observe(now, wire_bytes, bulk_rtt, mtu)?;
        clock.set_rate(update.rate, mtu);
        Some(update)
    }

    pub(crate) fn set_mtu(&mut self, mtu: u16) {
        if let Some(clock) = self.clock.as_mut() {
            clock.set_mtu(mtu);
        }
    }

    pub(crate) fn allowance(&mut self, now: Instant, competing: bool, mtu: u16) -> (usize, bool) {
        if (self.rate == 0 && !self.adaptive) || (!competing && !self.active) {
            return (usize::MAX, false);
        }
        if self.adaptive {
            return self.adaptive_allowance(now, competing, mtu);
        }
        let clock = self.clock.get_or_insert_with(|| Box::new(IfacePacer::new(self.rate, mtu)));
        let bytes = clock.allowance(now, competing);
        self.active = clock.active();
        (bytes, self.active)
    }

    #[inline(never)]
    fn adaptive_allowance(&mut self, now: Instant, competing: bool, mtu: u16) -> (usize, bool) {
        let clock = self.clock.get_or_insert_with(|| Box::new(IfacePacer::new(1, mtu)));
        if now >= clock.until && clock.deadline.is_none_or(|d| now >= d) {
            clock.adaptive = Some(Box::new(AdaptivePacer::new(now)));
            clock.active = false;
            clock.deadline = None;
        }
        if competing {
            clock.until = now + CONTENTION_HOLD;
        }
        if now >= clock.until {
            self.active = false;
            clock.active = false;
            clock.deadline = None;
            return (usize::MAX, false);
        }
        self.active = true;
        if clock.adaptive.as_ref().is_none_or(|a| a.rate() == 0) {
            // First observe actual delivery without an invented initial cap.
            return (usize::MAX, false);
        }
        if !clock.active {
            clock.at = now;
            clock.credit = clock.batch;
            clock.deadline = None;
        }
        let bytes = clock.allowance(now, competing);
        (bytes, clock.active())
    }

    pub(crate) fn charge(&mut self, bytes: usize) -> bool {
        debug_assert!(self.active);
        let clock = self.clock.as_mut().unwrap();
        if let Some(adaptive) = clock.adaptive.as_mut() {
            adaptive.sent(bytes);
        }
        clock.charge(bytes)
    }

    pub(crate) fn deadline(&self) -> Option<Instant> {
        // An inactive iface never follows the pointer into the cold clock.
        if self.active {
            self.clock.as_ref().unwrap().deadline()
        } else {
            None
        }
    }
}

pub(crate) struct IfacePacer {
    rate: u64,
    at: Instant,
    credit: i128,
    batch: i128,
    max_credit: i128,
    until: Instant,
    active: bool,
    deadline: Option<Instant>,
    adaptive: Option<Box<AdaptivePacer>>,
}

impl IfacePacer {
    pub(crate) fn new(rate: u64, mtu: u16) -> Self {
        debug_assert!(rate > 0);
        let mut p = Self {
            rate,
            at: Instant::ZERO,
            credit: BATCH_NS,
            batch: BATCH_NS,
            max_credit: MAX_CREDIT_NS,
            until: Instant::ZERO,
            active: false,
            deadline: None,
            adaptive: None,
        };
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
            if !self.active || (now >= self.until && self.deadline.is_none_or(|d| now >= d)) {
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
    fn adaptive_single_peer_is_lazy_and_modes_are_idempotent() {
        assert_eq!(core::mem::size_of::<IfacePacing>(), 24);
        let mut p = IfacePacing::default();
        p.set_adaptive(true);
        for i in 0..100 {
            assert_eq!(p.allowance(Instant::from_millis(i * 500), false, 1420), (usize::MAX, false));
            assert!(!p.tracks_delivery());
            assert!(p.clock.is_none());
        }
        let now = Instant::from_millis(50_000);
        assert_eq!(p.allowance(now, true, 1420), (usize::MAX, false));
        assert!(p.tracks_delivery());
        let update = p.on_delivery(now + Duration::from_millis(100), 1_250_000, Some((Duration::from_millis(10), Duration::from_millis(10))), 1420).unwrap();
        assert!(update.rate > 0);
        let (allowance, active) = p.allowance(now + Duration::from_millis(100), true, 1420);
        assert!(active && allowance > 0 && allowance < usize::MAX);
        assert!(p.charge(allowance));
        let deadline = p.deadline();
        p.set_adaptive(true);
        assert_eq!(p.deadline(), deadline);
        assert_eq!(p.clock.as_ref().unwrap().adaptive.as_ref().unwrap().rate(), update.rate);
        p.set_rate(0, 1420);
        assert!(!p.tracks_delivery() && p.clock.is_none());
        p.set_rate(12_500_000, 1420);
        p.set_adaptive(false);
        assert_eq!(p.allowance(now, true, 1420), (usize::MAX, false));
    }

    #[test]
    fn adaptive_restarts_learning_after_contention_expires() {
        let mut p = IfacePacing::default();
        p.set_adaptive(true);
        let now = Instant::from_millis(1);
        p.allowance(now, true, 1420);
        p.on_delivery(now + Duration::from_millis(100), 1_250_000, Some((Duration::from_millis(10), Duration::from_millis(10))), 1420).unwrap();
        p.allowance(now + Duration::from_millis(100), true, 1420);
        assert_eq!(p.allowance(now + Duration::from_secs(2), false, 1420), (usize::MAX, false));
        assert!(!p.tracks_delivery());
        assert_eq!(p.deadline(), None);
        assert_eq!(p.allowance(now + Duration::from_secs(3), true, 1420), (usize::MAX, false));
        assert_eq!(p.clock.as_ref().unwrap().adaptive.as_ref().unwrap().rate(), 0);
    }

    #[test]
    fn lazy_clock_uses_current_capacity_and_mtu_when_competition_first_arrives() {
        let mut p = IfacePacing::default();
        let now = Instant::from_millis(1000);
        p.set_rate(1000, 1420);
        p.set_rate(100, 1500);
        p.set_mtu(1500);
        for n in 0..10 {
            assert_eq!(p.allowance(now + Duration::from_secs(n), false, 1500), (usize::MAX, false));
            assert!(p.clock.is_none(), "single peer must not allocate a pacing clock");
            assert_eq!(p.deadline(), None);
        }
        let at = now + Duration::from_secs(10);
        assert_eq!(p.allowance(at, true, 1500), (1500, true));
        assert!(p.charge(1500));
        let deadline = p.deadline().unwrap();
        p.set_rate(100, 1500);
        assert_eq!(p.deadline(), Some(deadline), "same-rate update must not reset debt");
        p.set_rate(50, 1500);
        assert!(p.deadline().unwrap() > deadline);
        assert_eq!(p.allowance(at + Duration::from_secs(2), true, 1500), (0, true));
        p.set_rate(0, 1500);
        assert_eq!(p.deadline(), None);
        assert!(p.clock.is_none());
        assert_eq!(p.allowance(at, true, 1500), (usize::MAX, false));
    }

    #[test]
    fn inactive_clock_reactivates_with_the_same_credit_as_the_eager_clock() {
        let mut lazy = IfacePacing::default();
        lazy.set_rate(12_500_000, 1420);
        let mut eager = IfacePacer::new(12_500_000, 1420);
        let now = Instant::from_millis(1000);
        for (offset, competing, charge) in [(0, true, 26_000), (1, true, 0), (5, false, 27_000), (1100, false, 0), (1200, false, 0), (2000, true, 26_000)] {
            let at = now + Duration::from_millis(offset);
            let allowance = eager.allowance(at, competing);
            assert_eq!(lazy.allowance(at, competing, 1420), (allowance, eager.active()));
            if charge > 0 {
                assert_eq!(lazy.charge(charge), eager.charge(charge));
            }
            assert_eq!(lazy.deadline(), eager.deadline());
        }
        eprintln!("iface pacing hot state={} bytes; cold clock={} bytes", std::mem::size_of::<IfacePacing>(), std::mem::size_of::<IfacePacer>());
    }

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

    #[test]
    fn expired_contention_hold_cannot_refill_a_future_low_rate_deadline() {
        let mut p = IfacePacer::new(100, 1420);
        let now = Instant::from_millis(1000);
        assert_eq!(p.allowance(now, true), 1420);
        assert!(p.charge(1420));
        let deadline = p.deadline().unwrap();
        assert!(deadline > now + CONTENTION_HOLD);
        for seconds in [1, 3, 8] {
            assert_eq!(p.allowance(now + Duration::from_secs(seconds), true), 0);
            assert_eq!(p.deadline(), Some(deadline));
        }
        assert!(p.allowance(deadline, true) >= 1420);
    }
}
