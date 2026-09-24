//! CUBIC (RFC 9438) with HyStart++ delay-based slow-start exit (RFC 9406).

use super::{AckCtx, CongestionControl};
use crate::time::Instant;
use core::time::Duration;

const C: f64 = 0.4;
const BETA: f64 = 0.7;
const HS_MIN_RTT_THRESH: Duration = Duration::from_millis(4);
const HS_MAX_RTT_THRESH: Duration = Duration::from_millis(16);
const HS_N_RTT_SAMPLE: u32 = 8;
const HS_CSS_GROWTH_DIVISOR: u64 = 4;
const HS_CSS_ROUNDS: u32 = 5;

pub struct Cubic {
    mss: u32,
    cwnd: u64,
    ssthresh: u64,
    /// W_max in bytes.
    w_max: f64,
    w_last_max: f64,
    k: f64,
    epoch_start: Option<Instant>,
    /// Reno-friendly estimate in bytes.
    w_est: f64,
    cwnd_epoch: f64,
    /// Fractional increase accumulator (bytes).
    acc: f64,
    // HyStart++
    hs_last_round_min: Duration,
    hs_cur_round_min: Duration,
    hs_samples: u32,
    hs_css: Option<(Duration, u32)>,
    // Undo state
    prior_cwnd: u64,
    prior_ssthresh: u64,
    prior_w_max: f64,
}

impl Cubic {
    pub fn new(init_cwnd: u64, mss: u32) -> Self {
        Cubic {
            mss,
            cwnd: init_cwnd,
            ssthresh: u64::MAX,
            w_max: 0.0,
            w_last_max: 0.0,
            k: 0.0,
            epoch_start: None,
            w_est: 0.0,
            cwnd_epoch: 0.0,
            acc: 0.0,
            hs_last_round_min: Duration::MAX,
            hs_cur_round_min: Duration::MAX,
            hs_samples: 0,
            hs_css: None,
            prior_cwnd: 0,
            prior_ssthresh: 0,
            prior_w_max: 0.0,
        }
    }

    fn reduce(&mut self, mss: u32) {
        self.prior_cwnd = self.cwnd;
        self.prior_ssthresh = self.ssthresh;
        self.prior_w_max = self.w_max;
        let cwnd = self.cwnd as f64;
        // Fast convergence.
        if cwnd < self.w_last_max {
            self.w_last_max = cwnd;
            self.w_max = cwnd * (1.0 + BETA) / 2.0;
        } else {
            self.w_last_max = cwnd;
            self.w_max = cwnd;
        }
        self.ssthresh = ((cwnd * BETA) as u64).max(2 * mss as u64);
        self.epoch_start = None;
        self.hs_css = None;
    }

    fn hystart(&mut self, ctx: &AckCtx) {
        if ctx.round_start {
            self.hs_last_round_min = self.hs_cur_round_min;
            self.hs_cur_round_min = Duration::MAX;
            self.hs_samples = 0;
            if let Some((base, rounds)) = self.hs_css.as_mut() {
                *rounds += 1;
                let _ = base;
                if *rounds >= HS_CSS_ROUNDS {
                    // Leave slow start for congestion avoidance.
                    self.ssthresh = self.cwnd;
                    self.hs_css = None;
                    return;
                }
            }
        }
        let Some(rtt) = ctx.rs.rtt else { return };
        self.hs_cur_round_min = self.hs_cur_round_min.min(rtt);
        self.hs_samples += 1;
        if let Some((base, _)) = self.hs_css {
            if self.hs_samples >= HS_N_RTT_SAMPLE && self.hs_cur_round_min < base {
                // RTT went back down: spurious exit, resume slow start.
                self.hs_css = None;
            }
            return;
        }
        if self.hs_samples >= HS_N_RTT_SAMPLE
            && self.hs_last_round_min != Duration::MAX
            && self.hs_cur_round_min != Duration::MAX
        {
            let eta = (self.hs_last_round_min / 8).clamp(HS_MIN_RTT_THRESH, HS_MAX_RTT_THRESH);
            if self.hs_cur_round_min >= self.hs_last_round_min + eta {
                self.hs_css = Some((self.hs_cur_round_min, 0));
            }
        }
    }
}

