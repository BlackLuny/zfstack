//! Shard: single-threaded owner of a set of connections (§3, §8).
//!
//! Scheduling state per connection (§8.1): ready (per-peer DRR queues), pacing heap,
//! egress-blocked set of its iface, timer heap. Each round only touches connections
//! with events, so cost scales with active connections, not total connections.

use crate::budget::{Budget, GlobalBudget, Pressure};
use crate::buf::BlockPool;
use crate::config::StackConfig;
use crate::conn::{Conn, Ctx, Plan, PlanKind, ReadResult, State, SynParams, WriteResult};
use crate::heap::IndexedHeap;
use crate::seq::Seq;
use crate::time::Instant;
use crate::wire::{self, EmitOptions, EmitParams, TcpHeader, ACK, FIN, MAX_HEADER, RST, SYN};
use crate::{CloseReason, ConnId, ConnInfo, Event, IfaceId, PeerId};
use bytes::Bytes;
use core::time::Duration;
use std::collections::hash_map::RandomState;
use std::collections::{HashMap, VecDeque};
use std::hash::{BuildHasher, Hash, Hasher};
use std::net::SocketAddr;
use std::sync::Arc;

#[derive(Clone, Debug)]
pub struct IfaceConfig {
    /// IP MTU of the tunnel (e.g. 1420 for WireGuard).
    pub mtu: u16,
}

