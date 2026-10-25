//! Experimental, per-iface delivery/queue feedback. No configured link rate.
//!
//! Samples are summed over wall-clock windows, never sums of per-flow maxima.
//! Control work runs once per >= 2 RTT window. Periodic upward probes prevent
//! paced delivery from becoming a monotonically shrinking capacity estimate.
use crate::Instant;
use core::time::Duration;

#[derive(Clone, Copy, Debug)]
pub(crate) struct AdaptiveUpdate {
    pub rate: u64,
    pub delivery_rate: u64,
    pub queue_us: u64,
    pub probe: bool,
}

pub(crate) struct AdaptivePacer {
    start: Instant,
    delivered: u64,
    sent: u64,
    queue_sum: u128,
    queue_weight: u64,
    base_rtt: Duration,
    rate: u64,
    demand_windows: u8,
}

impl AdaptivePacer {
    pub fn new(now: Instant) -> Self {
        Self { start: now, delivered: 0, sent: 0, queue_sum: 0, queue_weight: 0, base_rtt: Duration::ZERO, rate: 0, demand_windows: 0 }
    }

    pub fn rate(&self) -> u64 {
        self.rate
    }

    pub fn sent(&mut self, bytes: usize) {
        self.sent = self.sent.saturating_add(bytes as u64);
    }

    fn window(&self) -> Duration {
        self.base_rtt.saturating_mul(2).max(Duration::from_millis(100))
    }

    fn clear_window(&mut self, now: Instant) {
        self.start = now;
        self.delivered = 0;
        self.sent = 0;
        self.queue_sum = 0;
        self.queue_weight = 0;
        self.base_rtt = Duration::ZERO;
    }

    pub fn observe(&mut self, now: Instant, wire_bytes: u64, bulk_rtt: Option<(Duration, Duration)>, mtu: u16) -> Option<AdaptiveUpdate> {
        self.delivered = self.delivered.saturating_add(wire_bytes);
        if let Some((rtt, base)) = bulk_rtt.filter(|(_, base)| !base.is_zero()) {
            self.base_rtt = self.base_rtt.max(base);
            let queue_ns = rtt.saturating_sub(base).as_nanos().min(u64::MAX as u128);
            self.queue_sum = self.queue_sum.saturating_add(queue_ns.saturating_mul(wire_bytes as u128));
            self.queue_weight = self.queue_weight.saturating_add(wire_bytes);
        }
        let elapsed = now.saturating_since(self.start);
        let window = self.window();
        if elapsed < window {
            return None;
        }
        // A sparse application or an idle restart is not evidence of a slow
        // physical link. Collect a fresh window instead of pricing its silence.
        if elapsed > window.saturating_mul(4) || self.queue_weight == 0 || self.delivered < u64::from(mtu) * 4 {
            self.clear_window(now);
            return None;
        }
        let delivery = (self.delivered as u128 * 1_000_000_000 / elapsed.as_nanos()).min(u64::MAX as u128) as u64;
        let queue_ns = (self.queue_sum / self.queue_weight as u128).min(u64::MAX as u128) as u64;
        let target_ns = (self.base_rtt / 64).clamp(Duration::from_millis(1), Duration::from_millis(5)).as_nanos();
        let expected = self.rate as u128 * elapsed.as_nanos() / 1_000_000_000;
        let demand = self.sent as u128 * 4 >= expected * 3;
        let mut probe = false;
        if self.rate == 0 {
            self.rate = scale(delivery, 99);
        } else if demand {
            self.demand_windows = (self.demand_windows + 1) % 4;
            if queue_ns as u128 > target_ns && delivery as u128 * 4 < self.rate as u128 * 3 {
                // A substantial capacity fall must not wait for an old peak
                // filter to expire. Subsequent probes can recover an underestimate.
                self.rate = scale(delivery, 99);
            } else if self.demand_windows == 0 {
                self.rate = scale(self.rate.max(delivery), 108);
                probe = true;
            } else if queue_ns as u128 > target_ns {
                self.rate = scale(self.rate, 98).min(scale(delivery, 99));
            } else {
                self.rate = scale(self.rate, 102).max(scale(delivery, 101));
            }
        }
        let update = AdaptiveUpdate { rate: self.rate, delivery_rate: delivery, queue_us: queue_ns / 1000, probe };
        self.clear_window(now);
        Some(update)
    }
}

