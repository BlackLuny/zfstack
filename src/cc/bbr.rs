//! BBRv3, following draft-ietf-ccwg-bbr (variable names from the draft:
//! `inflight_longterm` / `inflight_shortterm`, `bw_shortterm`, …) (§9.2).
//!
//! The connection provides delivery-rate samples (with `lost` / `tx_in_flight` of the
//! sampled packet) and round boundaries; BBR owns cwnd and pacing rate and does not
//! use PRR.

use super::{AckCtx, CongestionControl, RateSample};
use crate::time::Instant;
use core::time::Duration;

const STARTUP_PACING_GAIN: f64 = 2.77;
const STARTUP_CWND_GAIN: f64 = 2.0;
const DRAIN_PACING_GAIN: f64 = 0.35;
const DEFAULT_CWND_GAIN: f64 = 2.0;
const PROBE_UP_CWND_GAIN: f64 = 2.25;
const PACING_MARGIN: f64 = 0.01;
const LOSS_THRESH: f64 = 0.02;
const BETA: f64 = 0.7;
const HEADROOM: f64 = 0.15;
const FULL_BW_THRESH: f64 = 1.25;
const FULL_BW_COUNT: u32 = 3;
const STARTUP_FULL_LOSS_CNT: u32 = 6;
const PROBE_RTT_DURATION: Duration = Duration::from_millis(200);
const PROBE_RTT_INTERVAL: Duration = Duration::from_secs(5);
const MIN_RTT_FILTER_LEN: Duration = Duration::from_secs(10);
const EXTRA_ACKED_FILTER_LEN: u64 = 10;

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Mode {
    Startup,
    Drain,
    ProbeBwDown,
    ProbeBwCruise,
    ProbeBwRefill,
    ProbeBwUp,
    ProbeRtt,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum AckPhase {
    Init,
    ProbeStopping,
    Refilling,
    ProbeStarting,
    ProbeFeedback,
}

pub struct Bbr {
    mss: u64,
    mode: Mode,
    cwnd: u64,
    prior_cwnd: u64,
    pacing_rate: u64,
    pacing_gain: f64,
    cwnd_gain: f64,

    // Bandwidth model (bytes/s).
    max_bw: u64,
    max_bw_filter: [u64; 2],
    cycle_count: u64,
    bw_shortterm: u64,
    bw_latest: u64,
    inflight_latest: u64,
    inflight_longterm: u64,
    inflight_shortterm: u64,

    // RTT model.
    min_rtt: Duration,
    min_rtt_stamp: Instant,
    probe_rtt_min_delay: Duration,
    probe_rtt_min_stamp: Instant,
    probe_rtt_done_stamp: Option<Instant>,
    probe_rtt_round_done: bool,

    // Startup.
    full_bw: u64,
    full_bw_count: u32,
    full_bw_reached: bool,
    full_bw_now: bool,

    // Loss rounds.
    loss_round_delivered: u64,
    loss_in_round: bool,
    loss_events_in_round: u32,

    // ProbeBW.
    ack_phase: AckPhase,
    cycle_stamp: Instant,
    bw_probe_wait: Duration,
    rounds_since_bw_probe: u64,
    bw_probe_up_rounds: u32,
    bw_probe_up_acks: u64,
    probe_up_cnt: u64,
    bw_probe_samples: bool,
    rng: u64,

    // Ack aggregation.
    extra_acked: [u64; 2],
    extra_acked_win_rounds: u64,
    extra_acked_win_idx: usize,
    ack_epoch_start: Instant,
    ack_epoch_acked: u64,

    in_recovery: bool,
    idle_restart: bool,
    round_count: u64,
}

impl Bbr {
    pub fn new(init_cwnd: u64, mss: u32) -> Self {
        let now = Instant::ZERO;
        let mut b = Bbr {
            mss: mss as u64,
            mode: Mode::Startup,
            cwnd: init_cwnd,
            prior_cwnd: 0,
            pacing_rate: 0,
            pacing_gain: STARTUP_PACING_GAIN,
            cwnd_gain: STARTUP_CWND_GAIN,
            max_bw: 0,
            max_bw_filter: [0; 2],
            cycle_count: 0,
            bw_shortterm: u64::MAX,
            bw_latest: 0,
            inflight_latest: 0,
            inflight_longterm: u64::MAX,
            inflight_shortterm: u64::MAX,
            min_rtt: Duration::MAX,
            min_rtt_stamp: now,
            probe_rtt_min_delay: Duration::MAX,
            probe_rtt_min_stamp: now,
            probe_rtt_done_stamp: None,
            probe_rtt_round_done: false,
            full_bw: 0,
            full_bw_count: 0,
            full_bw_reached: false,
            full_bw_now: false,
            loss_round_delivered: 0,
            loss_in_round: false,
            loss_events_in_round: 0,
            ack_phase: AckPhase::Init,
            cycle_stamp: now,
            bw_probe_wait: Duration::from_secs(2),
            rounds_since_bw_probe: 0,
            bw_probe_up_rounds: 0,
            bw_probe_up_acks: 0,
            probe_up_cnt: u64::MAX,
            bw_probe_samples: false,
            rng: 0x243F_6A88_85A3_08D3,
            extra_acked: [0; 2],
            extra_acked_win_rounds: 0,
            extra_acked_win_idx: 0,
            ack_epoch_start: now,
            ack_epoch_acked: 0,
            in_recovery: false,
            idle_restart: false,
            round_count: 0,
        };
        // Initial pacing: startup gain × cwnd / 1 ms (refined after the first RTT sample).
        b.pacing_rate = (STARTUP_PACING_GAIN * init_cwnd as f64 / 0.001) as u64;
        b
    }

    pub fn mode(&self) -> Mode {
        self.mode
    }

    fn rand(&mut self) -> u64 {
        let mut x = self.rng;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.rng = x;
        x
    }

    fn bw(&self) -> u64 {
        self.max_bw.min(self.bw_shortterm)
    }

    fn min_pipe_cwnd(&self) -> u64 {
        4 * self.mss
    }

    /// gain × BDP in bytes.
    fn bdp_multiple(&self, bw: u64, gain: f64) -> u64 {
        if self.min_rtt == Duration::MAX || bw == 0 {
            return self.cwnd.max(10 * self.mss);
        }
        (gain * bw as f64 * self.min_rtt.as_secs_f64()) as u64
    }

    fn quantization_budget(&self, inflight: u64) -> u64 {
        let mut i = inflight.max(self.min_pipe_cwnd()) + 3 * self.send_quantum();
        if self.mode == Mode::ProbeBwUp {
            i += 2 * self.mss;
        }
        i
    }

    fn inflight(&self, bw: u64, gain: f64) -> u64 {
        self.quantization_budget(self.bdp_multiple(bw, gain))
    }

    fn send_quantum(&self) -> u64 {
        let q = self.pacing_rate / 1000; // 1 ms
        q.clamp((2 * self.mss).min((64 * 1024).max(self.mss)), (64 * 1024).max(self.mss))
    }

    fn inflight_with_headroom(&self) -> u64 {
        if self.inflight_longterm == u64::MAX {
            return u64::MAX;
        }
        let headroom = ((HEADROOM * self.inflight_longterm as f64) as u64).max(self.mss);
        self.inflight_longterm.saturating_sub(headroom).max(self.min_pipe_cwnd())
    }

    fn target_inflight(&self) -> u64 {
        self.bdp_multiple(self.bw(), 1.0).min(self.cwnd)
    }

    // ---- bandwidth filter ----

    fn update_max_bw(&mut self, rs: &RateSample) {
        let Some(rate) = rs.rate() else { return };
        if rate >= self.max_bw || !rs.is_app_limited {
            let slot = (self.cycle_count % 2) as usize;
            self.max_bw_filter[slot] = self.max_bw_filter[slot].max(rate);
            self.max_bw = self.max_bw_filter[0].max(self.max_bw_filter[1]);
        }
    }

    fn advance_max_bw_filter(&mut self) {
        self.cycle_count += 1;
        let slot = (self.cycle_count % 2) as usize;
        self.max_bw_filter[slot] = 0;
        self.max_bw = self.max_bw_filter[0].max(self.max_bw_filter[1]);
    }

    // ---- congestion signals ----

    fn update_latest_delivery_signals(&mut self, ctx: &AckCtx) -> bool {
        let rs = &ctx.rs;
        let mut loss_round_start = false;
        if let Some(rate) = rs.rate() {
            self.bw_latest = self.bw_latest.max(rate);
        }
        self.inflight_latest = self.inflight_latest.max(rs.delivered);
        if rs.prior_delivered >= self.loss_round_delivered && rs.delivered > 0 {
            self.loss_round_delivered = ctx.delivered;
            loss_round_start = true;
        }
        loss_round_start
    }

    fn advance_latest_delivery_signals(&mut self, loss_round_start: bool, rs: &RateSample) {
        if loss_round_start {
            self.bw_latest = rs.rate().unwrap_or(0);
            self.inflight_latest = rs.delivered;
        }
    }

    fn is_probing_bw(&self) -> bool {
        matches!(self.mode, Mode::Startup | Mode::ProbeBwRefill | Mode::ProbeBwUp)
    }

    fn update_congestion_signals(&mut self, rs: &RateSample, loss_round_start: bool) {
        self.update_max_bw(rs);
        if rs.newly_lost > 0 {
            self.loss_in_round = true;
            self.loss_events_in_round += 1;
        }
        if !loss_round_start {
            return;
        }
        if self.loss_in_round && !self.is_probing_bw() {
            // Adapt short-term bounds to the loss (§ BBRAdaptLowerBoundsFromCongestion).
            if self.bw_shortterm == u64::MAX {
                self.bw_shortterm = self.max_bw;
            }
            if self.inflight_shortterm == u64::MAX {
                self.inflight_shortterm = self.cwnd;
            }
            self.bw_shortterm = self.bw_latest.max((BETA * self.bw_shortterm as f64) as u64);
            self.inflight_shortterm = self.inflight_latest.max((BETA * self.inflight_shortterm as f64) as u64);
        }
        self.loss_in_round = false;
    }

    fn reset_shortterm_model(&mut self) {
        self.bw_shortterm = u64::MAX;
        self.inflight_shortterm = u64::MAX;
    }

    fn is_inflight_too_high(&self, rs: &RateSample) -> bool {
        rs.tx_in_flight > 0 && rs.lost as f64 > rs.tx_in_flight as f64 * LOSS_THRESH
    }

    fn handle_inflight_too_high(&mut self, rs: &RateSample, now: Instant) {
        self.bw_probe_samples = false;
        if !rs.is_app_limited {
            let target = (self.target_inflight() as f64 * BETA) as u64;
            self.inflight_longterm = rs.tx_in_flight.max(target).max(self.min_pipe_cwnd());
        }
        if self.mode == Mode::ProbeBwUp {
            if std::env::var_os("ZF_BBR_TRACE").is_some() {
                eprintln!("UP->DOWN inflight too high: lost={} tx_in_flight={}", rs.lost, rs.tx_in_flight);
            }
            self.start_probe_bw_down(now);
        }
    }

    // ---- aggregation ----

    fn update_ack_aggregation(&mut self, ctx: &AckCtx) {
        let acked = ctx.rs.newly_acked;
        let interval = ctx.now.saturating_since(self.ack_epoch_start).as_secs_f64();
        let expected = (self.bw() as f64 * interval) as u64;
        if self.ack_epoch_acked <= expected || self.ack_epoch_acked + acked >= self.cwnd.max(1) * 4 {
            self.ack_epoch_acked = 0;
            self.ack_epoch_start = ctx.now;
        }
        self.ack_epoch_acked += acked;
        let extra = self.ack_epoch_acked.saturating_sub((self.bw() as f64 * ctx.now.saturating_since(self.ack_epoch_start).as_secs_f64()) as u64);
        let extra = extra.min(self.cwnd);
        if ctx.round_start {
            self.extra_acked_win_rounds += 1;
            if self.extra_acked_win_rounds >= EXTRA_ACKED_FILTER_LEN {
                self.extra_acked_win_rounds = 0;
                self.extra_acked_win_idx ^= 1;
                self.extra_acked[self.extra_acked_win_idx] = 0;
            }
        }
        let i = self.extra_acked_win_idx;
        self.extra_acked[i] = self.extra_acked[i].max(extra);
    }

    fn extra_acked(&self) -> u64 {
        self.extra_acked[0].max(self.extra_acked[1])
    }

    // ---- startup / drain ----

    fn check_full_bw_reached(&mut self, ctx: &AckCtx) {
        if self.full_bw_now || !ctx.round_start || ctx.rs.is_app_limited {
            return;
        }
        if self.max_bw as f64 >= self.full_bw as f64 * FULL_BW_THRESH {
            self.full_bw = self.max_bw;
            self.full_bw_count = 0;
            return;
        }
        self.full_bw_count += 1;
        self.full_bw_now = self.full_bw_count >= FULL_BW_COUNT;
        if self.full_bw_now {
            self.full_bw_reached = true;
        }
    }

    fn reset_full_bw(&mut self) {
        self.full_bw = 0;
        self.full_bw_count = 0;
        self.full_bw_now = false;
    }

    fn check_startup_high_loss(&mut self, ctx: &AckCtx, loss_round_start: bool) {
        if self.full_bw_reached || self.mode != Mode::Startup {
            return;
        }
        if loss_round_start && self.loss_events_in_round >= STARTUP_FULL_LOSS_CNT && self.is_inflight_too_high(&ctx.rs) {
            self.inflight_longterm = self.bdp_multiple(self.max_bw, 1.0).max(self.inflight_latest);
            self.full_bw_reached = true;
        }
        if loss_round_start {
            self.loss_events_in_round = 0;
        }
    }

    fn check_startup_done(&mut self, ctx: &AckCtx, loss_round_start: bool) {
        self.check_startup_high_loss(ctx, loss_round_start);
        if self.mode == Mode::Startup && self.full_bw_reached {
            self.mode = Mode::Drain;
            self.pacing_gain = DRAIN_PACING_GAIN;
            self.cwnd_gain = STARTUP_CWND_GAIN;
        }
    }

    fn check_drain_done(&mut self, ctx: &AckCtx) {
        if self.mode == Mode::Drain && ctx.in_flight <= self.inflight(self.max_bw, 1.0) {
            self.start_probe_bw_down(ctx.now);
        }
    }

    // ---- ProbeBW cycle ----

    fn start_round(&mut self) {
        // Rounds are driven by the connection's delivered counter; nothing to do here.
    }

    fn pick_probe_wait(&mut self) {
        self.rounds_since_bw_probe = self.rand() % 2;
        let jitter_ms = self.rand() % 1000;
        self.bw_probe_wait = Duration::from_millis(2000 + jitter_ms);
    }

    fn start_probe_bw_down(&mut self, now: Instant) {
        self.reset_congestion_signals();
        self.probe_up_cnt = u64::MAX;
        self.pick_probe_wait();
        self.cycle_stamp = now;
        self.ack_phase = AckPhase::ProbeStopping;
        self.start_round();
        self.mode = Mode::ProbeBwDown;
        self.pacing_gain = 0.90;
        self.cwnd_gain = DEFAULT_CWND_GAIN;
    }

    fn start_probe_bw_cruise(&mut self) {
        self.mode = Mode::ProbeBwCruise;
        self.pacing_gain = 1.0;
        self.cwnd_gain = DEFAULT_CWND_GAIN;
    }

    fn start_probe_bw_refill(&mut self) {
        self.reset_shortterm_model();
        self.bw_probe_up_rounds = 0;
        self.bw_probe_up_acks = 0;
        self.ack_phase = AckPhase::Refilling;
        self.start_round();
        self.mode = Mode::ProbeBwRefill;
        self.pacing_gain = 1.0;
        self.cwnd_gain = DEFAULT_CWND_GAIN;
    }

    fn start_probe_bw_up(&mut self, now: Instant) {
        self.ack_phase = AckPhase::ProbeStarting;
        self.start_round();
        self.reset_full_bw();
        self.full_bw = self.max_bw;
        self.cycle_stamp = now;
        self.mode = Mode::ProbeBwUp;
        self.pacing_gain = 1.25;
        self.cwnd_gain = PROBE_UP_CWND_GAIN;
        self.raise_inflight_longterm_slope();
    }

    fn reset_congestion_signals(&mut self) {
        self.loss_in_round = false;
        self.bw_latest = 0;
        self.inflight_latest = 0;
    }

    fn raise_inflight_longterm_slope(&mut self) {
        let growth = self.mss << self.bw_probe_up_rounds.min(30);
        self.bw_probe_up_rounds = (self.bw_probe_up_rounds + 1).min(30);
        self.probe_up_cnt = (self.cwnd / growth.max(1)).max(1);
    }

    fn probe_inflight_longterm_upward(&mut self, ctx: &AckCtx) {
        // Byte-based cwnd may trail inflight_longterm by a few bytes; allow one MSS.
        if !ctx.cwnd_limited || self.cwnd + self.mss < self.inflight_longterm {
            return;
        }
        self.bw_probe_up_acks += ctx.rs.newly_acked;
        if self.bw_probe_up_acks >= self.probe_up_cnt {
            let delta = self.bw_probe_up_acks / self.probe_up_cnt;
            self.bw_probe_up_acks -= delta * self.probe_up_cnt;
            self.inflight_longterm = self.inflight_longterm.saturating_add(delta);
        }
        if ctx.round_start {
            self.raise_inflight_longterm_slope();
        }
    }

    fn adapt_longterm_model(&mut self, ctx: &AckCtx) {
        if self.ack_phase == AckPhase::ProbeStarting && ctx.round_start {
            self.ack_phase = AckPhase::ProbeFeedback;
        }
        if self.ack_phase == AckPhase::ProbeStopping && ctx.round_start {
            self.bw_probe_samples = false;
            self.ack_phase = AckPhase::Init;
            if matches!(self.mode, Mode::ProbeBwDown | Mode::ProbeBwCruise | Mode::ProbeBwRefill | Mode::ProbeBwUp) {
                self.advance_max_bw_filter();
            }
        }
        if self.is_inflight_too_high(&ctx.rs) {
            if self.bw_probe_samples || self.mode == Mode::ProbeBwUp {
                self.handle_inflight_too_high(&ctx.rs, ctx.now);
            }
            return;
        }
        if self.inflight_longterm == u64::MAX {
            return;
        }
        if ctx.rs.tx_in_flight > self.inflight_longterm {
            self.inflight_longterm = ctx.rs.tx_in_flight;
        }
        if self.mode == Mode::ProbeBwUp {
            self.probe_inflight_longterm_upward(ctx);
        }
    }

    fn has_elapsed_in_phase(&self, now: Instant, d: Duration) -> bool {
        now > self.cycle_stamp + d
    }

    fn is_reno_coexistence_probe_time(&self) -> bool {
        let reno_rounds = self.target_inflight() / self.mss.max(1);
        let rounds = reno_rounds.min(63);
        self.rounds_since_bw_probe >= rounds
    }

    fn is_time_to_probe_bw(&mut self, now: Instant) -> bool {
        if self.has_elapsed_in_phase(now, self.bw_probe_wait) || self.is_reno_coexistence_probe_time() {
            self.start_probe_bw_refill();
            return true;
        }
        false
    }

    fn is_time_to_cruise(&self, in_flight: u64) -> bool {
        if in_flight > self.inflight_with_headroom() {
            return false;
        }
        in_flight <= self.inflight(self.max_bw, 1.0)
    }

    fn is_time_to_go_down(&mut self, ctx: &AckCtx) -> bool {
        if ctx.cwnd_limited && self.cwnd + self.mss >= self.inflight_longterm {
            self.reset_full_bw();
            self.full_bw = self.max_bw;
        } else if self.full_bw_now {
            return true;
        }
        false
    }

    fn update_probe_bw_cycle_phase(&mut self, ctx: &AckCtx) {
        if !self.full_bw_reached {
            return;
        }
        self.adapt_longterm_model(ctx);
        if !matches!(self.mode, Mode::ProbeBwDown | Mode::ProbeBwCruise | Mode::ProbeBwRefill | Mode::ProbeBwUp) {
            return;
        }
        if ctx.round_start {
            self.rounds_since_bw_probe += 1;
        }
        match self.mode {
            Mode::ProbeBwDown => {
                if self.is_time_to_probe_bw(ctx.now) {
                    return;
                }
                if self.is_time_to_cruise(ctx.in_flight) {
                    self.start_probe_bw_cruise();
                }
            }
            Mode::ProbeBwCruise => {
                self.is_time_to_probe_bw(ctx.now);
            }
            Mode::ProbeBwRefill => {
                if ctx.round_start {
                    self.bw_probe_samples = true;
                    self.start_probe_bw_up(ctx.now);
                }
            }
            Mode::ProbeBwUp => {
                // Plateau detection inside UP reuses the full-bw estimator.
                if ctx.round_start && !ctx.rs.is_app_limited {
                    if self.max_bw as f64 >= self.full_bw as f64 * FULL_BW_THRESH {
                        self.full_bw = self.max_bw;
                        self.full_bw_count = 0;
                    } else {
                        self.full_bw_count += 1;
                        self.full_bw_now = self.full_bw_count >= FULL_BW_COUNT;
                    }
                }
                if self.is_time_to_go_down(ctx) {
                    if std::env::var_os("ZF_BBR_TRACE").is_some() {
                        eprintln!(
                            "UP->DOWN go_down: cwnd_limited={} cwnd={} ilt={} count={} rs.prior_in_flight={}",
                            ctx.cwnd_limited, self.cwnd, self.inflight_longterm, self.full_bw_count, ctx.rs.prior_in_flight
                        );
                    }
                    self.start_probe_bw_down(ctx.now);
                }
            }
            _ => {}
        }
    }

    // ---- ProbeRTT ----

    fn update_min_rtt(&mut self, ctx: &AckCtx) {
        let now = ctx.now;
        let probe_rtt_expired = now > self.probe_rtt_min_stamp + PROBE_RTT_INTERVAL;
        if let Some(rtt) = ctx.rs.rtt {
            if rtt < self.probe_rtt_min_delay || probe_rtt_expired {
                self.probe_rtt_min_delay = rtt;
                self.probe_rtt_min_stamp = now;
            }
        }
        let min_rtt_expired = now > self.min_rtt_stamp + MIN_RTT_FILTER_LEN;
        if self.probe_rtt_min_delay < self.min_rtt || min_rtt_expired {
            self.min_rtt = self.probe_rtt_min_delay;
            self.min_rtt_stamp = self.probe_rtt_min_stamp;
        }
    }

    fn probe_rtt_cwnd(&self) -> u64 {
        self.bdp_multiple(self.bw(), 0.5).max(self.min_pipe_cwnd())
    }

    fn check_probe_rtt(&mut self, ctx: &AckCtx) {
        let now = ctx.now;
        if self.mode != Mode::ProbeRtt && now > self.probe_rtt_min_stamp + PROBE_RTT_INTERVAL && !self.idle_restart && self.min_rtt != Duration::MAX {
            self.mode = Mode::ProbeRtt;
            self.pacing_gain = 1.0;
            self.cwnd_gain = 0.5;
            self.save_cwnd();
            self.probe_rtt_done_stamp = None;
            self.ack_phase = AckPhase::ProbeStopping;
            self.start_round();
        }
        if self.mode == Mode::ProbeRtt {
            self.handle_probe_rtt(ctx);
        }
        if ctx.rs.delivered > 0 {
            self.idle_restart = false;
        }
    }

    fn handle_probe_rtt(&mut self, ctx: &AckCtx) {
        let now = ctx.now;
        match self.probe_rtt_done_stamp {
            None => {
                if ctx.in_flight <= self.probe_rtt_cwnd() {
                    self.probe_rtt_done_stamp = Some(now + PROBE_RTT_DURATION);
                    self.probe_rtt_round_done = false;
                    self.start_round();
                }
            }
            Some(done) => {
                if ctx.round_start {
                    self.probe_rtt_round_done = true;
                }
                if self.probe_rtt_round_done && now > done {
                    self.probe_rtt_min_stamp = now;
                    self.restore_cwnd();
                    // Exit ProbeRTT.
                    self.reset_shortterm_model();
                    if self.full_bw_reached {
                        self.start_probe_bw_down(now);
                        self.start_probe_bw_cruise();
                    } else {
                        self.mode = Mode::Startup;
                        self.pacing_gain = STARTUP_PACING_GAIN;
                        self.cwnd_gain = STARTUP_CWND_GAIN;
                    }
                }
            }
        }
    }

    fn save_cwnd(&mut self) {
        self.prior_cwnd = if !self.in_recovery && self.mode != Mode::ProbeRtt { self.cwnd } else { self.cwnd.max(self.prior_cwnd) };
    }

    fn restore_cwnd(&mut self) {
        self.cwnd = self.cwnd.max(self.prior_cwnd);
    }

    // ---- control parameters ----

    fn set_pacing_rate(&mut self) {
        let bw = self.bw();
        if bw == 0 {
            return;
        }
        let rate = (self.pacing_gain * bw as f64 * (1.0 - PACING_MARGIN)) as u64;
        if self.full_bw_reached || rate > self.pacing_rate {
            self.pacing_rate = rate;
        }
    }

    fn bound_cwnd_for_model(&mut self) {
        let mut cap = u64::MAX;
        if matches!(self.mode, Mode::ProbeBwDown | Mode::ProbeBwRefill | Mode::ProbeBwUp) {
            cap = self.inflight_longterm;
        } else if matches!(self.mode, Mode::ProbeRtt | Mode::ProbeBwCruise) {
            cap = self.inflight_with_headroom();
        }
        cap = cap.min(self.inflight_shortterm).max(self.min_pipe_cwnd());
        self.cwnd = self.cwnd.min(cap);
    }

    fn set_cwnd(&mut self, ctx: &AckCtx) {
        let acked = ctx.rs.newly_acked;
        let max_inflight = self.inflight(self.bw(), self.cwnd_gain) + self.extra_acked();
        if self.in_recovery {
            // Packet conservation during the first round of recovery.
            self.cwnd = self.cwnd.saturating_sub(ctx.rs.newly_lost).max(ctx.in_flight + acked).max(self.mss);
        }
        if self.full_bw_reached {
            self.cwnd = (self.cwnd + acked).min(max_inflight);
        } else if self.cwnd < max_inflight || ctx.delivered < 10 * self.mss {
            self.cwnd += acked;
        }
        self.cwnd = self.cwnd.max(self.min_pipe_cwnd());
        if self.mode == Mode::ProbeRtt {
            self.cwnd = self.cwnd.min(self.probe_rtt_cwnd());
        }
        self.bound_cwnd_for_model();
    }
}

impl CongestionControl for Bbr {
    fn name(&self) -> &'static str {
        "bbr3"
    }

    fn on_ack(&mut self, ctx: &AckCtx) {
        self.mss = ctx.mss as u64;
        self.round_count = ctx.round_count;
        if !ctx.in_recovery && self.in_recovery {
            self.in_recovery = false;
            self.restore_cwnd();
        } else if ctx.in_recovery && !self.in_recovery {
            self.in_recovery = true;
            self.save_cwnd();
        }
        let loss_round_start = self.update_latest_delivery_signals(ctx);
        self.update_congestion_signals(&ctx.rs, loss_round_start);
        self.update_ack_aggregation(ctx);
        self.check_full_bw_reached(ctx);
        self.check_startup_done(ctx, loss_round_start);
        self.check_drain_done(ctx);
        self.update_probe_bw_cycle_phase(ctx);
        self.update_min_rtt(ctx);
        self.check_probe_rtt(ctx);
        self.advance_latest_delivery_signals(loss_round_start, &ctx.rs);
        // Bound bw for the model.
        self.set_pacing_rate();
        if self.pacing_rate == 0 || self.max_bw == 0 {
            // No bandwidth sample yet: pace from cwnd / srtt.
            let srtt = ctx.srtt.as_secs_f64().max(0.0005);
            self.pacing_rate = (STARTUP_PACING_GAIN * self.cwnd as f64 / srtt) as u64;
        }
        self.set_cwnd(ctx);
    }

    fn on_congestion_event(&mut self, _now: Instant, _in_flight: u64, _mss: u32) {
        // BBRv3 reacts to loss through rate samples (inflight_longterm / shortterm).
    }

    fn on_recovery_exit(&mut self, _now: Instant) {
        if self.in_recovery {
            self.in_recovery = false;
            self.restore_cwnd();
        }
    }

    fn on_rto(&mut self, _now: Instant, mss: u32) {
        self.save_cwnd();
        self.in_recovery = true;
        self.cwnd = mss as u64 * 4;
    }

    fn cwnd(&self) -> u64 {
        self.cwnd
    }

    fn ssthresh(&self) -> u64 {
        if self.full_bw_reached {
            self.cwnd
        } else {
            u64::MAX
        }
    }

    fn pacing_rate(&self) -> Option<u64> {
        if self.pacing_rate == 0 {
            None
        } else {
            Some(self.pacing_rate)
        }
    }

    fn uses_prr(&self) -> bool {
        false
    }

    fn debug(&self) -> String {
        let f = |v: u64| if v == u64::MAX { "inf".to_string() } else { v.to_string() };
        format!(
            "mode={:?} cwnd={} max_bw={} bw_st={} infl_lt={} infl_st={} pacing={} min_rtt={:?} full={} extra={}",
            self.mode,
            self.cwnd,
            self.max_bw,
            f(self.bw_shortterm),
            f(self.inflight_longterm),
            f(self.inflight_shortterm),
            self.pacing_rate,
            self.min_rtt,
            self.full_bw_reached,
            self.extra_acked()
        )
    }

    fn on_app_restart(&mut self, _now: Instant) {
        self.idle_restart = true;
        if matches!(self.mode, Mode::ProbeBwDown | Mode::ProbeBwCruise | Mode::ProbeBwRefill | Mode::ProbeBwUp) {
            self.pacing_rate = self.bw().max(self.pacing_rate / 2);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_exits_on_plateau_and_paces_near_bw() {
        let mss = 1000u32;
        let mut b = Bbr::new(10_000, mss);
        let bw = 10_000_000u64; // 10 MB/s
        let rtt = Duration::from_millis(20);
        let mut delivered = 0u64;
        let mut now = Instant::from_millis(1);
        for round in 0..40u64 {
            for i in 0..10 {
                delivered += 20_000;
                let rs = RateSample {
                    delivered: bw / 50,
                    interval: rtt,
                    prior_delivered: delivered - 20_000,
                    rtt: Some(rtt),
                    newly_acked: 20_000,
                    prior_in_flight: b.cwnd(),
                    tx_in_flight: b.cwnd(),
                    ..Default::default()
                };
                let ctx = AckCtx {
                    now,
                    mss,
                    rs,
                    delivered,
                    in_flight: b.cwnd().min(bw / 50),
                    srtt: rtt,
                    min_rtt: rtt,
                    in_recovery: false,
                    round_start: i == 0,
                    round_count: round,
                    cwnd_limited: true,
                };
                b.on_ack(&ctx);
                now += rtt / 10;
            }
        }
        assert_ne!(b.mode(), Mode::Startup);
        let pr = b.pacing_rate().unwrap() as f64;
        assert!(pr > bw as f64 * 0.8 && pr < bw as f64 * 1.3, "pacing {pr}");
        // cwnd ≈ 2×BDP (200 KB) plus budgets.
        assert!(b.cwnd() > 150_000 && b.cwnd() < 500_000, "cwnd {}", b.cwnd());
    }
}