impl CongestionControl for Cubic {
    fn name(&self) -> &'static str {
        "cubic"
    }

    fn on_ack(&mut self, ctx: &AckCtx) {
        self.mss = ctx.mss;
        let acked = ctx.rs.newly_acked;
        if acked == 0 || ctx.in_recovery {
            return;
        }
        // Do not grow cwnd when not cwnd-limited (RFC 9438 §4.8 / RFC 7661).
        let cwnd_limited = ctx.rs.prior_in_flight + 2 * ctx.mss as u64 >= self.cwnd || self.cwnd < self.ssthresh && ctx.rs.prior_in_flight * 2 >= self.cwnd;
        if !cwnd_limited {
            return;
        }
        if self.cwnd < self.ssthresh {
            self.hystart(ctx);
            let inc = if self.hs_css.is_some() { acked / HS_CSS_GROWTH_DIVISOR } else { acked };
            self.cwnd += inc;
            if self.cwnd >= self.ssthresh {
                self.cwnd = self.ssthresh;
            }
            return;
        }
        let mss = ctx.mss as f64;
        let now = ctx.now;
        if self.epoch_start.is_none() {
            self.epoch_start = Some(now);
            self.cwnd_epoch = self.cwnd as f64;
            self.w_est = self.cwnd as f64;
            if self.w_max < self.cwnd as f64 {
                self.w_max = self.cwnd as f64;
                self.k = 0.0;
            } else {
                // K = cbrt((W_max - cwnd_epoch)/C) in segments.
                self.k = ((self.w_max - self.cwnd as f64) / mss / C).cbrt();
            }
        }
        let t = (now - self.epoch_start.unwrap()).as_secs_f64();
        let rtt = ctx.srtt.as_secs_f64().max(0.001);
        let w_cubic = |t: f64| -> f64 { (C * (t - self.k).powi(3)) * mss + self.w_max };
        let cwnd = self.cwnd as f64;
        let target = w_cubic(t + rtt).clamp(cwnd, 1.5 * cwnd);
        // Reno-friendly region.
        let alpha = 3.0 * (1.0 - BETA) / (1.0 + BETA);
        self.w_est += alpha * (acked as f64 / cwnd) * mss;
        let inc = if w_cubic(t) < self.w_est {
            // Reno-friendly: grow like Reno.
            (self.w_est - cwnd).max(0.0).min(acked as f64)
        } else {
            (target - cwnd) / cwnd * acked as f64
        };
        self.acc += inc;
        if self.acc >= 1.0 {
            let whole = self.acc.floor();
            self.cwnd += whole as u64;
            self.acc -= whole;
        }
    }

    fn on_congestion_event(&mut self, _now: Instant, _in_flight: u64, mss: u32) {
        self.reduce(mss);
        // PRR drives cwnd during recovery; it lands on ssthresh at exit.
    }

    fn on_recovery_exit(&mut self, _now: Instant) {
        self.cwnd = self.ssthresh.max(2 * self.mss as u64);
    }

    fn on_rto(&mut self, _now: Instant, mss: u32) {
        self.reduce(mss);
        self.cwnd = mss as u64;
    }

    fn on_spurious(&mut self) {
        if self.prior_cwnd > 0 {
            self.cwnd = self.cwnd.max(self.prior_cwnd);
            self.ssthresh = self.prior_ssthresh;
            self.w_max = self.prior_w_max;
            self.epoch_start = None;
            self.prior_cwnd = 0;
        }
    }

    fn cwnd(&self) -> u64 {
        self.cwnd
    }
    fn ssthresh(&self) -> u64 {
        self.ssthresh
    }

    fn on_app_restart(&mut self, _now: Instant) {
        // Idle restart: re-anchor the cubic epoch so time spent idle does not count.
        self.epoch_start = None;
    }

    /// Recovery cwnd is set by the connection through PRR.
    fn uses_prr(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cc::RateSample;

    fn ctx(now_ms: u64, acked: u64, cwnd: u64) -> AckCtx {
        AckCtx {
            now: Instant::from_millis(now_ms),
            mss: 1000,
            rs: RateSample { newly_acked: acked, prior_in_flight: cwnd, rtt: Some(Duration::from_millis(50)), ..Default::default() },
            delivered: 0,
            in_flight: cwnd,
            srtt: Duration::from_millis(50),
            min_rtt: Duration::from_millis(50),
            in_recovery: false,
            round_start: false,
            round_count: 0,
        }
    }

    #[test]
    fn slow_start_then_reduce_then_regrow() {
        let mut c = Cubic::new(10_000, 1000);
        for _ in 0..10 {
            let w = c.cwnd();
            c.on_ack(&ctx(0, 1000, w));
        }
        assert_eq!(c.cwnd(), 20_000);
        c.on_congestion_event(Instant::ZERO, 20_000, 1000);
        assert_eq!(c.ssthresh(), 14_000);
        c.on_recovery_exit(Instant::ZERO);
        assert_eq!(c.cwnd(), 14_000);
        let mut t = 0;
        // Concave growth back towards W_max within a few seconds.
        for _ in 0..2000 {
            t += 5;
            let w = c.cwnd();
            c.on_ack(&ctx(t, 1000, w));
        }
        assert!(c.cwnd() > 20_000, "cwnd {}", c.cwnd());
    }
}