fn scale(rate: u64, percent: u64) -> u64 {
    (rate as u128 * percent as u128 / 100).clamp(1, u64::MAX as u128) as u64
}

#[cfg(test)]
mod tests {
    use super::*;

    fn epoch(p: &mut AdaptivePacer, now_ms: u64, delivered_rate: u64, sent_rate: u64, rtt_ms: u64, base_ms: u64) -> Option<AdaptiveUpdate> {
        let elapsed_ms = Instant::from_millis(now_ms).saturating_since(p.start).as_millis() as u64;
        p.sent((sent_rate as u128 * elapsed_ms as u128 / 1000) as usize);
        p.observe(
            Instant::from_millis(now_ms),
            (delivered_rate as u128 * elapsed_ms as u128 / 1000) as u64,
            Some((Duration::from_millis(rtt_ms), Duration::from_millis(base_ms))),
            1420,
        )
    }

    #[test]
    fn learns_aggregate_delivery_without_a_capacity_input_and_probes_up() {
        let mut p = AdaptivePacer::new(Instant::ZERO);
        let initial = epoch(&mut p, 100, 12_500_000, 13_000_000, 10, 10).unwrap();
        assert_eq!(initial.rate, 12_375_000);
        for i in 2..=9 {
            let rate = p.rate();
            epoch(&mut p, i * 100, rate, rate, 10, 10).unwrap();
        }
        assert!(p.rate() > initial.rate, "paced delivery must not shrink by another 1% each window");
    }

    #[test]
    fn persistent_external_delay_does_not_disable_upward_probes() {
        let mut p = AdaptivePacer::new(Instant::ZERO);
        epoch(&mut p, 100, 12_500_000, 13_000_000, 20, 10).unwrap();
        let initial = p.rate();
        let mut probes = 0;
        for i in 2..=41 {
            let rate = p.rate();
            probes += u32::from(epoch(&mut p, i * 100, rate, rate, 20, 10).unwrap().probe);
        }
        assert_eq!(probes, 10);
        assert!(p.rate() >= initial, "irreducible delay must not cause unbounded multiplicative collapse");
    }

    #[test]
    fn app_limited_windows_do_not_erase_capacity() {
        let mut p = AdaptivePacer::new(Instant::ZERO);
        epoch(&mut p, 100, 12_500_000, 13_000_000, 10, 10).unwrap();
        let rate = p.rate();
        for i in 2..=9 {
            epoch(&mut p, i * 100, 100_000, 100_000, 10, 10).unwrap();
        }
        assert_eq!(p.rate(), rate);
    }

    #[test]
    fn capacity_drop_reacts_without_waiting_for_a_historical_maximum() {
        let mut p = AdaptivePacer::new(Instant::ZERO);
        epoch(&mut p, 100, 12_500_000, 13_000_000, 10, 10).unwrap();
        let rate = p.rate();
        let update = epoch(&mut p, 200, 1_250_000, rate, 60, 10).unwrap();
        assert_eq!(update.rate, 1_237_500);
        assert!(!update.probe);
    }

    #[test]
    fn waits_two_rtts_and_discards_idle_gaps_and_non_bulk_samples() {
        let mut p = AdaptivePacer::new(Instant::ZERO);
        assert!(epoch(&mut p, 500, 12_500_000, 13_000_000, 500, 500).is_none());
        assert_eq!(p.rate(), 0);
        assert!(p.observe(Instant::from_millis(1000), 1_000_000, Some((Duration::from_millis(500), Duration::from_millis(500))), 1420).is_some());
        let rate = p.rate();
        assert!(epoch(&mut p, 10_000, 100_000, 100_000, 500, 500).is_none());
        assert_eq!(p.rate(), rate);
        assert!(p.observe(Instant::from_millis(11_000), 1_000_000, None, 1420).is_none());
    }
}
