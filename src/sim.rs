//! Deterministic simulator (§14.1): virtual clock, two shards joined by a modelled
//! link (bandwidth, propagation delay, drop-tail bottleneck queue, random loss,
//! reordering, duplication) and an injectable execution gap `G` (the shard only gets
//! CPU every `G`). Same seed → identical run.

use crate::shard::{EgressSinks, OutPacket, SendResult};
use crate::time::Instant;
use crate::{ConnId, Event, IfaceId, PeerId, ReadResult, Shard, WriteResult};
use bytes::Bytes;
use core::time::Duration;
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::net::SocketAddr;

/// xorshift64* — tiny deterministic RNG.
#[derive(Clone)]
pub struct Rng(u64);
impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1)
    }
    pub fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    pub fn chance(&mut self, p: f64) -> bool {
        p > 0.0 && (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64 <= p
    }
    pub fn below(&mut self, n: u64) -> u64 {
        if n == 0 {
            0
        } else {
            self.next_u64() % n
        }
    }
}

#[derive(Clone, Debug)]
pub struct LinkParams {
    /// Bottleneck rate in bits/s (0 = infinite).
    pub rate_bps: u64,
    /// One-way propagation delay.
    pub delay: Duration,
    /// Bottleneck queue limit in bytes (drop-tail).
    pub queue_bytes: usize,
    pub loss: f64,
    /// Probability that a packet is delayed by an extra `reorder_delay`.
    pub reorder: f64,
    pub reorder_delay: Duration,
    pub duplicate: f64,
    /// Gilbert-style burst loss: once a loss happens, the next packets are lost with this probability.
    pub burst_continue: f64,
}

impl Default for LinkParams {
    fn default() -> Self {
        LinkParams {
            rate_bps: 100_000_000,
            delay: Duration::from_millis(10),
            queue_bytes: 256 * 1024,
            loss: 0.0,
            reorder: 0.0,
            reorder_delay: Duration::from_millis(2),
            duplicate: 0.0,
            burst_continue: 0.0,
        }
    }
}

#[derive(Default, Debug, Clone)]
pub struct LinkStats {
    pub sent: u64,
    pub delivered: u64,
    pub queue_drops: u64,
    pub random_drops: u64,
    pub bytes_delivered: u64,
}

pub struct Link {
    pub p: LinkParams,
    busy_until: Instant,
    /// (departure time, bytes) of packets still in the bottleneck queue.
    queued: VecDeque<(Instant, usize)>,
    queued_bytes: usize,
    inflight: BTreeMap<(Instant, u64), (PeerId, Bytes)>,
    seq: u64,
    in_burst: bool,
    pub stats: LinkStats,
}

impl Link {
    pub fn new(p: LinkParams) -> Self {
        Link {
            p,
            busy_until: Instant::ZERO,
            queued: VecDeque::new(),
            queued_bytes: 0,
            inflight: BTreeMap::new(),
            seq: 0,
            in_burst: false,
            stats: LinkStats::default(),
        }
    }

    pub fn enqueue(&mut self, now: Instant, peer: PeerId, pkt: Bytes, rng: &mut Rng) {
        self.stats.sent += 1;
        while let Some(&(dep, n)) = self.queued.front() {
            if dep <= now {
                self.queued.pop_front();
                self.queued_bytes -= n;
            } else {
                break;
            }
        }
        let len = pkt.len();
        if self.p.rate_bps > 0 && self.queued_bytes + len > self.p.queue_bytes {
            self.stats.queue_drops += 1;
            return;
        }
        let lose = if self.in_burst { rng.chance(self.p.burst_continue) } else { rng.chance(self.p.loss) };
        self.in_burst = lose && self.p.burst_continue > 0.0;
        if lose {
            self.stats.random_drops += 1;
            return;
        }
        let dep = if self.p.rate_bps > 0 {
            let start = if self.busy_until > now { self.busy_until } else { now };
            let tx = Duration::from_nanos((len as u128 * 8 * 1_000_000_000 / self.p.rate_bps as u128) as u64);
            self.busy_until = start + tx;
            self.queued.push_back((self.busy_until, len));
            self.queued_bytes += len;
            self.busy_until
        } else {
            now
        };
        let mut at = dep + self.p.delay;
        if rng.chance(self.p.reorder) {
            at += self.p.reorder_delay;
        }
        self.seq += 1;
        self.inflight.insert((at, self.seq), (peer, pkt.clone()));
        if rng.chance(self.p.duplicate) {
            self.seq += 1;
            self.inflight.insert((at + Duration::from_micros(10), self.seq), (peer, pkt));
        }
    }

