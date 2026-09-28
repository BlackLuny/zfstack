//! TCP connection state machine.
//!
//! Positions are 64-bit offsets from the ISN (see [`crate::seq`]). Sending is split
//! into `plan` (decide what the next segment is, no state change), `build`
//! (serialize headers) and `commit` (the sink accepted the packet: advance state).
//! A packet refused by the sink therefore never counts as sent (§4.3).

use crate::budget::{Budget, Pressure};
use crate::buf::{BlockPool, OooQueue, RxQueue, TxBuf};
use crate::cc::{new_cc, AckCtx, CongestionControl, RateSample};
use crate::config::StackConfig;
use crate::rtt::RttEstimator;
use crate::scoreboard::*;
use crate::seq::{Seq, SeqSpace};
use crate::time::Instant;
use crate::wire::{self, EmitOptions, EmitParams, TcpHeader, ACK, FIN, MAX_HEADER, PSH, RST, SYN};
use crate::{CloseReason, ConnId, Event, IfaceId, PeerId};
use bytes::Bytes;
use core::time::Duration;
use std::collections::VecDeque;
use std::net::SocketAddr;

/// The WG integration borrows a decrypted packet only for the duration of
/// `Shard::ingress_borrowed`. Existing simulator/adapter callers can still pass
/// owned Bytes without copying them. Only data actually retained by TCP is
/// converted to owned storage.
pub(crate) enum IngressPayload<'a> {
    Borrowed(&'a [u8]),
    Owned(Bytes),
}