impl Default for IfaceConfig {
    fn default() -> Self {
        IfaceConfig { mtu: 1420 }
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Admission {
    Accept,
    /// Policy reject: reply RST (§10.2).
    Reject,
    /// Drop silently.
    Drop,
}

/// Synchronous admission decision for incoming SYNs (§4.1).
pub trait AdmissionPolicy: Send {
    fn on_syn(&mut self, iface: IfaceId, peer: PeerId, remote: SocketAddr, local: SocketAddr) -> Admission;
}

pub struct AcceptAll;
impl AdmissionPolicy for AcceptAll {
    fn on_syn(&mut self, _: IfaceId, _: PeerId, _: SocketAddr, _: SocketAddr) -> Admission {
        Admission::Accept
    }
}

/// An outgoing IP packet: header plus up to two payload slices (§4.2).
pub struct OutPacket<'a> {
    pub peer: PeerId,
    pub header: &'a [u8],
    pub payload: [&'a [u8]; 2],
}

impl OutPacket<'_> {
    pub fn len(&self) -> usize {
        self.header.len() + self.payload[0].len() + self.payload[1].len()
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// Append the contiguous packet to `out`.
    pub fn write_to(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(self.header);
        out.extend_from_slice(self.payload[0]);
        out.extend_from_slice(self.payload[1]);
    }
    pub fn to_vec(&self) -> Vec<u8> {
        let mut v = Vec::with_capacity(self.len());
        self.write_to(&mut v);
        v
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum SendResult {
    /// Committed to the sink's bounded queue (state ②, §4.3).
    Accepted,
    /// Sink full: the packet was not taken and does not count as sent.
    Full,
}

pub trait EgressSinks {
    fn send(&mut self, iface: IfaceId, pkt: &OutPacket<'_>) -> SendResult;
}

impl<F: FnMut(IfaceId, &OutPacket<'_>) -> SendResult> EgressSinks for F {
    fn send(&mut self, iface: IfaceId, pkt: &OutPacket<'_>) -> SendResult {
        self(iface, pkt)
    }
}

#[derive(Default, Debug, Clone)]
pub struct ShardStats {
    pub rx_packets: u64,
    pub rx_dropped_parse: u64,
    pub rx_dropped_no_iface: u64,
    pub rx_no_conn: u64,
    pub rst_sent: u64,
    pub syn_received: u64,
    pub syn_rejected: u64,
    pub syn_dropped: u64,
    pub syn_cookies_sent: u64,
    pub syn_cookies_ok: u64,
    pub syn_cookies_rejected_resources: u64,
    pub conns_created: u64,
    pub conns_freed: u64,
    pub time_wait_recycled: u64,
    pub tx_packets: u64,
    pub tx_bytes: u64,
    pub sink_full: u64,
    pub runs: u64,
    pub idle_rounds: u64,
    pub pacing_credit_dropped: u64,
    pub mtu_changes: u64,
    pub round_budget_exhausted: u64,
}

#[derive(Debug, Clone, Copy, Default)]
pub struct RunOutcome {
    pub packets: usize,
    pub bytes: usize,
    /// More work is ready (round budget exhausted): call `run` again soon.
    pub more: bool,
}

type Key = (IfaceId, SocketAddr, SocketAddr);

/// Per-iface two-level DRR ready structure (§8.2). While the iface's sink is
/// `Full` the structure is frozen in place (it *is* the blocked set, §8.1/§8.3):
/// nothing is retried until `egress_released`, and service resumes where it stopped.
#[derive(Default)]
struct Drr {
    ready_peers: VecDeque<PeerId>,
    peer_q: HashMap<PeerId, VecDeque<(u32, u32)>>,
}

impl Drr {
    fn push(&mut self, peer: PeerId, e: (u32, u32)) {
        let q = self.peer_q.entry(peer).or_default();
        if q.is_empty() {
            self.ready_peers.push_back(peer);
        }
        q.push_back(e);
    }
    fn is_empty(&self) -> bool {
        self.ready_peers.is_empty()
    }
}

struct Iface {
    cfg: IfaceConfig,
    full: bool,
    drr: Drr,
}

#[derive(Default)]
struct Sched {
    in_ready: bool,
}

struct Slot {
    gen: u32,
    conn: Option<Box<Conn>>,
    sched: Sched,
}

struct Stateless {
    iface: IfaceId,
    peer: PeerId,
    pkt: Vec<u8>,
}

/// Wake-interval tracker for pacing credit (§7.2).
struct WakeStats {
    last: Option<Instant>,
    samples: [u32; 128],
    n: usize,
    credit: Duration,
}

pub struct Shard {
    cfg: StackConfig,
    ifaces: Vec<Option<Iface>>,
    slots: Vec<Slot>,
    free: Vec<u32>,
    table: HashMap<Key, u32>,
    timers: IndexedHeap,
    pacing: IndexedHeap,
    /// Round-robin start index over ifaces.
    iface_rr: usize,
    events: VecDeque<Event>,
    pool: BlockPool,
    budget: Budget,
    admission: Box<dyn AdmissionPolicy>,
    stateless: VecDeque<Stateless>,
    hasher: RandomState,
    cookie_key: [RandomState; 2],
    cookie_gen: u64,
    half_open: usize,
    time_wait: VecDeque<(u32, u32)>,
    time_wait_n: usize,
    wake: WakeStats,
    hdr: [u8; MAX_HEADER],
    stats: ShardStats,
}

const COOKIE_PERIOD_S: u64 = 60;
const MSS_TABLE: [u16; 8] = [216, 536, 1024, 1220, 1340, 1360, 1400, 1460];
const STATELESS_MAX: usize = 1024;

impl Shard {
    pub fn new(cfg: StackConfig) -> Self {
        Self::with_budget(cfg, GlobalBudget::from_system())
    }

    pub fn with_budget(cfg: StackConfig, global: Arc<GlobalBudget>) -> Self {
        let credit = cfg.pacing_credit_min;
        Shard {
            cfg,
            ifaces: Vec::new(),
            slots: Vec::new(),
            free: Vec::new(),
            table: HashMap::new(),
            timers: IndexedHeap::default(),
            pacing: IndexedHeap::default(),
            iface_rr: 0,
            events: VecDeque::new(),
            pool: BlockPool::new(256),
            budget: Budget::new(global),
            admission: Box::new(AcceptAll),
            stateless: VecDeque::new(),
            hasher: RandomState::new(),
            cookie_key: [RandomState::new(), RandomState::new()],
            cookie_gen: 0,
            half_open: 0,
            time_wait: VecDeque::new(),
            time_wait_n: 0,
            wake: WakeStats { last: None, samples: [0; 128], n: 0, credit },
            hdr: [0; MAX_HEADER],
            stats: ShardStats::default(),
        }
    }

    pub fn config(&self) -> &StackConfig {
        &self.cfg
    }
    pub fn stats(&self) -> &ShardStats {
        &self.stats
    }
    pub fn budget(&self) -> &Budget {
        &self.budget
    }
    pub fn pacing_credit(&self) -> Duration {
        self.wake.credit
    }
    pub fn set_admission(&mut self, p: Box<dyn AdmissionPolicy>) {
        self.admission = p;
    }
    pub fn conn_count(&self) -> usize {
        self.table.len()
    }

    pub fn add_iface(&mut self, cfg: IfaceConfig) -> IfaceId {
        let id = self.ifaces.len();
        self.ifaces.push(Some(Iface { cfg, full: false, drr: Drr::default() }));
        IfaceId(id as u16)
    }

    /// Path MTU decreased (EMSGSIZE) or configured MTU changed (§10.4).
    pub fn set_iface_mtu(&mut self, id: IfaceId, mtu: u16) {
        let Some(Some(ifc)) = self.ifaces.get_mut(id.0 as usize) else { return };
        ifc.cfg.mtu = mtu;
        self.stats.mtu_changes += 1;
        for s in &mut self.slots {
            if let Some(c) = s.conn.as_mut() {
                if c.iface == id {
                    c.clamp_mtu(mtu);
                }
            }
        }
    }

    /// Remove an interface, resetting all its connections.
    pub fn remove_iface(&mut self, id: IfaceId) {
        let idxs: Vec<u32> = self.table.iter().filter(|(k, _)| k.0 == id).map(|(_, &v)| v).collect();
        for idx in idxs {
            if let Some(c) = self.slots[idx as usize].conn.as_mut() {
                let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
                c.abort(&mut ctx);
            }
            self.free_slot(idx);
        }
        if let Some(slot) = self.ifaces.get_mut(id.0 as usize) {
            *slot = None;
        }
    }

    pub fn poll_event(&mut self) -> Option<Event> {
        self.events.pop_front()
    }

    /// Earliest instant at which `run` must be called without new input.
    pub fn next_deadline(&self) -> Option<Instant> {
        let ready = self.ifaces.iter().flatten().any(|i| !i.full && !i.drr.is_empty());
        if ready || (!self.stateless.is_empty() && self.ifaces.iter().flatten().any(|i| !i.full)) {
            return Some(Instant::ZERO);
        }
        let a = self.timers.peek().map(|x| x.0);
        let b = self.pacing.peek().map(|x| x.0);
        match (a, b) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        }
    }

    // ------------------------------------------------------------------
    // Ingress

    /// Process one decrypted IP packet from `peer`.
    pub fn ingress(&mut self, now: Instant, iface: IfaceId, peer: PeerId, pkt: Bytes) {
        self.stats.rx_packets += 1;
        if self.ifaces.get(iface.0 as usize).is_none_or(|i| i.is_none()) {
            self.stats.rx_dropped_no_iface += 1;
            return;
        }
        let ip = match wire::parse_ip(&pkt) {
            Ok(ip) => ip,
            Err(_) => {
                self.stats.rx_dropped_parse += 1;
                return;
            }
        };
        let h = match wire::parse_tcp(&pkt, &ip) {
            Ok(h) => h,
            Err(_) => {
                self.stats.rx_dropped_parse += 1;
                return;
            }
        };
        let remote = SocketAddr::new(ip.src, h.src_port);
        let local = SocketAddr::new(ip.dst, h.dst_port);
        let payload = pkt.slice(ip.l4_off + h.data_off..ip.l4_off + ip.l4_len);
        let key = (iface, remote, local);
        if let Some(&idx) = self.table.get(&key) {
            self.input_existing(now, idx, &h, payload);
            return;
        }
        self.stats.rx_no_conn += 1;
        if h.has(RST) {
            return;
        }
        if h.has(SYN) && !h.has(ACK) {
            self.handle_syn(now, iface, peer, remote, local, &h);
            return;
        }
        if h.has(ACK) && !h.has(SYN) {
            if let Some(idx) = self.try_cookie(now, iface, peer, remote, local, &h) {
                self.input_existing(now, idx, &h, payload);
                return;
            }
        }
        self.reply_rst(iface, peer, remote, local, &h, payload.len());
    }

    fn input_existing(&mut self, now: Instant, idx: u32, h: &TcpHeader, payload: Bytes) {
        let slot = &mut self.slots[idx as usize];
        let conn = slot.conn.as_mut().unwrap();
        let was_syn_rcvd = conn.state == State::SynReceived;
        let was_tw = conn.state == State::TimeWait;
        {
            let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
            conn.input(now, h, payload, &mut ctx);
        }
        if conn.bad_ack_rst {
            conn.bad_ack_rst = false;
            let (iface, peer, remote, local) = (conn.iface, conn.peer, conn.remote, conn.local);
            self.push_stateless_rst(iface, peer, local, remote, h.ack, None);
        }
        let conn = self.slots[idx as usize].conn.as_ref().unwrap();
        if was_syn_rcvd && conn.state != State::SynReceived {
            self.half_open = self.half_open.saturating_sub(1);
        }
        if !was_tw && conn.state == State::TimeWait {
            self.note_time_wait(idx);
        }
        self.after_conn_change(idx);
    }

    fn note_time_wait(&mut self, idx: u32) {
        let gen = self.slots[idx as usize].gen;
        self.time_wait.push_back((idx, gen));
        self.time_wait_n += 1;
        while self.time_wait_n > self.cfg.max_time_wait {
            let Some((i, g)) = self.time_wait.pop_front() else { break };
            let s = &self.slots[i as usize];
            if s.gen == g && s.conn.as_ref().is_some_and(|c| c.state == State::TimeWait) {
                self.stats.time_wait_recycled += 1;
                self.time_wait_n -= 1;
                let c = self.slots[i as usize].conn.as_mut().unwrap();
                c.app_closed = true;
                c.state = State::Closed;
                self.free_slot(i);
            } else {
                self.time_wait_n = self.time_wait_n.saturating_sub(0);
            }
        }
        // Compact stale entries occasionally.
        if self.time_wait.len() > 2 * self.time_wait_n + 64 {
            let slots = &self.slots;
            self.time_wait.retain(|&(i, g)| {
                let s = &slots[i as usize];
                s.gen == g && s.conn.as_ref().is_some_and(|c| c.state == State::TimeWait)
            });
            self.time_wait_n = self.time_wait.len();
        }
    }

    /// Reschedule timers / tx after any change to a connection; reap if done.
    fn after_conn_change(&mut self, idx: u32) {
        let slot = &mut self.slots[idx as usize];
        let Some(conn) = slot.conn.as_ref() else { return };
        if conn.reapable() && !conn.has_rst_pending() {
            self.free_slot(idx);
            return;
        }
        match conn.next_deadline() {
            Some(d) => self.timers.set(idx, d),
            None => self.timers.remove(idx),
        }
        self.schedule(idx);
    }

    fn schedule(&mut self, idx: u32) {
        let slot = &mut self.slots[idx as usize];
        if slot.sched.in_ready || self.pacing.contains(idx) {
            return;
        }
        let Some(conn) = slot.conn.as_ref() else { return };
        if !conn.wants_tx() {
            return;
        }
        let peer = conn.peer;
        let gen = slot.gen;
        let Some(Some(ifc)) = self.ifaces.get_mut(conn.iface.0 as usize) else { return };
        slot.sched.in_ready = true;
        ifc.drr.push(peer, (idx, gen));
    }

    fn free_slot(&mut self, idx: u32) {
        let slot = &mut self.slots[idx as usize];
        let Some(mut conn) = slot.conn.take() else { return };
        {
            let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
            conn.destroy(&mut ctx);
        }
        if conn.state == State::SynReceived {
            self.half_open = self.half_open.saturating_sub(1);
        }
        self.table.remove(&(conn.iface, conn.remote, conn.local));
        self.budget.peer_conn_del(conn.peer);
        slot.gen = slot.gen.wrapping_add(1);
        slot.sched = Sched::default();
        self.timers.remove(idx);
        self.pacing.remove(idx);
        self.free.push(idx);
        self.stats.conns_freed += 1;
    }

    fn alloc_slot(&mut self) -> (u32, u32) {
        if let Some(i) = self.free.pop() {
            (i, self.slots[i as usize].gen)
        } else {
            self.slots.push(Slot { gen: 1, conn: None, sched: Sched::default() });
            ((self.slots.len() - 1) as u32, 1)
        }
    }

    fn isn(&self, now: Instant, remote: SocketAddr, local: SocketAddr) -> Seq {
        // RFC 6528: ISN = M + F(4-tuple, secret), M = 4 µs clock.
        let mut h = self.hasher.build_hasher();
        (0u8, remote, local).hash(&mut h);
        Seq(((now.as_micros() / 4) as u32).wrapping_add(h.finish() as u32))
    }

    fn ts_offset(&self, remote: SocketAddr, local: SocketAddr) -> u32 {
        let mut h = self.hasher.build_hasher();
        (1u8, remote, local).hash(&mut h);
        h.finish() as u32
    }

    fn iface_mtu(&self, iface: IfaceId) -> u16 {
        self.ifaces[iface.0 as usize].as_ref().map_or(1280, |i| i.cfg.mtu)
    }

    fn handle_syn(&mut self, now: Instant, iface: IfaceId, peer: PeerId, remote: SocketAddr, local: SocketAddr, h: &TcpHeader) {
        self.stats.syn_received += 1;
        match self.admission.on_syn(iface, peer, remote, local) {
            Admission::Accept => {}
            Admission::Reject => {
                self.stats.syn_rejected += 1;
                self.reply_rst(iface, peer, remote, local, h, 0);
                return;
            }
            Admission::Drop => {
                self.stats.syn_dropped += 1;
                return;
            }
        }
        // Resource limits: silently drop (§10.2).
        let peer_conns = self.budget.peers.get(&peer).map_or(0, |p| p.conns);
        if self.budget.level() >= Pressure::High || peer_conns >= self.budget.peer_max_conns || self.events.len() >= self.cfg.accept_backlog * 4 {
            self.stats.syn_dropped += 1;
            return;
        }
        let mtu = self.iface_mtu(iface);
        let syn = SynParams { mss: h.opts.mss, wscale: h.opts.wscale, sack: h.opts.sack_perm, ts: h.opts.ts };
        if self.half_open >= self.cfg.syn_backlog {
            self.send_cookie_synack(now, iface, peer, remote, local, h, mtu);
            return;
        }
        if !self.budget.peer_conn_add(peer) {
            self.stats.syn_dropped += 1;
            return;
        }
        let (idx, gen) = self.alloc_slot();
        let id = ConnId::new(idx, gen);
        let iss = self.isn(now, remote, local);
        let tso = self.ts_offset(remote, local);
        let conn = Conn::new_passive(id, iface, peer, local, remote, iss, tso, mtu, h.seq, &syn, &self.cfg, now);
        self.slots[idx as usize].conn = Some(Box::new(conn));
        self.table.insert((iface, remote, local), idx);
        self.half_open += 1;
        self.stats.conns_created += 1;
        self.after_conn_change(idx);
    }

    // ---- SYN cookies (§10.2) ----

    fn cookie_hash(&self, gen: u64, remote: SocketAddr, local: SocketAddr, peer_isn: Seq) -> u32 {
        let mut h = self.cookie_key[(gen & 1) as usize].build_hasher();
        (gen, remote, local, peer_isn.0).hash(&mut h);
        h.finish() as u32
    }

    fn rotate_cookie_keys(&mut self, now: Instant) {
        let gen = now.as_millis() / 1000 / COOKIE_PERIOD_S;
        if gen > self.cookie_gen {
            if gen == self.cookie_gen + 1 {
                self.cookie_key[(gen & 1) as usize] = RandomState::new();
            } else {
                self.cookie_key = [RandomState::new(), RandomState::new()];
            }
            self.cookie_gen = gen;
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn send_cookie_synack(&mut self, now: Instant, iface: IfaceId, peer: PeerId, remote: SocketAddr, local: SocketAddr, h: &TcpHeader, mtu: u16) {
        self.rotate_cookie_keys(now);
        let gen = self.cookie_gen;
        let our_mss = mtu as u32 - wire::ip_header_len(local.ip()) as u32 - 20;
        let peer_mss = h.opts.mss.unwrap_or(536) as u32;
        let mss = our_mss.min(peer_mss);
        let mss_idx = MSS_TABLE.iter().rposition(|&m| m as u32 <= mss).unwrap_or(0) as u32;
        let hash = self.cookie_hash(gen, remote, local, h.seq);
        let cookie = ((gen as u32 & 0x1f) << 27) | (mss_idx << 24) | (hash & 0x00ff_ffff);
        let mut o = EmitOptions { mss: Some(our_mss as u16), ..Default::default() };
        let ws_ok = self.cfg.window_scaling && h.opts.wscale.is_some();
        let sack_ok = self.cfg.sack && h.opts.sack_perm;
        if ws_ok {
            o.wscale = Some(self.cfg.rcv_wscale());
        }
        if let (true, Some((tsval, _))) = (self.cfg.timestamps, h.opts.ts) {
            // Encode WS (4 bits, 15 = none) + SACK (1 bit) in the low 6 bits of TSval,
            // never exceeding the current TS clock (Linux cookie_init_timestamp).
            let now_ts = (now.as_millis() as u32).wrapping_add(self.ts_offset(remote, local));
            let opts = (if ws_ok { h.opts.wscale.unwrap().min(14) as u32 } else { 15 }) | ((sack_ok as u32) << 4) | 0x20;
            let mut ts = (now_ts & !0x3f) | opts;
            if (ts.wrapping_sub(now_ts) as i32) > 0 {
                ts = ts.wrapping_sub(0x40);
            }
            o.ts = Some((ts, tsval));
            o.sack_perm = sack_ok;
        } else {
            // Without timestamps we cannot remember WS/SACK: offer neither.
            o.wscale = None;
        }
        let payload: [&[u8]; 0] = [];
        let n = wire::emit(
            &mut self.hdr,
            &EmitParams {
                src: local.ip(),
                dst: remote.ip(),
                src_port: local.port(),
                dst_port: remote.port(),
                seq: Seq(cookie),
                ack: h.seq.add(1),
                flags: SYN | ACK,
                window: (self.cfg.init_rcv_wnd.min(65535)) as u16,
                opts: &o,
                payload: &payload,
                ttl: self.cfg.ttl,
            },
        );
        let pkt = self.hdr[..n].to_vec();
        self.stats.syn_cookies_sent += 1;
        self.push_stateless(iface, peer, pkt);
    }

    fn try_cookie(&mut self, now: Instant, iface: IfaceId, peer: PeerId, remote: SocketAddr, local: SocketAddr, h: &TcpHeader) -> Option<u32> {
        if self.stats.syn_cookies_sent == 0 {
            return None;
        }
        self.rotate_cookie_keys(now);
        let cookie = h.ack.0.wrapping_sub(1);
        let peer_isn = h.seq.add(u32::MAX); // seq - 1
        let cg = (cookie >> 27) & 0x1f;
        let gen = [self.cookie_gen, self.cookie_gen.wrapping_sub(1)].into_iter().find(|g| (*g as u32 & 0x1f) == cg)?;
        if gen + 1 < self.cookie_gen {
            return None;
        }
        if self.cookie_hash(gen, remote, local, peer_isn) & 0x00ff_ffff != cookie & 0x00ff_ffff {
            return None;
        }
        let mss = MSS_TABLE[((cookie >> 24) & 7) as usize];
        let (mut wscale, mut sack, mut ts) = (None, false, None);
        if let Some((tsval, tsecr)) = h.opts.ts {
            if tsecr & 0x20 != 0 {
                let ws = tsecr & 0xf;
                if ws != 15 {
                    wscale = Some(ws as u8);
                }
                sack = tsecr & 0x10 != 0;
                ts = Some((tsval, tsecr));
            }
        }
        // Re-check admission and resources (§10.2): a failure drops this ACK.
        if self.budget.level() >= Pressure::High || !self.budget.peer_conn_add(peer) {
            self.stats.syn_cookies_rejected_resources += 1;
            return None;
        }
        if self.admission.on_syn(iface, peer, remote, local) != Admission::Accept {
            self.budget.peer_conn_del(peer);
            return None;
        }
        self.stats.syn_cookies_ok += 1;
        let mtu = self.iface_mtu(iface);
        let (idx, gen_slot) = self.alloc_slot();
        let id = ConnId::new(idx, gen_slot);
        let tso = self.ts_offset(remote, local);
        let syn = SynParams { mss: Some(mss), wscale, sack, ts };
        let mut conn = Conn::new_from_cookie(id, iface, peer, local, remote, Seq(cookie), tso, mtu, peer_isn, &syn, &self.cfg, now);
        conn.accepted = true;
        self.events.push_back(Event::Accepted(id));
        self.slots[idx as usize].conn = Some(Box::new(conn));
        self.table.insert((iface, remote, local), idx);
        self.stats.conns_created += 1;
        Some(idx)
    }

    fn reply_rst(&mut self, iface: IfaceId, peer: PeerId, remote: SocketAddr, local: SocketAddr, h: &TcpHeader, payload_len: usize) {
        if h.has(RST) {
            return;
        }
        if h.has(ACK) {
            self.push_stateless_rst(iface, peer, local, remote, h.ack, None);
        } else {
            let seg_len = payload_len as u32 + h.has(SYN) as u32 + h.has(FIN) as u32;
            self.push_stateless_rst(iface, peer, local, remote, Seq(0), Some(h.seq.add(seg_len)));
        }
    }

    fn push_stateless_rst(&mut self, iface: IfaceId, peer: PeerId, local: SocketAddr, remote: SocketAddr, seq: Seq, ack: Option<Seq>) {
        let o = EmitOptions::default();
        let payload: [&[u8]; 0] = [];
        let n = wire::emit(
            &mut self.hdr,
            &EmitParams {
                src: local.ip(),
                dst: remote.ip(),
                src_port: local.port(),
                dst_port: remote.port(),
                seq,
                ack: ack.unwrap_or(Seq(0)),
                flags: RST | if ack.is_some() { ACK } else { 0 },
                window: 0,
                opts: &o,
                payload: &payload,
                ttl: self.cfg.ttl,
            },
        );
        let pkt = self.hdr[..n].to_vec();
        self.stats.rst_sent += 1;
        self.push_stateless(iface, peer, pkt);
    }

    fn push_stateless(&mut self, iface: IfaceId, peer: PeerId, pkt: Vec<u8>) {
        if self.stateless.len() >= STATELESS_MAX {
            self.stateless.pop_front();
        }
        self.stateless.push_back(Stateless { iface, peer, pkt });
    }

    // ------------------------------------------------------------------
    // Run

    fn note_wake(&mut self, now: Instant) {
        if let Some(last) = self.wake.last {
            let d = now.saturating_since(last).as_micros().min(u32::MAX as u128) as u32;
            let w = &mut self.wake;
            w.samples[w.n % 128] = d;
            w.n += 1;
            if w.n % 128 == 0 {
                let mut s = w.samples;
                s.sort_unstable();
                let p99 = Duration::from_micros(s[126] as u64);
                w.credit = p99.clamp(self.cfg.pacing_credit_min, self.cfg.pacing_credit_max);
            }
        }
        self.wake.last = Some(now);
    }

    /// One scheduling round (§8): timers, pacing wakeups, then per-peer DRR.
    pub fn run(&mut self, now: Instant, sinks: &mut impl EgressSinks) -> RunOutcome {
        self.stats.runs += 1;
        self.note_wake(now);
        let mut out = RunOutcome::default();

        while let Some(idx) = self.timers.pop_due(now) {
            let slot = &mut self.slots[idx as usize];
            let Some(conn) = slot.conn.as_mut() else { continue };
            let was_tw = conn.state == State::TimeWait;
            {
                let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
                conn.on_timer(now, &mut ctx);
            }
            if was_tw && conn.state != State::TimeWait {
                self.time_wait_n = self.time_wait_n.saturating_sub(1);
            }
            self.after_conn_change(idx);
        }
        while let Some(idx) = self.pacing.pop_due(now) {
            self.schedule(idx);
        }

        // Stateless replies (RST, cookie SYN-ACKs).
        while let Some(s) = self.stateless.front() {
            if self.ifaces.get(s.iface.0 as usize).is_none_or(|i| i.as_ref().is_none_or(|i| i.full)) {
                if self.ifaces.get(s.iface.0 as usize).is_none_or(|i| i.is_none()) {
                    self.stateless.pop_front();
                    continue;
                }
                break;
            }
            let pkt = OutPacket { peer: s.peer, header: &s.pkt, payload: [&[], &[]] };
            match sinks.send(s.iface, &pkt) {
                SendResult::Accepted => {
                    out.packets += 1;
                    out.bytes += s.pkt.len();
                    self.stats.tx_packets += 1;
                    self.stats.tx_bytes += s.pkt.len() as u64;
                    self.stateless.pop_front();
                }
                SendResult::Full => {
                    let i = s.iface;
                    self.stats.sink_full += 1;
                    self.ifaces[i.0 as usize].as_mut().unwrap().full = true;
                    break;
                }
            }
        }

        let mut budget = self.cfg.round_bytes_cap;
        let mut served_any = false;
        let n_if = self.ifaces.len();
        for k in 0..n_if {
            let ii = (self.iface_rr + k) % n_if.max(1);
            while budget > 0 {
                let Some(Some(ifc)) = self.ifaces.get_mut(ii) else { break };
                if ifc.full {
                    break;
                }
                let Some(peer) = ifc.drr.ready_peers.pop_front() else { break };
                let Some(q) = ifc.drr.peer_q.get_mut(&peer) else { continue };
                let Some((idx, gen)) = q.pop_front() else {
                    ifc.drr.peer_q.remove(&peer);
                    continue;
                };
                let valid = self.slots[idx as usize].gen == gen && self.slots[idx as usize].conn.is_some();
                let mut again = false;
                if valid {
                    self.slots[idx as usize].sched.in_ready = false;
                    let (sent, more) = self.serve(idx, now, budget, sinks, &mut out);
                    served_any |= sent > 0;
                    budget = budget.saturating_sub(sent);
                    again = more;
                }
                let ifc = self.ifaces[ii].as_mut().unwrap();
                if again {
                    // Quantum used (or sink filled mid-quantum): back to the tail.
                    self.slots[idx as usize].sched.in_ready = true;
                    ifc.drr.peer_q.get_mut(&peer).unwrap().push_back((idx, gen));
                }
                let q = ifc.drr.peer_q.get_mut(&peer).unwrap();
                if q.is_empty() {
                    ifc.drr.peer_q.remove(&peer);
                } else {
                    ifc.drr.ready_peers.push_back(peer);
                }
                if valid {
                    self.after_conn_change_no_sched(idx);
                }
            }
        }
        if n_if > 0 {
            self.iface_rr = (self.iface_rr + 1) % n_if;
        }
        if self.ifaces.iter().flatten().any(|i| !i.full && !i.drr.is_empty()) {
            out.more = true;
            self.stats.round_budget_exhausted += 1;
        }
        if !served_any && out.packets == 0 {
            self.stats.idle_rounds += 1;
        }
        out
    }

    fn after_conn_change_no_sched(&mut self, idx: u32) {
        let slot = &mut self.slots[idx as usize];
        let Some(conn) = slot.conn.as_ref() else { return };
        if conn.reapable() && !conn.has_rst_pending() {
            self.free_slot(idx);
            return;
        }
        match conn.next_deadline() {
            Some(d) => self.timers.set(idx, d),
            None => self.timers.remove(idx),
        }
    }

    /// Serve one connection for up to one quantum. Returns (bytes sent, wants more).
    fn serve(&mut self, idx: u32, now: Instant, round_left: usize, sinks: &mut impl EgressSinks, out: &mut RunOutcome) -> (usize, bool) {
        let level = self.budget.level();
        let slot = &mut self.slots[idx as usize];
        let gen = slot.gen;
        let conn = slot.conn.as_mut().unwrap();
        let iface_id = conn.iface;
        let _ = gen;
        let Some(Some(iface)) = self.ifaces.get_mut(iface_id.0 as usize) else { return (0, false) };
        if iface.full {
            conn.note_egress_limited(now);
            return (0, true);
        }
        let rate = if self.cfg.pacing { conn.pacing_rate() } else { None };
        let mss = conn.mss as usize;
        let quantum = match rate {
            Some(r) => ((r / 1000) as usize).clamp(2 * mss, self.cfg.max_quantum),
            None => self.cfg.max_quantum,
        }
        .min(round_left.max(mss));
        let credit = self.wake.credit;
        let mut sent = 0usize;
        loop {
            let Some(plan): Option<Plan> = conn.plan(now, level) else { return (sent, false) };
            if plan.paced() {
                if let Some(_r) = rate {
                    if conn.next_send_time > now {
                        conn.note_pacing_limited(now);
                        let t = conn.next_send_time;
                        self.pacing.set(idx, t);
                        return (sent, false);
                    }
                    if now.saturating_since(conn.next_send_time) > credit + Duration::from_millis(1) && conn.next_send_time != Instant::ZERO {
                        self.stats.pacing_credit_dropped += 1;
                    }
                }
            }
            let peer = conn.peer;
            let (hl, payload) = conn.build(&plan, now, &mut self.hdr, self.cfg.ttl);
            let pkt = OutPacket { peer, header: &self.hdr[..hl], payload };
            let len = pkt.len();
            match sinks.send(iface_id, &pkt) {
                SendResult::Accepted => {}
                SendResult::Full => {
                    self.stats.sink_full += 1;
                    iface.full = true;
                    conn.note_egress_limited(now);
                    // Stay queued (tail of its peer queue): resumes on egress_released.
                    return (sent, true);
                }
            }
            conn.commit(&plan, now, &self.cfg);
            if plan.paced() {
                if let Some(r) = rate {
                    conn.on_paced_send(now, len, r, credit);
                }
            }
            if plan.kind == PlanKind::Rst {
                // Connection is closed; nothing else to send.
                sent += len;
                out.packets += 1;
                out.bytes += len;
                self.stats.tx_packets += 1;
                self.stats.tx_bytes += len as u64;
                return (sent, false);
            }
            sent += len;
            out.packets += 1;
            out.bytes += len;
            self.stats.tx_packets += 1;
            self.stats.tx_bytes += len as u64;
            if sent >= quantum {
                return (sent, true);
            }
        }
    }

    /// The sink for `iface` drained (state ③, §4.3): unblock its connections.
    pub fn egress_released(&mut self, iface: IfaceId) {
        if let Some(Some(ifc)) = self.ifaces.get_mut(iface.0 as usize) {
            ifc.full = false;
        }
    }

    // ------------------------------------------------------------------
    // Application API

    fn conn_mut(&mut self, id: ConnId) -> Option<&mut Conn> {
        let s = self.slots.get_mut(id.idx())?;
        if s.gen != id.gen() {
            return None;
        }
        s.conn.as_deref_mut()
    }

    fn with_conn<R>(&mut self, id: ConnId, f: impl FnOnce(&mut Conn, &mut Ctx) -> R) -> Option<R> {
        let s = self.slots.get_mut(id.idx())?;
        if s.gen != id.gen() {
            return None;
        }
        let conn = s.conn.as_deref_mut()?;
        let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
        let r = f(conn, &mut ctx);
        self.after_conn_change(id.idx() as u32);
        Some(r)
    }

    pub fn read(&mut self, now: Instant, id: ConnId, dst: &mut [u8]) -> ReadResult {
        self.with_conn(id, |c, ctx| c.read(dst, ctx, now)).unwrap_or(ReadResult::Closed(CloseReason::Aborted))
    }

    pub fn read_chunk(&mut self, now: Instant, id: ConnId, max: usize) -> Result<Bytes, ReadResult> {
        self.with_conn(id, |c, ctx| c.read_chunk(max, ctx, now)).unwrap_or(Err(ReadResult::Closed(CloseReason::Aborted)))
    }

    pub fn write(&mut self, id: ConnId, src: &[u8]) -> WriteResult {
        self.with_conn(id, |c, ctx| c.write(src, ctx)).unwrap_or(WriteResult::Closed)
    }

    /// Free send space (bytes) right now.
    pub fn send_space(&mut self, id: ConnId) -> usize {
        let cfg = self.cfg.clone();
        self.conn_mut(id).map_or(0, |c| c.send_space(&cfg))
    }

    pub fn shutdown_write(&mut self, id: ConnId) {
        self.with_conn(id, |c, _| c.shutdown_write());
    }

    /// Close the handle (§10.3). The id becomes invalid.
    pub fn close(&mut self, now: Instant, id: ConnId) {
        self.with_conn(id, |c, ctx| c.close(now, ctx));
    }

    pub fn abort(&mut self, id: ConnId) {
        self.with_conn(id, |c, ctx| c.abort(ctx));
    }

    pub fn info(&self, id: ConnId) -> Option<ConnInfo> {
        let s = self.slots.get(id.idx())?;
        if s.gen != id.gen() {
            return None;
        }
        s.conn.as_ref().map(|c| c.info())
    }

    /// Iterate over all live connections (diagnostics).
    pub fn conn_ids(&self) -> Vec<ConnId> {
        self.slots
            .iter()
            .enumerate()
            .filter_map(|(i, s)| s.conn.as_ref().map(|_| ConnId::new(i as u32, s.gen)))
            .collect()
    }

    /// Scheduling/container sizes (invariant checks, §14.4 #7).
    pub fn container_sizes(&self) -> (usize, usize, usize) {
        (self.timers.len(), self.pacing.len(), self.table.len())
    }

    /// Active open (test peer only, §0).
    #[cfg(any(test, feature = "test-peer"))]
    pub fn connect(&mut self, now: Instant, iface: IfaceId, peer: PeerId, local: SocketAddr, remote: SocketAddr) -> ConnId {
        let mtu = self.iface_mtu(iface);
        let (idx, gen) = self.alloc_slot();
        let id = ConnId::new(idx, gen);
        let iss = self.isn(now, remote, local);
        let tso = self.ts_offset(remote, local);
        let conn = Conn::new_active(id, iface, peer, local, remote, iss, tso, mtu, &self.cfg, now);
        self.slots[idx as usize].conn = Some(Box::new(conn));
        self.table.insert((iface, remote, local), idx);
        self.budget.peer_conn_add(peer);
        self.stats.conns_created += 1;
        self.after_conn_change(idx);
        id
    }

    #[cfg(any(test, feature = "test-peer"))]
    pub fn check_invariants(&self) {
        for s in &self.slots {
            if let Some(c) = s.conn.as_ref() {
                c.check_invariants();
            }
        }
        // Each connection appears at most once in ready queues.
        let mut seen = std::collections::HashSet::new();
        for q in self.ifaces.iter().flatten().flat_map(|i| i.drr.peer_q.values()) {
            for &(i, g) in q {
                if self.slots[i as usize].gen == g {
                    assert!(seen.insert(i), "conn {i} queued twice");
                }
            }
        }
        assert!(self.timers.len() <= self.slots.len());
        assert!(self.pacing.len() <= self.slots.len());
    }
}