    pub fn next_delivery(&self) -> Option<Instant> {
        self.inflight.keys().next().map(|k| k.0)
    }

    pub fn pop_due(&mut self, now: Instant) -> Option<(PeerId, Bytes)> {
        let (&k, _) = self.inflight.iter().next()?;
        if k.0 > now {
            return None;
        }
        let v = self.inflight.remove(&k).unwrap();
        self.stats.delivered += 1;
        self.stats.bytes_delivered += v.1.len() as u64;
        Some(v)
    }
}

/// A sink that feeds a link, optionally refusing packets (to exercise `Full`).
struct LinkSink<'a> {
    now: Instant,
    link: &'a mut Link,
    rng: &'a mut Rng,
    budget: Option<&'a mut usize>,
    tokens: Option<&'a mut f64>,
    full: Option<&'a mut bool>,
}

impl EgressSinks for LinkSink<'_> {
    fn send(&mut self, _iface: IfaceId, pkt: &OutPacket<'_>) -> SendResult {
        if let Some(b) = self.budget.as_mut() {
            if **b == 0 {
                return SendResult::Full;
            }
            **b -= 1;
        }
        if let Some(t) = self.tokens.as_mut() {
            if **t < pkt.len() as f64 {
                if let Some(f) = self.full.as_mut() {
                    **f = true;
                }
                return SendResult::Full;
            }
            **t -= pkt.len() as f64;
        }
        self.link.enqueue(self.now, pkt.peer, Bytes::from(pkt.to_vec()), self.rng);
        SendResult::Accepted
    }
}

pub fn pattern_byte(i: u64) -> u8 {
    (i % 251) as u8
}

/// Per-connection application behaviour.
#[derive(Clone, Debug, Default)]
pub struct AppConn {
    /// Bytes still to write (u64::MAX = forever).
    pub to_send: u64,
    pub sent: u64,
    pub received: u64,
    /// Stop reading until this time (zero-window tests).
    pub read_paused_until: Instant,
    pub eof: bool,
    pub closed: Option<crate::CloseReason>,
    /// Shut the write side once `to_send` is exhausted.
    pub fin_after_send: bool,
    pub fin_sent: bool,
    pub corrupt: bool,
    /// Handle closed by the app.
    pub released: bool,
}

/// Side of the simulation: a shard plus its application.
pub struct Side {
    pub shard: Shard,
    pub iface: IfaceId,
    pub conns: HashMap<ConnId, AppConn>,
    /// Default behaviour for connections accepted on this side.
    pub accept_template: AppConn,
    pub accepted: Vec<ConnId>,
    pub events: Vec<Event>,
    pub egress_budget: Option<usize>,
    /// Local egress rate limit in bits/s (bottleneck at the sink, §8.2/§8.3).
    pub egress_rate: Option<u64>,
    egress_tokens: f64,
    egress_last: Instant,
    egress_full: bool,
    buf: Vec<u8>,
}

impl Side {
    pub fn new(shard: Shard, iface: IfaceId) -> Self {
        Side {
            shard,
            iface,
            conns: HashMap::new(),
            accept_template: AppConn::default(),
            accepted: Vec::new(),
            events: Vec::new(),
            egress_budget: None,
            egress_rate: None,
            egress_tokens: 0.0,
            egress_last: Instant::ZERO,
            egress_full: false,
            buf: vec![0u8; 256 * 1024],
        }
    }

    fn app(&mut self, now: Instant) {
        while let Some(ev) = self.shard.poll_event() {
            self.events.push(ev);
            match ev {
                Event::Accepted(id) => {
                    self.accepted.push(id);
                    self.conns.insert(id, self.accept_template.clone());
                }
                Event::Connected(_) => {}
                Event::Closed(id, r) => {
                    if let Some(c) = self.conns.get_mut(&id) {
                        c.closed = Some(r);
                    }
                }
                _ => {}
            }
        }
        let ids: Vec<ConnId> = self.conns.keys().copied().collect();
        for id in ids {
            self.pump(now, id);
        }
    }