impl IngressPayload<'_> {
    pub(crate) fn len(&self) -> usize {
        match self {
            Self::Borrowed(v) => v.len(),
            Self::Owned(v) => v.len(),
        }
    }

    fn is_empty(&self) -> bool {
        self.len() == 0
    }

    fn slice(self, start: usize) -> Self {
        match self {
            Self::Borrowed(v) => Self::Borrowed(&v[start..]),
            Self::Owned(v) => Self::Owned(v.slice(start..)),
        }
    }

    fn truncate(&mut self, len: usize) {
        match self {
            Self::Borrowed(v) => *v = &v[..len],
            Self::Owned(v) => v.truncate(len),
        }
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum State {
    SynSent,
    SynReceived,
    Established,
    FinWait1,
    FinWait2,
    CloseWait,
    Closing,
    LastAck,
    TimeWait,
    Closed,
}

impl State {
    fn can_recv_data(self) -> bool {
        matches!(self, State::Established | State::FinWait1 | State::FinWait2)
    }
    pub fn can_send_data(self) -> bool {
        matches!(self, State::Established | State::CloseWait | State::FinWait1 | State::Closing | State::LastAck)
    }
    fn synchronized(self) -> bool {
        !matches!(self, State::SynSent | State::SynReceived | State::Closed)
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub(crate) enum PlanKind {
    Syn,
    SynAck,
    New,
    Rtx,
    TlpProbe,
    Ack,
    /// Zero-window or keepalive probe: seq = snd_una - 1, no data.
    Probe,
    Rst,
}

#[derive(Copy, Clone, Debug)]
pub(crate) struct Plan {
    pub kind: PlanKind,
    pub flags: u8,
    pub seq_off: u64,
    pub len: u32,
    pub window: u16,
    pub rec_idx: usize,
}

impl Plan {
    pub fn paced(&self) -> bool {
        matches!(self.kind, PlanKind::New | PlanKind::Rtx | PlanKind::TlpProbe) && self.len > 0
    }
    pub fn wire_len(&self) -> usize {
        self.len as usize
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum TimerKind {
    SynAck,
    Rto,
    Tlp,
    Reorder,
    Persist,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum AckNeed {
    None,
    Delayed,
    Now,
}

struct Recovery {
    point: u64,
    prr_delivered: u64,
    prr_out: u64,
    recover_fs: u64,
    quota: u64,
}

#[derive(Default)]
struct Rack {
    xmit: Instant,
    end: u64,
    rtt: Duration,
    valid: bool,
    fack: u64,
    reordering_seen: bool,
    reo_wnd_mult: u32,
    reo_wnd_persist: u32,
    dsack_round: Option<u64>,
}

#[derive(Default)]
struct Tlp {
    /// Probe requested by PTO expiry, not yet sent.
    pending: bool,
    /// snd_max at the time the probe was sent (end of the probe), if outstanding.
    end: Option<u64>,
    /// snd_una when the last probe was sent: at most one probe per snd_una (RFC 8985 §7.2).
    probed_una: Option<u64>,
    is_retrans: bool,
    /// Peer's ACK delay for a lone segment, measured as RTT sample − min RTT
    /// when the whole flight was one segment: a decaying max (jumps up at once,
    /// decays by 1/8 per sample). Replaces the fixed worst-case delayed-ACK
    /// allowance (WCDelAckT) in the single-segment PTO once known.
    lone_ack_delay: Option<Duration>,
    /// Probe of a lone segment that was a retransmission: (end offset, original
    /// send time, probe send time). An ACK for it arriving sooner than min RTT
    /// after the probe acknowledges the original, so the probe was spurious and
    /// the ACK delay was underestimated; Karn's rule would otherwise hide that.
    lone_probe: Option<(u64, Instant, Instant)>,
}

/// Delivery-rate estimation state (draft-cheng-iccrg-delivery-rate-estimation).
#[derive(Default)]
struct Delivery {
    delivered: u64,
    delivered_ts: Instant,
    first_tx_ts: Instant,
    /// Non-zero: app-limited until `delivered` exceeds this.
    app_limited: u64,
    lost_total: u64,
    next_round_delivered: u64,
    round_count: u64,
}

/// Counters exported through [`crate::ConnInfo`].
#[derive(Default, Clone, Debug)]
pub struct ConnStats {
    pub segs_out: u64,
    pub segs_in: u64,
    pub bytes_sent: u64,
    pub bytes_retrans: u64,
    pub bytes_received: u64,
    pub rto_count: u64,
    pub tlp_count: u64,
    pub fast_recoveries: u64,
    pub spurious_rtos: u64,
    pub dsacks: u64,
    pub ooo_segs: u64,
    pub dropped_no_mem: u64,
    pub paws_drops: u64,
    pub challenge_acks: u64,
    pub zero_window_probes: u64,
    /// New-data segments shorter than the MSS.
    pub small_segs: u64,
    /// Send records visited by RACK loss detection (CPU diagnostics).
    pub rack_scanned: u64,
    /// Time spent limited by each factor (ns): rwnd, cwnd, pacing, app.
    pub limited_rwnd_ns: u64,
    pub limited_cwnd_ns: u64,
    pub limited_pacing_ns: u64,
    pub limited_egress_ns: u64,
}

pub struct Conn {
    /// Host stream state reserved with this connection at admission; the
    /// adapter takes it on accept. Boxed with the connection, so its slot
    /// costs nothing while the connection lives.
    pub(crate) stream_memory: Option<crate::budget::MemoryLease>,
    pub id: ConnId,
    pub iface: IfaceId,
    pub peer: PeerId,
    pub local: SocketAddr,
    pub remote: SocketAddr,
    pub state: State,

    // ---- send side ----
    tx_sp: SeqSpace,
    snd_una: u64,
    snd_nxt: u64,
    snd_max: u64,
    snd_wnd: u64,
    max_snd_wnd: u64,
    snd_wl1: u64,
    snd_wl2: u64,
    snd_wscale: u8,
    tx: TxBuf,
    /// Stream offset of `tx[0]`.
    tx_base: u64,
    /// Offset of our FIN once the write side is shut.
    fin_off: Option<u64>,
    sb: Scoreboard,
    pub mss: u32,
    rtt: RttEstimator,
    cc: Box<dyn CongestionControl>,
    recovery: Option<Recovery>,
    /// After an RTO, no new recovery episode until snd_una passes this point.
    rto_recovery: Option<u64>,
    rto_backoff: u32,
    rto_base: Instant,
    /// Last forward progress of the send side (user timeout, RFC 5482).
    progress_ts: Instant,
    /// TS value sent with the first RTO retransmission (Eifel spurious detection).
    rto_tsval: Option<u32>,
    rack: Rack,
    tlp: Tlp,
    dl: Delivery,
    timer: Option<(TimerKind, Instant)>,
    synack_tries: u32,
    synack_ts: Instant,
    persist_backoff: u32,
    probe_pending: bool,
    rst_pending: bool,
    syn_pending: bool,
    pub(crate) bad_ack_rst: bool,
    /// Consecutive stall epochs with desync evidence (see `note_desync`).
    desync_count: u32,
    /// Timer-driven transmissions without progress (RTO, zero-window probe,
    /// keepalive): the stimuli desync evidence is counted against.
    stall_epoch: u64,
    /// `stall_epoch` and peer TSval of the last counted desync evidence.
    desync_epoch: u64,
    desync_tsval: u32,
    ws_ok: bool,
    rack_timeout: Option<Instant>,
    rto_tsval_pending: bool,
    cwnd_limited_now: bool,
    cwnd_limited_prev: bool,
    adv_mss: u16,
    /// Next time a paced segment may leave (EDT).
    pub next_send_time: Instant,

    // ---- receive side ----
    rx_sp: SeqSpace,
    rcv_nxt: u64,
    /// Right edge of the advertised window (never retreats).
    rcv_adv: u64,
    last_ack_sent: u64,
    rcv_wscale: u8,
    rx: RxQueue,
    ooo: Option<Box<OooQueue>>,
    fin_rcvd: Option<u64>,
    rcv_target: u64,
    rcv_charged: u64,
    /// Bytes handed to an async adapter but not consumed by its application.
    adapter_unread: u64,
    rcvq_space: u64,
    rcvq_copied: u64,
    rcvq_time: Instant,
    rcvq_off: u64,
    rcv_rtt: Duration,
    rcv_mss: u32,
    ack_need: AckNeed,
    unacked_bytes: u64,
    delack_at: Option<Instant>,
    dsack: Option<(u64, u64)>,
    sack_scratch: Vec<(u64, u64)>,

    // ---- options ----
    ts_ok: bool,
    ts_recent: u32,
    ts_recent_time: Instant,
    ts_offset: u32,
    sack_ok: bool,

    // ---- timers / liveness ----
    last_recv: Instant,
    keepalive_at: Option<Instant>,
    keepalive_sent: u32,
    /// TIME_WAIT expiry, orphan timeout or user timeout.
    life_at: Option<Instant>,
    challenge_ack_ts: Instant,
    challenge_acks_this_sec: u32,

    // ---- application interface ----
    pub accepted: bool,
    pub app_closed: bool,
    want_read: bool,
    want_write: bool,
    pub close_reason: Option<CloseReason>,
    closed_notified: bool,
    tx_charged: u64,
    /// Bytes the adapter TX queue holds for this connection, not yet moved
    /// into the core send buffer (docs/design/0007 §2.1).
    adapter_tx: u32,
    /// Counted in the port/peer sender tally while holding send-side bytes.
    sender: bool,
    /// Snapshot for limited-time accounting.
    limit_state: u8,
    limit_since: Instant,

    pub stats: ConnStats,
}

/// Wire state needed after a full connection has completed the FIN exchange.
/// Application unread data must be drained before creating this snapshot.
#[derive(Clone, Copy)]
pub(crate) struct TimeWaitState {
    pub iface: IfaceId,
    pub peer: PeerId,
    pub local: SocketAddr,
    pub remote: SocketAddr,
    pub snd_seq: Seq,
    pub rcv_seq: Seq,
    pub last_fin_seq: Seq,
    pub ts_ok: bool,
    pub ts_recent: u32,
    pub ts_offset: u32,
    pub expires: Instant,
    pub pending_ack: bool,
}

impl TimeWaitState {
    /// RFC 6191 §2: compare the new SYN's timestamp and, when required, its
    /// ISN with the old peer FIN sequence (not the last packet header SEQ).
    pub fn can_reuse(&self, syn: &TcpHeader, timestamps_enabled: bool) -> bool {
        if !syn.has(SYN) || syn.has(ACK | FIN | RST) {
            return false;
        }
        let new_ts = if timestamps_enabled { syn.opts.ts.map(|(v, _)| v) } else { None };
        let newer_seq = syn.seq.gt(self.last_fin_seq);
        match (self.ts_ok, new_ts) {
            (true, Some(ts)) => {
                let delta = ts.wrapping_sub(self.ts_recent) as i32;
                delta > 0 || (delta == 0 && newer_seq)
            }
            (true, None) => newer_seq,
            (false, Some(_)) => true,
            (false, None) => newer_seq,
        }
    }
}

pub(crate) struct Ctx<'a> {
    pub cfg: &'a StackConfig,
    pub pool: &'a mut BlockPool,
    pub budget: &'a mut Budget,
    pub events: &'a mut VecDeque<Event>,
}

/// Result of handing a write to a connection.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WriteResult {
    Written(usize),
    /// TCP send window is full; an ACK produces a `Writable` event.
    WouldBlock,
    /// Logical port or peer payload quota is full.
    QuotaBlocked,
    /// The next TX block could not acquire physical backing.
    MemoryBlocked,
    Closed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReadResult {
    Data(usize),
    WouldBlock,
    Eof,
    Closed(CloseReason),
}

const MAX_DELACK_SEGS: u64 = 2;
/// Worst-case delayed-ACK time of a peer (RFC 8985 §7.2 WCDelAckT).
const WC_DEL_ACK: Duration = Duration::from_millis(200);
const DEFAULT_MSS_V4: u32 = 536;
const DEFAULT_MSS_V6: u32 = 1220;
const TS_VALID_FOR: Duration = Duration::from_secs(24 * 24 * 3600);
/// Desync detection: evidence in this many consecutive stall epochs resets the connection.
const DESYNC_EPOCHS: u32 = 3;
const LIM_NONE: u8 = 0;
const LIM_RWND: u8 = 1;
const LIM_CWND: u8 = 2;
const LIM_PACING: u8 = 3;
const LIM_EGRESS: u8 = 4;

pub(crate) struct SynParams {
    pub mss: Option<u16>,
    pub wscale: Option<u8>,
    pub sack: bool,
    pub ts: Option<(u32, u32)>,
}

impl Conn {
    pub(crate) fn ready_for_time_wait_compaction(&self) -> bool {
        self.state == State::TimeWait
            && self.app_closed
            && self.rx.is_empty()
            && self.ooo.as_ref().is_none_or(|q| q.is_empty())
            && self.adapter_unread == 0
            && !self.wants_tx()
    }

    pub(crate) fn time_wait_snapshot(&self) -> TimeWaitState {
        debug_assert!(self.ready_for_time_wait_compaction());
        TimeWaitState {
            iface: self.iface,
            peer: self.peer,
            local: self.local,
            remote: self.remote,
            snd_seq: self.tx_sp.seq(self.snd_nxt),
            rcv_seq: self.rx_sp.seq(self.rcv_nxt),
            last_fin_seq: self.rx_sp.seq(self.fin_rcvd.expect("TIME_WAIT requires peer FIN")),
            ts_ok: self.ts_ok,
            ts_recent: self.ts_recent,
            ts_offset: self.ts_offset,
            expires: self.life_at.expect("TIME_WAIT requires expiry"),
            pending_ack: false,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn blank(
        id: ConnId,
        iface: IfaceId,
        peer: PeerId,
        local: SocketAddr,
        remote: SocketAddr,
        iss: Seq,
        ts_offset: u32,
        mtu: u16,
        cfg: &StackConfig,
        now: Instant,
    ) -> Conn {
        let hdr = wire::ip_header_len(local.ip()) as u32 + 20;
        let mss = (mtu as u32).saturating_sub(hdr).max(64);
        Conn {
            stream_memory: None,
            id,
            iface,
            peer,
            local,
            remote,
            state: State::Closed,
            tx_sp: SeqSpace { isn: iss },
            snd_una: 0,
            snd_nxt: 0,
            snd_max: 0,
            snd_wnd: 0,
            max_snd_wnd: 0,
            snd_wl1: 0,
            snd_wl2: 0,
            snd_wscale: 0,
            tx: TxBuf::default(),
            tx_base: 1,
            fin_off: None,
            sb: Scoreboard::default(),
            mss,
            rtt: RttEstimator::new(cfg.init_rto, cfg.min_rto, cfg.max_rto),
            cc: new_cc(cfg.cc, mss, cfg.init_cwnd_segs),
            recovery: None,
            rto_recovery: None,
            rto_backoff: 0,
            rto_base: now,
            progress_ts: now,
            rto_tsval: None,
            rack: Rack { reo_wnd_mult: 1, ..Default::default() },
            tlp: Tlp::default(),
            dl: Delivery { delivered_ts: now, first_tx_ts: now, ..Default::default() },
            timer: None,
            synack_tries: 0,
            synack_ts: now,
            persist_backoff: 0,
            probe_pending: false,
            rst_pending: false,
            syn_pending: false,
            bad_ack_rst: false,
            desync_count: 0,
            stall_epoch: 0,
            desync_epoch: 0,
            desync_tsval: 0,
            ws_ok: false,
            rack_timeout: None,
            rto_tsval_pending: false,
            cwnd_limited_now: false,
            cwnd_limited_prev: false,
            adv_mss: mss.min(65535) as u16,
            next_send_time: Instant::ZERO,
            rx_sp: SeqSpace { isn: Seq(0) },
            rcv_nxt: 0,
            rcv_adv: 0,
            last_ack_sent: 0,
            rcv_wscale: 0,
            rx: RxQueue::default(),
            ooo: None,
            fin_rcvd: None,
            rcv_target: cfg.init_rcv_wnd as u64,
            rcv_charged: 0,
            adapter_unread: 0,
            rcvq_space: 0,
            rcvq_copied: 0,
            rcvq_time: now,
            rcvq_off: 0,
            rcv_rtt: Duration::ZERO,
            rcv_mss: mss,
            ack_need: AckNeed::None,
            unacked_bytes: 0,
            delack_at: None,
            dsack: None,
            sack_scratch: Vec::new(),
            ts_ok: false,
            ts_recent: 0,
            ts_recent_time: now,
            ts_offset,
            sack_ok: false,
            last_recv: now,
            keepalive_at: None,
            keepalive_sent: 0,
            life_at: None,
            challenge_ack_ts: now,
            challenge_acks_this_sec: 0,
            accepted: false,
            app_closed: false,
            want_read: true,
            want_write: false,
            close_reason: None,
            closed_notified: false,
            tx_charged: 0,
            adapter_tx: 0,
            sender: false,
            limit_state: LIM_NONE,
            limit_since: now,
            stats: ConnStats::default(),
        }
    }

    fn apply_syn_options(&mut self, p: &SynParams, cfg: &StackConfig) {
        let default_mss = if self.local.is_ipv4() { DEFAULT_MSS_V4 } else { DEFAULT_MSS_V6 };
        let peer_mss = p.mss.map(|m| m as u32).unwrap_or(default_mss).max(64);
        self.mss = self.mss.min(peer_mss);
        match (cfg.window_scaling, p.wscale) {
            (true, Some(ws)) => {
                self.snd_wscale = ws.min(14);
                self.rcv_wscale = cfg.rcv_wscale();
                self.ws_ok = true;
            }
            _ => {
                self.snd_wscale = 0;
                self.rcv_wscale = 0;
            }
        }
        self.sack_ok = cfg.sack && p.sack;
        if cfg.timestamps {
            if let Some((tsval, _)) = p.ts {
                self.ts_ok = true;
                self.ts_recent = tsval;
            }
        }
        if self.ts_ok {
            self.mss -= 12;
        }
        self.rcv_mss = self.mss;
        self.cc = new_cc(cfg.cc, self.mss, cfg.init_cwnd_segs);
        if self.rcv_wscale == 0 {
            self.rcv_target = self.rcv_target.min(65535);
        }
    }

    /// Passive open from a SYN (§10.1). The connection holds metadata only.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new_passive(
        id: ConnId,
        iface: IfaceId,
        peer: PeerId,
        local: SocketAddr,
        remote: SocketAddr,
        iss: Seq,
        ts_offset: u32,
        mtu: u16,
        irs: Seq,
        syn: &SynParams,
        cfg: &StackConfig,
        now: Instant,
    ) -> Conn {
        let mut c = Conn::blank(id, iface, peer, local, remote, iss, ts_offset, mtu, cfg, now);
        c.state = State::SynReceived;
        c.rx_sp = SeqSpace { isn: irs };
        c.rcv_nxt = 1;
        c.last_ack_sent = 1;
        c.apply_syn_options(syn, cfg);
        c.ts_recent_time = now;
        c.synack_tries = 0;
        c.syn_pending = true;
        c
    }

    /// Connection established directly from a valid SYN cookie (§10.2).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new_from_cookie(
        id: ConnId,
        iface: IfaceId,
        peer: PeerId,
        local: SocketAddr,
        remote: SocketAddr,
        iss: Seq,
        ts_offset: u32,
        mtu: u16,
        irs: Seq,
        syn: &SynParams,
        cfg: &StackConfig,
        now: Instant,
    ) -> Conn {
        let mut c = Conn::blank(id, iface, peer, local, remote, iss, ts_offset, mtu, cfg, now);
        c.rx_sp = SeqSpace { isn: irs };
        c.rcv_nxt = 1;
        c.last_ack_sent = 1;
        c.apply_syn_options(syn, cfg);
        c.snd_una = 1;
        c.snd_nxt = 1;
        c.snd_max = 1;
        c.rcv_adv = 1 + c.rcv_target.min(65535);
        c.state = State::Established;
        c
    }

    /// Active open (test peer / simulation only).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new_active(
        id: ConnId,
        iface: IfaceId,
        peer: PeerId,
        local: SocketAddr,
        remote: SocketAddr,
        iss: Seq,
        ts_offset: u32,
        mtu: u16,
        cfg: &StackConfig,
        now: Instant,
    ) -> Conn {
        let mut c = Conn::blank(id, iface, peer, local, remote, iss, ts_offset, mtu, cfg, now);
        c.state = State::SynSent;
        c.accepted = true;
        c.syn_pending = true;
        if cfg.window_scaling {
            c.rcv_wscale = cfg.rcv_wscale();
        }
        c
    }

    // ------------------------------------------------------------------
    // Accessors

    #[inline]
    fn ts_val(&self, now: Instant) -> u32 {
        (now.as_millis() as u32).wrapping_add(self.ts_offset)
    }

    #[inline]
    pub fn pipe(&self) -> u64 {
        self.sb.pipe(self.snd_nxt - self.snd_una)
    }

    fn tx_end(&self) -> u64 {
        self.tx_base + self.tx.len() as u64
    }

    fn unsent(&self) -> u64 {
        let end = self.tx_end();
        end.saturating_sub(self.snd_nxt.max(self.tx_base))
    }

    pub fn cwnd(&self) -> u64 {
        self.cc.cwnd()
    }

    pub fn in_recovery(&self) -> bool {
        self.recovery.is_some()
    }

    pub fn srtt(&self) -> Duration {
        self.rtt.srtt_or(Duration::ZERO)
    }

    pub fn is_closed(&self) -> bool {
        self.state == State::Closed
    }

    /// Whether the slot can be freed.
    pub fn reapable(&self) -> bool {
        self.state == State::Closed && (self.app_closed || !self.accepted)
    }

    pub fn rx_available(&self) -> usize {
        self.rx.len()
    }

    /// Pacing rate in bytes/s (None = unpaced).
    pub fn pacing_rate(&self) -> Option<u64> {
        if let Some(r) = self.cc.pacing_rate() {
            return Some(r.max(1));
        }
        if !self.rtt.has_sample {
            return None;
        }
        let srtt = self.rtt.srtt.max(Duration::from_micros(100));
        let cwnd = self.cc.cwnd().max(self.mss as u64);
        // Linux: 200% in slow start (cwnd < ssthresh/2), 120% otherwise.
        let gain = if self.cc.cwnd() < self.cc.ssthresh() / 2 { 2.0 } else { 1.2 };
        let rate = cwnd as f64 / srtt.as_secs_f64() * gain;
        Some(rate.max(1.0) as u64)
    }

    /// Rate the output path paces at, or None when this connection is not paced now
    /// (see `StackConfig::pacing_min_cwnd_segs`).
    pub fn egress_pacing_rate(&self, cfg: &StackConfig) -> Option<u64> {
        if !cfg.pacing {
            return None;
        }
        if self.cc.pacing_rate().is_none() && self.cc.cwnd() < cfg.pacing_min_cwnd_segs as u64 * self.mss as u64 {
            return None;
        }
        self.pacing_rate()
    }

    /// Send buffer limit: in-flight window plus a time-bounded prefetch (§6.5).
    fn sndbuf_limit(&self, cfg: &StackConfig) -> u64 {
        let inflight = self.snd_nxt.saturating_sub(self.snd_una);
        let win = inflight.max(self.cc.cwnd()).min(cfg.max_snd_inflight as u64);
        let prefetch = match self.pacing_rate() {
            Some(r) => ((r as u128).saturating_mul(cfg.prefetch_time.as_nanos()) / 1_000_000_000).min(u64::MAX as u128) as u64,
            None => (cfg.min_prefetch as u64).max(self.cc.cwnd()),
        }
        .max(cfg.min_prefetch as u64)
        .min(cfg.prefetch_max as u64);
        win.saturating_add(prefetch)
    }

    /// Send-side bytes this connection is charged with for the sender tally:
    /// the core buffer (in-flight unacknowledged plus unsent) and the adapter
    /// queue (docs/design/0007 §2.1).
    pub(crate) fn send_occupancy(&self) -> u64 {
        self.tx.len() as u64 + u64::from(self.adapter_tx)
    }

    /// Keep the port/peer sender tally in step with what this connection
    /// holds (docs/design/0007 §2.2).
    fn sync_sender(&mut self, ctx: &mut Ctx) {
        let sending = self.send_occupancy() > 0;
        if sending != self.sender {
            self.sender = sending;
            if sending {
                ctx.budget.sender_add(self.peer);
            } else {
                ctx.budget.sender_del(self.peer);
            }
        }
    }

    /// Bytes the adapter TX queue currently holds for this connection. A
    /// closed connection can never drain the queue, so it reports zero
    /// (docs/design/0007 §2.2).
    #[cfg(feature = "tokio")]
    pub(crate) fn set_adapter_tx(&mut self, bytes: u32, ctx: &mut Ctx) {
        let bytes = if self.state == State::Closed { 0 } else { bytes };
        if self.adapter_tx != bytes {
            self.adapter_tx = bytes;
            self.sync_sender(ctx);
        }
    }

    /// Free send space: the connection's own send-buffer limit and its fair
    /// share of the port/peer budget, whichever is smaller
    /// (docs/design/0007 §2.3). The adapter queue is not subtracted here:
    /// moving bytes between it and the core buffer must stay neutral.
    /// Bytes in the core send buffer: in-flight unacknowledged plus unsent.
    #[cfg(feature = "tokio")]
    pub(crate) fn tx_queued_len(&self) -> usize {
        self.tx.len()
    }

    pub fn send_space(&self, cfg: &StackConfig, budget: &Budget) -> usize {
        let cap = self.sndbuf_limit(cfg).min(budget.send_share(self.peer));
        cap.saturating_sub(self.tx.len() as u64) as usize
    }

    pub fn next_deadline(&self) -> Option<Instant> {
        let mut d: Option<Instant> = None;
        let mut take = |x: Option<Instant>| {
            if let Some(x) = x {
                d = Some(d.map_or(x, |y: Instant| y.min(x)));
            }
        };
        take(self.timer.map(|t| t.1));
        take(self.delack_at);
        take(self.keepalive_at);
        take(self.life_at);
        d
    }

    // ------------------------------------------------------------------
    // Events

    fn notify_readable(&mut self, ev: &mut VecDeque<Event>) {
        if self.want_read && self.accepted {
            self.want_read = false;
            ev.push_back(Event::Readable(self.id));
        }
    }

    pub(crate) fn notify_writable(&mut self, ev: &mut VecDeque<Event>, cfg: &StackConfig, budget: &Budget) {
        if self.want_write && self.accepted && self.fin_off.is_none()
            && matches!(self.state, State::Established | State::CloseWait | State::SynReceived)
            && self.send_space(cfg, budget) >= cfg.write_low_watermark as usize
        {
            self.want_write = false;
            ev.push_back(Event::Writable(self.id));
        }
    }

    /// Only share-bound writers need a retry on another connection's release.
    /// Congestion/window-bound writers continue to wait for their own ACKs.
    pub(crate) fn share_write_blocked(&self, cfg: &StackConfig, budget: &Budget) -> bool {
        self.want_write && self.accepted && self.fin_off.is_none()
            && matches!(self.state, State::Established | State::CloseWait | State::SynReceived)
            && budget.send_share(self.peer) < self.sndbuf_limit(cfg)
            && self.send_space(cfg, budget) < cfg.write_low_watermark as usize
    }

    /// The adapter parked the app writer while this connection held its whole
    /// send share, possibly with an empty adapter queue, so no core write
    /// blocks to set `want_write` by itself (docs/design/0007 §2.4). Mark the
    /// pending write here or a later drain would never re-notify the stream.
    #[cfg(feature = "tokio")]
    pub(crate) fn note_write_parked(&mut self) {
        self.want_write = true;
    }

    fn set_closed(&mut self, reason: CloseReason, ctx: &mut Ctx) {
        if self.state != State::Closed {
            self.state = State::Closed;
        }
        if self.close_reason.is_none() {
            self.close_reason = Some(reason);
        }
        self.timer = None;
        self.delack_at = None;
        self.keepalive_at = None;
        self.life_at = None;
        if reason == CloseReason::Normal {
            // Orderly close: received data stays readable until the app reads or
            // closes the handle (like Linux keeps the receive queue after tcp_done).
            self.release_send_side(ctx);
        } else {
            self.release_buffers(ctx);
        }
        if self.accepted && !self.closed_notified && !self.app_closed {
            self.closed_notified = true;
            ctx.events.push_back(Event::Closed(self.id, reason));
        }
    }

    fn release_buffers(&mut self, ctx: &mut Ctx) {
        self.tx.release_all(ctx.pool);
        self.sb.clear();
        let unread = self.rx.len() as u64;
        self.rx.clear();
        let ooo = self.ooo.take().map(|mut o| o.clear() as u64).unwrap_or(0);
        let _ = (unread, ooo);
        ctx.budget.release(self.peer, self.rcv_charged + self.tx_charged);
        self.rcv_charged = 0;
        self.adapter_unread = 0;
        self.tx_charged = 0;
        self.adapter_tx = 0;
        self.sync_sender(ctx);
    }

    fn release_send_side(&mut self, ctx: &mut Ctx) {
        self.tx.release_all(ctx.pool);
        self.sb.clear();
        ctx.budget.release(self.peer, self.tx_charged);
        self.tx_charged = 0;
        self.adapter_tx = 0;
        self.sync_sender(ctx);
    }

    /// Free everything (slot is being reclaimed).
    pub(crate) fn destroy(&mut self, ctx: &mut Ctx) {
        self.release_buffers(ctx);
    }

    fn enter_time_wait(&mut self, now: Instant, ctx: &mut Ctx) {
        self.state = State::TimeWait;
        self.timer = None;
        self.keepalive_at = None;
        self.life_at = Some(now + ctx.cfg.time_wait);
        // Keep only the compact part of the state: data buffers are gone.
        self.tx.release_all(ctx.pool);
        self.sb.clear();
        ctx.budget.release(self.peer, self.tx_charged);
        self.tx_charged = 0;
        self.adapter_tx = 0;
        self.sync_sender(ctx);
    }

    // ------------------------------------------------------------------
    // Application API

    pub fn read(&mut self, dst: &mut [u8], ctx: &mut Ctx, now: Instant) -> ReadResult {
        if self.rx.is_empty() {
            if let Some(r) = self.close_reason {
                if r != CloseReason::Normal {
                    return ReadResult::Closed(r);
                }
            }
            if self.fin_rcvd.is_some_and(|f| self.rcv_nxt > f) {
                return ReadResult::Eof;
            }
            if self.state == State::Closed {
                return ReadResult::Eof;
            }
            self.want_read = true;
            return ReadResult::WouldBlock;
        }
        let n = self.rx.read(dst);
        self.after_app_read(n, ctx, now);
        if self.rx.is_empty() {
            self.want_read = true;
        }
        ReadResult::Data(n)
    }

    pub fn read_chunk(&mut self, max: usize, ctx: &mut Ctx, now: Instant) -> Result<Bytes, ReadResult> {
        match self.rx.read_chunk(max) {
            Some(b) => {
                self.after_app_read(b.len(), ctx, now);
                if self.rx.is_empty() {
                    self.want_read = true;
                }
                Ok(b)
            }
            None => {
                let mut z = [0u8; 0];
                Err(self.read(&mut z, ctx, now))
            }
        }
    }

    /// Transfer bytes to an async adapter without treating the transfer as an
    /// application read. The advertised window and budget remain occupied until
    /// `consume_adapter` reports the bytes actually copied by poll_read.
    pub fn read_chunk_for_adapter(&mut self, max: usize, ctx: &mut Ctx, now: Instant) -> Result<Bytes, ReadResult> {
        match self.rx.read_chunk(max) {
            Some(b) => {
                self.adapter_unread += b.len() as u64;
                if self.rx.is_empty() {
                    self.want_read = true;
                }
                Ok(b)
            }
            None => {
                let mut z = [0u8; 0];
                Err(self.read(&mut z, ctx, now))
            }
        }
    }

    pub fn consume_adapter(&mut self, n: usize, ctx: &mut Ctx, now: Instant) {
        debug_assert!(n as u64 <= self.adapter_unread);
        let n = (n as u64).min(self.adapter_unread);
        self.adapter_unread -= n;
        self.after_app_read(n as usize, ctx, now);
    }

    fn after_app_read(&mut self, n: usize, ctx: &mut Ctx, now: Instant) {
        let n = n as u64;
        let rel = n.min(self.rcv_charged);
        self.rcv_charged -= rel;
        ctx.budget.release(self.peer, rel);
        self.rcvq_copied += n;
        self.rcv_space_adjust(now, ctx);
        // Window update when the free window at least doubled (and by ≥ 2 MSS), or
        // when it re-opens from (almost) zero.
        let cur = self.rcv_adv.saturating_sub(self.rcv_nxt);
        let new = self.calc_window(ctx);
        let mss = self.rcv_mss as u64;
        if self.state.can_recv_data() && new > cur && new - cur >= 2 * mss && (new >= 2 * cur || cur < 2 * mss) {
            self.ack_need = AckNeed::Now;
        }
    }

    /// Receive buffer autotuning (§6.6, Linux `tcp_rcv_space_adjust`).
    fn rcv_space_adjust(&mut self, now: Instant, ctx: &mut Ctx) {
        let rtt = if self.rcv_rtt.is_zero() { self.rtt.srtt_or(Duration::ZERO) } else { self.rcv_rtt };
        if rtt.is_zero() || now.saturating_since(self.rcvq_time) < rtt {
            return;
        }
        let copied = self.rcvq_copied;
        if copied > self.rcvq_space {
            let mss = self.rcv_mss as u64;
            let mut win = 2 * copied + 16 * mss;
            if self.rcvq_space > 0 {
                let grow = win * (copied - self.rcvq_space) / self.rcvq_space;
                win += grow.min(win);
            }
            let allowed = ctx.budget.level_for(self.peer) < Pressure::Low;
            let max = if self.rcv_wscale == 0 { 65535 } else { ctx.cfg.max_rcv_buf as u64 };
            if allowed && win > self.rcv_target {
                self.rcv_target = win.min(max);
            }
            self.rcvq_space = copied;
        }
        self.rcvq_copied = 0;
        self.rcvq_time = now;
        self.rcvq_off = self.rcv_nxt;
    }

    #[cfg(feature = "tokio")]
    pub(crate) fn write_allocation_charge(&self, bytes: usize, cfg: &StackConfig, budget: &Budget) -> u64 {
        self.tx.write_allocation_charge(bytes.min(self.send_space(cfg, budget)))
    }

    pub fn write(&mut self, src: &[u8], ctx: &mut Ctx) -> WriteResult {
        if !matches!(self.state, State::Established | State::CloseWait | State::SynReceived) || self.fin_off.is_some() {
            return WriteResult::Closed;
        }
        let space = self.send_space(ctx.cfg, ctx.budget);
        let n = space.min(src.len());
        if n == 0 {
            self.want_write = true;
            // A share-bound connection waits for any budget release or sender
            // change; a send-buffer-bound one waits for its own ACKs
            // (docs/design/0007 §2.3).
            if ctx.budget.send_share(self.peer) < self.sndbuf_limit(ctx.cfg) {
                ctx.budget.stats().note_share_blocked();
                return WriteResult::QuotaBlocked;
            }
            return WriteResult::WouldBlock;
        }
        if !ctx.budget.try_reserve(self.peer, n as u64, false) {
            self.want_write = true;
            return WriteResult::QuotaBlocked;
        }
        if !self.tx.push(ctx.pool, ctx.budget, self.peer, &src[..n]) {
            ctx.budget.cancel_reserve(self.peer, n as u64);
            self.want_write = true;
            return WriteResult::MemoryBlocked;
        }
        self.tx_charged += n as u64;
        self.sync_sender(ctx);
        if n < src.len() {
            self.want_write = true;
        }
        WriteResult::Written(n)
    }

    pub fn shutdown_write(&mut self) {
        if self.fin_off.is_some() {
            return;
        }
        match self.state {
            State::Established | State::SynReceived => {
                self.fin_off = Some(self.tx_end());
                if self.state == State::Established {
                    self.state = State::FinWait1;
                }
            }
            State::CloseWait => {
                self.fin_off = Some(self.tx_end());
                self.state = State::LastAck;
            }
            _ => {}
        }
    }

    /// `close` (§10.3). Unread data → RST. Otherwise orphan: finish sending, then FIN.
    pub fn close(&mut self, now: Instant, ctx: &mut Ctx) {
        self.app_closed = true;
        if self.state == State::Closed {
            return;
        }
        if !self.rx.is_empty() || self.ooo.as_ref().is_some_and(|o| !o.is_empty()) {
            self.abort(ctx);
            return;
        }
        self.shutdown_write();
        if self.state != State::TimeWait {
            self.life_at = Some(now + ctx.cfg.orphan_timeout);
        }
    }

    /// Local abort: RST to the peer, no `Closed` event (the app asked for it).
    pub fn abort(&mut self, ctx: &mut Ctx) {
        if self.state.synchronized() || self.state == State::SynReceived {
            self.rst_pending = self.state != State::TimeWait;
        }
        self.app_closed = true;
        self.set_closed(CloseReason::Aborted, ctx);
    }

    // ------------------------------------------------------------------
    // Input

    /// Process one segment addressed to this connection.
    pub(crate) fn input(&mut self, now: Instant, h: &TcpHeader, payload: IngressPayload<'_>, ctx: &mut Ctx) {
        self.stats.segs_in += 1;
        match self.state {
            State::Closed => {}
            State::SynSent => self.input_syn_sent(now, h, ctx),
            State::SynReceived => self.input_sync(now, h, payload, ctx, true),
            _ => self.input_sync(now, h, payload, ctx, false),
        }
    }

    fn input_syn_sent(&mut self, now: Instant, h: &TcpHeader, ctx: &mut Ctx) {
        if h.has(ACK) {
            let a = self.tx_sp.off(h.ack, 0);
            if a != 1 {
                if !h.has(RST) {
                    self.bad_ack_rst = true;
                }
                return;
            }
        }
        if h.has(RST) {
            if h.has(ACK) {
                self.set_closed(CloseReason::Refused, ctx);
            }
            return;
        }
        if !h.has(SYN) {
            return;
        }
        self.rx_sp = SeqSpace { isn: h.seq };
        self.rcv_nxt = 1;
        self.apply_syn_options(&SynParams { mss: h.opts.mss, wscale: h.opts.wscale, sack: h.opts.sack_perm, ts: h.opts.ts }, ctx.cfg);
        if h.has(ACK) {
            self.snd_una = 1;
            self.snd_wnd = h.window as u64; // SYN windows are never scaled
            self.max_snd_wnd = self.snd_wnd;
            self.snd_wl1 = 0;
            self.snd_wl2 = 1;
            if self.synack_tries <= 1 {
                self.rtt.sample(now, now - self.synack_ts);
            }
            self.state = State::Established;
            self.timer = None;
            self.rcv_adv = self.rcv_nxt + self.calc_window(ctx);
            self.ack_need = AckNeed::Now;
            self.last_recv = now;
            self.arm_keepalive(now, ctx.cfg);
            ctx.events.push_back(Event::Connected(self.id));
            self.notify_writable_initial(ctx);
        }
    }

    fn notify_writable_initial(&mut self, ctx: &mut Ctx) {
        self.want_write = true;
        self.notify_writable(ctx.events, ctx.cfg, ctx.budget);
    }

    /// Desync detection. A synchronized peer's cumulative ACK and TSval never go
    /// backwards (both are monotonic in its send order), so these segments cannot
    /// come from a peer that agrees with us:
    /// - an ACK outside [SND.UNA, SND.MAX] with TSval > TS.Recent (sent after every
    ///   segment we accepted, yet acking below what they acked or beyond what we sent);
    /// - a PAWS failure acking beyond SND.UNA (newer than every accepted segment,
    ///   yet with an older TSval).
    ///
    /// Reordered or duplicated old segments satisfy neither. Such `proof` means a
    /// forged in-window ACK or timestamp was accepted, and nothing repairs that: data
    /// a forged ACK released is gone and the peer's segments keep being discarded.
    ///
    /// Evidence is counted at most once per stall epoch (each RTO, zero-window probe
    /// or keepalive we send without progress), and only from a segment whose TSval is
    /// newer than the previous evidence's, i.e. a new segment the peer sent in reply,
    /// not a reordered or duplicated old one. Evidence in `DESYNC_EPOCHS` consecutive
    /// epochs with no good segment in between resets the connection instead of
    /// waiting for the user timeout. Needs timestamps; without them the user timeout
    /// remains the backstop. Anyone able to forge such segments could already reset
    /// the connection (RFC 5961 exact-sequence RST), so this adds no attack surface.
    fn note_desync(&mut self, h: &TcpHeader, proof: bool, ctx: &mut Ctx) -> bool {
        let Some((tsval, _)) = h.opts.ts else { return false };
        if !proof || !self.ts_ok || self.stall_epoch <= self.desync_epoch {
            return false;
        }
        if self.desync_count > 0 && (tsval.wrapping_sub(self.desync_tsval) as i32) <= 0 {
            return false;
        }
        self.desync_count += 1;
        self.desync_epoch = self.stall_epoch;
        self.desync_tsval = tsval;
        if self.desync_count >= DESYNC_EPOCHS {
            self.rst_pending = true;
            self.set_closed(CloseReason::Desync, ctx);
            return true;
        }
        false
    }

    /// A segment consistent with our state arrived: any desync evidence was stale.
    fn desync_clear(&mut self) {
        self.desync_count = 0;
        self.desync_epoch = self.stall_epoch;
    }

    fn challenge_ack(&mut self, now: Instant) {
        // RFC 5961 §7: rate-limit challenge ACKs.
        if now.saturating_since(self.challenge_ack_ts) >= Duration::from_secs(1) {
            self.challenge_ack_ts = now;
            self.challenge_acks_this_sec = 0;
        }
        if self.challenge_acks_this_sec < 10 {
            self.challenge_acks_this_sec += 1;
            self.stats.challenge_acks += 1;
            self.ack_need = AckNeed::Now;
        }
    }

    fn input_sync(&mut self, now: Instant, h: &TcpHeader, mut payload: IngressPayload<'_>, ctx: &mut Ctx, syn_rcvd: bool) {
        let seg_off = self.rx_sp.off(h.seq, self.rcv_nxt);
        let mut seg_len = payload.len() as i64 + h.has(SYN) as i64 + h.has(FIN) as i64;
        let rcv_nxt = self.rcv_nxt as i64;
        let wnd = self.rcv_adv.saturating_sub(self.rcv_nxt) as i64;
        let mut fin_dropped = false;

        // Duplicate SYN in SYN-RECEIVED: retransmit the SYN-ACK.
        if syn_rcvd && h.has(SYN) && !h.has(ACK) && seg_off == 0 {
            self.syn_pending = true;
            return;
        }

        // PAWS (RFC 7323 §5).
        if self.ts_ok && !h.has(RST) {
            if let Some((tsval, _)) = h.opts.ts {
                if (tsval.wrapping_sub(self.ts_recent) as i32) < 0 && now.saturating_since(self.ts_recent_time) < TS_VALID_FOR {
                    self.stats.paws_drops += 1;
                    self.ack_need = AckNeed::Now;
                    if !syn_rcvd && h.has(ACK) {
                        let a = self.tx_sp.off(h.ack, self.snd_una);
                        let proof = a > self.snd_una as i64 && a <= self.snd_max as i64;
                        self.note_desync(h, proof, ctx);
                    }
                    return;
                }
            }
        }

        // Sequence acceptability (RFC 9293 §3.10.7.4).
        let acceptable = if seg_len == 0 {
            if wnd == 0 {
                seg_off == rcv_nxt
            } else {
                rcv_nxt <= seg_off && seg_off < rcv_nxt + wnd || seg_off == rcv_nxt - 1
                // keepalive / zero-window probe style
            }
        } else if wnd == 0 {
            // Zero window: still process the ACK of an in-sequence segment.
            if seg_off == rcv_nxt {
                // The data is dropped, so a FIN riding on it must be ignored too.
                payload = IngressPayload::Borrowed(&[]);
                seg_len = 0;
                fin_dropped = true;
                true
            } else {
                false
            }
        } else {
            let end = seg_off + seg_len - 1;
            (rcv_nxt <= seg_off && seg_off < rcv_nxt + wnd) || (rcv_nxt <= end && end < rcv_nxt + wnd) || (seg_off < rcv_nxt && end >= rcv_nxt + wnd)
        };
        if !acceptable {
            if !h.has(RST) {
                if seg_len > 0 && seg_off + seg_len <= rcv_nxt && self.sack_ok {
                    // Entirely old duplicate: report with D-SACK (RFC 2883).
                    self.dsack = Some((seg_off.max(0) as u64, (seg_off + seg_len) as u64));
                }
                self.ack_need = AckNeed::Now;
            }
            return;
        }

        // RST (RFC 5961 §3).
        if h.has(RST) {
            if seg_off == rcv_nxt {
                self.set_closed(CloseReason::Reset, ctx);
            } else {
                self.challenge_ack(now);
            }
            return;
        }

        // SYN in a synchronized state (RFC 5961 §4).
        if h.has(SYN) {
            self.challenge_ack(now);
            return;
        }

        if !h.has(ACK) {
            return;
        }

        self.last_recv = now;
        self.keepalive_sent = 0;
        self.arm_keepalive(now, ctx.cfg);

        let ack_off = self.tx_sp.off(h.ack, self.snd_una);
        // Validate the ACK field before trusting anything else in the segment
        // (TS.Recent must not be updated from a segment we then drop).
        // Sent after every segment we accepted (desync proof for a bad ACK below).
        let fresh = self.ts_ok && h.opts.ts.is_some_and(|(v, _)| (v.wrapping_sub(self.ts_recent) as i32) > 0);
        if !syn_rcvd {
            // Both are impossible from a synchronized peer's fresh segment.
            if ack_off > self.snd_max as i64 {
                self.ack_need = AckNeed::Now;
                self.note_desync(h, fresh, ctx);
                return;
            }
            if ack_off < self.snd_una as i64 - self.max_snd_wnd.max(65535) as i64 {
                // Too old (RFC 5961 §5).
                self.challenge_ack(now);
                self.note_desync(h, fresh, ctx);
                return;
            }
        }

        // Timestamps: TS.Recent update and receiver RTT sample.
        if self.ts_ok {
            if let Some((tsval, tsecr)) = h.opts.ts {
                if seg_off <= self.last_ack_sent as i64 && (tsval.wrapping_sub(self.ts_recent) as i32) >= 0 {
                    self.ts_recent = tsval;
                    self.ts_recent_time = now;
                }
                if tsecr != 0 && !payload.is_empty() {
                    let d = self.ts_val(now).wrapping_sub(tsecr);
                    if d < 60_000 {
                        let s = Duration::from_millis(d.max(1) as u64);
                        self.rcv_rtt = if self.rcv_rtt.is_zero() { s } else { (self.rcv_rtt * 7 + s) / 8 }.min(s.max(self.rcv_rtt));
                    }
                }
            }
        }

        if !syn_rcvd {
            if ack_off < self.snd_una as i64 {
                if self.note_desync(h, fresh, ctx) {
                    return;
                }
            } else {
                self.desync_clear();
            }
        }

        if syn_rcvd {
            if ack_off != 1 || self.snd_max < 1 {
                // Bad ACK in SYN-RECEIVED: reset with seq = SEG.ACK (sent by the shard).
                self.bad_ack_rst = true;
                return;
            }
            self.snd_una = 1;
            self.snd_wnd = (h.window as u64) << self.snd_wscale;
            self.max_snd_wnd = self.snd_wnd;
            self.snd_wl1 = seg_off as u64;
            self.snd_wl2 = 1;
            if self.synack_tries <= 1 {
                self.rtt.sample(now, now - self.synack_ts);
            }
            self.timer = None;
            self.state = State::Established;
            if self.fin_off.is_some() {
                self.state = State::FinWait1;
            }
            self.accepted = true;
            ctx.events.push_back(Event::Accepted(self.id));
            self.notify_writable_initial(ctx);
        }

        // ACK processing.
        if ack_off > self.snd_max as i64 {
            self.ack_need = AckNeed::Now;
            return;
        }
        if ack_off < self.snd_una as i64 - self.max_snd_wnd.max(65535) as i64 {
            // Too old (RFC 5961 §5).
            self.challenge_ack(now);
            return;
        }
        self.process_ack(now, h, seg_off as u64, ack_off.max(0) as u64, ctx);
        if self.state == State::Closed {
            return;
        }

        // Data arriving after the app closed the handle cannot be delivered: RST
        // (as Linux does for orphaned sockets).
        if !payload.is_empty() && self.app_closed && self.state.can_recv_data() {
            self.rst_pending = true;
            self.set_closed(CloseReason::Aborted, ctx);
            return;
        }
        // Data.
        if !payload.is_empty() && self.state.can_recv_data() {
            self.process_data(now, seg_off, payload, h.has(PSH), ctx);
        } else if !payload.is_empty() {
            // Data after our side stopped receiving (e.g. CLOSE_WAIT): just ACK.
            self.ack_need = AckNeed::Now;
        }

        // FIN.
        if h.has(FIN) && !fin_dropped {
            let fin_at = (seg_off + seg_len - 1) as u64;
            if self.fin_rcvd.is_none() && fin_at >= self.rcv_nxt {
                self.fin_rcvd = Some(fin_at);
            }
            self.ack_need = AckNeed::Now;
        }
        self.try_consume_fin(now, ctx);
    }

    fn try_consume_fin(&mut self, now: Instant, ctx: &mut Ctx) {
        let Some(f) = self.fin_rcvd else { return };
        if self.rcv_nxt != f {
            return;
        }
        self.rcv_nxt = f + 1;
        self.ack_need = AckNeed::Now;
        match self.state {
            State::Established => self.state = State::CloseWait,
            State::FinWait1 => {
                // Our FIN not yet acked (else we'd be in FIN-WAIT-2): simultaneous close.
                self.state = State::Closing;
            }
            State::FinWait2 => {
                self.enter_time_wait(now, ctx);
            }
            _ => {}
        }
        // EOF is a read event.
        self.want_read = true;
        self.notify_readable(ctx.events);
        if self.app_closed && self.state == State::CloseWait {
            // Orphaned and peer finished too: close our side.
            self.shutdown_write();
        }
    }

    fn process_data(&mut self, now: Instant, seg_off: i64, mut payload: IngressPayload<'_>, _psh: bool, ctx: &mut Ctx) {
        let mut off = seg_off;
        let rcv_nxt = self.rcv_nxt as i64;
        if off < rcv_nxt {
            // Partial duplicate: D-SACK the duplicate part.
            let dup = (rcv_nxt - off) as usize;
            if dup >= payload.len() {
                if self.sack_ok {
                    self.dsack = Some((off as u64, (off + payload.len() as i64) as u64));
                }
                self.ack_need = AckNeed::Now;
                return;
            }
            if self.sack_ok {
                self.dsack = Some((off as u64, rcv_nxt as u64));
            }
            payload = payload.slice(dup);
            off = rcv_nxt;
        }
        // Trim to the advertised right edge.
        let right = self.rcv_adv as i64;
        if off + payload.len() as i64 > right {
            let keep = (right - off).max(0) as usize;
            payload.truncate(keep);
            self.ack_need = AckNeed::Now;
            if payload.is_empty() {
                return;
            }
        }
        let len = payload.len() as u64;
        self.rcv_mss = self.rcv_mss.max(len as u32);
        let in_order = off as u64 == self.rcv_nxt;

        // Memory: reserve before keeping the bytes (§6.3/§6.4). In-order data may use
        // the per-connection progress reserve (2 MSS) beyond peer quota / pressure.
        let level = ctx.budget.level_for(self.peer);
        let progress = in_order && self.rcv_charged < 2 * self.rcv_mss as u64;
        if !in_order && level >= Pressure::Pressure {
            self.stats.dropped_no_mem += 1;
            self.ack_need = AckNeed::Now;
            return;
        }
        if !ctx.budget.try_reserve(self.peer, len, progress) {
            self.stats.dropped_no_mem += 1;
            self.ack_need = AckNeed::Now;
            return;
        }
        if in_order {
            let was_empty = self.rx.is_empty();
            let retained = match &payload {
                IngressPayload::Borrowed(v) => self.rx.push_charged(v, ctx.budget, self.peer),
                IngressPayload::Owned(v) => self.rx.push_charged(v, ctx.budget, self.peer),
            };
            if !retained {
                ctx.budget.release(self.peer, len);
                self.stats.dropped_no_mem += 1;
                self.ack_need = AckNeed::Now;
                return;
            }
            self.rcv_charged += len;
            self.stats.bytes_received += len;
            self.rcv_nxt += len;
            let had_ooo = self.ooo.as_ref().is_some_and(|o| !o.is_empty());
            if had_ooo {
                let o = self.ooo.as_mut().unwrap();
                let (next, discarded) = o.pop_contiguous(self.rcv_nxt, &mut self.rx);
                self.rcv_nxt = next;
                // Out-of-order bytes overlapped by the in-order segment were charged twice.
                let rel = discarded.min(self.rcv_charged);
                self.rcv_charged -= rel;
                ctx.budget.release(self.peer, rel);
                if o.is_empty() {
                    self.ooo = None;
                }
                // Filled a hole: ACK immediately (RFC 5681 §4.2).
                self.ack_need = AckNeed::Now;
            } else {
                self.unacked_bytes += len;
                if self.unacked_bytes >= MAX_DELACK_SEGS * self.rcv_mss as u64 {
                    self.ack_need = AckNeed::Now;
                } else if self.ack_need == AckNeed::None {
                    self.ack_need = AckNeed::Delayed;
                    if self.delack_at.is_none() {
                        self.delack_at = Some(now + ctx.cfg.delayed_ack);
                    }
                }
            }
            if was_empty || !self.rx.is_empty() {
                self.notify_readable(ctx.events);
            }
            self.rcv_space_adjust(now, ctx);
        } else {
            self.rcv_charged += len;
            self.stats.bytes_received += len;
            self.stats.ooo_segs += 1;
            // Descriptor bound: an average of ≥ 512 bytes per stored segment over the
            // receive buffer (full-size segments always fit; 1-byte floods do not).
            let seg_limit = ((self.rcv_target / 512) as usize).max(64);
            if self.ooo.as_ref().is_some_and(|o| o.is_full(seg_limit)) {
                // Bound per-connection descriptor state (§6.7): drop, do not ACK-stall.
                self.rcv_charged -= len;
                ctx.budget.release(self.peer, len);
                self.stats.dropped_no_mem += 1;
                self.ack_need = AckNeed::Now;
                return;
            }
            let o = self.ooo.get_or_insert_with(Default::default);
            let added = match &payload {
                IngressPayload::Borrowed(v) => o.insert_charged(off as u64, v, ctx.budget, self.peer),
                IngressPayload::Owned(v) => o.insert_charged(off as u64, v, ctx.budget, self.peer),
            };
            let Some(added) = added else {
                self.rcv_charged -= len;
                ctx.budget.release(self.peer, len);
                self.stats.dropped_no_mem += 1;
                self.ack_need = AckNeed::Now;
                if o.is_empty() {
                    self.ooo = None;
                }
                return;
            };
            if (added as u64) < len {
                let dup = len - added as u64;
                self.rcv_charged -= dup;
                ctx.budget.release(self.peer, dup);
                if added == 0 && self.sack_ok {
                    self.dsack = Some((off as u64, off as u64 + len));
                }
            }
            self.ack_need = AckNeed::Now;
        }
    }

    // ------------------------------------------------------------------
    // ACK processing

    fn process_ack(&mut self, now: Instant, h: &TcpHeader, seg_off: u64, ack: u64, ctx: &mut Ctx) {
        let prior_in_flight = self.pipe();
        let prior_una = self.snd_una;

        // Window update (RFC 9293 SND.WL1/WL2 rule).
        let new_wnd = (h.window as u64) << self.snd_wscale;
        if self.snd_wl1 < seg_off || (self.snd_wl1 == seg_off && self.snd_wl2 <= ack) {
            let opened = new_wnd > self.snd_wnd;
            self.snd_wnd = new_wnd;
            self.max_snd_wnd = self.max_snd_wnd.max(new_wnd);
            self.snd_wl1 = seg_off;
            self.snd_wl2 = ack;
            if opened && new_wnd > 0 {
                self.persist_backoff = 0;
            }
        }

        let mut rs = RateSample { prior_in_flight, ..Default::default() };
        let mut newest_rtt: Option<(Instant, Duration)> = None;
        let mut sampled: Option<Rec> = None;
        let mut newly_acked = 0u64;
        let mut reordering = false;

        // Eifel spurious RTO detection (RFC 3522) using timestamps.
        if let (Some(rto_ts), Some((_, tsecr))) = (self.rto_tsval, h.opts.ts) {
            if ack > self.snd_una {
                if (tsecr.wrapping_sub(rto_ts) as i32) < 0 {
                    self.stats.spurious_rtos += 1;
                    self.cc.on_spurious();
                    self.rto_recovery = None;
                    // Undo the loss marking of the RTO.
                    for i in 0..self.sb.recs.len() {
                        let r = self.sb.recs[i];
                        if r.has(F_LOST) && !r.has(F_RETRANS) {
                            // Not yet retransmitted: no longer considered lost.
                            let mut m = r;
                            m.flags &= !F_LOST;
                            self.sb.recs[i] = m;
                            self.sb.lost -= m.len();
                        }
                    }
                }
                self.rto_tsval = None;
            }
        }

        // Cumulative ACK.
        if ack > self.snd_una {
            let dl = &mut self.dl;
            let rack = &mut self.rack;
            let min_rtt = self.rtt.min_rtt_or(Duration::MAX);
            self.sb.ack_to(ack, |r| {
                newly_acked += r.len();
                Self::on_delivered(dl, rack, r, now, &mut sampled, &mut newest_rtt, min_rtt, &mut reordering);
            });
            self.snd_una = ack;
            if self.snd_nxt < ack {
                self.snd_nxt = ack;
            }
            // Release acknowledged TX bytes.
            let data_end = self.fin_off.unwrap_or(u64::MAX).min(ack);
            if data_end > self.tx_base {
                let n = (data_end - self.tx_base) as usize;
                self.tx.consume(ctx.pool, n);
                self.tx_base += n as u64;
                let rel = (n as u64).min(self.tx_charged);
                self.tx_charged -= rel;
                ctx.budget.release(self.peer, rel);
                self.sync_sender(ctx);
            }
            self.rto_backoff = 0;
            self.rto_base = now;
            self.progress_ts = now;
            self.keepalive_sent = 0;
        }

        // SACK blocks.
        let mut dsack_seen = false;
        let mut newly_sacked = 0u64;
        if self.sack_ok && h.opts.sack_n > 0 {
            for (i, &(l, r)) in h.opts.sack_blocks().iter().enumerate() {
                let lo = self.tx_sp.off(l, self.snd_una);
                let hi = self.tx_sp.off(r, self.snd_una);
                if hi <= lo {
                    continue;
                }
                // D-SACK: first block below the cumulative ACK, or inside the second block.
                if i == 0 {
                    let below = hi <= ack as i64;
                    let inside = h.opts.sack_n > 1 && {
                        let (l2, r2) = h.opts.sack[1];
                        let lo2 = self.tx_sp.off(l2, self.snd_una);
                        let hi2 = self.tx_sp.off(r2, self.snd_una);
                        lo >= lo2 && hi <= hi2
                    };
                    if below || inside {
                        dsack_seen = true;
                        self.stats.dsacks += 1;
                        continue;
                    }
                }
                if lo < self.snd_una as i64 || hi > self.snd_max as i64 {
                    continue;
                }
                let dl = &mut self.dl;
                let rack = &mut self.rack;
                let min_rtt = self.rtt.min_rtt_or(Duration::MAX);
                newly_sacked += self.sb.sack(lo as u64, hi as u64, |r| {
                    Self::on_delivered(dl, rack, r, now, &mut sampled, &mut newest_rtt, min_rtt, &mut reordering);
                });
            }
        }
        if reordering {
            self.rack.reordering_seen = true;
        }
        if dsack_seen {
            // RFC 8985 §6.2 step 4: grow reo_wnd once per round with DSACK.
            let round = self.dl.round_count;
            if self.rack.dsack_round.map_or(true, |r| round > r) {
                self.rack.dsack_round = Some(round);
                self.rack.reo_wnd_mult = (self.rack.reo_wnd_mult + 1).min(64);
                self.rack.reo_wnd_persist = 16;
            }
            self.rack.reordering_seen = true;
        }

        // A lone-segment probe acknowledged faster than any round trip: the ACK is
        // for the original, which was only delayed. Learn the real delay from it.
        if let Some((end, orig, probe)) = self.tlp.lone_probe {
            if ack >= end {
                self.tlp.lone_probe = None;
                if self.rtt.has_sample && now.saturating_since(probe) < self.rtt.min_rtt {
                    let d = now.saturating_since(orig).saturating_sub(self.rtt.min_rtt).min(WC_DEL_ACK);
                    self.tlp.lone_ack_delay = Some(self.tlp.lone_ack_delay.map_or(d, |e| e.max(d)));
                }
            }
        }

        // RTT sample (Karn: never from retransmitted records).
        if let Some((_, rtt)) = newest_rtt {
            self.rtt.sample(now, rtt);
            rs.rtt = Some(rtt);
            // A lone segment acked on its own: its extra delay over min RTT is the
            // peer's delayed-ACK timer (RFC 8985 §7.2 WCDelAckT, measured).
            if self.snd_max - prior_una <= self.mss as u64 && self.snd_una == self.snd_max {
                let d = rtt.saturating_sub(self.rtt.min_rtt).min(WC_DEL_ACK);
                self.tlp.lone_ack_delay = Some(match self.tlp.lone_ack_delay {
                    Some(e) if e > d => e - (e - d) / 8,
                    _ => d,
                });
            }
        }

        // RACK loss detection (RFC 8985 §6.2).
        let newly_lost = if self.sb.is_empty() { 0 } else { self.rack_detect(now) };

        // TLP episode resolution (RFC 8985 §7.4).
        if let Some(end) = self.tlp.end {
            if ack >= end {
                if self.tlp.is_retrans && !dsack_seen && prior_una < end {
                    // The probe repaired a real loss: reduce once.
                    self.cc.on_congestion_event(now, prior_in_flight, self.mss);
                    self.cc.on_recovery_exit(now);
                }
                self.tlp.end = None;
            } else if dsack_seen {
                self.tlp.end = None;
            }
        }

        let delivered_now = newly_acked + newly_sacked;

        // Delivery rate sample + rounds.
        let mut round_start = false;
        if let Some(r) = sampled {
            rs.prior_delivered = r.delivered;
            rs.is_app_limited = r.app_limited;
            rs.tx_in_flight = r.tx_in_flight;
            rs.lost = self.dl.lost_total.saturating_sub(r.lost_at_send);
            rs.delivered = self.dl.delivered - r.delivered;
            let send_elapsed = r.xmit.saturating_since(r.first_tx_ts);
            let ack_elapsed = self.dl.delivered_ts.saturating_since(r.delivered_ts);
            rs.interval = send_elapsed.max(ack_elapsed);
            let min = self.rtt.min_rtt;
            if min != Duration::MAX && rs.interval < min {
                // Implausibly short interval (ACK compression): invalidate the rate.
                rs.interval = Duration::ZERO;
            }
            if r.delivered >= self.dl.next_round_delivered {
                self.dl.next_round_delivered = self.dl.delivered;
                self.dl.round_count += 1;
                round_start = true;
                self.cwnd_limited_prev = self.cwnd_limited_now;
                self.cwnd_limited_now = false;
            }
        }
        if self.dl.app_limited != 0 && self.dl.delivered > self.dl.app_limited {
            self.dl.app_limited = 0;
        }
        rs.newly_acked = delivered_now;
        rs.newly_lost = newly_lost;

        // Loss recovery state machine.
        if let Some(p) = self.rto_recovery {
            if self.snd_una >= p {
                self.rto_recovery = None;
                if !self.cc.uses_prr() {
                    self.cc.on_recovery_exit(now);
                }
            }
        }
        if let Some(rec) = &self.recovery {
            if self.snd_una >= rec.point {
                self.recovery = None;
                self.cc.on_recovery_exit(now);
            }
        }
        if newly_lost > 0 {
            self.maybe_enter_recovery(now, prior_in_flight);
        }
        if let Some(rec) = self.recovery.as_mut() {
            // PRR (RFC 6937) with SSRB.
            rec.prr_delivered += delivered_now;
            let pipe = self.sb.pipe(self.snd_nxt - self.snd_una);
            let ssthresh = self.cc.ssthresh();
            let mss = self.mss as u64;
            let sndcnt = if pipe > ssthresh {
                (rec.prr_delivered * ssthresh).div_ceil(rec.recover_fs) as i64 - rec.prr_out as i64
            } else {
                let limit = (rec.prr_delivered as i64 - rec.prr_out as i64).max(delivered_now as i64) + mss as i64;
                (ssthresh as i64 - pipe as i64).min(limit)
            };
            let mut sndcnt = sndcnt.max(0) as u64;
            if rec.prr_out == 0 && sndcnt == 0 && delivered_now > 0 {
                sndcnt = mss;
            }
            rec.quota = sndcnt;
        }

        // Congestion control.
        if delivered_now > 0 {
            let ctx_ack = AckCtx {
                now,
                mss: self.mss,
                rs,
                delivered: self.dl.delivered,
                in_flight: self.pipe(),
                srtt: self.rtt.srtt_or(Duration::from_millis(100)),
                min_rtt: self.rtt.min_rtt_or(Duration::from_millis(100)),
                in_recovery: self.recovery.is_some() || self.rto_recovery.is_some(),
                round_start,
                round_count: self.dl.round_count,
                cwnd_limited: self.cwnd_limited_now || self.cwnd_limited_prev || prior_in_flight + self.mss as u64 >= self.cc.cwnd(),
            };
            self.cc.on_ack(&ctx_ack);
        } else if newly_lost > 0 && !self.cc.uses_prr() {
            let ctx_ack = AckCtx {
                now,
                mss: self.mss,
                rs,
                delivered: self.dl.delivered,
                in_flight: self.pipe(),
                srtt: self.rtt.srtt_or(Duration::from_millis(100)),
                min_rtt: self.rtt.min_rtt_or(Duration::from_millis(100)),
                in_recovery: true,
                round_start,
                round_count: self.dl.round_count,
                cwnd_limited: true,
            };
            self.cc.on_ack(&ctx_ack);
        }

        // FIN acknowledged → state transitions.
        if let Some(f) = self.fin_off {
            if self.snd_una > f {
                match self.state {
                    State::FinWait1 => {
                        self.state = State::FinWait2;
                        if self.app_closed {
                            self.life_at = Some(now + ctx.cfg.orphan_timeout);
                        }
                    }
                    State::Closing => self.enter_time_wait(now, ctx),
                    State::LastAck => {
                        self.set_closed(CloseReason::Normal, ctx);
                        return;
                    }
                    _ => {}
                }
            }
        }

        if ack > prior_una {
            self.notify_writable(ctx.events, ctx.cfg, ctx.budget);
            if self.snd_una == self.snd_max {
                self.life_at = self.life_at.filter(|_| self.app_closed || self.state == State::TimeWait);
            }
        }
        self.rearm_rtx_timer(now);
    }

    #[allow(clippy::too_many_arguments)]
    fn on_delivered(
        dl: &mut Delivery,
        rack: &mut Rack,
        r: &Rec,
        now: Instant,
        sampled: &mut Option<Rec>,
        newest_rtt: &mut Option<(Instant, Duration)>,
        min_rtt: Duration,
        reordering: &mut bool,
    ) {
        dl.delivered += r.len();
        dl.delivered_ts = now;
        // Rate sample uses the most recently sent delivered record.
        if sampled.map_or(true, |s| r.delivered > s.delivered || (r.delivered == s.delivered && r.xmit > s.xmit)) {
            *sampled = Some(*r);
            dl.first_tx_ts = r.xmit;
        }
        let rtt = now.saturating_since(r.xmit);
        let ever_rtx = r.has(F_EVER_RETRANS);
        if !ever_rtx && newest_rtt.map_or(true, |(x, _)| r.xmit >= x) {
            *newest_rtt = Some((r.xmit, rtt));
        }
        // RACK update (RFC 8985 §6.2 step 2): ignore likely-original ACKs of retransmits.
        if ever_rtx && rtt < min_rtt {
            return;
        }
        if r.end < rack.fack && !ever_rtx {
            *reordering = true;
        }
        rack.fack = rack.fack.max(r.end);
        if !rack.valid || r.xmit > rack.xmit || (r.xmit == rack.xmit && r.end > rack.end) {
            rack.xmit = r.xmit;
            rack.end = r.end;
            rack.rtt = rtt;
            rack.valid = true;
        }
    }

    fn reo_wnd(&self) -> Duration {
        let sacked_segs = self.sb.sacked / self.mss.max(1) as u64;
        if !self.rack.reordering_seen && (self.recovery.is_some() || self.rto_recovery.is_some() || sacked_segs >= 3) {
            return Duration::ZERO;
        }
        let min_rtt = self.rtt.min_rtt_or(Duration::ZERO);
        (min_rtt / 4 * self.rack.reo_wnd_mult).min(self.rtt.srtt_or(Duration::ZERO))
    }

    /// Mark lost every record sent before the most recently delivered one by more than
    /// the reordering window. Returns newly lost bytes and arms the reorder timer.
    fn rack_detect(&mut self, now: Instant) -> u64 {
        if !self.rack.valid {
            return 0;
        }
        let reo = self.reo_wnd();
        let mut newly = 0;
        let mut timeout: Option<Instant> = None;
        let mut i = 0;
        let rack_rtt = self.rack.rtt;
        while i < self.sb.recs.len() {
            let r = self.sb.recs[i];
            if r.start >= self.rack.end {
                break;
            }
            self.stats.rack_scanned += 1;
            if r.has(F_SACKED) || (r.has(F_LOST) && !r.has(F_RETRANS)) {
                i += 1;
                continue;
            }
            let sent_before = r.xmit < self.rack.xmit || (r.xmit == self.rack.xmit && r.end < self.rack.end);
            if sent_before {
                let deadline = r.xmit + rack_rtt + reo;
                if deadline <= now {
                    newly += self.sb.mark_lost(i);
                } else {
                    timeout = Some(timeout.map_or(deadline, |t: Instant| t.min(deadline)));
                }
            }
            i += 1;
        }
        self.dl.lost_total += newly;
        self.rack_timeout = timeout;
        newly
    }
}

// Extra field kept outside the main struct literal for readability.
impl Conn {
    #[inline]
    fn arm_keepalive(&mut self, now: Instant, cfg: &StackConfig) {
        if self.state.synchronized() && self.state != State::TimeWait {
            self.keepalive_at = Some(now + cfg.keepalive_idle);
        }
    }
}

// ======================================================================
// Recovery helpers, output, timers

impl Conn {
    fn maybe_enter_recovery(&mut self, now: Instant, prior_in_flight: u64) {
        if self.recovery.is_some() || self.rto_recovery.is_some() {
            return;
        }
        self.stats.fast_recoveries += 1;
        self.cc.on_congestion_event(now, prior_in_flight, self.mss);
        self.recovery = Some(Recovery {
            point: self.snd_max,
            prr_delivered: 0,
            prr_out: 0,
            recover_fs: prior_in_flight.max(1),
            // Allow the first retransmission immediately (RFC 6937: fast retransmit).
            quota: self.mss as u64,
        });
    }

    /// Bytes that congestion control allows to be sent now.
    fn send_room(&self) -> u64 {
        match &self.recovery {
            Some(r) if self.cc.uses_prr() => r.quota,
            _ => self.cc.cwnd().saturating_sub(self.pipe()),
        }
    }

    fn calc_window(&self, ctx: &Ctx) -> u64 {
        // Local TX/cache pressure must not close a healthy receive window.
        // level_for still limits receive autotuning and OOO retention; actual
        // RX allocations remain subject to all three physical hard limits.
        self.calc_window_l(ctx.budget.level())
    }

    fn calc_window_l(&self, level: Pressure) -> u64 {
        let unread = self.rx.len() as u64 + self.adapter_unread;
        let free = self.rcv_target.saturating_sub(unread);
        let mut right = self.rcv_nxt + free;
        // Receiver SWS avoidance (RFC 9293 §3.8.6.2.2): only move the right edge by at
        // least min(buffer/2, MSS).
        if right > self.rcv_adv && self.rcv_adv > 0 && right - self.rcv_adv < (self.rcv_target / 2).min(self.rcv_mss as u64) {
            right = self.rcv_adv;
        }
        if level >= Pressure::Pressure {
            // Stop moving the right edge under pressure (§6.7).
            right = right.min(self.rcv_adv.max(self.rcv_nxt));
        }
        // The advertised right edge never retreats.
        right = right.max(self.rcv_adv);
        let wnd = right - self.rcv_nxt;
        wnd.min(65535u64 << self.rcv_wscale)
    }

    fn window_field(&self, level: Pressure) -> u16 {
        let w = self.calc_window_l(level);
        let ws = self.rcv_wscale;
        (w.div_ceil(1u64 << ws)).min(65535) as u16
    }

    fn fin_pending(&self) -> bool {
        matches!(self.fin_off, Some(f) if self.snd_nxt <= f)
    }

    fn mark_app_limited(&mut self) {
        let pipe = self.pipe();
        if self.dl.app_limited == 0 && pipe < self.cc.cwnd() {
            self.dl.app_limited = (self.dl.delivered + pipe).max(1);
        }
    }

    /// Decide the next segment. Does not change protocol state except for splitting
    /// send records to the current MSS and bookkeeping of limits.
    pub(crate) fn plan(&mut self, now: Instant, level: Pressure) -> Option<Plan> {
        let base = |kind, flags, seq_off, len| Plan { kind, flags, seq_off, len, window: 0, rec_idx: usize::MAX };
        if self.rst_pending {
            return Some(base(PlanKind::Rst, RST | ACK, self.snd_nxt, 0));
        }
        let mut p = match self.state {
            State::Closed => return None,
            State::SynSent => {
                if !self.syn_pending {
                    return None;
                }
                let mut p = base(PlanKind::Syn, SYN, 0, 0);
                p.window = self.rcv_target.min(65535) as u16;
                return Some(p);
            }
            State::SynReceived => {
                if !self.syn_pending {
                    if self.ack_need == AckNeed::Now {
                        // Retransmitting the SYN-ACK is the only valid response.
                        self.ack_need = AckNeed::None;
                    }
                    return None;
                }
                let mut p = base(PlanKind::SynAck, SYN | ACK, 0, 0);
                p.window = self.rcv_target.min(65535) as u16;
                return Some(p);
            }
            _ => self.plan_data(now),
        };
        if p.is_none() {
            if self.probe_pending {
                p = Some(base(PlanKind::Probe, ACK, self.snd_una.wrapping_sub(1), 0));
            } else if self.ack_need == AckNeed::Now {
                p = Some(base(PlanKind::Ack, ACK, self.snd_nxt, 0));
            }
        }
        if let Some(p) = p.as_mut() {
            p.window = self.window_field(level);
        }
        p
    }

    /// Plan while no send record can be added (docs/design/0005 §3). A lost
    /// record that still fits one MSS is retransmitted in its existing slot,
    /// so a sender that cannot grow its records still repairs the losses
    /// whose ACKs free them; new data, probes and splits wait.
    pub(crate) fn plan_in_place(&mut self, now: Instant, level: Pressure) -> Option<Plan> {
        if !self.rst_pending && self.state.can_send_data() {
            if let Some(mut p) = self.plan_data_with(now, false) {
                p.window = self.window_field(level);
                return Some(p);
            }
        }
        self.plan_control(level)
    }

    /// A lost record that [`Self::plan_in_place`] could retransmit.
    pub(crate) fn has_lost_pending(&self) -> bool {
        self.sb.lost_pending() > 0
    }

    /// A pure control segment (probe or ACK), used when data is pacing-blocked.
    pub(crate) fn plan_control(&mut self, level: Pressure) -> Option<Plan> {
        let base = |kind, flags, seq_off| Plan { kind, flags, seq_off, len: 0, window: 0, rec_idx: usize::MAX };
        let mut p = if self.probe_pending {
            base(PlanKind::Probe, ACK, self.snd_una.wrapping_sub(1))
        } else if self.ack_need == AckNeed::Now && self.state.synchronized() {
            base(PlanKind::Ack, ACK, self.snd_nxt)
        } else {
            return None;
        };
        p.window = self.window_field(level);
        Some(p)
    }

    fn plan_data(&mut self, now: Instant) -> Option<Plan> {
        self.plan_data_with(now, true)
    }

    /// `can_insert` is false when the send records have no spare slot: only a
    /// retransmission that reuses its record is planned.
    fn plan_data_with(&mut self, now: Instant, can_insert: bool) -> Option<Plan> {
        if !self.state.can_send_data() {
            return None;
        }
        let mss = self.mss as u64;
        let room = self.send_room();
        let fin_off = self.fin_off.unwrap_or(u64::MAX);

        // 1. Retransmit records marked lost.
        if let Some(i) = self.sb.next_lost() {
            let r = self.sb.recs[i];
            if r.len() > mss {
                if !can_insert {
                    return None;
                }
                self.sb.split(i, r.start + mss);
            }
            let r = self.sb.recs[i];
            let data_len = r.end.min(fin_off) - r.start.min(fin_off);
            let seg = data_len.max(1);
            if seg <= room || (self.recovery.is_none() && self.pipe() == 0) {
                let fin = r.end > fin_off;
                return Some(Plan {
                    kind: PlanKind::Rtx,
                    flags: ACK | if fin { FIN } else { 0 } | if data_len > 0 { PSH } else { 0 },
                    seq_off: r.start,
                    len: data_len as u32,
                    window: 0,
                    rec_idx: i,
                });
            }
            self.set_limit(now, LIM_CWND);
            return None;
        }
        if !can_insert {
            return None;
        }

        let unsent = self.unsent();
        let wnd_right = self.snd_una + self.snd_wnd;

        // 2. Tail loss probe.
        if self.tlp.pending {
            let wnd_room = wnd_right.saturating_sub(self.snd_nxt);
            if unsent > 0 && wnd_room > 0 {
                let len = unsent.min(mss).min(wnd_room);
                let fin = self.snd_nxt + len == fin_off;
                return Some(Plan {
                    kind: PlanKind::TlpProbe,
                    flags: ACK | PSH | if fin { FIN } else { 0 },
                    seq_off: self.snd_nxt,
                    len: len as u32,
                    window: 0,
                    rec_idx: usize::MAX,
                });
            }
            if let Some(last) = self.sb.recs.len().checked_sub(1) {
                let r = self.sb.recs[last];
                if r.len() > mss {
                    self.sb.split(last, r.end - mss);
                }
                let i = self.sb.recs.len() - 1;
                let r = self.sb.recs[i];
                let data_len = r.end.min(fin_off) - r.start.min(fin_off);
                let fin = r.end > fin_off;
                return Some(Plan {
                    kind: PlanKind::TlpProbe,
                    flags: ACK | if fin { FIN } else { 0 } | if data_len > 0 { PSH } else { 0 },
                    seq_off: r.start,
                    len: data_len as u32,
                    window: 0,
                    rec_idx: i,
                });
            }
            self.tlp.pending = false;
        }

        // 3. New data.
        if unsent > 0 {
            let wnd_room = wnd_right.saturating_sub(self.snd_nxt);
            let len = unsent.min(mss).min(wnd_room);
            if len == 0 {
                self.set_limit(now, LIM_RWND);
                return None;
            }
            // Sender-side SWS avoidance: don't send a small segment just because the
            // window is small, unless it's all we have.
            if len < mss && len < unsent && len < self.max_snd_wnd / 2 && self.pipe() > 0 {
                self.set_limit(now, LIM_RWND);
                return None;
            }
            if len > room {
                self.set_limit(now, LIM_CWND);
                self.cwnd_limited_now = true;
                return None;
            }
            let fin = self.snd_nxt + len == fin_off;
            // PSH marks the end of what the application has written so far.
            let psh = len == unsent;
            return Some(Plan {
                kind: PlanKind::New,
                flags: ACK | if psh { PSH } else { 0 } | if fin { FIN } else { 0 },
                seq_off: self.snd_nxt,
                len: len as u32,
                window: 0,
                rec_idx: usize::MAX,
            });
        }
        if self.fin_pending() && self.snd_nxt == fin_off {
            return Some(Plan { kind: PlanKind::New, flags: ACK | FIN, seq_off: fin_off, len: 0, window: 0, rec_idx: usize::MAX });
        }
        if self.state == State::Established || self.state == State::CloseWait {
            self.mark_app_limited();
        }
        self.set_limit(now, LIM_NONE);
        None
    }

    /// Serialize the planned segment. Returns header length and payload slices.
    pub(crate) fn build(&mut self, p: &Plan, now: Instant, hdr: &mut [u8; MAX_HEADER], ttl: u8) -> (usize, [&[u8]; 2]) {
        let mut o = EmitOptions::default();
        let is_syn = p.flags & SYN != 0;
        if is_syn {
            o.mss = Some(self.adv_mss);
            let offer_ws = self.state == State::SynSent || self.ws_ok;
            if offer_ws && self.rcv_wscale_cfg_offer() {
                o.wscale = Some(self.rcv_wscale);
            }
            o.sack_perm = self.state == State::SynSent || self.sack_ok;
            if self.state == State::SynSent {
                o.ts = Some((self.ts_val(now), 0));
            } else if self.ts_ok {
                o.ts = Some((self.ts_val(now), self.ts_recent));
            }
        } else if p.kind != PlanKind::Rst {
            if self.ts_ok {
                o.ts = Some((self.ts_val(now), self.ts_recent));
            }
            if self.sack_ok && p.flags & ACK != 0 {
                let max = wire::max_sack_blocks(self.ts_ok);
                let mut n = 0usize;
                if let Some((l, r)) = self.dsack {
                    o.sack[0] = (self.rx_sp.seq(l), self.rx_sp.seq(r));
                    n = 1;
                }
                if let Some(ooo) = self.ooo.as_ref() {
                    let mut blocks = std::mem::take(&mut self.sack_scratch);
                    ooo.sack_blocks(max - n, &mut blocks);
                    for &(l, r) in &blocks {
                        // An out-of-order FIN is part of the block it ends (as Linux does).
                        let r = if self.fin_rcvd == Some(r) { r + 1 } else { r };
                        o.sack[n] = (self.rx_sp.seq(l), self.rx_sp.seq(r));
                        n += 1;
                    }
                    self.sack_scratch = blocks;
                }
                o.sack_n = n as u8;
            }
        }
        let payload: [&[u8]; 2] = if p.len > 0 {
            let off = (p.seq_off - self.tx_base) as usize;
            self.tx.slices(off, p.len as usize)
        } else {
            [&[], &[]]
        };
        let ack = if p.flags & ACK != 0 { self.rx_sp.seq(self.rcv_nxt) } else { Seq(0) };
        let n = wire::emit(
            hdr,
            &EmitParams {
                src: self.local.ip(),
                dst: self.remote.ip(),
                src_port: self.local.port(),
                dst_port: self.remote.port(),
                seq: self.tx_sp.seq(p.seq_off),
                ack,
                flags: p.flags,
                window: p.window,
                opts: &o,
                payload: &payload,
                ttl,
            },
        );
        (n, payload)
    }

    fn rcv_wscale_cfg_offer(&self) -> bool {
        self.rcv_wscale > 0 || self.ws_ok
    }

    /// The sink accepted the planned segment.
    pub(crate) fn commit(&mut self, p: &Plan, now: Instant, cfg: &StackConfig) {
        self.stats.segs_out += 1;
        if p.flags & ACK != 0 && p.flags & SYN == 0 {
            self.last_ack_sent = self.rcv_nxt;
            self.ack_need = AckNeed::None;
            self.delack_at = None;
            self.unacked_bytes = 0;
            self.dsack = None;
            let right = self.rcv_nxt + ((p.window as u64) << self.rcv_wscale);
            self.rcv_adv = self.rcv_adv.max(right);
        }
        match p.kind {
            PlanKind::Syn | PlanKind::SynAck => {
                self.syn_pending = false;
                self.synack_ts = now;
                self.synack_tries += 1;
                self.snd_nxt = 1;
                self.snd_max = 1;
                if p.kind == PlanKind::SynAck {
                    self.last_ack_sent = self.rcv_nxt;
                    self.rcv_adv = self.rcv_adv.max(self.rcv_nxt + p.window as u64);
                    self.ack_need = AckNeed::None;
                }
                let backoff = (self.synack_tries - 1).min(6);
                self.timer = Some((TimerKind::SynAck, now + cfg.init_rto * (1 << backoff)));
            }
            PlanKind::New => self.commit_new(p, now),
            PlanKind::Rtx => self.commit_rtx(p, now),
            PlanKind::TlpProbe => {
                self.stats.tlp_count += 1;
                self.tlp.pending = false;
                if p.rec_idx == usize::MAX {
                    self.commit_new(p, now);
                    self.tlp.is_retrans = false;
                } else {
                    let orig = self.sb.recs[p.rec_idx].xmit;
                    self.tlp.lone_probe = (self.snd_max - self.snd_una <= self.mss as u64).then_some((self.snd_max, orig, now));
                    self.commit_rtx(p, now);
                    self.tlp.is_retrans = true;
                }
                self.tlp.end = Some(self.snd_max);
                self.tlp.probed_una = Some(self.snd_una);
                self.rearm_rtx_timer(now);
            }
            PlanKind::Probe => {
                self.probe_pending = false;
                self.stats.zero_window_probes += 1;
                self.stall_epoch += 1;
            }
            PlanKind::Rst => {
                self.rst_pending = false;
            }
            PlanKind::Ack => {}
        }
    }

    fn snapshot_rec(&self, now: Instant) -> Rec {
        Rec {
            start: 0,
            end: 0,
            xmit: now,
            delivered: self.dl.delivered,
            delivered_ts: self.dl.delivered_ts,
            first_tx_ts: self.dl.first_tx_ts,
            tx_in_flight: self.pipe(),
            lost_at_send: self.dl.lost_total,
            flags: 0,
            app_limited: self.dl.app_limited != 0,
        }
    }

    fn commit_new(&mut self, p: &Plan, now: Instant) {
        let fin = p.flags & FIN != 0;
        let end = p.seq_off + p.len as u64 + fin as u64;
        let idle = self.snd_una == self.snd_max;
        if self.pipe() == 0 {
            // Draft-cheng: restart the delivery-rate interval when nothing is in flight.
            self.dl.first_tx_ts = now;
            self.dl.delivered_ts = now;
        }
        let mut r = self.snapshot_rec(now);
        r.start = p.seq_off;
        r.end = end;
        r.tx_in_flight += end - p.seq_off;
        if fin {
            r.flags |= F_FIN;
        }
        self.sb.push(r);
        self.snd_nxt = end;
        self.snd_max = self.snd_max.max(end);
        self.stats.bytes_sent += p.len as u64;
        if (p.len as u64) < self.mss as u64 && p.len > 0 {
            self.stats.small_segs += 1;
        }
        if let Some(rec) = self.recovery.as_mut() {
            rec.prr_out += end - p.seq_off;
            rec.quota = rec.quota.saturating_sub(end - p.seq_off);
        }
        if idle {
            self.rto_base = now;
            self.progress_ts = now;
            self.rearm_rtx_timer(now);
        } else if self.timer.is_none() {
            self.rearm_rtx_timer(now);
        } else if matches!(self.timer, Some((TimerKind::Tlp, _))) {
            // Push the probe timeout past this transmission.
            self.rearm_rtx_timer(now);
        }
    }

    fn commit_rtx(&mut self, p: &Plan, now: Instant) {
        let i = p.rec_idx;
        let r = self.sb.recs[i];
        let snap = self.snapshot_rec(now);
        self.sb.mark_retransmitted(i, now);
        {
            let m = &mut self.sb.recs[i];
            m.delivered = snap.delivered;
            m.delivered_ts = snap.delivered_ts;
            m.first_tx_ts = snap.first_tx_ts;
            m.tx_in_flight = snap.tx_in_flight + r.len();
            m.lost_at_send = snap.lost_at_send;
            m.app_limited = snap.app_limited;
        }
        self.stats.bytes_retrans += p.len as u64;
        if let Some(rec) = self.recovery.as_mut() {
            rec.prr_out += r.len();
            rec.quota = rec.quota.saturating_sub(r.len());
        }
        if self.rto_tsval_pending {
            self.rto_tsval_pending = false;
            if self.ts_ok {
                self.rto_tsval = Some(self.ts_val(now));
            }
        }
        if self.timer.is_none() {
            self.rearm_rtx_timer(now);
        }
    }

    /// Account paced transmission (EDT, §7.2).
    pub(crate) fn on_paced_send(&mut self, now: Instant, bytes: usize, rate: u64, credit: Duration) {
        let floor = now - credit;
        let start = if self.next_send_time < floor { floor } else { self.next_send_time };
        let dt = Duration::from_nanos((bytes as u128 * 1_000_000_000 / rate.max(1) as u128) as u64);
        self.next_send_time = start + dt;
    }

    fn pto(&self) -> Duration {
        let srtt = self.rtt.srtt_or(Duration::from_millis(100));
        let mut pto = (srtt * 2).max(Duration::from_millis(10));
        if self.snd_max - self.snd_una <= self.mss as u64 {
            // One segment in flight: its ACK may be held by the peer's delayed-ACK
            // timer. RFC 8985 §7.2 allows for the worst case (200 ms), which makes a
            // lost lone segment (the typical reply of a request/response exchange)
            // wait for the 200 ms RTO. Once the peer's actual delay is measured, allow
            // that plus a margin instead; never more than the worst case.
            pto = match self.tlp.lone_ack_delay {
                Some(d) => pto.max(srtt + (d * 5 / 4 + Duration::from_millis(2)).min(WC_DEL_ACK)),
                None => pto.max(srtt * 3 / 2 + WC_DEL_ACK),
            };
        }
        pto
    }

    fn rto_deadline(&self) -> Instant {
        let rto = (self.rtt.rto * (1u32 << self.rto_backoff.min(16))).min(self.rtt.max_rto());
        self.rto_base + rto
    }

    fn rearm_rtx_timer(&mut self, now: Instant) {
        if matches!(self.state, State::SynSent | State::SynReceived | State::Closed | State::TimeWait) {
            return;
        }
        if self.snd_una < self.snd_max {
            let rto_at = self.rto_deadline();
            if let Some(t) = self.rack_timeout {
                if t < rto_at {
                    self.timer = Some((TimerKind::Reorder, t));
                    return;
                }
            }
            let tlp_ok = self.sack_ok
                && self.recovery.is_none()
                && self.rto_recovery.is_none()
                && self.tlp.end.is_none()
                && !self.tlp.pending
                && self.tlp.probed_una != Some(self.snd_una)
                && self.rto_backoff == 0
                && self.rtt.has_sample;
            if tlp_ok {
                let t = (now + self.pto()).min(rto_at);
                self.timer = Some((TimerKind::Tlp, t));
            } else {
                self.timer = Some((TimerKind::Rto, rto_at));
            }
        } else if self.snd_wnd == 0 && (self.unsent() > 0 || self.fin_pending()) {
            if !matches!(self.timer, Some((TimerKind::Persist, _))) {
                let d = (self.rtt.rto * (1u32 << self.persist_backoff.min(10))).min(self.rtt.max_rto());
                self.timer = Some((TimerKind::Persist, now + d));
            }
        } else {
            self.timer = None;
        }
    }

    /// Fire expired timers. Returns true if the connection may have something to send.
    pub(crate) fn on_timer(&mut self, now: Instant, ctx: &mut Ctx) -> bool {
        let mut wake = false;
        if self.delack_at.is_some_and(|t| t <= now) {
            self.delack_at = None;
            if self.ack_need != AckNeed::None {
                self.ack_need = AckNeed::Now;
                wake = true;
            }
        }
        if self.life_at.is_some_and(|t| t <= now) {
            self.life_at = None;
            match self.state {
                State::TimeWait => {
                    self.set_closed(CloseReason::Normal, ctx);
                    return false;
                }
                _ if self.app_closed => {
                    // Orphan timeout.
                    self.rst_pending = true;
                    self.set_closed(CloseReason::Timeout, ctx);
                    return true;
                }
                _ => {}
            }
        }
        if self.keepalive_at.is_some_and(|t| t <= now) {
            self.keepalive_at = None;
            if self.state.synchronized() && self.state != State::TimeWait {
                if self.snd_una == self.snd_max {
                    if self.keepalive_sent >= ctx.cfg.keepalive_probes {
                        self.rst_pending = true;
                        self.set_closed(CloseReason::Timeout, ctx);
                        return true;
                    }
                    self.keepalive_sent += 1;
                    self.stall_epoch += 1;
                    self.probe_pending = true;
                    self.keepalive_at = Some(now + ctx.cfg.keepalive_interval);
                    wake = true;
                } else {
                    self.keepalive_at = Some(now + ctx.cfg.keepalive_idle);
                }
            }
        }
        if let Some((kind, at)) = self.timer {
            if at <= now {
                self.timer = None;
                match kind {
                    TimerKind::SynAck => {
                        if self.synack_tries > ctx.cfg.synack_retries {
                            self.set_closed(CloseReason::Timeout, ctx);
                            return false;
                        }
                        self.syn_pending = true;
                        wake = true;
                    }
                    TimerKind::Rto => {
                        if self.on_rto(now, ctx) {
                            wake = true;
                        } else {
                            return true;
                        }
                    }
                    TimerKind::Tlp => {
                        self.tlp.pending = true;
                        // RFC 8985 §7.3: the RTO is re-armed from the probe time, so the
                        // probe gets a full RTO before loss recovery by timeout.
                        self.rto_base = now;
                        self.timer = Some((TimerKind::Rto, self.rto_deadline()));
                        wake = true;
                    }
                    TimerKind::Reorder => {
                        self.rack_timeout = None;
                        let prior = self.pipe();
                        let lost = self.rack_detect(now);
                        if lost > 0 {
                            self.maybe_enter_recovery(now, prior);
                            wake = true;
                        }
                        self.rearm_rtx_timer(now);
                    }
                    TimerKind::Persist => {
                        self.persist_backoff += 1;
                        self.probe_pending = true;
                        self.rearm_rtx_timer(now);
                        wake = true;
                    }
                }
            }
        }
        wake
    }

    /// Retransmission timeout. Returns false if the connection was closed.
    fn on_rto(&mut self, now: Instant, ctx: &mut Ctx) -> bool {
        if self.snd_una >= self.snd_max {
            self.rearm_rtx_timer(now);
            return true;
        }
        if now.saturating_since(self.progress_ts) >= ctx.cfg.user_timeout || self.rto_backoff >= 15 {
            self.rst_pending = true;
            self.set_closed(CloseReason::Timeout, ctx);
            return false;
        }
        self.stats.rto_count += 1;
        self.stall_epoch += 1;
        // First RTO keeps SACK information; a repeated RTO for the same data suggests
        // reneging (or forged SACKs), so forget it (RFC 2018 §8).
        let newly = if self.rto_backoff >= 1 { self.sb.renege_all() } else { self.sb.mark_all_lost() };
        self.dl.lost_total += newly;
        // Repeated timeouts for the same data do not reduce ssthresh again (RFC 5681).
        if self.rto_backoff == 0 {
            self.cc.on_rto(now, self.mss);
        }
        self.recovery = None;
        self.rto_recovery = Some(self.snd_max);
        self.rto_backoff += 1;
        self.rto_tsval_pending = true;
        self.rto_tsval = None;
        // The peer's measured ACK delay survives the reset of the probe state.
        self.tlp = Tlp { lone_ack_delay: self.tlp.lone_ack_delay, ..Tlp::default() };
        self.rack_timeout = None;
        self.timer = Some((TimerKind::Rto, self.rto_deadline()));
        // Keep rto_base: the deadline doubles relative to the last progress.
        self.rto_base = now;
        self.timer = Some((TimerKind::Rto, self.rto_deadline()));
        true
    }

    fn set_limit(&mut self, now: Instant, kind: u8) {
        if kind == self.limit_state {
            return;
        }
        let d = now.saturating_since(self.limit_since).as_nanos() as u64;
        match self.limit_state {
            LIM_RWND => self.stats.limited_rwnd_ns += d,
            LIM_CWND => self.stats.limited_cwnd_ns += d,
            LIM_PACING => self.stats.limited_pacing_ns += d,
            LIM_EGRESS => self.stats.limited_egress_ns += d,
            _ => {}
        }
        self.limit_state = kind;
        self.limit_since = now;
    }

    pub(crate) fn note_pacing_limited(&mut self, now: Instant) {
        self.set_limit(now, LIM_PACING);
    }
    pub(crate) fn note_egress_limited(&mut self, now: Instant) {
        self.set_limit(now, LIM_EGRESS);
    }

    pub fn info(&self) -> crate::ConnInfo {
        crate::ConnInfo {
            state: self.state,
            iface: self.iface,
            peer: self.peer,
            local: self.local,
            remote: self.remote,
            srtt: self.rtt.srtt_or(Duration::ZERO),
            rttvar: self.rtt.rttvar,
            min_rtt: self.rtt.min_rtt_or(Duration::ZERO),
            rto: self.rtt.rto,
            cwnd: self.cc.cwnd(),
            ssthresh: self.cc.ssthresh(),
            pipe: self.pipe(),
            pacing_rate: self.pacing_rate(),
            mss: self.mss,
            snd_wnd: self.snd_wnd,
            rcv_wnd: self.rcv_adv.saturating_sub(self.rcv_nxt),
            rcv_target: self.rcv_target,
            tx_queued: self.tx.len(),
            tx_unsent: self.unsent(),
            rx_queued: self.rx.len(),
            ooo_bytes: self.ooo.as_ref().map_or(0, |o| o.bytes()),
            sacked: self.sb.sacked,
            lost: self.sb.lost,
            retrans_out: self.sb.retrans_out,
            in_recovery: self.recovery.is_some(),
            want_write: self.want_write,
            delivered: self.dl.delivered,
            cc: self.cc.name(),
            cc_debug: self.cc.debug(),
            stats: self.stats.clone(),
        }
    }

    #[cfg(any(test, feature = "test-peer"))]
    pub(crate) fn check_invariants(&self) {
        self.sb.check();
        assert!(self.snd_una <= self.snd_nxt && self.snd_nxt <= self.snd_max);
        assert!(self.rcv_adv >= self.rcv_nxt || self.state == State::SynReceived || self.state == State::Closed || self.state == State::SynSent);
        if let Some(f) = self.fin_off {
            assert!(self.tx_end() == f || self.state == State::Closed || self.state == State::TimeWait);
        }
    }
}

impl Conn {
    /// A data plan may split one retransmission record or commit one new
    /// segment. Pure control output needs no send-record allocation.
    pub(crate) fn needs_record_spare(&self) -> bool {
        !self.rst_pending && self.state.can_send_data() && (self.sb.lost_pending() > 0 || self.unsent() > 0 || self.fin_pending() || self.tlp.pending)
    }

    /// An ACK can free a record slot without releasing its backing allocation.
    /// A memory-blocked sender may resume immediately in that case.
    pub(crate) fn has_record_spare(&self) -> bool {
        self.sb.recs.len() < self.sb.recs.capacity()
    }

    pub(crate) fn record_reserve_bytes_needed(&self) -> Option<u64> {
        self.sb.reserve_bytes_needed()
    }

    pub(crate) fn try_reserve_send_record(&mut self, budget: &mut Budget) -> bool {
        self.sb.try_reserve_one(budget, self.peer)
    }

    /// Lower the effective MSS after an MTU decrease (§10.4). Retransmissions are
    /// re-split to the new MSS when planned.
    pub(crate) fn clamp_mtu(&mut self, mtu: u16) {
        let hdr = wire::ip_header_len(self.local.ip()) as u32 + 20 + if self.ts_ok { 12 } else { 0 };
        let m = (mtu as u32).saturating_sub(hdr).max(64);
        if m < self.mss {
            self.mss = m;
        }
        self.adv_mss = self.adv_mss.min((mtu as u32).saturating_sub(hdr - if self.ts_ok { 12 } else { 0 }) as u16);
    }

    pub(crate) fn has_rst_pending(&self) -> bool {
        self.rst_pending
    }

    /// Control segments that are never paced (ACK, RST, SYN/SYN-ACK, probes).
    pub(crate) fn wants_unpaced_tx(&self) -> bool {
        self.rst_pending || self.syn_pending || self.ack_need == AckNeed::Now || self.probe_pending
    }

    /// Whether `plan` could produce a segment (used to avoid needless scheduling).
    pub(crate) fn wants_tx(&self) -> bool {
        self.rst_pending
            || self.syn_pending
            || self.ack_need == AckNeed::Now
            || self.probe_pending
            || self.tlp.pending
            || (self.state.can_send_data() && (self.sb.lost_pending() > 0 || self.unsent() > 0 || self.fin_pending()))
    }
}