    fn pump(&mut self, now: Instant, id: ConnId) {
        let Some(c) = self.conns.get_mut(&id) else { return };
        if c.released {
            return;
        }
        // Abnormal close: release immediately. Orderly close: drain reads first.
        if c.closed.is_some_and(|r| r != crate::CloseReason::Normal) || (c.closed.is_some() && c.eof) {
            c.released = true;
            self.shard.close(now, id);
            return;
        }
        // Read.
        if now >= c.read_paused_until && !c.eof {
            loop {
                match self.shard.read(now, id, &mut self.buf) {
                    ReadResult::Data(n) => {
                        for (k, &b) in self.buf[..n].iter().enumerate() {
                            if b != pattern_byte(c.received + k as u64) {
                                c.corrupt = true;
                            }
                        }
                        c.received += n as u64;
                    }
                    ReadResult::Eof => {
                        c.eof = true;
                        break;
                    }
                    ReadResult::WouldBlock => break,
                    ReadResult::Closed(r) => {
                        c.closed = Some(r);
                        c.released = true;
                        self.shard.close(now, id);
                        return;
                    }
                }
            }
        }
        if c.closed.is_some() {
            if c.eof || now < c.read_paused_until {
                if c.eof {
                    c.released = true;
                    self.shard.close(now, id);
                }
                return;
            }
        }
        // Write.
        while c.sent < c.to_send {
            let n = ((c.to_send - c.sent) as usize).min(64 * 1024);
            for (k, b) in self.buf[..n].iter_mut().enumerate() {
                *b = pattern_byte(c.sent + k as u64);
            }
            match self.shard.write(id, &self.buf[..n]) {
                WriteResult::Written(w) => c.sent += w as u64,
                _ => break,
            }
        }
        if c.fin_after_send && !c.fin_sent && c.sent >= c.to_send {
            c.fin_sent = true;
            self.shard.shutdown_write(id);
        }
        // Done in both directions: release the handle.
        if c.eof && (c.fin_sent || (c.to_send == 0 && c.fin_after_send)) {
            c.released = true;
            self.shard.close(now, id);
        }
    }
}

pub struct Sim {
    pub now: Instant,
    pub rng: Rng,
    /// Server (passive) side.
    pub a: Side,
    /// Client (active, test-peer) side.
    pub b: Side,
    pub ab: Link,
    pub ba: Link,
    /// Execution gap for side A (0 = runs whenever needed).
    pub gap_a: Duration,
    pub check_invariants: bool,
    pub steps: u64,
}

pub fn addr(s: &str) -> SocketAddr {
    s.parse().unwrap()
}

impl Sim {
    pub fn new(seed: u64, cfg_a: crate::StackConfig, cfg_b: crate::StackConfig, down: LinkParams, up: LinkParams) -> Self {
        let mut sa = Shard::with_budget(cfg_a, crate::budget::GlobalBudget::new(1 << 30));
        let ia = sa.add_iface(Default::default());
        let mut sb = Shard::with_budget(cfg_b, crate::budget::GlobalBudget::new(1 << 30));
        let ib = sb.add_iface(Default::default());
        Sim {
            now: Instant::from_millis(1000),
            rng: Rng::new(seed),
            a: Side::new(sa, ia),
            b: Side::new(sb, ib),
            ab: Link::new(down),
            ba: Link::new(up),
            gap_a: Duration::ZERO,
            check_invariants: true,
            steps: 0,
        }
    }

    /// Open a connection from B (client) to A (server).
    pub fn connect(&mut self, port: u16, client: AppConn) -> ConnId {
        self.connect_as(PeerId(1), port, client)
    }

    /// Open a connection whose packets A attributes to WG peer `peer`.
    pub fn connect_as(&mut self, peer: PeerId, port: u16, client: AppConn) -> ConnId {
        let local = addr(&format!("10.0.0.2:{port}"));
        let remote = addr("10.0.0.1:80");
        let id = self.b.shard.connect(self.now, self.b.iface, peer, local, remote);
        self.b.conns.insert(id, client);
        id
    }

    fn quantize_a(&self, t: Instant) -> Instant {
        if self.gap_a.is_zero() {
            return t;
        }
        let g = self.gap_a.as_nanos() as u64;
        Instant::from_nanos(t.as_nanos().div_ceil(g) * g)
    }

    fn next_time(&self) -> Option<Instant> {
        let mut t: Option<Instant> = None;
        let mut take = |x: Option<Instant>| {
            if let Some(x) = x {
                t = Some(t.map_or(x, |y: Instant| y.min(x)));
            }
        };
        take(self.a.shard.next_deadline().map(|d| self.quantize_a(d.max(self.now))));
        take(self.b.shard.next_deadline());
        take(self.ab.next_delivery());
        take(self.ba.next_delivery().map(|d| self.quantize_a(d)));
        if let (Some(rate), true) = (self.a.egress_rate, self.a.egress_full) {
            let need = (1500.0 - self.a.egress_tokens).max(0.0) * 8.0 / rate as f64;
            take(Some(self.quantize_a(self.now + Duration::from_secs_f64(need) + Duration::from_nanos(1))));
        }
        t
    }

    /// Advance until `until` (or until nothing is scheduled).
    pub fn run_until(&mut self, until: Instant) {
        let mut same = 0u32;
        let mut last = self.now;
        loop {
            if self.now == last {
                same += 1;
                if same > 99_990 {
                    eprintln!(
                        "stuck: now={:?} a.dl={:?} b.dl={:?} ab={:?} ba={:?} a.stats={:?}",
                        self.now,
                        self.a.shard.next_deadline(),
                        self.b.shard.next_deadline(),
                        self.ab.next_delivery(),
                        self.ba.next_delivery(),
                        self.a.shard.stats()
                    );
                }
                assert!(same < 100_000, "simulation stuck at {:?}", self.now);
            } else {
                same = 0;
                last = self.now;
            }
            let Some(t) = self.next_time() else {
                self.now = until;
                // Nothing scheduled: still let the apps react once (e.g. resumed reads).
                self.step();
                if self.next_time().is_some_and(|t| t <= until) {
                    continue;
                }
                break;
            };
            if t > until {
                self.now = until;
                // Let apps react (e.g. read pauses ending) once at the boundary.
                self.step();
                break;
            }
            if t > self.now {
                self.now = t;
            }
            self.step();
        }
    }

    fn step(&mut self) {
        self.steps += 1;
        let now = self.now;
        let a_may_run = self.gap_a.is_zero() || self.quantize_a(now) == now;
        // Deliver packets.
        while let Some((peer, pkt)) = self.ab.pop_due(now) {
            let _ = peer;
            self.b.shard.ingress(now, self.b.iface, PeerId(0), pkt);
        }
        if a_may_run {
            while let Some((peer, pkt)) = self.ba.pop_due(now) {
                self.a.shard.ingress(now, self.a.iface, peer, pkt);
            }
        }
        for _ in 0..64 {
            let mut progressed = false;
            if a_may_run {
                self.a.app(now);
                if let Some(rate) = self.a.egress_rate {
                    let dt = now.saturating_since(self.a.egress_last).as_secs_f64();
                    self.a.egress_last = now;
                    // Bucket depth: 2 ms worth of bytes (egress_delay_cap, §7.1).
                    let cap = rate as f64 / 8.0 * 0.002;
                    self.a.egress_tokens = (self.a.egress_tokens + dt * rate as f64 / 8.0).min(cap);
                    if self.a.egress_full && self.a.egress_tokens >= 1500.0 {
                        self.a.egress_full = false;
                        self.a.shard.egress_released(self.a.iface);
                    }
                }
                let mut budget = self.a.egress_budget;
                let limited = self.a.egress_rate.is_some();
                let (tokens, full) = (&mut self.a.egress_tokens, &mut self.a.egress_full);
                let mut sink = LinkSink {
                    now,
                    link: &mut self.ab,
                    rng: &mut self.rng,
                    budget: budget.as_mut(),
                    tokens: if limited { Some(tokens) } else { None },
                    full: if limited { Some(full) } else { None },
                };
                let o = self.a.shard.run(now, &mut sink);
                self.a.egress_budget = budget;
                progressed |= o.packets > 0 || o.more;
            }
            self.b.app(now);
            let mut sink = LinkSink { now, link: &mut self.ba, rng: &mut self.rng, budget: None, tokens: None, full: None };
            let o = self.b.shard.run(now, &mut sink);
            progressed |= o.packets > 0 || o.more;
            if self.check_invariants {
                self.a.shard.check_invariants();
                self.b.shard.check_invariants();
            }
            if !progressed {
                break;
            }
        }
    }
}

#[cfg(test)]
mod tests;
