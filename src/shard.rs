//! Shard: single-threaded owner of a set of connections (§3, §8).
//!
//! Scheduling state per connection (§8.1): ready (per-peer DRR queues), pacing heap,
//! egress-blocked set of its iface, timer heap. Each round only touches connections
//! with events, so cost scales with active connections, not total connections.

use crate::budget::{AdmitDebt, Budget, ConnectionPermit, GlobalBudget, Level, MemoryHandle, MemoryLease, Pressure, RetainedMetadata};
use crate::buf::BlockPool;
use crate::config::StackConfig;
use crate::conn::{Conn, Ctx, IngressPayload, Plan, PlanKind, ReadResult, State, SynParams, TimeWaitState, WriteResult};
use crate::heap::IndexedHeap;
use crate::iface_pacing::IfacePacer;
use crate::pktpool::PacketBuf;
use crate::seq::Seq;
use crate::time::Instant;
use crate::wire::{self, EmitOptions, EmitParams, TcpHeader, ACK, FIN, MAX_HEADER, RST, SYN};
use crate::{CloseReason, ConnId, ConnInfo, Event, IfaceId, PeerId};
use bytes::Bytes;
use core::time::Duration;
use std::collections::hash_map::RandomState;
use std::collections::{HashMap, HashSet, VecDeque};
use std::hash::{BuildHasher, DefaultHasher, Hash, Hasher};
use std::net::SocketAddr;
use std::sync::Arc;

/// Keys for the shard's keyed hashes: ISN / timestamp secret, SYN cookie keys
/// and the waiter sets whose iteration order drives scheduling. Random per
/// instance in production; derived from the simulator seed under test so a run
/// replays exactly (zfstack#13) without weakening production secrets.
#[derive(Clone)]
enum Keys {
    Random(RandomState),
    Seeded(u64),
}

impl BuildHasher for Keys {
    type Hasher = DefaultHasher;
    fn build_hasher(&self) -> DefaultHasher {
        match self {
            Keys::Random(s) => s.build_hasher(),
            Keys::Seeded(k) => {
                let mut h = DefaultHasher::new();
                h.write_u64(*k);
                h
            }
        }
    }
}

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

/// What the host's device already established about an ingress packet's TCP
/// checksum.
#[derive(Copy, Clone, Debug, Default, PartialEq, Eq)]
pub enum RxChecksum {
    /// Verify it (the default for `ingress` / `ingress_borrowed`).
    #[default]
    Verify,
    /// Skip verification: the device validated the checksum, or the packet
    /// never left the host and carries a partial one (a Linux TUN with
    /// `IFF_VNET_HDR` + `TUN_F_CSUM` reports `VIRTIO_NET_HDR_F_NEEDS_CSUM` or
    /// `DATA_VALID`). Only for links that cannot corrupt packets; the IP
    /// header checksum is still checked.
    Trusted,
}

/// An outgoing IP packet: header plus up to two payload slices (§4.2).
pub struct OutPacket<'a> {
    pub peer: PeerId,
    pub header: &'a [u8],
    pub payload: [&'a [u8]; 2],
    /// The TCP checksum is left partial for the device (the iface has TX
    /// checksum offload, see [`Shard::set_iface_tx_checksum_offload`]): sum
    /// from the TCP header (`csum_start` = IP header length) and store the
    /// complement at offset 16. For a virtio-net header use
    /// [`crate::offload::VirtioNetHdr::for_packet`].
    pub csum_partial: bool,
    /// Non-zero: a TSO super-segment the device must cut into segments of
    /// this many payload bytes (see [`Shard::set_iface_tso`]).
    pub gso_size: u16,
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
    pub rx_dropped_peer_mismatch: u64,
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
    /// Aggregate iface pacing, distinct from per-connection CC pacing.
    pub egress_pacing_pauses: u64,
    pub egress_paced_bytes: u64,
    pub mtu_changes: u64,
    pub round_budget_exhausted: u64,
    /// Rounds that dropped idle TX blocks on request, and the bytes released.
    pub cache_reclaims: u64,
    pub cache_reclaimed_bytes: u64,
    /// Experimental coarse TX-pool maintenance, separate from pressure reclaim.
    pub tx_cache_maintenance_checks: u64,
    pub tx_cache_idle_trims: u64,
    pub tx_cache_idle_trimmed_blocks: u64,
    pub tx_cache_idle_trimmed_bytes: u64,
    /// New connections refused for memory that left an admission debt, and
    /// debts dropped because the peer did not retry in time.
    pub admission_debts: u64,
    pub admission_debts_expired: u64,
    /// Debts moved to another level, and debts lifted because the owed
    /// level stopped releasing TX blocks and send records.
    pub admission_debts_moved: u64,
    pub admission_debts_stalled: u64,
}

/// How long a refused connection keeps room owed for its next SYN
/// (docs/design/0006 §2). SYN retransmits back off exponentially, so the
/// wait for the next one is about the time since the first: hold twice that
/// plus slack, at least 4s (a 3s initial RTO) and at most 64s.
fn admission_hold(since_first: core::time::Duration) -> core::time::Duration {
    const MIN: core::time::Duration = core::time::Duration::from_secs(4);
    const MAX: core::time::Duration = core::time::Duration::from_secs(64);
    (since_first * 2 + core::time::Duration::from_secs(2)).clamp(MIN, MAX)
}

/// An owed level that releases no TX block or send record for this long
/// cannot make room by holding refills back (docs/design/0006 §3): its
/// holders wait on a zero window or a stalled host. The debt is lifted.
const ADMISSION_DEBT_STALL: core::time::Duration = core::time::Duration::from_secs(1);

/// A connection refused for memory, whose retransmitted SYN the refusing
/// level keeps room for.
struct PendingAdmission {
    key: (IfaceId, SocketAddr, SocketAddr),
    first: Instant,
    until: Instant,
    /// The level that refused the latest attempt, and its debt. `None`
    /// after the level stopped releasing; the next refusal owes again.
    debt: Option<(Level, AdmitDebt)>,
    released: u64,
    progress_at: Instant,
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
    /// Drained per-peer queues kept for reuse. A peer whose ready queue is
    /// fully served currently frees its `VecDeque` backing and the next ready
    /// burst re-allocates it. Retaining bounded, small backings avoids that
    /// malloc/free on every burst without letting stale peers or oversized
    /// queues accumulate: only empty queues up to `DRR_SPARE_CAP` capacity are
    /// kept, and the pool itself is capped at `DRR_SPARE_Q` entries.
    spare_q: Vec<VecDeque<(u32, u32)>>,
}

/// Hard cap on retained empty per-peer DRR queues (one iface-local pool).
const DRR_SPARE_Q: usize = 64;
/// Only retain queues whose backing is small; larger ones are dropped so a
/// past burst cannot pin a big allocation.
const DRR_SPARE_CAP: usize = 16;

impl Drr {
    fn push(&mut self, peer: PeerId, e: (u32, u32)) {
        match self.peer_q.entry(peer) {
            std::collections::hash_map::Entry::Occupied(mut o) => {
                if o.get().is_empty() {
                    self.ready_peers.push_back(peer);
                }
                o.get_mut().push_back(e);
            }
            std::collections::hash_map::Entry::Vacant(v) => {
                let mut q = self.spare_q.pop().unwrap_or_default();
                q.clear();
                self.ready_peers.push_back(peer);
                v.insert(q).push_back(e);
            }
        }
    }
    /// Recycle a drained (empty) per-peer queue instead of dropping its
    /// backing on the floor. Oversized backings are released as before.
    fn recycle(&mut self, peer: PeerId) {
        if let Some(q) = self.peer_q.remove(&peer) {
            debug_assert!(q.is_empty(), "only a drained DRR queue may be recycled");
            if self.spare_q.len() < DRR_SPARE_Q && q.capacity() <= DRR_SPARE_CAP {
                self.spare_q.push(q);
            }
        }
    }
    fn is_empty(&self) -> bool {
        self.ready_peers.is_empty()
    }
}

struct Iface {
    cfg: IfaceConfig,
    /// Data segments leave with a partial checksum (`OutPacket::csum_partial`).
    tx_csum_offload: bool,
    /// Largest TSO super-segment payload; 0 = off.
    tso_max: u32,
    full: bool,
    pacer: Option<IfacePacer>,
    drr: Drr,
    tw_pending: usize,
}

#[derive(Default)]
struct Sched {
    in_ready: bool,
    /// Counted in `half_open` / the TIME_WAIT table (reconciled in `sync_counts`).
    half_open: bool,
    time_wait: bool,
}

struct Slot {
    gen: u32,
    conn: Option<Box<Conn>>,
    tomb: Option<TimeWaitState>,
    permit: Option<ConnectionPermit>,
    active_memory: Option<MemoryLease>,
    sched: Sched,
    retained: bool,
}

// Conn lives in a Box; the surplus covers its CC object and the initial
// tuple/timer/event descriptors. Growing containers need separate accounting.
pub(crate) const ACTIVE_STATE_BYTES: u64 = std::mem::size_of::<Conn>() as u64 + 1024;

type AdmittedMemory = (MemoryLease, Option<MemoryLease>, Option<Arc<crate::buf::RxProgress>>);

/// DRR/pacing quantum clamp(rate × 1 ms, 2 MSS, max_quantum). A jumbo MSS (a
/// 64 KiB TUN MTU) raises the ceiling to one segment so a quantum always fits one.
fn quantum_for(rate: u64, mss: usize, max_quantum: usize) -> usize {
    let hi = max_quantum.max(mss);
    ((rate / 1000) as usize).clamp((2 * mss).min(hi), hi)
}

/// Pacing granularity: one quantum worth of time.
fn pacing_ahead(rate: u64, mss: usize, max_quantum: usize) -> Duration {
    let pq = quantum_for(rate, mss, max_quantum);
    Duration::from_nanos((pq as u128 * 1_000_000_000 / rate.max(1) as u128).min(u64::MAX as u128) as u64)
}

enum CookieResult {
    Established(u32),
    NoResources,
    Invalid,
}

struct Stateless {
    iface: IfaceId,
    peer: PeerId,
    header: [u8; MAX_HEADER],
    len: usize,
    _memory: MemoryLease,
}

// Charge the inline packet and conservative VecDeque growth slack while it is
// queued. The queue is bounded and sparse backing is reclaimed after bursts.
const STATELESS_CHARGE: u64 = (std::mem::size_of::<Stateless>() * 4) as u64;

/// Wake-interval tracker for pacing credit (§7.2).
struct WakeStats {
    last: Option<Instant>,
    samples: [u32; 128],
    n: usize,
    credit: Duration,
}

/// Experimental TX-pool idle trim. No policy is installed by default.
/// The host must honor Shard::next_deadline(), including during complete idle.
/// Each warm window retires unused free-list prefixes above the warm floors,
/// preserving recently reused blocks even during low-rate traffic. Large and
/// small classes have independent cold clocks; complete class idle releases
/// its remaining cache. No per-block clocks or connection scans are needed.
#[derive(Clone, Copy, Debug)]
pub struct TxCacheTrimPolicy {
    pub check_interval: Duration,
    /// Observation window for unused free-list prefixes. Zero checks every tick.
    /// Blocks returned during a window can age through the following window.
    pub warm_after: Duration,
    /// Complete-idle delay for each size class, sampled at check_interval.
    pub cold_after: Duration,
    /// Warm retention floors, not hard caps on recently reused blocks.
    pub warm_large: usize,
    pub warm_small: usize,
}

#[derive(Clone, Copy)]
struct TxCacheIdle {
    next_check: Instant,
    quiet_since: [Instant; 2],
    warm_since: Instant,
}

pub struct Shard {
    cfg: StackConfig,
    ifaces: Vec<Option<Iface>>,
    slots: Vec<Slot>,
    /// One generation per index ever allocated. Keep only these compact values
    /// when an idle shard releases its much larger slot/table high-water mark.
    retired_generations: Vec<u32>,
    free: Vec<u32>,
    table: HashMap<Key, u32>,
    timers: IndexedHeap,
    pacing: IndexedHeap,
    /// Round-robin start index over ifaces.
    iface_rr: usize,
    events: VecDeque<Event>,
    pool: BlockPool,
    budget: Budget,
    /// Connections waiting for a send-record backing allocation. A release
    /// epoch change retries each once; ingress ACKs can also reschedule them.
    budget_blocked: HashSet<(u32, u32), Keys>,
    budget_wait_epoch: Option<u64>,
    /// Share-bound application writers, keyed by slot generation to prevent
    /// a released slot from waking a replacement connection.
    share_blocked: HashSet<(u32, u32), Keys>,
    share_wait_epoch: Option<u64>,
    share_seen_epoch: Option<u64>,
    /// Last cache reclaim request this shard has served.
    cache_reclaim_seen: u64,
    tx_cache_trim: Option<TxCacheTrimPolicy>,
    tx_cache_idle: Option<TxCacheIdle>,
    /// Bytes a host adapter needs per accepted stream, reserved at SYN time.
    stream_state_bytes: u64,
    /// At most `ADMIT_BURST` refused connections, oldest first.
    pending_admissions: VecDeque<PendingAdmission>,
    admission: Box<dyn AdmissionPolicy>,
    stateless: VecDeque<Stateless>,
    hasher: Keys,
    cookie_key: [Keys; 2],
    /// Key stream of a seeded (simulator) shard; `None` draws random keys.
    key_seed: Option<u64>,
    cookie_gen: u64,
    half_open: usize,
    time_wait_n: usize,
    tw_ready: VecDeque<(u32, u32)>,
    wake: WakeStats,
    hdr: [u8; MAX_HEADER],
    stats: ShardStats,
    /// Must drop after slot/index containers so retained capacity stays charged
    /// through their deallocation.
    retained_metadata: RetainedMetadata,
}

const COOKIE_PERIOD_S: u64 = 60;
const MSS_TABLE: [u16; 8] = [216, 536, 1024, 1220, 1340, 1360, 1400, 1460];
const STATELESS_MAX: usize = 1024;
const IDLE_SLOT_RECLAIM_THRESHOLD: usize = 2048;

impl Shard {
    pub fn new(cfg: StackConfig) -> Self {
        Self::with_budget(cfg, GlobalBudget::from_system())
    }

    pub fn with_budget(cfg: StackConfig, global: Arc<GlobalBudget>) -> Self {
        let credit = cfg.pacing_credit_min;
        let retained_metadata = RetainedMetadata::new(Arc::clone(&global));
        let cache_reclaim_seen = global.cache_reclaim_epoch();
        Shard {
            cfg,
            ifaces: Vec::new(),
            slots: Vec::new(),
            retired_generations: Vec::new(),
            free: Vec::new(),
            table: HashMap::new(),
            timers: IndexedHeap::default(),
            pacing: IndexedHeap::default(),
            iface_rr: 0,
            events: VecDeque::new(),
            // Idle TX blocks are per shard. Bound the retained cache to 1 MiB
            // so adding WG ports does not strand 16 MiB per idle port.
            pool: BlockPool::new(16),
            budget: Budget::new(global),
            budget_blocked: HashSet::with_hasher(Keys::Random(RandomState::new())),
            budget_wait_epoch: None,
            share_blocked: HashSet::with_hasher(Keys::Random(RandomState::new())),
            share_wait_epoch: None,
            share_seen_epoch: None,
            cache_reclaim_seen,
            tx_cache_trim: None,
            tx_cache_idle: None,
            stream_state_bytes: 0,
            pending_admissions: VecDeque::new(),
            admission: Box::new(AcceptAll),
            stateless: VecDeque::new(),
            hasher: Keys::Random(RandomState::new()),
            cookie_key: [Keys::Random(RandomState::new()), Keys::Random(RandomState::new())],
            key_seed: None,
            cookie_gen: 0,
            half_open: 0,
            time_wait_n: 0,
            tw_ready: VecDeque::new(),
            wake: WakeStats { last: None, samples: [0; 128], n: 0, credit },
            hdr: [0; MAX_HEADER],
            stats: ShardStats::default(),
            retained_metadata,
        }
    }

    /// A shard whose keyed hashes (ISN, timestamp offsets, SYN cookies, waiter
    /// order) all derive from `seed`: identical inputs replay identically.
    #[cfg(any(test, feature = "test-peer"))]
    pub fn with_budget_seeded(cfg: StackConfig, global: Arc<GlobalBudget>, seed: u64) -> Self {
        let mut s = Self::with_budget(cfg, global);
        s.key_seed = Some(seed);
        s.hasher = s.next_keys();
        s.cookie_key = [s.next_keys(), s.next_keys()];
        s.budget_blocked = HashSet::with_hasher(s.next_keys());
        s.share_blocked = HashSet::with_hasher(s.next_keys());
        s
    }

    fn next_keys(&mut self) -> Keys {
        match self.key_seed.as_mut() {
            None => Keys::Random(RandomState::new()),
            Some(state) => {
                // splitmix64
                *state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
                let mut z = *state;
                z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
                z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
                Keys::Seeded(z ^ (z >> 31))
            }
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
    #[cfg(test)]
    pub(crate) fn budget_mut_for_test(&mut self) -> &mut Budget {
        &mut self.budget
    }
    /// Epoch to register with a caller's budget-release waker, if any core
    /// senders need an allocation or an application writer awaits more share.
    pub fn budget_wait_epoch(&self) -> Option<u64> {
        match (self.budget_wait_epoch, self.share_wait_epoch) {
            (Some(a), Some(b)) => Some(a.min(b)),
            (a, b) => a.or(b),
        }
    }
    /// Epoch for physical send-record allocations only. The Tokio driver runs
    /// `Shard::run` after every local sender change, so it only needs the
    /// cross-thread release waker when a connection is blocked on `SendRecord`.
    #[cfg(feature = "tokio")]
    pub(crate) fn record_wait_epoch(&self) -> Option<u64> {
        self.budget_wait_epoch
    }
    pub fn memory_handle(&mut self, peer: PeerId) -> MemoryHandle {
        self.budget.memory_handle(peer)
    }
    pub fn set_budget_limits(&mut self, port_bytes: u64, peer_bytes: u64, peer_max_conns: u32) {
        self.budget.set_limits(port_bytes, peer_bytes, peer_max_conns);
    }
    pub fn pacing_credit(&self) -> Duration {
        self.wake.credit
    }
    pub fn set_admission(&mut self, p: Box<dyn AdmissionPolicy>) {
        self.admission = p;
    }
    /// Half-open (SYN-RECEIVED) connections and TIME_WAIT entries.
    pub fn half_open_and_time_wait(&self) -> (usize, usize) {
        (self.half_open, self.time_wait_n)
    }
    pub fn conn_count(&self) -> usize {
        self.table.len()
    }

    pub fn add_iface(&mut self, cfg: IfaceConfig) -> IfaceId {
        self.ensure_rx_admit_reserve(cfg.mtu);
        let id = self.ifaces.len();
        self.ifaces.push(Some(Iface { cfg, tx_csum_offload: false, tso_max: 0, full: false, pacer: None, drr: Drr::default(), tw_pending: 0 }));
        IfaceId(id as u16)
    }

    /// Path MTU decreased (EMSGSIZE) or configured MTU changed (§10.4).
    pub fn set_iface_mtu(&mut self, id: IfaceId, mtu: u16) {
        let Some(Some(ifc)) = self.ifaces.get_mut(id.0 as usize) else { return };
        ifc.cfg.mtu = mtu;
        if let Some(pacer) = ifc.pacer.as_mut() {
            pacer.set_mtu(mtu);
        }
        self.ensure_rx_admit_reserve(mtu);
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
        self.stateless.retain(|reply| reply.iface != id);
        self.shrink_stateless_queue();
        let mut idxs: Vec<u32> = self.table.iter().filter(|(k, _)| k.0 == id).map(|(_, &v)| v).collect();
        // Close events in slot order, not hash order (replayable).
        idxs.sort_unstable();
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
        let ready = self.ifaces.iter().flatten().any(|i| !i.full && !i.drr.is_empty() && i.pacer.as_ref().and_then(IfacePacer::deadline).is_none());
        if ready
            || (!self.stateless.is_empty() && self.ifaces.iter().flatten().any(|i| !i.full))
            || self.ifaces.iter().flatten().any(|i| !i.full && i.tw_pending > 0)
        {
            return Some(Instant::ZERO);
        }
        let a = self.timers.peek().map(|x| x.0);
        let b = self.pacing.peek().map(|x| x.0);
        let c = self.pending_admissions.iter().map(|p| if p.debt.is_some() { p.until.min(p.progress_at + ADMISSION_DEBT_STALL) } else { p.until }).min();
        let maintenance = self.tx_cache_trim.and_then(|_| self.pool.has_retained().then(|| self.tx_cache_idle.map_or(Instant::ZERO, |idle| idle.next_check)));
        let iface_pacing = self.ifaces.iter().flatten().filter(|i| !i.full && !i.drr.is_empty()).filter_map(|i| i.pacer.as_ref()?.deadline()).min();
        [a, b, c, maintenance, iface_pacing].into_iter().flatten().min()
    }

    /// Pace an iface's aggregate output when different peers contend. The host
    /// supplies its known bottleneck capacity in IP bytes/second; zero disables
    /// the policy. Packets remain in the existing peer/connection DRR queues.
    /// A single peer follows the original path. Contention is remembered for
    /// one second between bursts; 2-ms service batches amortize timer wakeups.
    /// Honor `next_deadline()` even without ingress. This controls a downstream
    /// FIFO, not an upstream provider queue, and does not estimate link capacity.
    pub fn set_iface_egress_rate(&mut self, id: IfaceId, bytes_per_second: u64) {
        if let Some(Some(iface)) = self.ifaces.get_mut(id.0 as usize) {
            if iface.pacer.as_ref().map_or(0, IfacePacer::rate) == bytes_per_second {
                return;
            }
            iface.pacer = (bytes_per_second != 0).then(|| IfacePacer::new(bytes_per_second, iface.cfg.mtu));
        }
    }

    // ------------------------------------------------------------------
    // Ingress

    /// Process one decrypted IP packet from `peer`.
    pub fn ingress(&mut self, now: Instant, iface: IfaceId, peer: PeerId, pkt: Bytes) {
        self.ingress_with(now, iface, peer, pkt, RxChecksum::Verify);
    }

    /// Process a packet borrowed from a caller-owned pool. The caller may
    /// recycle its buffer as soon as this returns; retained TCP data is copied
    /// into stack-owned storage only after the packet passes admission checks.
    pub fn ingress_borrowed(&mut self, now: Instant, iface: IfaceId, peer: PeerId, pkt: &[u8]) {
        self.ingress_borrowed_with(now, iface, peer, pkt, RxChecksum::Verify);
    }

    /// `ingress` with the device's checksum verdict (TUN / virtio-net offload,
    /// see [`crate::offload`]).
    pub fn ingress_with(&mut self, now: Instant, iface: IfaceId, peer: PeerId, pkt: Bytes, csum: RxChecksum) {
        self.ingress_owned(now, iface, peer, pkt, csum, false);
    }

    /// `ingress_borrowed` with the device's checksum verdict.
    pub fn ingress_borrowed_with(&mut self, now: Instant, iface: IfaceId, peer: PeerId, pkt: &[u8], csum: RxChecksum) {
        self.ingress_slice(now, iface, peer, pkt, csum);
    }

    /// Zero-copy ingress of a packet read into a pooled, budget-charged
    /// buffer: the packet is `buf[start..]` (`start` skips a device header).
    /// An in-order payload filling at least half the buffer is kept as a
    /// slice of it (the buffer returns to its pool once that slice is
    /// consumed); anything else is copied as with `ingress_borrowed` and the
    /// buffer goes back at once.
    pub fn ingress_buf(&mut self, now: Instant, iface: IfaceId, peer: PeerId, buf: PacketBuf, start: usize, csum: RxChecksum) {
        let keep = buf.len().saturating_sub(start) * 2 >= buf.capacity();
        if keep {
            self.ingress_owned(now, iface, peer, Bytes::from_owner(buf).slice(start..), csum, true);
        } else if start <= buf.len() {
            self.ingress_slice(now, iface, peer, &buf[start..], csum);
        }
    }

    /// TX checksum offload for `iface`: data segments leave with a partial
    /// TCP checksum (`OutPacket::csum_partial`) for the device to complete,
    /// e.g. a Linux TUN with `IFF_VNET_HDR` and `TUN_F_CSUM`. Control and
    /// stateless replies keep full checksums.
    pub fn set_iface_tx_checksum_offload(&mut self, id: IfaceId, on: bool) {
        if let Some(Some(i)) = self.ifaces.get_mut(id.0 as usize) {
            i.tx_csum_offload = on;
            if !on {
                // A device only segments packets whose checksum it completes.
                i.tso_max = 0;
            }
        }
    }

    /// TX segmentation offload for `iface`: new data leaves in super-segments
    /// of up to `max_payload` bytes (whole MSS multiples) with
    /// `OutPacket::gso_size` = MSS, for a device that segments them (a Linux
    /// TUN with `TUN_F_TSO4 | TUN_F_TSO6`). Implies TX checksum offload. 0
    /// turns it off. Retransmissions and probes stay single segments.
    pub fn set_iface_tso(&mut self, id: IfaceId, max_payload: u32) {
        if let Some(Some(i)) = self.ifaces.get_mut(id.0 as usize) {
            // The whole IP packet must fit the 16-bit IPv4 total length.
            i.tso_max = max_payload.min((u16::MAX as usize - wire::MAX_HEADER) as u32);
            if i.tso_max > 0 {
                i.tx_csum_offload = true;
            }
        }
    }

    fn parse_ingress(&mut self, pkt: &[u8], iface: IfaceId, csum: RxChecksum) -> Option<(wire::IpInfo, TcpHeader)> {
        self.stats.rx_packets += 1;
        if self.ifaces.get(iface.0 as usize).is_none_or(|i| i.is_none()) {
            self.stats.rx_dropped_no_iface += 1;
            return None;
        }
        let ip = match wire::parse_ip(pkt) {
            Ok(ip) => ip,
            Err(_) => {
                self.stats.rx_dropped_parse += 1;
                return None;
            }
        };
        match wire::parse_tcp_with(pkt, &ip, csum == RxChecksum::Verify) {
            Ok(h) => Some((ip, h)),
            Err(_) => {
                self.stats.rx_dropped_parse += 1;
                None
            }
        }
    }

    /// Owned / leased ingress: parse from a borrow, then slice the same `Bytes`
    /// once. The previous call sites cloned the whole packet so parse could
    /// keep a `&[u8]` while the owner moved.
    fn ingress_owned(&mut self, now: Instant, iface: IfaceId, peer: PeerId, pkt: Bytes, csum: RxChecksum, leased: bool) {
        let Some((ip, h)) = self.parse_ingress(pkt.as_ref(), iface, csum) else { return };
        let start = ip.l4_off + h.data_off;
        let end = ip.l4_off + ip.l4_len;
        let remote = SocketAddr::new(ip.src, h.src_port);
        let local = SocketAddr::new(ip.dst, h.dst_port);
        let payload = if leased { IngressPayload::Leased(pkt.slice(start..end)) } else { IngressPayload::Owned(pkt.slice(start..end)) };
        self.dispatch_ingress(now, iface, peer, remote, local, h, payload);
    }

    fn ingress_slice(&mut self, now: Instant, iface: IfaceId, peer: PeerId, pkt: &[u8], csum: RxChecksum) {
        let Some((ip, h)) = self.parse_ingress(pkt, iface, csum) else { return };
        let start = ip.l4_off + h.data_off;
        let end = ip.l4_off + ip.l4_len;
        let remote = SocketAddr::new(ip.src, h.src_port);
        let local = SocketAddr::new(ip.dst, h.dst_port);
        self.dispatch_ingress(now, iface, peer, remote, local, h, IngressPayload::Borrowed(&pkt[start..end]));
    }

    fn dispatch_ingress(
        &mut self,
        now: Instant,
        iface: IfaceId,
        peer: PeerId,
        remote: SocketAddr,
        local: SocketAddr,
        h: TcpHeader,
        payload: IngressPayload<'_>,
    ) {
        let key = (iface, remote, local);
        if let Some(&idx) = self.table.get(&key) {
            self.input_existing(now, idx, peer, &h, payload);
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
            match self.try_cookie(now, iface, peer, remote, local, &h) {
                CookieResult::Established(idx) => {
                    self.input_existing(now, idx, peer, &h, payload);
                    return;
                }
                // Valid cookie but no resources: drop the ACK silently so the client
                // retransmits and the connection can be established later (§10.2).
                CookieResult::NoResources => return,
                CookieResult::Invalid => {}
            }
        }
        self.reply_rst(iface, peer, remote, local, &h, payload.len());
    }

    fn input_existing(&mut self, now: Instant, idx: u32, peer: PeerId, h: &TcpHeader, payload: IngressPayload<'_>) {
        if let Some(tomb) = self.slots[idx as usize].tomb {
            if tomb.peer != peer {
                self.stats.rx_dropped_peer_mismatch += 1;
                return;
            }
            if h.has(SYN) {
                if payload.len() == 0 && tomb.can_reuse(h, self.cfg.timestamps) {
                    self.reuse_time_wait(now, idx, peer, h);
                }
                return;
            }
            if h.has(RST) {
                return;
            }
            if h.has(FIN) {
                let expires = now + self.cfg.time_wait;
                self.slots[idx as usize].tomb.as_mut().unwrap().expires = expires;
                self.timers.set(idx, expires);
            }
            if !self.slots[idx as usize].tomb.as_ref().unwrap().pending_ack {
                self.slots[idx as usize].tomb.as_mut().unwrap().pending_ack = true;
                self.tw_ready.push_back((idx, self.slots[idx as usize].gen));
                self.ifaces[tomb.iface.0 as usize].as_mut().unwrap().tw_pending += 1;
            }
            return;
        }
        let slot = &mut self.slots[idx as usize];
        let conn = slot.conn.as_mut().unwrap();
        if conn.peer != peer {
            self.stats.rx_dropped_peer_mismatch += 1;
            return;
        }
        {
            let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
            conn.input(now, h, payload, &mut ctx);
        }
        if conn.bad_ack_rst {
            conn.bad_ack_rst = false;
            let (iface, peer, remote, local) = (conn.iface, conn.peer, conn.remote, conn.local);
            self.push_stateless_rst(iface, peer, local, remote, h.ack, None);
        }
        self.reprice_pacing(idx, now);
        self.after_conn_change(idx);
    }

    /// Reprice outstanding pacing debt at the instant CC changes the rate,
    /// including while the connection has nothing to send (§7.2). An armed
    /// pacing wake is left to `schedule`, which detects the rate change and
    /// lets `serve` reprice and re-arm it.
    fn reprice_pacing(&mut self, idx: u32, now: Instant) {
        if self.pacing.contains(idx) {
            return;
        }
        let Some(conn) = self.slots[idx as usize].conn.as_mut() else { return };
        let rate = conn.egress_pacing_rate(&self.cfg);
        conn.sync_pacing_debt(now, rate);
    }

    /// Reconcile the half-open and TIME_WAIT counters with the connection state.
    /// Every state change passes through here (or `free_slot`), whatever caused it.
    fn sync_counts(&mut self, idx: u32) {
        let slot = &mut self.slots[idx as usize];
        let Some(conn) = slot.conn.as_ref() else { return };
        let st = conn.state;
        if slot.sched.half_open && st != State::SynReceived {
            slot.sched.half_open = false;
            self.half_open = self.half_open.saturating_sub(1);
        }
        if slot.sched.time_wait && st != State::TimeWait {
            slot.sched.time_wait = false;
            self.time_wait_n = self.time_wait_n.saturating_sub(1);
        } else if !slot.sched.time_wait && st == State::TimeWait {
            slot.sched.time_wait = true;
            self.time_wait_n += 1;
        }
    }

    fn sync_share_waiter(&mut self, idx: u32, register: bool) {
        let slot = &mut self.slots[idx as usize];
        let key = (idx, slot.gen);
        if !register && !self.share_blocked.contains(&key) {
            return;
        }
        if slot.conn.as_ref().is_some_and(|c| c.share_write_blocked(&self.cfg, &self.budget)) {
            self.share_blocked.insert(key);
            self.share_wait_epoch.get_or_insert_with(|| self.budget.global().release_epoch());
            self.share_seen_epoch.get_or_insert_with(|| self.budget.share_epoch());
        } else {
            if self.share_blocked.remove(&key) {
                // A read/control operation can visit this connection after
                // another sender released its share but before run's retry.
                // Do not discard its pending notification on that path.
                if let Some(conn) = slot.conn.as_mut() {
                    conn.notify_writable(&mut self.events, &self.cfg, &self.budget);
                }
            }
            if self.share_blocked.is_empty() {
                self.share_wait_epoch = None;
                self.share_seen_epoch = None;
            }
        }
    }

    fn retry_share_writers(&mut self) {
        if self.share_blocked.is_empty() {
            self.share_wait_epoch = None;
            self.share_seen_epoch = None;
            return;
        }
        let share_epoch = self.budget.share_epoch();
        if self.share_seen_epoch == Some(share_epoch) {
            // Nothing that can grow a share changed, so skip the scan, but
            // refresh the epoch `budget_wait_epoch` hands out: a stale one
            // makes a bare-Shard driver's `register_waiter` wake immediately,
            // every round, after any unrelated release.
            self.share_wait_epoch = Some(self.budget.global().release_epoch());
            return;
        }
        self.share_blocked.retain(|&(idx, gen)| {
            let Some(slot) = self.slots.get_mut(idx as usize).filter(|s| s.gen == gen) else { return false };
            let Some(conn) = slot.conn.as_mut() else { return false };
            if conn.share_write_blocked(&self.cfg, &self.budget) {
                return true;
            }
            conn.notify_writable(&mut self.events, &self.cfg, &self.budget);
            false
        });
        if self.share_blocked.is_empty() {
            self.share_wait_epoch = None;
            self.share_seen_epoch = None;
        } else {
            self.share_wait_epoch = Some(self.budget.global().release_epoch());
            self.share_seen_epoch = Some(share_epoch);
        }
    }

    /// Reschedule timers / tx after any change to a connection; reap if done.
    fn after_conn_change(&mut self, idx: u32) {
        self.sync_counts(idx);
        self.sync_share_waiter(idx, false);
        let gen = self.slots[idx as usize].gen;
        if !self.budget_blocked.is_empty()
            && self.slots[idx as usize].conn.as_ref().is_none_or(|c| !c.needs_record_spare())
            && self.budget_blocked.remove(&(idx, gen))
            && self.budget_blocked.is_empty()
        {
            self.budget_wait_epoch = None;
        }
        if self.slots[idx as usize].conn.as_ref().is_some_and(|c| c.ready_for_time_wait_compaction()) {
            self.compact_time_wait(idx);
            return;
        }
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

    fn compact_time_wait(&mut self, idx: u32) {
        let slot = &mut self.slots[idx as usize];
        let mut conn = slot.conn.take().expect("TIME_WAIT connection");
        let tomb = conn.time_wait_snapshot();
        let expires = tomb.expires;
        {
            let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
            conn.destroy(&mut ctx);
        }
        self.budget.peer_conn_del(conn.peer);
        slot.tomb = Some(tomb);
        slot.active_memory.take();
        slot.permit.as_mut().expect("connection permit").to_time_wait();
        self.share_blocked.remove(&(idx, slot.gen));
        if self.share_blocked.is_empty() {
            self.share_wait_epoch = None;
            self.share_seen_epoch = None;
        }
        self.budget_blocked.remove(&(idx, slot.gen));
        if self.budget_blocked.is_empty() {
            self.budget_wait_epoch = None;
        }
        slot.sched.in_ready = false;
        self.timers.set(idx, expires);
        self.pacing.remove(idx);
        self.stats.conns_freed += 1;
    }

    fn schedule(&mut self, idx: u32) {
        let slot = &mut self.slots[idx as usize];
        if slot.sched.in_ready {
            return;
        }
        let Some(conn) = slot.conn.as_ref() else { return };
        if self.budget_blocked.contains(&(idx, slot.gen))
            && conn.needs_record_spare()
            && !conn.has_record_spare()
            && !conn.wants_unpaced_tx()
            && !conn.has_lost_pending()
        {
            // ACKs and app writes must not spin on the same failed allocation.
            // A release epoch reschedules blocked senders in `run`; an ACK that
            // frees an existing record slot, new control work, or a loss that
            // can be retransmitted in place bypasses this.
            return;
        }
        if self.pacing.contains(idx) {
            // Control work and a changed pacing rate must be served now. `serve`
            // reprices outstanding debt before installing the updated data wake.
            if !conn.wants_unpaced_tx() && !conn.pacing_rate_changed(conn.egress_pacing_rate(&self.cfg)) {
                return;
            }
            self.pacing.remove(idx);
        }
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
        let conn = slot.conn.take();
        let tomb = slot.tomb.take();
        if conn.is_none() && tomb.is_none() {
            return;
        }
        if let Some(mut conn) = conn {
            let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
            conn.destroy(&mut ctx);
            self.table.remove(&(conn.iface, conn.remote, conn.local));
            self.budget.peer_conn_del(conn.peer);
        } else if let Some(tomb) = tomb {
            self.table.remove(&(tomb.iface, tomb.remote, tomb.local));
            if tomb.pending_ack {
                self.ifaces[tomb.iface.0 as usize].as_mut().unwrap().tw_pending -= 1;
            }
        }
        if slot.sched.half_open {
            self.half_open = self.half_open.saturating_sub(1);
        }
        if slot.sched.time_wait {
            self.time_wait_n = self.time_wait_n.saturating_sub(1);
        }
        slot.active_memory.take();
        if let Some(permit) = slot.permit.take() {
            self.retained_metadata.retain_slot(permit);
            slot.retained = true;
        }
        self.share_blocked.remove(&(idx, slot.gen));
        if self.share_blocked.is_empty() {
            self.share_wait_epoch = None;
            self.share_seen_epoch = None;
        }
        self.budget_blocked.remove(&(idx, slot.gen));
        if self.budget_blocked.is_empty() {
            self.budget_wait_epoch = None;
        }
        slot.gen = slot.gen.wrapping_add(1);
        slot.sched = Sched::default();
        self.timers.remove(idx);
        self.pacing.remove(idx);
        self.free.push(idx);
        self.stats.conns_freed += 1;
        if self.table.is_empty() && self.slots.capacity() > IDLE_SLOT_RECLAIM_THRESHOLD {
            self.reclaim_idle_containers();
        } else if self.table.capacity() > 512 && self.table.len() <= self.table.capacity() / 8 {
            self.shrink_sparse_table();
        }
    }

    /// Short-connection bursts can leave a large tuple table while one long
    /// connection keeps the shard alive. Prepare the smaller table first so an
    /// allocation failure leaves every tuple reachable in the old table.
    fn shrink_sparse_table(&mut self) {
        let mut compact = HashMap::new();
        if compact.try_reserve(self.table.len()).is_err() {
            return;
        }
        compact.extend(self.table.drain());
        self.table = compact;
    }

    fn reclaim_idle_containers(&mut self) {
        debug_assert!(self.table.is_empty());
        if self.slots.iter().any(|slot| slot.conn.is_some() || slot.tomb.is_some()) {
            return;
        }
        if self.retired_generations.len() < self.slots.len() {
            self.retired_generations.resize(self.slots.len(), 1);
        }
        for (saved, slot) in self.retired_generations.iter_mut().zip(&self.slots) {
            *saved = slot.gen;
        }
        self.slots = Vec::new();
        self.free = Vec::new();
        self.table = HashMap::new();
        self.timers = IndexedHeap::default();
        self.pacing = IndexedHeap::default();
        self.tw_ready = VecDeque::new();
        // A burst can leave thousands of buckets after all waiters exited.
        // Reclaim only with the other completely idle containers; never
        // reallocate a sparse set on the ACK/share-release path.
        debug_assert!(self.share_blocked.is_empty() && self.budget_blocked.is_empty());
        self.share_blocked.shrink_to_fit();
        self.budget_blocked.shrink_to_fit();
        for iface in self.ifaces.iter_mut().flatten() {
            iface.drr = Drr::default();
            debug_assert_eq!(iface.tw_pending, 0);
        }
        self.retained_metadata.reclaim_to_generations((self.retired_generations.capacity() * std::mem::size_of::<u32>()) as u64);
    }

    fn alloc_slot(&mut self) -> (u32, u32) {
        if let Some(i) = self.free.pop() {
            let slot = &mut self.slots[i as usize];
            if slot.retained {
                slot.retained = false;
                self.retained_metadata.reuse_slot();
            }
            (i, slot.gen)
        } else {
            let idx = self.slots.len();
            let gen = self.retired_generations.get(idx).copied().unwrap_or(1);
            self.slots.push(Slot { gen, conn: None, tomb: None, permit: None, active_memory: None, sched: Sched::default(), retained: false });
            (idx as u32, gen)
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
        if self.budget.level() >= Pressure::High
            || peer_conns >= self.budget.peer_max_conns
            || self.time_wait_n >= self.cfg.max_time_wait
            || self.events.len() >= self.cfg.accept_backlog * 4
        {
            self.stats.syn_dropped += 1;
            return;
        }
        let mtu = self.iface_mtu(iface);
        let syn = SynParams { mss: h.opts.mss, wscale: h.opts.wscale, sack: h.opts.sack_perm, ts: h.opts.ts };
        if self.half_open >= self.cfg.syn_backlog {
            self.send_cookie_synack(now, iface, peer, remote, local, h, mtu);
            return;
        }
        let Some(permit) = self.budget.global().try_acquire_connection() else {
            self.stats.syn_dropped += 1;
            return;
        };
        if !self.budget.peer_conn_add(peer) {
            self.stats.syn_dropped += 1;
            return;
        }
        let Some((active_memory, stream_memory, rx_progress)) = self.admit_memory(now, (iface, remote, local), peer) else {
            self.budget.peer_conn_del(peer);
            self.stats.syn_dropped += 1;
            return;
        };
        let (idx, gen) = self.alloc_slot();
        let id = ConnId::new(idx, gen);
        let iss = self.isn(now, remote, local);
        let tso = self.ts_offset(remote, local);
        let mut conn = Conn::new_passive(id, iface, peer, local, remote, iss, tso, mtu, h.seq, &syn, &self.cfg, now);
        conn.stream_memory = stream_memory;
        if let Some(credit) = rx_progress {
            conn.set_rx_progress(credit);
        }
        self.slots[idx as usize].conn = Some(Box::new(conn));
        self.slots[idx as usize].permit = Some(permit);
        self.slots[idx as usize].active_memory = Some(active_memory);
        self.table.insert((iface, remote, local), idx);
        self.half_open += 1;
        self.slots[idx as usize].sched.half_open = true;
        self.stats.conns_created += 1;
        self.after_conn_change(idx);
    }

    fn reuse_time_wait(&mut self, now: Instant, idx: u32, peer: PeerId, h: &TcpHeader) {
        let old = self.slots[idx as usize].tomb.expect("TIME_WAIT tuple");
        self.stats.syn_received += 1;
        match self.admission.on_syn(old.iface, peer, old.remote, old.local) {
            Admission::Accept => {}
            Admission::Reject => {
                self.stats.syn_rejected += 1;
                return;
            }
            Admission::Drop => {
                self.stats.syn_dropped += 1;
                return;
            }
        }
        if self.budget.level() >= Pressure::High
            || self.half_open >= self.cfg.syn_backlog
            || self.events.len() >= self.cfg.accept_backlog * 4
            || !self.budget.peer_conn_add(peer)
        {
            self.stats.syn_dropped += 1;
            return;
        }
        if !self.slots[idx as usize].permit.as_mut().expect("TIME_WAIT permit").try_reactivate() {
            self.budget.peer_conn_del(peer);
            self.stats.syn_dropped += 1;
            return;
        }
        let Some((active_memory, stream_memory, rx_progress)) = self.admit_memory(now, (old.iface, old.remote, old.local), peer) else {
            self.slots[idx as usize].permit.as_mut().unwrap().to_time_wait();
            self.budget.peer_conn_del(peer);
            self.stats.syn_dropped += 1;
            return;
        };

        let mtu = self.iface_mtu(old.iface);
        let syn = SynParams { mss: h.opts.mss, wscale: h.opts.wscale, sack: h.opts.sack_perm, ts: h.opts.ts };
        let iss = self.isn(now, old.remote, old.local);
        let tso = self.ts_offset(old.remote, old.local);
        let slot = &mut self.slots[idx as usize];
        if old.pending_ack {
            self.ifaces[old.iface.0 as usize].as_mut().unwrap().tw_pending -= 1;
        }
        slot.tomb = None;
        slot.gen = slot.gen.wrapping_add(1);
        let id = ConnId::new(idx, slot.gen);
        let mut conn = Conn::new_passive(id, old.iface, peer, old.local, old.remote, iss, tso, mtu, h.seq, &syn, &self.cfg, now);
        conn.stream_memory = stream_memory;
        if let Some(credit) = rx_progress {
            conn.set_rx_progress(credit);
        }
        slot.conn = Some(Box::new(conn));
        slot.active_memory = Some(active_memory);
        slot.sched.time_wait = false;
        slot.sched.half_open = true;
        self.time_wait_n -= 1;
        self.half_open += 1;
        self.timers.remove(idx);
        self.stats.time_wait_recycled += 1;
        self.stats.conns_created += 1;
        self.after_conn_change(idx);
    }

    // ---- SYN cookies (§10.2) ----

    fn cookie_hash(&self, gen: u64, iface: IfaceId, peer: PeerId, remote: SocketAddr, local: SocketAddr, peer_isn: Seq) -> u32 {
        let mut h = self.cookie_key[(gen & 1) as usize].build_hasher();
        (gen, iface, peer, remote, local, peer_isn.0).hash(&mut h);
        h.finish() as u32
    }

    fn rotate_cookie_keys(&mut self, now: Instant) {
        let gen = now.as_millis() / 1000 / COOKIE_PERIOD_S;
        if gen > self.cookie_gen {
            if gen == self.cookie_gen + 1 {
                self.cookie_key[(gen & 1) as usize] = self.next_keys();
            } else {
                self.cookie_key = [self.next_keys(), self.next_keys()];
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
        let hash = self.cookie_hash(gen, iface, peer, remote, local, h.seq);
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
                csum_partial: false,
            },
        );
        if self.push_stateless(iface, peer, n) {
            self.stats.syn_cookies_sent += 1;
        }
    }

    fn try_cookie(&mut self, now: Instant, iface: IfaceId, peer: PeerId, remote: SocketAddr, local: SocketAddr, h: &TcpHeader) -> CookieResult {
        match self.try_cookie_inner(now, iface, peer, remote, local, h) {
            Ok(idx) => CookieResult::Established(idx),
            Err(true) => CookieResult::NoResources,
            Err(false) => CookieResult::Invalid,
        }
    }

    /// Err(true) = valid cookie but resources exhausted; Err(false) = not a valid cookie.
    fn try_cookie_inner(&mut self, now: Instant, iface: IfaceId, peer: PeerId, remote: SocketAddr, local: SocketAddr, h: &TcpHeader) -> Result<u32, bool> {
        if self.stats.syn_cookies_sent == 0 {
            return Err(false);
        }
        self.rotate_cookie_keys(now);
        let cookie = h.ack.0.wrapping_sub(1);
        let peer_isn = h.seq.add(u32::MAX); // seq - 1
        let cg = (cookie >> 27) & 0x1f;
        let Some(gen) = [self.cookie_gen, self.cookie_gen.wrapping_sub(1)].into_iter().find(|g| (*g as u32 & 0x1f) == cg) else {
            return Err(false);
        };
        if gen + 1 < self.cookie_gen {
            return Err(false);
        }
        if self.cookie_hash(gen, iface, peer, remote, local, peer_isn) & 0x00ff_ffff != cookie & 0x00ff_ffff {
            return Err(false);
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
        if self.budget.level() >= Pressure::High {
            self.stats.syn_cookies_rejected_resources += 1;
            return Err(true);
        }
        let Some(permit) = self.budget.global().try_acquire_connection() else {
            self.stats.syn_cookies_rejected_resources += 1;
            return Err(true);
        };
        if !self.budget.peer_conn_add(peer) {
            self.stats.syn_cookies_rejected_resources += 1;
            return Err(true);
        }
        if self.admission.on_syn(iface, peer, remote, local) != Admission::Accept {
            self.budget.peer_conn_del(peer);
            return Err(false);
        }
        let Some((active_memory, stream_memory, rx_progress)) = self.admit_memory(now, (iface, remote, local), peer) else {
            self.budget.peer_conn_del(peer);
            self.stats.syn_cookies_rejected_resources += 1;
            return Err(true);
        };
        self.stats.syn_cookies_ok += 1;
        let mtu = self.iface_mtu(iface);
        let (idx, gen_slot) = self.alloc_slot();
        let id = ConnId::new(idx, gen_slot);
        let tso = self.ts_offset(remote, local);
        let syn = SynParams { mss: Some(mss), wscale, sack, ts };
        let mut conn = Conn::new_from_cookie(id, iface, peer, local, remote, Seq(cookie), tso, mtu, peer_isn, &syn, &self.cfg, now);
        conn.stream_memory = stream_memory;
        if let Some(credit) = rx_progress {
            conn.set_rx_progress(credit);
        }
        conn.accepted = true;
        self.events.push_back(Event::Accepted(id));
        self.slots[idx as usize].conn = Some(Box::new(conn));
        self.slots[idx as usize].permit = Some(permit);
        self.slots[idx as usize].active_memory = Some(active_memory);
        self.table.insert((iface, remote, local), idx);
        self.stats.conns_created += 1;
        Ok(idx)
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
                csum_partial: false,
            },
        );
        if self.push_stateless(iface, peer, n) {
            self.stats.rst_sent += 1;
        }
    }

    fn push_stateless(&mut self, iface: IfaceId, peer: PeerId, len: usize) -> bool {
        debug_assert!(len <= MAX_HEADER);
        // A full queue already discards its oldest reply. Release that charge
        // before trying to admit the replacement under memory pressure.
        if self.stateless.len() >= STATELESS_MAX {
            self.stateless.pop_front();
        }
        let Some(memory) = self.budget.try_allocate_kind(peer, STATELESS_CHARGE, crate::budget::AllocationKind::Stateless) else {
            return false;
        };
        let mut header = [0; MAX_HEADER];
        header[..len].copy_from_slice(&self.hdr[..len]);
        self.stateless.push_back(Stateless { iface, peer, header, len, _memory: memory });
        true
    }

    fn shrink_stateless_queue(&mut self) {
        if self.stateless.is_empty() && self.stateless.capacity() > 8 {
            self.stateless = VecDeque::new();
        } else if self.stateless.capacity() > 32 && self.stateless.len() * 4 < self.stateless.capacity() {
            self.stateless.shrink_to_fit();
        }
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

    pub fn tx_cache_snapshot(&self) -> crate::buf::TxCacheSnapshot {
        self.pool.snapshot()
    }

    // Seed the real driver test without creating connections, TCP timers or input.
    #[cfg(test)]
    pub(crate) fn prime_tx_cache_for_test(&mut self, blocks: usize) {
        assert!(blocks > 0 && blocks <= self.pool.max_cached);
        let mut tx = crate::buf::TxBuf::default();
        assert!(tx.push(&mut self.pool, &mut self.budget, PeerId(1), &vec![7; blocks * crate::buf::TX_BLOCK]));
        tx.release_all(&mut self.pool);
        drop(tx);
        assert_eq!(self.pool.cached(), blocks);
    }

    /// Explicitly opt into experimental idle trim, or disable it with None.
    /// This adds conditional activity/low-water tracking per successful block
    /// get/put and coarse maintenance wakes. It remains disabled by default.
    pub fn set_tx_cache_trim(&mut self, policy: Option<TxCacheTrimPolicy>) -> Result<(), &'static str> {
        if let Some(policy) = policy {
            if policy.check_interval.is_zero() || policy.cold_after.is_zero() || policy.warm_after > policy.cold_after {
                return Err("TX cache trim needs a positive interval/cold delay and warm <= cold");
            }
        }
        self.tx_cache_trim = policy;
        self.tx_cache_idle = None;
        self.pool.set_activity_tracking(policy.is_some());
        Ok(())
    }

    fn arm_tx_cache_maintenance(&mut self, now: Instant) {
        let Some(policy) = self.tx_cache_trim else { return };
        if !self.pool.has_retained() {
            self.tx_cache_idle = None;
            self.pool.take_activity();
        } else if self.tx_cache_idle.is_none() {
            self.pool.take_activity();
            self.pool.reset_activity_window();
            self.tx_cache_idle = Some(TxCacheIdle { next_check: now + policy.check_interval, quiet_since: [now; 2], warm_since: now });
        }
    }

    fn note_idle_tx_cache_trim(&mut self, blocks: usize, bytes: u64) {
        if bytes != 0 {
            self.stats.tx_cache_idle_trims += 1;
            self.stats.tx_cache_idle_trimmed_blocks += blocks as u64;
            self.stats.tx_cache_idle_trimmed_bytes += bytes;
        }
    }

    fn maintain_tx_cache(&mut self, now: Instant) {
        let Some(policy) = self.tx_cache_trim else { return };
        if !self.pool.has_retained() || self.tx_cache_idle.is_none() {
            self.arm_tx_cache_maintenance(now);
            return;
        }
        let mut idle = self.tx_cache_idle.unwrap();
        if now < idle.next_check {
            return;
        }
        self.stats.tx_cache_maintenance_checks += 1;
        for (since, touched) in idle.quiet_since.iter_mut().zip(self.pool.take_activity()) {
            if touched {
                // Upper-bound this class's last activity by the coarse sample.
                *since = now;
            }
        }
        let warm_due = now.saturating_since(idle.warm_since) >= policy.warm_after;
        let mut targets = if warm_due { self.pool.warm_trim_targets([policy.warm_large, policy.warm_small]) } else { self.pool.cached_by_class() };
        let mut trim_due = warm_due;
        for ((target, since), retained) in targets.iter_mut().zip(idle.quiet_since).zip(self.pool.retained_by_class()) {
            if retained && now.saturating_since(since) >= policy.cold_after {
                *target = 0;
                trim_due = true;
            }
        }
        if trim_due {
            let (blocks, bytes) = self.pool.trim_cached_to(targets[0], targets[1]);
            self.note_idle_tx_cache_trim(blocks, bytes);
        }
        if warm_due {
            self.pool.reset_activity_window();
            idle.warm_since = now;
        }
        // No catch-up loop after a delayed host wake.
        idle.next_check = now + policy.check_interval;
        self.tx_cache_idle = self.pool.has_retained().then_some(idle);
    }

    /// Drop idle TX blocks after any allocation failed while they were cached
    /// (docs/design/0005 §4). The release wakes the parked allocator.
    fn reclaim_cache_if_requested(&mut self) {
        let epoch = self.budget.global().cache_reclaim_epoch();
        if epoch == self.cache_reclaim_seen {
            return;
        }
        self.cache_reclaim_seen = epoch;
        let (_, pool_bytes) = self.pool.reclaim();
        self.tx_cache_idle = None;
        let bytes = pool_bytes + self.reclaim_idle_tx_indexes();
        if bytes != 0 {
            self.stats.cache_reclaims += 1;
            self.stats.cache_reclaimed_bytes += bytes;
        }
    }

    /// Reserve `bytes` of host stream state with every passive connection, in
    /// the same admission step as its core state. A shortage then drops the
    /// SYN (the peer retries) instead of the host having to reset a connection
    /// it cannot attach after the handshake (#664).
    pub fn reserve_stream_state(&mut self, bytes: u64) {
        self.stream_state_bytes = bytes;
        for iface in self.ifaces.iter().flatten() {
            self.ensure_rx_admit_reserve(iface.cfg.mtu);
        }
    }

    fn ensure_rx_admit_reserve(&self, mtu: u16) {
        self.budget.global().ensure_rx_admit_reserve(ACTIVE_STATE_BYTES + self.stream_state_bytes, crate::buf::rx_progress_charge(mtu));
    }

    /// The stream state reserved for `id`, handed over once on accept.
    pub fn take_stream_memory(&mut self, id: ConnId) -> Option<MemoryLease> {
        let slot = self.slots.get_mut(id.idx())?;
        if slot.gen != id.gen() {
            return None;
        }
        slot.conn.as_mut()?.stream_memory.take()
    }

    /// Core state plus reserved stream state for a new connection. If this
    /// shard's own idle TX blocks hold the room, drop them and retry once, so
    /// the SYN is not dropped for memory that was only cached.
    ///
    /// A refusal leaves an admission debt on the level that refused, so
    /// buffering there frees room for the peer's SYN retransmit instead of
    /// taking every released block back (docs/design/0006 §2).
    fn admit_memory(&mut self, now: Instant, key: (IfaceId, SocketAddr, SocketAddr), peer: PeerId) -> Option<AdmittedMemory> {
        let charge = crate::buf::rx_progress_charge(self.iface_mtu(key.0));
        let admitted = match self.try_admit_memory(peer, charge) {
            Err(_) if self.reclaim_own_cache() => self.try_admit_memory(peer, charge),
            r => r,
        };
        let pending = self.pending_admissions.iter().position(|p| p.key == key);
        match admitted {
            Ok(memory) => {
                if let Some(k) = pending {
                    self.pending_admissions.remove(k);
                }
                Some(memory)
            }
            Err(level) => {
                let bytes = ACTIVE_STATE_BYTES + self.stream_state_bytes;
                let k = match pending {
                    Some(k) => k,
                    None if self.pending_admissions.len() < crate::budget::ADMIT_BURST as usize => {
                        self.pending_admissions.push_back(PendingAdmission { key, first: now, until: now, debt: None, released: 0, progress_at: now });
                        self.stats.admission_debts += 1;
                        self.pending_admissions.len() - 1
                    }
                    None => return None,
                };
                let p = &mut self.pending_admissions[k];
                p.until = now + admission_hold(now.saturating_since(p.first));
                // Owe the level that refused this attempt; a retransmit may
                // hit a different level than the first SYN did.
                let owed = p.debt.as_ref().map(|(l, _)| *l);
                if owed != Some(level) {
                    if owed.is_some() {
                        self.stats.admission_debts_moved += 1;
                    }
                    p.debt = None;
                    let debt = self.budget.admit_debt(peer, level, bytes);
                    let p = &mut self.pending_admissions[k];
                    p.released = debt.released();
                    p.progress_at = now;
                    p.debt = Some((level, debt));
                }
                None
            }
        }
    }

    fn reclaim_own_cache(&mut self) -> bool {
        let (_, pool_bytes) = self.pool.reclaim();
        self.tx_cache_idle = None;
        let bytes = pool_bytes + self.reclaim_idle_tx_indexes();
        if bytes == 0 {
            return false;
        }
        self.stats.cache_reclaims += 1;
        self.stats.cache_reclaimed_bytes += bytes;
        true
    }

    fn reclaim_idle_tx_indexes(&mut self) -> u64 {
        self.slots.iter_mut().filter_map(|slot| slot.conn.as_mut()).map(|conn| conn.reclaim_idle_tx_index()).sum()
    }

    fn try_admit_memory(&mut self, peer: PeerId, charge: u64) -> Result<AdmittedMemory, Level> {
        let memory = self.budget.memory_scope(peer);
        let active = memory.try_allocate_level(ACTIVE_STATE_BYTES, crate::budget::AllocationKind::ActiveState)?;
        let stream = if self.stream_state_bytes == 0 {
            None
        } else {
            Some(memory.try_allocate_level(self.stream_state_bytes, crate::budget::AllocationKind::StreamState)?)
        };
        let rx = memory.try_rx_progress(charge).map(|lease| crate::buf::RxProgress::new(lease, charge));
        Ok((active, stream, rx))
    }

    /// Drop admission debts whose peer did not retry within the hold, and
    /// lift debts on levels that stopped releasing TX blocks and records.
    fn expire_admission_debts(&mut self, now: Instant) {
        let before = self.pending_admissions.len();
        self.pending_admissions.retain(|p| p.until > now);
        self.stats.admission_debts_expired += (before - self.pending_admissions.len()) as u64;
        for p in &mut self.pending_admissions {
            let Some((_, debt)) = &p.debt else { continue };
            let released = debt.released();
            if released != p.released {
                p.released = released;
                p.progress_at = now;
            } else if now.saturating_since(p.progress_at) >= ADMISSION_DEBT_STALL {
                p.debt = None;
                self.stats.admission_debts_stalled += 1;
            }
        }
    }

    /// Connections waiting for a send-record backing allocation.
    pub fn record_waiters(&self) -> usize {
        self.budget_blocked.len()
    }

    /// One scheduling round (§8): timers, pacing wakeups, then per-peer DRR.
    pub fn run(&mut self, now: Instant, sinks: &mut impl EgressSinks) -> RunOutcome {
        self.stats.runs += 1;
        self.note_wake(now);
        let mut out = RunOutcome::default();
        self.reclaim_cache_if_requested();
        self.maintain_tx_cache(now);
        self.expire_admission_debts(now);
        self.retry_share_writers();

        if self.budget_wait_epoch.is_some_and(|e| self.budget.global().release_epoch() != e) {
            self.budget_wait_epoch = None;
            let keys = self.budget_blocked.hasher().clone();
            for (idx, gen) in std::mem::replace(&mut self.budget_blocked, HashSet::with_hasher(keys)) {
                let retry = self.slots.get(idx as usize).filter(|s| s.gen == gen).and_then(|s| s.conn.as_ref()).is_some_and(|conn| {
                    !conn.needs_record_spare()
                        || conn.has_record_spare()
                        || conn.wants_unpaced_tx()
                        || conn
                            .record_reserve_bytes_needed()
                            .is_some_and(|bytes| self.budget.can_allocate(conn.peer, bytes, crate::budget::AllocationKind::SendRecord))
                });
                if retry {
                    self.schedule(idx);
                } else if self.slots.get(idx as usize).is_some_and(|s| s.gen == gen && s.conn.is_some()) {
                    self.budget_blocked.insert((idx, gen));
                }
            }
            if !self.budget_blocked.is_empty() {
                self.budget_wait_epoch = Some(self.budget.global().release_epoch());
            }
        }

        while let Some(idx) = self.timers.pop_due(now) {
            let slot = &mut self.slots[idx as usize];
            if slot.tomb.is_some() {
                self.free_slot(idx);
                continue;
            }
            let Some(conn) = slot.conn.as_mut() else { continue };
            {
                let mut ctx = Ctx { cfg: &self.cfg, pool: &mut self.pool, budget: &mut self.budget, events: &mut self.events };
                conn.on_timer(now, &mut ctx);
            }
            self.reprice_pacing(idx, now);
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
                    self.shrink_stateless_queue();
                    continue;
                }
                break;
            }
            let pkt = OutPacket { peer: s.peer, header: &s.header[..s.len], payload: [&[], &[]], csum_partial: false, gso_size: 0 };
            match sinks.send(s.iface, &pkt) {
                SendResult::Accepted => {
                    out.packets += 1;
                    out.bytes += s.len;
                    self.stats.tx_packets += 1;
                    self.stats.tx_bytes += s.len as u64;
                    self.stateless.pop_front();
                    self.shrink_stateless_queue();
                }
                SendResult::Full => {
                    let i = s.iface;
                    self.stats.sink_full += 1;
                    self.ifaces[i.0 as usize].as_mut().unwrap().full = true;
                    break;
                }
            }
        }
        // Each tombstone has at most one pending reply. A Full sink leaves it
        // queued and no TCP send state is advanced before acceptance.
        let mut tw_budget = self.cfg.round_bytes_cap;
        let mut tw_checked = 0;
        let tw_round = self.tw_ready.len();
        while tw_budget > 0 && tw_checked < tw_round {
            tw_checked += 1;
            let Some(&(idx, gen)) = self.tw_ready.front() else { break };
            let slot = &self.slots[idx as usize];
            if slot.gen != gen || slot.tomb.is_none() {
                self.tw_ready.pop_front();
                continue;
            }
            let tomb = slot.tomb.as_ref().unwrap();
            let iface = tomb.iface;
            let peer = tomb.peer;
            if self.ifaces[iface.0 as usize].as_ref().is_none_or(|i| i.full) {
                self.tw_ready.rotate_left(1);
                continue;
            }
            let opts = EmitOptions { ts: tomb.ts_ok.then(|| ((now.as_millis() as u32).wrapping_add(tomb.ts_offset), tomb.ts_recent)), ..Default::default() };
            let empty: [&[u8]; 0] = [];
            let len = wire::emit(
                &mut self.hdr,
                &EmitParams {
                    src: tomb.local.ip(),
                    dst: tomb.remote.ip(),
                    src_port: tomb.local.port(),
                    dst_port: tomb.remote.port(),
                    seq: tomb.snd_seq,
                    ack: tomb.rcv_seq,
                    flags: ACK,
                    window: 0,
                    opts: &opts,
                    payload: &empty,
                    ttl: self.cfg.ttl,
                    csum_partial: false,
                },
            );
            let pkt = OutPacket { peer, header: &self.hdr[..len], payload: [&[], &[]], csum_partial: false, gso_size: 0 };
            match sinks.send(iface, &pkt) {
                SendResult::Accepted => {
                    self.slots[idx as usize].tomb.as_mut().unwrap().pending_ack = false;
                    self.ifaces[iface.0 as usize].as_mut().unwrap().tw_pending -= 1;
                    self.tw_ready.pop_front();
                    out.packets += 1;
                    out.bytes += len;
                    self.stats.tx_packets += 1;
                    self.stats.tx_bytes += len as u64;
                    tw_budget = tw_budget.saturating_sub(len);
                }
                SendResult::Full => {
                    self.stats.sink_full += 1;
                    self.ifaces[iface.0 as usize].as_mut().unwrap().full = true;
                    self.tw_ready.rotate_left(1);
                }
            }
        }
        if !self.tw_ready.is_empty() && tw_budget == 0 {
            out.more = true;
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
                let allowance = ifc.pacer.as_mut().map_or(usize::MAX, |p| p.allowance(now, ifc.drr.ready_peers.len() > 1));
                if allowance == 0 {
                    break;
                }
                let Some(peer) = ifc.drr.ready_peers.pop_front() else { break };
                let Some(q) = ifc.drr.peer_q.get_mut(&peer) else { continue };
                let Some((idx, gen)) = q.pop_front() else {
                    ifc.drr.recycle(peer);
                    continue;
                };
                let valid = self.slots[idx as usize].gen == gen && self.slots[idx as usize].conn.is_some();
                let mut again = false;
                if valid {
                    self.slots[idx as usize].sched.in_ready = false;
                    let (sent, wire_sent, more) = self.serve(idx, now, budget.min(allowance), sinks, &mut out);
                    served_any |= sent > 0;
                    budget = budget.saturating_sub(sent);
                    if let Some(pacer) = self.ifaces[ii].as_mut().unwrap().pacer.as_mut() {
                        if pacer.active() {
                            self.stats.egress_paced_bytes += wire_sent as u64;
                        }
                        if pacer.charge(wire_sent) {
                            self.stats.egress_pacing_pauses += 1;
                        }
                    }
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
                    ifc.drr.recycle(peer);
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
        if self.ifaces.iter().flatten().any(|i| !i.full && !i.drr.is_empty() && i.pacer.as_ref().and_then(IfacePacer::deadline).is_none()) {
            out.more = true;
            self.stats.round_budget_exhausted += 1;
        }
        if !served_any && out.packets == 0 {
            self.stats.idle_rounds += 1;
        }
        self.retry_share_writers();
        self.arm_tx_cache_maintenance(now);
        out
    }

    fn after_conn_change_no_sched(&mut self, idx: u32) {
        self.sync_counts(idx);
        self.sync_share_waiter(idx, false);
        if self.slots[idx as usize].conn.as_ref().is_some_and(|c| c.ready_for_time_wait_compaction()) {
            self.compact_time_wait(idx);
            return;
        }
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

    /// Serve one connection for up to one quantum. Returns (buffer bytes,
    /// post-segmentation IP bytes, wants more).
    fn serve(&mut self, idx: u32, now: Instant, round_left: usize, sinks: &mut impl EgressSinks, out: &mut RunOutcome) -> (usize, usize, bool) {
        // Packet window fields must use the same policy as calc_window.
        // Local TX/cache occupancy only gates RX growth, not RX progress.
        let level = self.budget.level();
        let slot = &mut self.slots[idx as usize];
        let gen = slot.gen;
        let conn = slot.conn.as_mut().unwrap();
        let iface_id = conn.iface;
        let _ = gen;
        let Some(Some(iface)) = self.ifaces.get_mut(iface_id.0 as usize) else { return (0, 0, false) };
        if iface.full {
            conn.note_egress_limited(now);
            return (0, 0, true);
        }
        let rate = conn.egress_pacing_rate(&self.cfg);
        conn.sync_pacing_debt(now, rate);
        conn.tso_max = iface.tso_max;
        let mss = conn.mss as usize;
        let aggregate_pacing = iface.pacer.as_ref().is_some_and(IfacePacer::active);
        if aggregate_pacing {
            conn.tso_max = conn.tso_max.min(round_left.max(mss).min(u32::MAX as usize) as u32);
        }
        let quantum = match rate {
            Some(r) => quantum_for(r, mss, self.cfg.max_quantum),
            None => self.cfg.max_quantum.max(mss),
        }
        .min(round_left.max(mss));
        let credit = self.wake.credit;
        let mut sent = 0usize;
        let mut wire_sent = 0usize;
        loop {
            let has_record_room = !conn.needs_record_spare() || conn.try_reserve_send_record(&mut self.budget);
            let Some(mut plan): Option<Plan> = (if has_record_room {
                if !self.budget_blocked.is_empty() && self.budget_blocked.remove(&(idx, gen)) && self.budget_blocked.is_empty() {
                    self.budget_wait_epoch = None;
                }
                conn.plan(now, level)
            } else {
                self.budget_wait_epoch.get_or_insert_with(|| self.budget.global().release_epoch());
                self.budget_blocked.insert((idx, gen));
                conn.plan_in_place(now, level)
            }) else {
                return (sent, wire_sent, false);
            };
            if plan.paced() && rate.is_some() && conn.next_send_time > now + pacing_ahead(rate.unwrap(), mss, self.cfg.max_quantum) {
                // Data must wait for pacing, but a pending ACK / probe goes now.
                if let Some(c) = conn.plan_control(level) {
                    plan = c;
                }
            }
            if plan.paced() {
                if let Some(r) = rate {
                    // Pacing granularity = one scheduling quantum, clamp(rate × 1 ms, 2 MSS,
                    // 64 KiB) (§7.1; Linux TSO autosizing uses the same ~1 ms): a segment
                    // may leave up to one quantum's worth of time ahead of its EDT slot.
                    let ahead = pacing_ahead(r, mss, self.cfg.max_quantum);
                    if conn.next_send_time > now + ahead {
                        conn.note_pacing_limited(now);
                        // A finite small burst (including unacknowledged and
                        // adapter bytes) cannot use a full batch: wake as soon
                        // as the unchanged pacing gate allows it. Bulk keeps
                        // full-quantum batching instead of per-packet wakeups.
                        let small_burst = conn.send_occupancy() <= quantum_for(r, mss, self.cfg.max_quantum) as u64;
                        let t = if small_burst { conn.next_send_time - ahead } else { conn.next_send_time };
                        self.pacing.set(idx, t);
                        return (sent, wire_sent, false);
                    }
                    if now.saturating_since(conn.next_send_time) > credit + Duration::from_millis(1) && conn.next_send_time != Instant::ZERO {
                        self.stats.pacing_credit_dropped += 1;
                    }
                }
            }
            let peer = conn.peer;
            let csum_partial = iface.tx_csum_offload;
            let gso_size = if plan.len as usize > mss { mss as u16 } else { 0 };
            let (hl, payload) = conn.build(&plan, now, &mut self.hdr, self.cfg.ttl, csum_partial);
            let pkt = OutPacket { peer, header: &self.hdr[..hl], payload, csum_partial, gso_size };
            let len = pkt.len();
            match sinks.send(iface_id, &pkt) {
                SendResult::Accepted => {}
                SendResult::Full => {
                    self.stats.sink_full += 1;
                    iface.full = true;
                    conn.note_egress_limited(now);
                    // Stay queued (tail of its peer queue): resumes on egress_released.
                    return (sent, wire_sent, true);
                }
            }
            conn.commit(&plan, now, &self.cfg);
            wire_sent += len;
            if aggregate_pacing && gso_size != 0 {
                // Each device-created segment carries its own IP/TCP header.
                wire_sent += (plan.len as usize).div_ceil(mss).saturating_sub(1) * hl;
            }
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
                return (sent, wire_sent, false);
            }
            sent += len;
            out.packets += 1;
            out.bytes += len;
            self.stats.tx_packets += 1;
            self.stats.tx_bytes += len as u64;
            if sent >= quantum {
                return (sent, wire_sent, has_record_room);
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
        if self.is_tombstone(id) {
            return ReadResult::Eof;
        }
        self.with_conn(id, |c, ctx| c.read(dst, ctx, now)).unwrap_or(ReadResult::Closed(CloseReason::Aborted))
    }

    pub fn read_chunk(&mut self, now: Instant, id: ConnId, max: usize) -> Result<Bytes, ReadResult> {
        if self.is_tombstone(id) {
            return Err(ReadResult::Eof);
        }
        self.with_conn(id, |c, ctx| c.read_chunk(max, ctx, now)).unwrap_or(Err(ReadResult::Closed(CloseReason::Aborted)))
    }

    pub fn read_chunk_for_adapter(&mut self, now: Instant, id: ConnId, max: usize) -> Result<Bytes, ReadResult> {
        if self.is_tombstone(id) {
            return Err(ReadResult::Eof);
        }
        self.with_conn(id, |c, ctx| c.read_chunk_for_adapter(max, ctx, now)).unwrap_or(Err(ReadResult::Closed(CloseReason::Aborted)))
    }

    pub fn consume_adapter(&mut self, now: Instant, id: ConnId, bytes: usize) {
        self.with_conn(id, |c, ctx| c.consume_adapter(bytes, ctx, now));
    }

    fn is_tombstone(&self, id: ConnId) -> bool {
        self.slots.get(id.idx()).is_some_and(|s| s.gen == id.gen() && s.tomb.is_some())
    }

    pub fn write(&mut self, id: ConnId, src: &[u8]) -> WriteResult {
        let result = self.write_inner(id, src);
        if self.slots.get(id.idx()).is_some_and(|s| s.gen == id.gen() && s.conn.is_some()) {
            self.sync_share_waiter(id.idx() as u32, true);
        }
        result
    }

    /// Zero-copy `write`: `fill` writes up to `max` bytes directly into the
    /// connection's send buffer — at most two slices, filled in order like a
    /// `readv` — and returns how many it wrote. A relay can read its upstream
    /// socket straight into the buffer, or a proxy decrypt into it. Results
    /// and Writable arming are as for `write`; an error from `fill` keeps
    /// nothing and is returned as is. `fill` is not called when the
    /// connection cannot take any bytes.
    pub fn write_with<E>(&mut self, id: ConnId, max: usize, fill: impl FnOnce([&mut [u8]; 2]) -> Result<usize, E>) -> Result<WriteResult, E> {
        let result = self.with_conn(id, |c, ctx| c.write_with(max, ctx, fill)).unwrap_or(Ok(WriteResult::Closed));
        if self.slots.get(id.idx()).is_some_and(|s| s.gen == id.gen() && s.conn.is_some()) {
            self.sync_share_waiter(id.idx() as u32, true);
        }
        result
    }

    /// Adapter entry for the uninitialized relay readv path. The fill receives
    /// raw TX capacity and must initialize exactly the bytes it reports; a
    /// socket `read`/`readv` satisfies that. See `Conn::write_with_uninit`.
    ///
    /// # Safety
    ///
    /// `fill` must initialize the first `n` bytes it reports, in window order.
    #[cfg(feature = "tokio")]
    pub(crate) unsafe fn write_with_uninit_for_adapter<E>(
        &mut self,
        id: ConnId,
        max: usize,
        fill: impl FnOnce([&mut [std::mem::MaybeUninit<u8>]; 2]) -> Result<usize, E>,
    ) -> Result<WriteResult, E> {
        let result = self.with_conn(id, |c, ctx| c.write_with_uninit(max, ctx, fill)).unwrap_or(Ok(WriteResult::Closed));
        if self.slots.get(id.idx()).is_some_and(|s| s.gen == id.gen() && s.conn.is_some()) {
            self.sync_share_waiter(id.idx() as u32, true);
        }
        result
    }

    fn write_inner(&mut self, id: ConnId, src: &[u8]) -> WriteResult {
        self.with_conn(id, |c, ctx| c.write(src, ctx)).unwrap_or(WriteResult::Closed)
    }

    #[cfg(feature = "tokio")]
    pub(crate) fn write_for_adapter(&mut self, id: ConnId, src: &[u8]) -> WriteResult {
        self.write(id, src)
    }

    #[cfg(feature = "tokio")]
    pub(crate) fn write_allocation_charge(&mut self, id: ConnId, bytes: usize) -> u64 {
        self.with_conn(id, |c, ctx| c.write_allocation_charge(bytes, ctx.cfg, ctx.budget)).unwrap_or(0)
    }

    /// Free send space (bytes) right now.
    pub fn send_space(&mut self, id: ConnId) -> usize {
        let cfg = self.cfg.clone();
        let Some(s) = self.slots.get_mut(id.idx()) else { return 0 };
        if s.gen != id.gen() {
            return 0;
        }
        s.conn.as_ref().map_or(0, |c| c.send_space(&cfg, &self.budget))
    }

    /// Per-connection send share of the port/peer budget (docs/design/0007).
    #[cfg(feature = "tokio")]
    pub(crate) fn send_share(&self, peer: PeerId) -> u64 {
        self.budget.send_share(peer)
    }

    /// Check write closure without reserving budget or preparing TX backing.
    pub(crate) fn accepts_writes_for_adapter(&self, id: ConnId) -> bool {
        self.slots.get(id.idx()).filter(|s| s.gen == id.gen()).and_then(|s| s.conn.as_ref()).is_some_and(|c| c.accepts_writes())
    }

    /// Bytes in the connection's core send buffer (in-flight plus unsent).
    #[cfg(feature = "tokio")]
    pub(crate) fn tx_queued(&self, id: ConnId) -> Option<usize> {
        let s = self.slots.get(id.idx())?;
        if s.gen != id.gen() {
            return None;
        }
        s.conn.as_ref().map(|c| c.tx_queued_len())
    }

    /// Publish the adapter TX queue length into the connection's send-side
    /// accounting (docs/design/0007 §2.1).
    #[cfg(feature = "tokio")]
    pub(crate) fn set_adapter_tx(&mut self, id: ConnId, bytes: u32) {
        self.with_conn(id, |c, ctx| c.set_adapter_tx(bytes, ctx));
    }

    /// The adapter parked the app writer on the send share
    /// (docs/design/0007 §2.4).
    #[cfg(feature = "tokio")]
    pub(crate) fn note_write_parked(&mut self, id: ConnId) {
        self.with_conn(id, |c, _| c.note_write_parked());
        if self.slots.get(id.idx()).is_some_and(|s| s.gen == id.gen() && s.conn.is_some()) {
            self.sync_share_waiter(id.idx() as u32, true);
        }
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

    #[cfg(test)]
    pub(crate) fn disable_rx_progress(&mut self, id: ConnId) {
        let slot = &mut self.slots[id.idx()];
        assert_eq!(slot.gen, id.gen());
        slot.conn.as_mut().unwrap().disable_rx_progress();
    }

    /// Iterate over all live connections (diagnostics).
    pub fn conn_ids(&self) -> Vec<ConnId> {
        self.slots.iter().enumerate().filter_map(|(i, s)| s.conn.as_ref().map(|_| ConnId::new(i as u32, s.gen))).collect()
    }

    /// Scheduling/container sizes (invariant checks, §14.4 #7).
    pub fn container_sizes(&self) -> (usize, usize, usize) {
        (self.timers.len(), self.pacing.len(), self.table.len())
    }

    /// Active open (test peer only, §0).
    #[cfg(any(test, feature = "test-peer"))]
    pub fn connect(&mut self, now: Instant, iface: IfaceId, peer: PeerId, local: SocketAddr, remote: SocketAddr) -> ConnId {
        let permit = self.budget.global().try_acquire_connection().expect("test peer connection limit");
        assert!(self.budget.peer_conn_add(peer), "test peer connection limit");
        let mtu = self.iface_mtu(iface);
        let (active_memory, stream_memory, rx_progress) = self.try_admit_memory(peer, crate::buf::rx_progress_charge(mtu)).expect("test peer memory limit");
        let (idx, gen) = self.alloc_slot();
        let id = ConnId::new(idx, gen);
        let iss = self.isn(now, remote, local);
        let tso = self.ts_offset(remote, local);
        let mut conn = Conn::new_active(id, iface, peer, local, remote, iss, tso, mtu, &self.cfg, now);
        conn.stream_memory = stream_memory;
        if let Some(credit) = rx_progress {
            conn.set_rx_progress(credit);
        }
        self.slots[idx as usize].conn = Some(Box::new(conn));
        self.slots[idx as usize].permit = Some(permit);
        self.slots[idx as usize].active_memory = Some(active_memory);
        self.table.insert((iface, remote, local), idx);
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

#[cfg(test)]
mod share_waiter_observation {
    use super::*;

    #[test]
    #[ignore = "capacity and scan-cost observation after a real connection burst"]
    fn issue43_waiter_burst_then_one_long_connection() {
        const COUNT: usize = 4096;
        let cfg = StackConfig { accept_backlog: COUNT * 2, min_prefetch: 1 << 20, prefetch_max: 1 << 20, ..StackConfig::default() };
        let link = crate::sim::LinkParams { rate_bps: 0, delay: Duration::ZERO, queue_bytes: 32 << 20, ..Default::default() };
        let mut sim = crate::sim::Sim::new(43003, cfg, StackConfig::default(), link.clone(), link);
        sim.check_invariants = false;
        sim.a.shard.set_budget_limits(512 << 20, 192 << 10, 8);
        for n in 0..COUNT {
            sim.connect_as(PeerId(n as u64 + 1), 10_000 + n as u16, Default::default());
        }
        sim.run_until(sim.now + Duration::from_millis(100));
        assert_eq!(sim.a.accepted.len(), COUNT);
        let ids = sim.a.accepted.clone();
        let data = vec![3; 64 << 10];
        for &id in &ids {
            assert_eq!(sim.a.shard.write(id, &data), WriteResult::Written(data.len()));
            assert_eq!(sim.a.shard.write(id, &[3]), WriteResult::QuotaBlocked);
        }
        let sh = &mut sim.a.shard;
        let peak = sh.share_blocked.capacity();
        assert_eq!(sh.share_blocked.len(), COUNT);
        for &id in &ids[1..] {
            sh.abort(id);
        }
        // abort queues a RST; the slot is reaped only after egress accepts it.
        for _ in 0..COUNT {
            if sh.table.len() == 1 {
                break;
            }
            sh.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted);
        }
        assert_eq!(sh.table.len(), 1);
        sh.retry_share_writers();
        assert_eq!(sh.share_blocked.len(), 1);
        let sparse = sh.share_blocked.capacity();
        let start = std::time::Instant::now();
        for _ in 0..10_000 {
            sh.budget.sender_add(PeerId(u64::MAX));
            sh.budget.sender_del(PeerId(u64::MAX));
            sh.retry_share_writers();
            std::hint::black_box(&sh.share_blocked);
        }
        let scan_ns = start.elapsed().as_nanos() / 10_000;
        sh.abort(ids[0]);
        sh.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted);
        assert!(sh.table.is_empty());
        eprintln!("issue43 waiter capacity: burst={COUNT} peak={peak} sparse={sparse} idle={} sparse_scan_ns={scan_ns}", sh.share_blocked.capacity());
        assert!(sh.share_blocked.is_empty());
        assert_eq!(sh.share_blocked.capacity(), 0);
    }
}

#[cfg(test)]
mod tx_cache_trim_tests {
    use super::*;
    use crate::buf::{TxBuf, TX_BLOCK, TX_BLOCK_CHARGE};

    fn policy() -> TxCacheTrimPolicy {
        TxCacheTrimPolicy {
            check_interval: Duration::from_millis(10),
            warm_after: Duration::from_millis(20),
            cold_after: Duration::from_millis(60),
            warm_large: 2,
            warm_small: 1,
        }
    }

    fn shard() -> (Shard, Arc<GlobalBudget>) {
        let global = GlobalBudget::new(8 << 20);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        sh.set_budget_limits(global.high(), global.high(), 8);
        (sh, global)
    }

    fn cache_large(sh: &mut Shard, blocks: usize) {
        let mut tx = TxBuf::default();
        assert!(tx.push(&mut sh.pool, &mut sh.budget, PeerId(1), &vec![7; blocks * TX_BLOCK]));
        tx.release_all(&mut sh.pool);
        drop(tx);
        assert_eq!(sh.pool.cached(), blocks);
    }

    fn run(sh: &mut Shard, ms: u64) {
        let out = sh.run(Instant::from_millis(ms), &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted);
        assert_eq!(out.packets, 0);
        assert!(!out.more, "a future maintenance deadline must not force a busy loop");
    }

    #[test]
    fn trim_is_disabled_by_default_then_complete_idle_releases_pool() {
        let (mut sh, global) = shard();
        cache_large(&mut sh, 4);
        let before = global.reserved();
        assert_eq!(sh.next_deadline(), None);
        run(&mut sh, 1000);
        assert_eq!(global.reserved(), before);
        assert_eq!(sh.stats.tx_cache_maintenance_checks, 0);
        sh.set_tx_cache_trim(Some(policy())).unwrap();
        assert_eq!(sh.next_deadline(), Some(Instant::ZERO), "run once to arm without reading a clock");
        run(&mut sh, 1000);
        assert_eq!(sh.next_deadline(), Some(Instant::from_millis(1010)));
        run(&mut sh, 1010);
        run(&mut sh, 1019);
        assert_eq!(sh.pool.cached(), 4);
        run(&mut sh, 1020);
        assert_eq!(sh.pool.cached(), 2);
        assert!(sh.pool.descriptor_bytes() != 0);
        assert_eq!(sh.next_deadline(), Some(Instant::from_millis(1030)));
        run(&mut sh, 1060);
        assert!(!sh.pool.has_retained());
        assert_eq!(sh.next_deadline(), None);
        assert_eq!(global.cached_bytes(), 0);
        assert_eq!(global.reserved(), 0);
        assert_eq!(sh.stats.tx_cache_idle_trims, 2);
        assert_eq!(sh.stats.tx_cache_idle_trimmed_blocks, 4);
        assert_eq!(sh.stats.tx_cache_idle_trimmed_bytes, before);
        assert_eq!(sh.stats.cache_reclaims, 0);
    }

    #[test]
    fn block_reuse_resets_cold_clock_without_postponing_warm_decay() {
        let (mut sh, global) = shard();
        sh.set_tx_cache_trim(Some(policy())).unwrap();
        cache_large(&mut sh, 4);
        run(&mut sh, 0);
        run(&mut sh, 10);
        let mut tx = TxBuf::default();
        assert!(tx.push(&mut sh.pool, &mut sh.budget, PeerId(2), &vec![9; TX_BLOCK]));
        tx.consume(&mut sh.pool, TX_BLOCK);
        drop(tx);
        run(&mut sh, 19);
        assert_eq!(sh.next_deadline(), Some(Instant::from_millis(20)));
        run(&mut sh, 20);
        assert_eq!(sh.pool.cached(), 2);
        run(&mut sh, 30);
        run(&mut sh, 39);
        assert_eq!(sh.pool.cached(), 2);
        run(&mut sh, 40);
        assert_eq!(sh.pool.cached(), 2);
        run(&mut sh, 80);
        assert_eq!(global.reserved(), 0);
        assert_eq!(sh.next_deadline(), None);
    }

    #[test]
    fn small_traffic_does_not_keep_historical_large_blocks() {
        let (mut sh, global) = shard();
        sh.set_tx_cache_trim(Some(policy())).unwrap();
        cache_large(&mut sh, 4);
        run(&mut sh, 0);
        for ms in (10..=80).step_by(10) {
            let mut tx = TxBuf::default();
            assert!(tx.push(&mut sh.pool, &mut sh.budget, PeerId(2), &[9; 64]));
            tx.consume(&mut sh.pool, 64);
            drop(tx);
            run(&mut sh, ms);
            if ms == 20 {
                assert_eq!(sh.tx_cache_snapshot().large_cached, 2);
            }
        }
        let cache = sh.tx_cache_snapshot();
        assert_eq!(cache.large_cached, 0);
        assert_eq!(cache.large_capacity, 0);
        assert_eq!(cache.small_cached, 1);
        run(&mut sh, 140);
        assert_eq!(global.reserved(), 0);
        assert_eq!(sh.next_deadline(), None);
    }

    #[test]
    fn low_rate_large_block_reuse_releases_only_unused_history() {
        for warm_large in [0, 2] {
            let (mut sh, global) = shard();
            let mut p = policy();
            p.warm_large = warm_large;
            sh.set_tx_cache_trim(Some(p)).unwrap();
            cache_large(&mut sh, 4);
            run(&mut sh, 0);
            for ms in (10..=200).step_by(10) {
                let mut tx = TxBuf::default();
                assert!(tx.push(&mut sh.pool, &mut sh.budget, PeerId(2), &vec![9; TX_BLOCK]));
                assert_eq!(tx.slices(0, TX_BLOCK)[0], &vec![9; TX_BLOCK]);
                tx.consume(&mut sh.pool, TX_BLOCK);
                drop(tx);
                run(&mut sh, ms);
                if ms >= 20 {
                    assert_eq!(sh.tx_cache_snapshot().large_cached, warm_large.max(1));
                }
            }
            // Repeated maintenance must not discard and reallocate the hot tail.
            assert_eq!(sh.stats.tx_cache_idle_trimmed_blocks, (4 - warm_large.max(1)) as u64);
            run(&mut sh, 260);
            assert_eq!(global.reserved(), 0);
            assert_eq!(sh.next_deadline(), None);
        }
    }

    #[test]
    fn sustained_use_preserves_cache_above_warm_target() {
        let (mut sh, _) = shard();
        sh.set_tx_cache_trim(Some(policy())).unwrap();
        cache_large(&mut sh, 4);
        run(&mut sh, 0);
        for ms in (10..=100).step_by(10) {
            let mut tx = TxBuf::default();
            assert!(tx.push(&mut sh.pool, &mut sh.budget, PeerId(2), &vec![9; 4 * TX_BLOCK]));
            tx.release_all(&mut sh.pool);
            drop(tx);
            run(&mut sh, ms);
            assert_eq!(sh.tx_cache_snapshot().large_cached, 4);
        }
        assert_eq!(sh.stats.tx_cache_idle_trimmed_blocks, 0);
    }

    #[test]
    fn descriptor_only_idle_trim_leaves_active_tx_intact() {
        let (mut sh, global) = shard();
        sh.set_tx_cache_trim(Some(policy())).unwrap();
        cache_large(&mut sh, 4);
        let mut tx = TxBuf::default();
        let input = vec![9; 4 * TX_BLOCK];
        assert!(tx.push(&mut sh.pool, &mut sh.budget, PeerId(2), &input));
        assert_eq!(sh.pool.cached(), 0);
        let backing = sh.pool.descriptor_bytes();
        assert!(backing != 0);
        let before = global.reserved();
        run(&mut sh, 0);
        assert_eq!(sh.next_deadline(), Some(Instant::from_millis(10)));
        run(&mut sh, 20);
        assert_eq!(sh.pool.descriptor_bytes(), 0);
        assert_eq!(global.reserved(), before - backing);
        assert!(global.reserved() >= 4 * TX_BLOCK_CHARGE);
        assert_eq!(tx.len(), input.len());
        assert_eq!(tx.slices(0, TX_BLOCK)[0], &input[..TX_BLOCK]);
        assert_eq!(sh.stats.tx_cache_idle_trimmed_blocks, 0);
        assert_eq!(sh.stats.tx_cache_idle_trimmed_bytes, backing);
        assert_eq!(sh.next_deadline(), None);
        drop(tx);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn pressure_reclaim_ignores_cooldown_and_does_not_become_idle_trim() {
        let (mut sh, global) = shard();
        sh.set_tx_cache_trim(Some(policy())).unwrap();
        cache_large(&mut sh, 4);
        let before = global.reserved();
        run(&mut sh, 0);
        let observed = global.cache_reclaim_epoch();
        global.request_cache_reclaim();
        assert!(global.cache_reclaim_epoch() > observed);
        run(&mut sh, 1);
        assert_eq!(global.reserved(), 0);
        assert_eq!(sh.stats.cache_reclaims, 1);
        assert_eq!(sh.stats.cache_reclaimed_bytes, before);
        assert_eq!(sh.stats.tx_cache_idle_trims, 0);
        assert_eq!(sh.next_deadline(), None);
    }

    #[test]
    fn disabling_trim_cancels_deadline_without_releasing_cache() {
        let (mut sh, global) = shard();
        sh.set_tx_cache_trim(Some(policy())).unwrap();
        cache_large(&mut sh, 4);
        run(&mut sh, 0);
        let before = global.reserved();
        sh.set_tx_cache_trim(None).unwrap();
        assert_eq!(sh.next_deadline(), None);
        run(&mut sh, 1000);
        assert_eq!(sh.pool.cached(), 4);
        assert_eq!(global.reserved(), before);
        assert_eq!(sh.stats.tx_cache_idle_trims, 0);
    }

    #[test]
    fn invalid_policy_does_not_enable_tracking_or_deadline() {
        let (mut sh, _) = shard();
        cache_large(&mut sh, 4);
        let mut invalid = policy();
        invalid.check_interval = Duration::ZERO;
        assert!(sh.set_tx_cache_trim(Some(invalid)).is_err());
        invalid = policy();
        invalid.cold_after = Duration::ZERO;
        assert!(sh.set_tx_cache_trim(Some(invalid)).is_err());
        invalid = policy();
        invalid.warm_after = invalid.cold_after + Duration::from_millis(1);
        assert!(sh.set_tx_cache_trim(Some(invalid)).is_err());
        assert_eq!(sh.next_deadline(), None);
        assert_eq!(sh.pool.take_activity(), [false; 2]);
    }
}

#[cfg(test)]
mod admission_tests {
    use super::*;
    use crate::wire::TcpOptions;

    #[test]
    fn stateless_replies_obey_budget_and_release_on_send_or_iface_removal() {
        let global = GlobalBudget::new(STATELESS_CHARGE * 2);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        sh.set_budget_limits(global.high(), global.high(), 4);
        let removed = sh.add_iface(IfaceConfig::default());
        let active = sh.add_iface(IfaceConfig::default());
        sh.hdr[..3].copy_from_slice(&[1, 2, 3]);
        assert!(sh.push_stateless(removed, PeerId(1), 3));
        assert!(sh.push_stateless(active, PeerId(2), 3));
        assert!(!sh.push_stateless(active, PeerId(2), 3));
        assert_eq!(global.reserved(), STATELESS_CHARGE * 2);

        sh.remove_iface(removed);
        assert_eq!(global.reserved(), STATELESS_CHARGE);
        assert_eq!(sh.stateless.len(), 1);

        let mut full = |_: IfaceId, _: &OutPacket<'_>| SendResult::Full;
        sh.run(Instant::ZERO, &mut full);
        assert_eq!(global.reserved(), STATELESS_CHARGE);

        sh.egress_released(active);
        let mut sent = Vec::new();
        sh.run(Instant::ZERO, &mut |iface: IfaceId, pkt: &OutPacket<'_>| {
            sent.push((iface, pkt.peer, pkt.header.to_vec()));
            SendResult::Accepted
        });
        assert_eq!(sent, vec![(active, PeerId(2), vec![1, 2, 3])]);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn stateless_burst_releases_high_water_backing_after_drain() {
        let global = GlobalBudget::new(STATELESS_CHARGE * 128);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        sh.set_budget_limits(global.high(), global.high(), 4);
        let iface = sh.add_iface(IfaceConfig::default());
        for _ in 0..128 {
            assert!(sh.push_stateless(iface, PeerId(1), 40));
        }
        assert!(sh.stateless.capacity() >= 128);
        assert_eq!(global.reserved(), global.high());
        let mut accepted = |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted;
        sh.run(Instant::ZERO, &mut accepted);
        assert!(sh.stateless.is_empty());
        assert_eq!(sh.stateless.capacity(), 0);
        assert_eq!(global.reserved(), 0);
    }

    fn tombstone_container_capacity_bytes(sh: &Shard) -> u64 {
        // HashMap::capacity is its usable element count; recover the power-of-
        // two bucket allocation and include control bytes. The remaining
        // vectors report their allocated element capacity directly.
        let buckets = if sh.table.capacity() == 0 { 0 } else { (sh.table.capacity() + 1).next_power_of_two() };
        let tuple_bytes = buckets * (std::mem::size_of::<(Key, u32)>() + 1) + if buckets == 0 { 0 } else { 16 };
        (sh.slots.capacity() * std::mem::size_of::<Slot>()
            + sh.retired_generations.capacity() * std::mem::size_of::<u32>()
            + sh.free.capacity() * std::mem::size_of::<u32>()
            + tuple_bytes
            + sh.timers.backing_capacity_bytes()
            + sh.pacing.backing_capacity_bytes()
            + sh.tw_ready.capacity() * std::mem::size_of::<(u32, u32)>()) as u64
    }

    #[test]
    fn tombstone_slot_reservation_covers_fixed_containers() {
        // Hash table load factor and vector capacity slack are rounded up.
        let tuple_index = (std::mem::size_of::<(Key, u32)>() + 1).div_ceil(7) * 8;
        let fixed =
            std::mem::size_of::<Slot>() + tuple_index + std::mem::size_of::<(Instant, u32)>() + std::mem::size_of::<u32>() + std::mem::size_of::<(u32, u32)>();
        eprintln!(
            "TIME_WAIT fixed container estimate: {fixed} bytes (slot={}, tuple={}, timer={}, free={}, ready={}), reserved: {} bytes",
            std::mem::size_of::<Slot>(),
            tuple_index,
            std::mem::size_of::<(Instant, u32)>(),
            std::mem::size_of::<u32>(),
            std::mem::size_of::<(u32, u32)>(),
            crate::budget::TIME_WAIT_SLOT_BYTES
        );
        assert!(fixed < crate::budget::TIME_WAIT_SLOT_BYTES as usize, "fixed bytes {fixed}");
    }

    #[test]
    fn connection_limit_is_shared_across_shards_and_released_on_iface_removal() {
        let global = GlobalBudget::with_connection_limits(1 << 20, 1, 2);
        let mut a = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        let mut b = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        let ai = a.add_iface(IfaceConfig::default());
        let bi = b.add_iface(IfaceConfig::default());
        let remote = SocketAddr::from(([10, 0, 0, 2], 40000));
        let local = SocketAddr::from(([10, 0, 0, 1], 443));
        let syn = TcpHeader {
            src_port: remote.port(),
            dst_port: local.port(),
            seq: Seq(100),
            ack: Seq(0),
            flags: SYN,
            window: 65535,
            opts: TcpOptions::default(),
            data_off: 20,
        };

        a.handle_syn(Instant::ZERO, ai, PeerId(1), remote, local, &syn);
        b.handle_syn(Instant::ZERO, bi, PeerId(2), remote, local, &syn);
        assert_eq!(a.conn_count(), 1);
        assert_eq!(b.conn_count(), 0);
        assert_eq!(global.connection_counts(), (1, 1));
        assert_eq!(b.stats().syn_dropped, 1);

        a.remove_iface(ai);
        assert_eq!(global.connection_counts(), (0, 0));
        b.handle_syn(Instant::ZERO, bi, PeerId(2), remote, local, &syn);
        assert_eq!(b.conn_count(), 1);
        drop(b);
        assert_eq!(global.connection_counts(), (0, 0));
    }

    struct AdmitRig {
        global: Arc<GlobalBudget>,
        sh: Shard,
        iface: IfaceId,
    }

    impl AdmitRig {
        fn new(port: u64, peer: u64) -> Self {
            let global = GlobalBudget::new(8 << 20);
            let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
            sh.set_budget_limits(port, peer, 64);
            let iface = sh.add_iface(IfaceConfig::default());
            AdmitRig { global, sh, iface }
        }

        fn syn(&mut self, ms: u64, peer: u64, port: u16) {
            let local = SocketAddr::from(([10, 0, 0, 1], 443));
            let h =
                TcpHeader { src_port: port, dst_port: 443, seq: Seq(100), ack: Seq(0), flags: SYN, window: 65535, opts: TcpOptions::default(), data_off: 20 };
            self.sh.handle_syn(Instant::from_millis(ms), self.iface, PeerId(peer), SocketAddr::from(([10, 0, 0, 2], port)), local, &h);
        }

        fn run(&mut self, ms: u64) {
            self.sh.run(Instant::from_millis(ms), &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted);
        }

        /// Take TX blocks for `peer` until its share refuses one.
        fn fill(&mut self, peer: u64) -> Vec<MemoryLease> {
            let memory = self.sh.memory_handle(PeerId(peer));
            std::iter::from_fn(|| memory.try_allocate_kind(crate::buf::TX_BLOCK_CHARGE, crate::budget::AllocationKind::TxBlock)).collect()
        }

        fn block_fits(&mut self, peer: u64) -> bool {
            self.sh.memory_handle(PeerId(peer)).try_allocate_kind(crate::buf::TX_BLOCK_CHARGE, crate::budget::AllocationKind::TxBlock).is_some()
        }

        fn debt(&self) -> [u64; 3] {
            self.sh.budget.admission_debt()
        }
    }

    #[test]
    fn refused_syn_leaves_an_admission_debt_until_admitted() {
        let mut r = AdmitRig::new(819_200, 819_200);
        // TX blocks above the admission line, as busy senders keep them.
        let mut blocks = r.fill(1);
        let admit_line = 819_200 - r.global.headroom(819_200, crate::budget::Tier::Admit);
        assert!(r.sh.budget.physical_used() > admit_line);

        r.syn(0, 1, 40000);
        assert_eq!(r.sh.conn_count(), 0);
        assert_eq!(r.sh.stats().admission_debts, 1);
        let owed = r.debt()[1];
        assert!(owed >= ACTIVE_STATE_BYTES, "{owed}");
        // A retransmit refused by the same level refreshes, not adds.
        r.syn(1000, 1, 40000);
        assert_eq!(r.sh.stats().admission_debts, 1);
        assert_eq!(r.debt()[1], owed);
        // A released block is not taken back while the debt is owed.
        blocks.pop();
        assert!(!r.block_fits(1));
        // The next retransmit gets in and repays the debt.
        blocks.pop();
        r.syn(3000, 1, 40000);
        assert_eq!(r.sh.conn_count(), 1);
        assert_eq!(r.debt(), [0; 3]);
    }

    #[test]
    fn issue37_optional_credit_never_refuses_admission_or_adds_debt() {
        for level in [Level::Global, Level::Port, Level::Peer] {
            let limit = 1 << 20;
            let high = if level == Level::Global { limit } else { 2 * limit };
            let port = if level == Level::Peer { high } else { limit };
            let global = GlobalBudget::new(high);
            let mut sh = Shard::with_budget(StackConfig::default(), global.clone());
            sh.set_budget_limits(port, limit, 16);
            sh.reserve_stream_state(1024);
            let iface = sh.add_iface(IfaceConfig::default());
            let charge = crate::buf::rx_progress_charge(1500);
            let admission = ACTIVE_STATE_BYTES + 1024;
            let peer = PeerId(1);
            let ceiling = limit - global.headroom(limit, crate::budget::Tier::Admit);
            let key = (iface, "10.0.0.2:40000".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
            // Enough state, but one byte short of the optional credit.
            let blocker = sh.memory_handle(peer).try_allocate(ceiling - admission - charge + 1).unwrap();
            let before = global.reserved();
            let admitted = sh.admit_memory(Instant::ZERO, key, peer).unwrap();
            assert!(admitted.2.is_none());
            assert_eq!(global.reserved(), before + admission);
            assert_eq!(sh.budget.rx_progress_bytes(), [0; 3]);
            assert_eq!(sh.budget.admission_debt(), [0; 3]);
            drop((admitted, blocker));
            // State itself still fails transactionally, owing only state bytes.
            let blocker = sh.memory_handle(peer).try_allocate(ceiling - admission + 1).unwrap();
            let before = global.reserved();
            assert!(sh.admit_memory(Instant::ZERO, key, peer).is_none());
            assert_eq!(global.reserved(), before);
            let index = level as usize;
            assert_eq!(sh.budget.admission_debt()[index], admission);
            drop(blocker);
            let admitted = sh.admit_memory(Instant::from_millis(1000), key, peer).unwrap();
            assert!(admitted.2.is_some());
            assert_eq!(global.reserved(), admission + charge);
            assert_eq!(sh.budget.admission_debt(), [0; 3]);
            drop(admitted);
            drop(sh);
            assert_eq!(global.reserved(), 0);
        }
    }

    #[test]
    fn admission_debt_follows_the_level_that_refused_the_retransmit() {
        // Peer shares of 512 KiB inside an 819 KiB port.
        let mut r = AdmitRig::new(819_200, 512 << 10);
        // Peer 2 fills the port: the first SYN from peer 1 is refused there.
        let mut other = r.fill(2);
        let mut own = r.fill(1);
        r.syn(0, 1, 40000);
        assert!(r.debt()[1] > 0 && r.debt()[2] == 0, "{:?}", r.debt());
        // Peer 2 goes idle and peer 1 fills its own share: the retransmit is
        // now refused by the peer level, which must owe the room.
        other.clear();
        own.extend(r.fill(1));
        r.syn(1000, 1, 40000);
        assert!(r.debt()[1] == 0 && r.debt()[2] > 0, "{:?}", r.debt());
        assert_eq!(r.sh.stats().admission_debts_moved, 1);
        // Peer 1 frees a block; the debt keeps it from being taken back, so
        // the next retransmit fits the peer share.
        own.pop();
        assert!(!r.block_fits(1));
        own.pop();
        r.syn(3000, 1, 40000);
        assert_eq!(r.sh.conn_count(), 1);
        assert_eq!(r.debt(), [0; 3]);
    }

    #[test]
    fn admission_debt_outlasts_backed_off_retransmits() {
        // SYN retransmits at 0, 1, 3, 7 and 15 s. Meanwhile the level keeps
        // releasing TX blocks, and send records (not held back) retake the
        // room, so the first four attempts fail. The debt must still hold
        // TX blocks back when each later retransmit arrives.
        let mut r = AdmitRig::new(819_200, 819_200);
        let _blocks = r.fill(1);
        let records = r.sh.memory_handle(PeerId(1));
        let record = || records.try_allocate_kind(4096, crate::budget::AllocationKind::SendRecord).unwrap();
        let mut held: std::collections::VecDeque<_> = (0..4).map(|_| record()).collect();
        let mut t = 0;
        for next in [1000, 3000, 7000, 15000] {
            r.syn(t, 1, 40000);
            assert_eq!(r.sh.conn_count(), 0, "at {t} ms");
            while t + 500 < next {
                t += 500;
                // An ACK frees a record, a new segment takes one again.
                held.pop_front();
                held.push_back(record());
                assert!(!r.block_fits(1), "TX block refilled at {t} ms");
                r.run(t);
            }
            t = next - 1;
            r.run(t);
            assert!(r.debt()[1] > 0, "debt gone before the retransmit at {next} ms");
            t = next;
        }
        held.clear();
        drop(_blocks);
        r.syn(t, 1, 40000);
        assert_eq!(r.sh.conn_count(), 1);
        assert_eq!(r.debt(), [0; 3]);
        assert_eq!(r.sh.stats().admission_debts_stalled, 0);
    }

    #[test]
    fn admission_debt_is_lifted_when_the_level_stops_releasing() {
        // Holders that never release (zero window, stalled host) cannot make
        // room. After a second without releases the debt stops holding back
        // the other senders; a peer that never retries expires later.
        let mut r = AdmitRig::new(819_200, 819_200);
        let mut blocks = r.fill(1);
        blocks.pop();
        blocks.pop();
        blocks.pop();
        let _stalled = r.fill(2);
        r.syn(0, 1, 40000);
        assert!(r.debt()[1] > 0);
        blocks.pop();
        assert!(!r.block_fits(1), "held back while releases may still come");
        r.run(500);
        assert!(r.debt()[1] > 0);
        assert!(r.sh.next_deadline().is_some_and(|d| d <= Instant::from_millis(1500)));
        r.run(1500);
        assert_eq!(r.debt(), [0; 3]);
        assert_eq!(r.sh.stats().admission_debts_stalled, 1);
        assert!(r.block_fits(1), "a stalled debt no longer holds TX blocks back");
        r.run(4000);
        assert_eq!(r.sh.stats().admission_debts_expired, 1);
        drop(blocks);
    }

    #[test]
    fn rfc6191_reuse_requires_timestamp_or_fin_sequence_progress() {
        let local = SocketAddr::from(([10, 0, 0, 1], 443));
        let remote = SocketAddr::from(([10, 0, 0, 2], 40000));
        let tomb = TimeWaitState {
            iface: IfaceId(0),
            peer: PeerId(1),
            local,
            remote,
            snd_seq: Seq(300),
            rcv_seq: Seq(201),
            last_fin_seq: Seq(200),
            ts_ok: true,
            ts_recent: 1000,
            ts_offset: 0,
            expires: Instant::from_millis(60_000),
            pending_ack: false,
        };
        let mut syn = TcpHeader {
            src_port: remote.port(),
            dst_port: local.port(),
            seq: Seq(50),
            ack: Seq(0),
            flags: SYN,
            window: 65535,
            opts: TcpOptions::default(),
            data_off: 20,
        };
        syn.opts.ts = Some((1001, 0));
        assert!(tomb.can_reuse(&syn, true));
        syn.opts.ts = Some((1000, 0));
        assert!(!tomb.can_reuse(&syn, true));
        syn.seq = Seq(201);
        assert!(tomb.can_reuse(&syn, true));
        syn.opts.ts = Some((999, 0));
        assert!(!tomb.can_reuse(&syn, true));
        syn.opts.ts = None;
        assert!(tomb.can_reuse(&syn, true));
        syn.seq = Seq(200);
        assert!(!tomb.can_reuse(&syn, true));
        syn.flags = SYN | ACK;
        assert!(!tomb.can_reuse(&syn, true));

        let no_ts = TimeWaitState { ts_ok: false, ..tomb };
        syn.flags = SYN;
        syn.opts.ts = Some((1, 0));
        assert!(no_ts.can_reuse(&syn, true));
        assert!(!no_ts.can_reuse(&syn, false));
        syn.seq = Seq(0);
        let wrap = TimeWaitState { last_fin_seq: Seq(u32::MAX), ts_ok: false, ..tomb };
        syn.opts.ts = None;
        assert!(wrap.can_reuse(&syn, false));
    }

    #[test]
    fn time_wait_reuse_keeps_old_state_when_active_admission_fails() {
        let global = GlobalBudget::with_connection_limits(1 << 20, 1, 2);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        let iface = sh.add_iface(IfaceConfig::default());
        let local = SocketAddr::from(([10, 0, 0, 1], 443));
        let remote = SocketAddr::from(([10, 0, 0, 2], 40000));
        let (idx, old_gen) = sh.alloc_slot();
        let mut permit = global.try_acquire_connection().unwrap();
        permit.to_time_wait();
        sh.slots[idx as usize].permit = Some(permit);
        sh.slots[idx as usize].tomb = Some(TimeWaitState {
            iface,
            peer: PeerId(1),
            local,
            remote,
            snd_seq: Seq(300),
            rcv_seq: Seq(201),
            last_fin_seq: Seq(200),
            ts_ok: true,
            ts_recent: 1000,
            ts_offset: 0,
            expires: Instant::from_millis(60_000),
            pending_ack: false,
        });
        sh.slots[idx as usize].sched.time_wait = true;
        sh.time_wait_n = 1;
        sh.table.insert((iface, remote, local), idx);
        sh.timers.set(idx, Instant::from_millis(60_000));
        let syn = TcpHeader {
            src_port: remote.port(),
            dst_port: local.port(),
            seq: Seq(50),
            ack: Seq(0),
            flags: SYN,
            window: 65535,
            opts: TcpOptions { ts: Some((1001, 0)), ..Default::default() },
            data_off: 20,
        };

        let other_active = global.try_acquire_connection().unwrap();
        sh.input_existing(Instant::from_millis(1), idx, PeerId(1), &syn, IngressPayload::Borrowed(&[]));
        assert!(sh.slots[idx as usize].tomb.is_some());
        assert_eq!(sh.slots[idx as usize].gen, old_gen);
        assert_eq!(global.connection_counts(), (1, 2));
        drop(other_active);

        let fin = TcpHeader { seq: Seq(200), flags: FIN | ACK, ..syn };
        sh.input_existing(Instant::from_millis(2), idx, PeerId(1), &fin, IngressPayload::Borrowed(&[]));
        assert!(sh.slots[idx as usize].tomb.as_ref().unwrap().pending_ack);
        let mut refused = |_: IfaceId, _: &OutPacket<'_>| SendResult::Full;
        sh.run(Instant::from_millis(2), &mut refused);
        assert!(sh.slots[idx as usize].tomb.as_ref().unwrap().pending_ack);
        sh.egress_released(iface);
        let mut accepted = |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted;
        sh.run(Instant::from_millis(2), &mut accepted);
        assert!(!sh.slots[idx as usize].tomb.as_ref().unwrap().pending_ack);

        sh.input_existing(Instant::from_millis(3), idx, PeerId(1), &syn, IngressPayload::Borrowed(&[]));
        assert!(sh.slots[idx as usize].conn.is_some());
        assert_ne!(sh.slots[idx as usize].gen, old_gen);
        assert_eq!(sh.time_wait_n, 0);
        assert_eq!(global.connection_counts(), (1, 1));
        sh.remove_iface(iface);
        assert_eq!(global.connection_counts(), (0, 0));
    }

    #[test]
    fn full_time_wait_table_rejects_new_tuple_until_expiry() {
        let global = GlobalBudget::with_connection_limits(1 << 20, 2, 1);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        let iface = sh.add_iface(IfaceConfig::default());
        let local = SocketAddr::from(([10, 0, 0, 1], 443));
        let remote = SocketAddr::from(([10, 0, 0, 2], 40000));
        let (idx, _) = sh.alloc_slot();
        let mut permit = global.try_acquire_connection().unwrap();
        permit.to_time_wait();
        sh.slots[idx as usize].permit = Some(permit);
        sh.slots[idx as usize].tomb = Some(TimeWaitState {
            iface,
            peer: PeerId(1),
            local,
            remote,
            snd_seq: Seq(300),
            rcv_seq: Seq(201),
            last_fin_seq: Seq(200),
            ts_ok: false,
            ts_recent: 0,
            ts_offset: 0,
            expires: Instant::from_millis(60_000),
            pending_ack: false,
        });
        sh.slots[idx as usize].sched.time_wait = true;
        sh.time_wait_n = 1;
        sh.table.insert((iface, remote, local), idx);
        sh.timers.set(idx, Instant::from_millis(60_000));

        let other = SocketAddr::from(([10, 0, 0, 2], 40001));
        let syn = TcpHeader {
            src_port: other.port(),
            dst_port: local.port(),
            seq: Seq(1),
            ack: Seq(0),
            flags: SYN,
            window: 65535,
            opts: TcpOptions::default(),
            data_off: 20,
        };
        sh.handle_syn(Instant::ZERO, iface, PeerId(1), other, local, &syn);
        assert!(sh.slots[idx as usize].tomb.is_some());
        assert_eq!(sh.conn_count(), 1);
        assert_eq!(global.connection_counts(), (0, 1));

        let mut accepted = |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted;
        sh.run(Instant::from_millis(60_001), &mut accepted);
        assert_eq!(sh.conn_count(), 0);
        assert_eq!(global.connection_counts(), (0, 0));
        sh.handle_syn(Instant::from_millis(60_001), iface, PeerId(1), other, local, &syn);
        assert_eq!(global.connection_counts(), (1, 1));
    }

    #[test]
    fn idle_high_water_reclaims_slots_without_reusing_old_connection_ids() {
        let global = GlobalBudget::with_resource_limits(8 << 20, 4096, 4096, 4 << 20);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        let iface = sh.add_iface(IfaceConfig::default());
        let local = SocketAddr::from(([10, 0, 0, 1], 443));
        let mut old_id = None;
        for i in 0..2100u16 {
            let (idx, gen) = sh.alloc_slot();
            if i == 0 {
                old_id = Some(ConnId::new(idx, gen));
            }
            let remote = SocketAddr::from(([10, 0, 0, 2], 10_000 + i));
            let mut permit = global.try_acquire_connection().unwrap();
            permit.to_time_wait();
            sh.slots[idx as usize].permit = Some(permit);
            sh.slots[idx as usize].tomb = Some(TimeWaitState {
                iface,
                peer: PeerId(1),
                local,
                remote,
                snd_seq: Seq(300),
                rcv_seq: Seq(201),
                last_fin_seq: Seq(200),
                ts_ok: false,
                ts_recent: 0,
                ts_offset: 0,
                expires: Instant::from_millis(1),
                pending_ack: false,
            });
            sh.slots[idx as usize].sched.time_wait = true;
            sh.time_wait_n += 1;
            sh.table.insert((iface, remote, local), idx);
            sh.timers.set(idx, Instant::from_millis(1));
        }
        assert!(sh.slots.capacity() > IDLE_SLOT_RECLAIM_THRESHOLD);
        let mut accepted = |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted;
        sh.run(Instant::from_millis(2), &mut accepted);
        assert!(sh.table.is_empty());
        assert_eq!(sh.slots.capacity(), 0);
        assert_eq!(sh.retired_generations.len(), 2100);
        assert_eq!(global.connection_counts(), (0, 0));
        assert_eq!(global.reserved(), sh.retained_metadata.total_bytes());
        let (idx, gen) = sh.alloc_slot();
        assert_eq!(idx, 0);
        assert_ne!(ConnId::new(idx, gen), old_id.unwrap());
        for _ in 1..2050 {
            sh.alloc_slot();
        }
        sh.reclaim_idle_containers();
        assert_eq!(sh.retired_generations.len(), 2100, "smaller idle cycle lost older slot generations");
        for _ in 0..2050 {
            sh.alloc_slot();
        }
        let (idx, gen) = sh.alloc_slot();
        assert_eq!(idx, 2050);
        assert_eq!(gen, 2, "older high-water slot must not restart at generation 1");
        drop(sh);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn tuple_table_shrinks_while_one_time_wait_remains() {
        let global = GlobalBudget::with_resource_limits(8 << 20, 8192, 8192, 4 << 20);
        let mut sh = Shard::with_budget(StackConfig::default(), global.clone());
        let iface = sh.add_iface(IfaceConfig::default());
        let local = SocketAddr::from(([10, 0, 0, 1], 443));
        for i in 0..6000u16 {
            let (idx, _) = sh.alloc_slot();
            let remote = SocketAddr::from(([10, 0, 0, 2], 10_000 + i));
            let mut permit = global.try_acquire_connection().unwrap();
            permit.to_time_wait();
            sh.slots[idx as usize].permit = Some(permit);
            sh.slots[idx as usize].tomb = Some(TimeWaitState {
                iface,
                peer: PeerId(1),
                local,
                remote,
                snd_seq: Seq(300),
                rcv_seq: Seq(201),
                last_fin_seq: Seq(200),
                ts_ok: false,
                ts_recent: 0,
                ts_offset: 0,
                expires: Instant::from_millis(if i == 5999 { 60_000 } else { 1 }),
                pending_ack: false,
            });
            sh.slots[idx as usize].sched.time_wait = true;
            sh.time_wait_n += 1;
            sh.table.insert((iface, remote, local), idx);
            sh.timers.set(idx, sh.slots[idx as usize].tomb.as_ref().unwrap().expires);
            assert!(tombstone_container_capacity_bytes(&sh) <= global.reserved() + 8192, "growing tombstone containers exceeded slot permits at {i}");
        }
        let peak = sh.table.capacity();
        assert!(peak > 4096);
        let mut accepted = |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted;
        sh.run(Instant::from_millis(2), &mut accepted);
        assert_eq!(sh.table.len(), 1);
        assert_eq!(sh.time_wait_n, 1);
        assert!(sh.table.capacity() < peak / 4);
        assert_eq!(sh.table.get(&(iface, SocketAddr::from(([10, 0, 0, 2], 15_999)), local)), Some(&5999));
        assert_eq!(sh.retained_metadata.free_slot_bytes(), 5999 * crate::budget::RETAINED_SLOT_BYTES);
        assert!(tombstone_container_capacity_bytes(&sh) <= global.reserved() + 8192, "sparse slot high-water mark escaped the retained budget");
        let retained_before_reuse = sh.retained_metadata.free_slot_bytes();
        let newcomer = SocketAddr::from(([10, 0, 0, 3], 40_001));
        let syn = TcpHeader {
            src_port: newcomer.port(),
            dst_port: local.port(),
            seq: Seq(1),
            ack: Seq(0),
            flags: SYN,
            window: 65_535,
            opts: TcpOptions::default(),
            data_off: 20,
        };
        sh.handle_syn(Instant::from_millis(3), iface, PeerId(1), newcomer, local, &syn);
        let reused = *sh.table.get(&(iface, newcomer, local)).unwrap();
        assert_eq!(sh.retained_metadata.free_slot_bytes(), retained_before_reuse - crate::budget::RETAINED_SLOT_BYTES);
        sh.abort(ConnId::new(reused, sh.slots[reused as usize].gen));
        sh.run(Instant::from_millis(3), &mut accepted);
        assert_eq!(sh.retained_metadata.free_slot_bytes(), retained_before_reuse);
        sh.run(Instant::from_millis(60_001), &mut accepted);
        assert!(sh.table.is_empty());
        assert_eq!(global.connection_counts(), (0, 0));
        assert_eq!(global.reserved(), sh.retained_metadata.total_bytes());
        drop(sh);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn hundred_twenty_thousand_time_wait_slots_remain_charged_after_expiry() {
        const COUNT: u32 = 120_000;
        let global = GlobalBudget::with_resource_limits(256 << 20, COUNT as u64, COUNT as u64, 128 << 20);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        let iface = sh.add_iface(IfaceConfig::default());
        let local = SocketAddr::from(([10, 0, 0, 1], 443));
        for i in 0..COUNT {
            let (idx, _) = sh.alloc_slot();
            let remote = SocketAddr::from(([10, 1, (i >> 16) as u8, (i >> 8) as u8], 10_000 + (i & 255) as u16));
            let mut permit = global.try_acquire_connection().unwrap();
            permit.to_time_wait();
            sh.slots[idx as usize].permit = Some(permit);
            let expires = Instant::from_millis(if i + 1 == COUNT { 60_000 } else { 1 });
            sh.slots[idx as usize].tomb = Some(TimeWaitState {
                iface,
                peer: PeerId(1),
                local,
                remote,
                snd_seq: Seq(300),
                rcv_seq: Seq(201),
                last_fin_seq: Seq(200),
                ts_ok: false,
                ts_recent: 0,
                ts_offset: 0,
                expires,
                pending_ack: false,
            });
            sh.slots[idx as usize].sched.time_wait = true;
            sh.time_wait_n += 1;
            sh.table.insert((iface, remote, local), idx);
            sh.timers.set(idx, expires);
            if i % 8192 == 0 || i + 1 == COUNT {
                assert!(
                    tombstone_container_capacity_bytes(&sh) <= global.reserved() + 8192,
                    "120k TIME_WAIT high-water capacity escaped the reserved bytes at {i}"
                );
            }
        }
        let mut accepted = |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted;
        sh.run(Instant::from_millis(2), &mut accepted);
        assert_eq!(sh.table.len(), 1);
        assert_eq!(sh.retained_metadata.free_slot_bytes(), (COUNT as u64 - 1) * crate::budget::RETAINED_SLOT_BYTES);
        assert!(tombstone_container_capacity_bytes(&sh) <= global.reserved() + 8192);
        sh.run(Instant::from_millis(60_001), &mut accepted);
        assert_eq!(global.connection_counts(), (0, 0));
        assert_eq!(global.reserved(), sh.retained_metadata.total_bytes());
        drop(sh);
        assert_eq!(global.reserved(), 0);
    }

    struct CountWake(std::sync::atomic::AtomicUsize);

    impl std::task::Wake for CountWake {
        fn wake(self: Arc<Self>) {
            self.0.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    }

    #[test]
    fn metadata_only_pool_reclaim_reports_bytes_and_wakes_waiter() {
        use crate::budget::{AllocationKind, Tier};
        use crate::buf::TxBuf;
        let global = GlobalBudget::new(8 << 20);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        sh.set_budget_limits(819_200, 819_200, 64);
        let mut first = TxBuf::default();
        assert!(first.push(&mut sh.pool, &mut sh.budget, PeerId(1), &[1]));
        first.release_all(&mut sh.pool);
        let mut second = TxBuf::default();
        assert!(second.push(&mut sh.pool, &mut sh.budget, PeerId(1), &[2]));
        drop((first, second));
        assert_eq!(sh.pool.cached(), 0);
        let bytes = sh.pool.descriptor_bytes();
        assert!(bytes > 0);
        assert_eq!(global.cached_bytes(), bytes);
        let memory = sh.memory_handle(PeerId(2));
        let ceiling = 819_200 - global.headroom(819_200, Tier::Block);
        let blocker = memory.try_allocate_kind(ceiling - sh.budget.physical_used(), AllocationKind::TxIndex).unwrap();
        let wake = Arc::new(CountWake(Default::default()));
        let waiter = global.new_waiter_id();
        let observed = global.release_epoch();
        assert!(memory.try_allocate_kind(1, AllocationKind::TxIndex).is_none());
        global.register_physical_waiter(waiter, observed, &std::task::Waker::from(wake.clone()), &memory, 1, AllocationKind::TxIndex);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 0);
        assert!(sh.reclaim_own_cache(), "zero payload blocks still retain descriptor bytes");
        assert_eq!(sh.stats().cache_reclaimed_bytes, bytes);
        assert_eq!(global.cached_bytes(), 0);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 1);
        let lease = memory.try_allocate_kind(1, AllocationKind::TxIndex).unwrap();
        global.remove_waiter(waiter);
        drop((lease, blocker, sh));
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn reclaim_epoch_releases_idle_connection_tx_index() {
        use crate::budget::{AllocationKind, Tier};
        let mut r = AdmitRig::new(819_200, 819_200);
        r.syn(0, 1, 40000);
        let id = r.sh.conn_ids()[0];
        assert_eq!(r.sh.write(id, &[1]), WriteResult::Written(1));
        // Keep the connection in its slot after its send side has emptied.
        r.sh.with_conn(id, |conn, ctx| conn.destroy(ctx)).unwrap();
        r.sh.pool.reclaim();
        let bytes = r.global.cached_bytes();
        assert!(bytes > 0);
        let memory = r.sh.memory_handle(PeerId(2));
        let ceiling = 819_200 - r.global.headroom(819_200, Tier::Block);
        let blocker = memory.try_allocate_kind(ceiling - r.sh.budget.physical_used(), AllocationKind::TxIndex).unwrap();
        let wake = Arc::new(CountWake(Default::default()));
        let waiter = r.global.new_waiter_id();
        let observed = r.global.release_epoch();
        assert!(memory.try_allocate_kind(1, AllocationKind::TxIndex).is_none());
        r.global.register_physical_waiter(waiter, observed, &std::task::Waker::from(wake.clone()), &memory, 1, AllocationKind::TxIndex);
        r.sh.reclaim_cache_if_requested();
        assert_eq!(r.global.cached_bytes(), 0);
        assert_eq!(r.sh.stats().cache_reclaims, 1);
        assert_eq!(r.sh.stats().cache_reclaimed_bytes, bytes);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 1);
        let lease = memory.try_allocate_kind(1, AllocationKind::TxIndex).unwrap();
        r.global.remove_waiter(waiter);
        drop((lease, blocker, r));
    }

    #[test]
    fn peer_pressure_reclaims_descriptor_only_cache_without_busy_wake() {
        use crate::budget::{AllocationKind, Level, Tier};
        const PEER: u64 = 819_200;
        let mut r = AdmitRig::new(2 * PEER, PEER);
        r.syn(0, 1, 40000);
        let id = r.sh.conn_ids()[0];
        assert_eq!(r.sh.write(id, &[1]), WriteResult::Written(1));
        r.sh.with_conn(id, |conn, ctx| conn.destroy(ctx)).unwrap();
        r.sh.pool.reclaim();
        assert_eq!((r.sh.pool.cached(), r.sh.pool.descriptor_bytes()), (0, 0));
        let bytes = r.global.cached_bytes();
        assert!(bytes > 0, "empty TX backing must stay charged and reclaimable");
        let memory = r.sh.memory_handle(PeerId(1));
        let ceiling = PEER - r.global.headroom(PEER, Tier::Block);
        let blocker = memory.try_allocate_kind(ceiling - r.sh.budget.physical_used(), AllocationKind::TxIndex).unwrap();
        let held = r.sh.budget.physical_used();
        let epoch = r.global.cache_reclaim_epoch();
        let observed = r.global.release_epoch();
        let failures = r.sh.budget.stats().failures_by_level()[Level::Peer as usize];
        assert!(memory.try_allocate_kind(1, AllocationKind::TxIndex).is_none());
        assert_eq!(r.sh.budget.physical_used(), held, "refusal must retain the old index charge");
        assert_eq!(r.sh.budget.stats().failures_by_level()[Level::Peer as usize], failures + 1);
        assert!(r.global.cache_reclaim_epoch() > epoch);
        let wake = Arc::new(CountWake(Default::default()));
        let waiter = r.global.new_waiter_id();
        r.global.register_physical_waiter(waiter, observed, &std::task::Waker::from(wake.clone()), &memory, 1, AllocationKind::TxIndex);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 0);
        r.sh.reclaim_cache_if_requested();
        assert_eq!(r.global.cached_bytes(), 0);
        assert_eq!(r.sh.budget.physical_used(), held - bytes);
        assert_eq!(r.sh.stats().cache_reclaimed_bytes, bytes);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 1);
        let retry = memory.try_allocate_kind(1, AllocationKind::TxIndex).expect("same peer must fit after descriptor-only reclaim");
        for _ in 0..3 {
            r.sh.reclaim_cache_if_requested();
        }
        assert_eq!(r.sh.stats().cache_reclaims, 1);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 1, "no repeated wake without another release");
        r.global.remove_waiter(waiter);
        let global = r.global.clone();
        drop((retry, blocker, r));
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn cached_payload_peer_refusal_reclaims_same_peer_idle_tx_indexes() {
        use crate::budget::{AllocationKind, Level, Tier};
        use crate::buf::{TX_BLOCK, TX_BLOCK_CHARGE, TX_FIRST_WRITE_CHARGE};
        // Three connection states stay live; add their fixed charge to the
        // 66112-byte (64-bit) payload/index boundary used by standalone TxBufs.
        let peer_limit = TX_FIRST_WRITE_CHARGE + 3 * ACTIVE_STATE_BYTES;
        let mut r = AdmitRig::new(2 * peer_limit, peer_limit);
        assert_eq!(r.global.headroom(peer_limit, Tier::Block), 0);
        let mut ids = Vec::new();
        for port in 40000..40003 {
            r.syn(0, 1, port);
            let id = *r.sh.conn_ids().last().unwrap();
            assert_eq!(r.sh.write(id, &[1]), WriteResult::Written(1));
            r.sh.with_conn(id, |conn, ctx| conn.destroy(ctx)).unwrap();
            ids.push(id);
        }
        let indexes = r.global.cached_bytes() - r.sh.pool.descriptor_bytes() - r.sh.pool.cached() as u64 * 2048;
        assert!(indexes > TX_FIRST_WRITE_CHARGE - TX_BLOCK_CHARGE, "retained indexes must consume the first-write descriptor allowance");
        // Seed the other peer's cached block directly: no fourth live
        // connection/RX promise belongs in this exact TX quota boundary.
        let mut seed = crate::buf::TxBuf::default();
        assert!(seed.push(&mut r.sh.pool, &mut r.sh.budget, PeerId(2), &vec![2; TX_BLOCK]));
        seed.consume(&mut r.sh.pool, TX_BLOCK);
        drop(seed);
        let cached = r.global.cached_bytes();
        let held = r.sh.budget.physical_used();
        let epoch = r.global.cache_reclaim_epoch();
        let observed = r.global.release_epoch();
        let failures = r.sh.budget.stats().failures_by_level()[Level::Peer as usize];
        assert!(!r.sh.budget.can_allocate(PeerId(1), TX_BLOCK_CHARGE, AllocationKind::TxBlock));
        assert_eq!(r.sh.write(ids[0], &vec![3; TX_BLOCK]), WriteResult::MemoryBlocked);
        assert_eq!(r.sh.budget.physical_used(), held, "cached assign refusal must retain every lease");
        assert_eq!(r.sh.budget.stats().failures_by_level()[Level::Peer as usize], failures + 1);
        println!("CACHED_ASSIGN peer_limit={peer_limit} fixed_states={} retained_indexes={indexes} payload_charge={TX_BLOCK_CHARGE} room={} epoch_before={epoch} epoch_after={}", 3 * ACTIVE_STATE_BYTES, TX_FIRST_WRITE_CHARGE - indexes, r.global.cache_reclaim_epoch());
        assert!(r.global.cache_reclaim_epoch() > epoch, "cached payload peer refusal must request reclaim without another allocator failure");
        let memory = r.sh.memory_handle(PeerId(1));
        let wake = Arc::new(CountWake(Default::default()));
        let waiter = r.global.new_waiter_id();
        r.global.register_physical_waiter(waiter, observed, &std::task::Waker::from(wake.clone()), &memory, TX_BLOCK_CHARGE, AllocationKind::TxBlock);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 0, "retained peer indexes must prevent a premature cache-retry wake");
        r.sh.reclaim_cache_if_requested();
        assert_eq!(r.global.cached_bytes(), 0);
        assert_eq!(r.sh.budget.physical_used(), held - cached);
        assert_eq!(r.sh.stats().cache_reclaimed_bytes, cached);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 1);
        assert_eq!(r.sh.write(ids[0], &vec![3; TX_BLOCK]), WriteResult::Written(TX_BLOCK), "first write must fit after same-peer indexes are reclaimed");
        for _ in 0..3 {
            r.sh.reclaim_cache_if_requested();
        }
        assert_eq!(r.sh.stats().cache_reclaims, 1);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 1);
        r.global.remove_waiter(waiter);
        let global = r.global.clone();
        drop(r);
        assert_eq!(global.reserved(), 0);
    }

    /// #664: idle TX blocks fill the port share, so an admission allocation
    /// fails. The failure must ask the shard to drop its cache; the release
    /// then wakes the parked allocator and the same allocation succeeds.
    #[test]
    fn idle_tx_cache_is_reclaimed_when_it_blocks_another_allocation() {
        use crate::budget::AllocationKind;
        use crate::buf::{TxBuf, TX_BLOCK_CHARGE};
        let global = GlobalBudget::new(8 << 20);
        let mut sh = Shard::with_budget(StackConfig::default(), Arc::clone(&global));
        sh.set_budget_limits(819_200, 819_200, 64);
        let mut bufs: Vec<TxBuf> = (0..12).map(|_| TxBuf::default()).collect();
        for tx in &mut bufs {
            assert!(tx.push(&mut sh.pool, &mut sh.budget, PeerId(1), &vec![1; crate::buf::TX_BLOCK]));
        }
        for tx in &mut bufs {
            tx.consume(&mut sh.pool, crate::buf::TX_BLOCK);
        }
        assert_eq!(sh.pool.cached(), 12);
        drop(bufs);
        let cached = 12 * TX_BLOCK_CHARGE + sh.pool.descriptor_bytes();
        assert_eq!(global.cached_bytes(), cached);

        let memory = sh.memory_handle(PeerId(2));
        let wake = Arc::new(CountWake(Default::default()));
        let waiter = global.new_waiter_id();
        let observed = global.release_epoch();
        assert!(memory.try_allocate_kind(65_600, AllocationKind::AdapterTx).is_none());
        global.register_physical_waiter(waiter, observed, &std::task::Waker::from(wake.clone()), &memory, 65_600, AllocationKind::AdapterTx);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 0, "nothing was released yet");

        sh.run(Instant::ZERO, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted);
        assert_eq!(global.cached_bytes(), 0);
        assert_eq!(sh.stats().cache_reclaims, 1);
        assert_eq!(sh.stats().cache_reclaimed_bytes, cached);
        assert_eq!(wake.0.load(std::sync::atomic::Ordering::Relaxed), 1, "reclaim must wake the parked allocator");
        let lease = memory.try_allocate_kind(65_600, AllocationKind::AdapterTx).expect("share freed by reclaim");
        global.remove_waiter(waiter);
        drop(lease);

        // Without a new failure, later rounds keep the cache.
        let mut tx = TxBuf::default();
        assert!(tx.push(&mut sh.pool, &mut sh.budget, PeerId(1), &[1]));
        tx.consume(&mut sh.pool, 1);
        sh.run(Instant::ZERO, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted);
        assert_eq!(sh.pool.cached(), 1);
        assert_eq!(sh.stats().cache_reclaims, 1);
    }
}

#[cfg(test)]
mod ingress_buf_tests {
    use super::*;
    use crate::pktpool::PacketPool;

    /// Move every packet between a test-peer client and a server fed through
    /// `ingress_buf`; `reorder` holds back each third data packet one round.
    fn exchange(c: &mut Shard, s: &mut Shard, pool: &PacketPool, now: Instant, held: &mut Vec<Vec<u8>>, reorder: bool) {
        let mut to_s: Vec<Vec<u8>> = std::mem::take(held);
        let mut n = 0;
        c.run(now, &mut |_: IfaceId, p: &OutPacket<'_>| {
            n += 1;
            if reorder && n % 3 == 0 && !p.payload[0].is_empty() {
                held.push(p.to_vec());
            } else {
                to_s.push(p.to_vec());
            }
            SendResult::Accepted
        });
        for p in to_s {
            let mut b = pool.get().unwrap();
            let room = b.spare_capacity_mut();
            for (d, x) in room.iter_mut().zip(&p) {
                d.write(*x);
            }
            unsafe { b.set_len(p.len()) };
            s.ingress_buf(now, IfaceId(0), PeerId(1), b, 0, RxChecksum::Verify);
        }
        let mut to_c = Vec::new();
        s.run(now, &mut |_: IfaceId, p: &OutPacket<'_>| {
            to_c.push(Bytes::from(p.to_vec()));
            SendResult::Accepted
        });
        for p in to_c {
            c.ingress(now, IfaceId(0), PeerId(1), p);
        }
    }

    #[test]
    fn pooled_ingress_keeps_large_payloads_without_copy_and_returns_every_buffer() {
        for (mtu, reorder) in [(65535u16, false), (65535, true), (9000, false), (1500, false)] {
            let global = GlobalBudget::new(256 << 20);
            let mut s = Shard::with_budget(StackConfig::client(), Arc::clone(&global));
            s.set_budget_limits(global.high(), global.high(), 16);
            s.add_iface(IfaceConfig { mtu });
            let pool = PacketPool::new(s.memory_handle(PeerId(1)), mtu as usize, 1024);
            let mut c = Shard::with_budget(StackConfig::default(), GlobalBudget::new(256 << 20));
            c.add_iface(IfaceConfig { mtu });
            let mut now = Instant::from_millis(1);
            let cid = c.connect(now, IfaceId(0), PeerId(1), "10.0.0.2:4000".parse().unwrap(), "10.9.9.9:443".parse().unwrap());
            const TOTAL: usize = 4 << 20;
            let data: Vec<u8> = (0..TOTAL).map(|i| (i % 251) as u8).collect();
            let (mut sent, mut got, mut sid) = (0usize, Vec::with_capacity(TOTAL), None);
            let mut held = Vec::new();
            let mut kept_zero_copy = false;
            for _ in 0..20_000 {
                now += Duration::from_micros(200);
                if sent < TOTAL {
                    if let WriteResult::Written(n) = c.write(cid, &data[sent..(sent + 256 * 1024).min(TOTAL)]) {
                        sent += n;
                    }
                }
                exchange(&mut c, &mut s, &pool, now, &mut held, reorder);
                while let Some(ev) = s.poll_event() {
                    if let Event::Accepted(id) = ev {
                        sid = Some(id);
                    }
                }
                while c.poll_event().is_some() {}
                if let Some(id) = sid {
                    // A retained jumbo payload pins its pooled buffer until read.
                    // Unread jumbo payload stays in its pooled buffer.
                    if s.info(id).is_some_and(|i| i.rx_queued >= 32 * 1024) && pool.in_use() > 0 {
                        kept_zero_copy = true;
                    }
                    while let Ok(b) = s.read_chunk(now, id, usize::MAX) {
                        got.extend_from_slice(&b);
                    }
                }
                if got.len() == TOTAL {
                    break;
                }
            }
            assert_eq!(got.len(), TOTAL, "mtu {mtu} reorder {reorder}");
            assert!(got == data, "byte stream corrupted (mtu {mtu} reorder {reorder})");
            // Full-size segments fill at least half of an MTU-sized buffer,
            // so in-order data stays in its buffer at every MTU.
            assert!(kept_zero_copy, "mtu {mtu} reorder {reorder}");
            // Once read, every buffer is back; the shard and pool release
            // the whole budget.
            assert_eq!(pool.in_use(), 0);
            s.abort(sid.unwrap());
            drop(s);
            drop(pool);
            assert_eq!(global.reserved(), 0);
        }
    }
}

#[cfg(test)]
mod tso_loss_tests {
    use super::*;
    use crate::wire::{checksum_fold, parse_ip, pseudo_sum, sum_bytes, FIN, PSH};
    use crate::ConnStats;

    /// Cut a packet into the segments a device would put on the wire for it
    /// (GSO): sequence numbers advance by `gso_size`, FIN/PSH stay on the last
    /// segment, and every checksum is completed.
    fn device_segments(p: &OutPacket<'_>) -> Vec<Vec<u8>> {
        let pkt = p.to_vec();
        let hl = p.header.len();
        let ip = parse_ip(&pkt).unwrap();
        let l4 = ip.l4_off;
        let payload = &pkt[hl..];
        let seg = if p.gso_size > 0 { p.gso_size as usize } else { payload.len().max(1) };
        let chunks: Vec<&[u8]> = if payload.is_empty() { vec![&[][..]] } else { payload.chunks(seg).collect() };
        let seq0 = u32::from_be_bytes(pkt[l4 + 4..l4 + 8].try_into().unwrap());
        let n = chunks.len();
        chunks
            .iter()
            .enumerate()
            .map(|(k, c)| {
                let mut v = pkt[..hl].to_vec();
                v[l4 + 4..l4 + 8].copy_from_slice(&seq0.wrapping_add((k * seg) as u32).to_be_bytes());
                if k + 1 < n {
                    v[l4 + 13] &= !(FIN | PSH);
                }
                if l4 == 20 {
                    v[2..4].copy_from_slice(&((hl + c.len()) as u16).to_be_bytes());
                    v[10..12].copy_from_slice(&[0, 0]);
                    let ipc = checksum_fold(sum_bytes(0, &v[..20]));
                    v[10..12].copy_from_slice(&ipc.to_be_bytes());
                } else {
                    v[4..6].copy_from_slice(&((hl - 40 + c.len()) as u16).to_be_bytes());
                }
                v.extend_from_slice(c);
                v[l4 + 16..l4 + 18].copy_from_slice(&[0, 0]);
                let sum = pseudo_sum(ip.src, ip.dst, (v.len() - l4) as u32);
                let tc = checksum_fold(sum_bytes(sum, &v[l4..]));
                v[l4 + 16..l4 + 18].copy_from_slice(&tc.to_be_bytes());
                v
            })
            .collect()
    }

    /// A TSO super-segment loses one of its segments on the wire (first,
    /// middle or last). The peer SACKs the rest; recovery must retransmit only
    /// the missing segment, without an RTO.
    fn lose_one_segment_of_a_super_segment(which: usize) -> ConnStats {
        let global = GlobalBudget::new(256 << 20);
        let mut s = Shard::with_budget(StackConfig::client(), Arc::clone(&global));
        s.set_budget_limits(global.high(), global.high(), 16);
        let si = s.add_iface(IfaceConfig { mtu: 1500 });
        s.set_iface_tso(si, 65_000);
        let mut c = Shard::with_budget(StackConfig::default(), GlobalBudget::new(256 << 20));
        let ci = c.add_iface(IfaceConfig { mtu: 1500 });
        let mut now = Instant::from_millis(1);
        let cid = c.connect(now, ci, PeerId(1), "10.0.0.2:4000".parse().unwrap(), "10.9.9.9:443".parse().unwrap());
        const TOTAL: usize = 2 << 20;
        let data: Vec<u8> = (0..TOTAL).map(|i| (i % 251) as u8).collect();
        let (mut sent, mut got, mut sid, mut dropped) = (0usize, Vec::with_capacity(TOTAL), None, false);
        let mut buf = vec![0u8; 256 * 1024];
        for _ in 0..20_000 {
            now += Duration::from_millis(1);
            let mut to_s = Vec::new();
            c.run(now, &mut |_: IfaceId, p: &OutPacket<'_>| {
                to_s.push(Bytes::from(p.to_vec()));
                SendResult::Accepted
            });
            for p in to_s {
                s.ingress(now, si, PeerId(1), p);
            }
            while let Some(ev) = s.poll_event() {
                if let Event::Accepted(id) = ev {
                    sid = Some(id);
                }
            }
            if let Some(id) = sid {
                if sent < TOTAL {
                    if let WriteResult::Written(n) = s.write(id, &data[sent..(sent + 256 * 1024).min(TOTAL)]) {
                        sent += n;
                    }
                }
            }
            let mut to_c = Vec::new();
            s.run(now, &mut |_: IfaceId, p: &OutPacket<'_>| {
                let mut segs = device_segments(p);
                if !dropped && p.gso_size > 0 && segs.len() >= 3 {
                    let k = match which {
                        0 => 0,
                        1 => segs.len() / 2,
                        _ => segs.len() - 1,
                    };
                    segs.remove(k);
                    dropped = true;
                }
                to_c.extend(segs.into_iter().map(Bytes::from));
                SendResult::Accepted
            });
            for p in to_c {
                c.ingress(now, ci, PeerId(1), p);
            }
            while c.poll_event().is_some() {}
            while let ReadResult::Data(n) = c.read(now, cid, &mut buf) {
                got.extend_from_slice(&buf[..n]);
            }
            if got.len() == TOTAL {
                break;
            }
        }
        assert!(dropped, "no super-segment was sent");
        assert_eq!(got.len(), TOTAL, "transfer stalled (segment {which})");
        assert!(got == data, "byte stream corrupted");
        s.info(sid.unwrap()).unwrap().stats
    }

    #[test]
    fn lost_segment_inside_a_tso_super_segment_is_recovered_by_sack() {
        // Before SACK blocks could cut a super-segment record, a lost first
        // segment cost a TLP and two segments, a lost middle one the whole
        // rest of the super-segment.
        for which in 0..3 {
            let st = lose_one_segment_of_a_super_segment(which);
            assert_eq!((st.rto_count, st.tlp_count, st.fast_recoveries), (0, 0, 1), "segment {which}: {st:?}");
            // Exactly the missing segment (MSS 1448 with timestamps).
            assert_eq!(st.bytes_retrans, 1448, "segment {which}");
        }
    }
}

#[cfg(test)]
mod pacing_wake_tests {
    use super::*;
    use crate::cc::CcAlgo;
    use crate::sim::{pattern_byte, LinkParams, Sim};

    fn connected(rate: u64, cwnd_segs: u32) -> (Sim, ConnId, ConnId) {
        let cfg = StackConfig { cc: CcAlgo::Brutal(rate), init_cwnd_segs: cwnd_segs, ..Default::default() };
        let link = LinkParams { rate_bps: 0, delay: Duration::from_millis(1), queue_bytes: 1 << 20, ..Default::default() };
        let mut sim = Sim::new(4405, cfg, StackConfig::default(), link.clone(), link);
        let client = sim.connect(44005, Default::default());
        sim.run_until(sim.now + Duration::from_millis(100));
        let server = sim.a.accepted[0];
        assert_eq!(sim.a.shard.info(server).unwrap().state, State::Established);
        (sim, server, client)
    }

    fn collect(shard: &mut Shard, now: Instant) -> (RunOutcome, Vec<(usize, Vec<u8>)>) {
        let mut packets = Vec::new();
        let outcome = shard.run(now, &mut |_: IfaceId, pkt: &OutPacket<'_>| {
            packets.push((pkt.len(), pkt.payload.concat()));
            SendResult::Accepted
        });
        shard.check_invariants();
        (outcome, packets)
    }

    #[test]
    fn small_reply_wakes_at_eligibility_without_a_control_packet() {
        for rate in [505, 632] {
            let (mut sim, server, _) = connected(rate, 10);
            let now = sim.now;
            let sh = &mut sim.a.shard;
            let info = sh.info(server).unwrap();
            let ahead = pacing_ahead(rate, info.mss as usize, sh.cfg.max_quantum);
            // A reachable state after earlier paced transmissions: EDT still
            // carries debt, while this reply has never entered the wire.
            let eligible = now + Duration::from_millis(20);
            let edt = eligible + ahead;
            sh.slots[server.idx()].conn.as_mut().unwrap().next_send_time = edt;
            assert_eq!(sh.write(server, &[7; 256]), WriteResult::Written(256));
            assert!(collect(sh, now).1.is_empty());
            assert_eq!(sh.next_deadline(), Some(eligible), "small reply at {rate} B/s must not wait for a full quantum");
            assert!(collect(sh, eligible - Duration::from_nanos(1)).1.is_empty(), "no early send");
            let (out, packets) = collect(sh, eligible);
            assert_eq!(out.packets, 1, "timer alone must resume the eligible reply");
            assert_eq!(packets[0].1, [7; 256]);
            let after = sh.info(server).unwrap();
            assert_eq!((after.cwnd, after.pacing_rate), (info.cwnd, Some(rate)));
            assert!(after.pipe <= after.cwnd && after.pipe <= after.snd_wnd);
            let charged = Duration::from_nanos((packets[0].0 as u128 * 1_000_000_000 / rate as u128) as u64);
            assert_eq!(sh.slots[server.idx()].conn.as_ref().unwrap().next_send_time, edt + charged, "wire bytes must retain the full pacing charge");
        }
    }

    fn replies_with_retransmission_debt(rate: u64) {
        let (mut sim, server, client) = connected(rate, 10);
        // The established flow has a 200 ms RTO; an abrupt one-way delay
        // causes genuine retransmissions before its 256-byte reply arrives.
        assert_eq!(sim.a.shard.info(server).unwrap().rto, Duration::from_millis(200));
        sim.ab.p.delay = Duration::from_millis(500);
        let mut slowest = Duration::ZERO;
        for n in 0..24 {
            let payload: Vec<_> = (n * 256..(n + 1) * 256).map(pattern_byte).collect();
            assert_eq!(sim.a.shard.write(server, &payload), WriteResult::Written(256));
            let start = sim.now;
            let expected = (n + 1) * 256;
            while sim.b.conns[&client].received < expected && sim.now < start + Duration::from_secs(3) {
                sim.run_until(sim.now + Duration::from_millis(10));
            }
            let info = sim.a.shard.info(server).unwrap();
            slowest = slowest.max(sim.now - start);
            assert_eq!(
                sim.b.conns[&client].received,
                expected,
                "reply {n}, rate {rate}, elapsed {:?}, EDT {:?}, {info:?}",
                sim.now - start,
                sim.a.shard.slots[server.idx()].conn.as_ref().unwrap().next_send_time
            );
            assert!(!sim.b.conns[&client].corrupt);
            assert_eq!(info.pacing_rate, Some(rate));
        }
        let info = sim.a.shard.info(server).unwrap();
        assert!(info.stats.bytes_retrans > 0 && info.stats.rto_count > 0, "test must create real retransmission debt");
        assert!(info.stats.limited_pacing_ns > 0, "test must reach the pacing gate");
        eprintln!("small replies: rate={rate} B/s, slowest={slowest:?}, retransmitted={}, RTOs={}", info.stats.bytes_retrans, info.stats.rto_count);
    }

    #[test]
    fn small_replies_with_retransmission_debt_at_505_bytes_per_second() {
        replies_with_retransmission_debt(505);
    }

    #[test]
    fn small_replies_with_retransmission_debt_at_632_bytes_per_second() {
        replies_with_retransmission_debt(632);
    }

    #[test]
    fn continuous_backlog_keeps_full_quantum_wake_and_batch() {
        for rate in [500_000, 62_500_000] {
            let (mut sim, server, _) = connected(rate, 128);
            let now = sim.now;
            let sh = &mut sim.a.shard;
            let mss = sh.info(server).unwrap().mss as usize;
            let quantum = quantum_for(rate, mss, sh.cfg.max_quantum);
            let ahead = pacing_ahead(rate, mss, sh.cfg.max_quantum);
            let eligible = now + Duration::from_millis(20);
            let edt = eligible + ahead;
            sh.slots[server.idx()].conn.as_mut().unwrap().next_send_time = edt;
            assert_eq!(sh.write(server, &vec![3; quantum * 3]), WriteResult::Written(quantum * 3));
            assert!(collect(sh, now).1.is_empty());
            assert_eq!(sh.next_deadline(), Some(edt), "bulk must retain coalesced wakeups");
            assert!(collect(sh, eligible).1.is_empty(), "a bulk flow must not gain a per-packet wakeup");
            assert!(collect(sh, edt - Duration::from_nanos(1)).1.is_empty());
            let (out, packets) = collect(sh, edt);
            assert!(out.bytes >= quantum, "the wake must still serve a full batch");
            assert!(out.bytes <= quantum + mss + MAX_HEADER, "the unchanged gate bounds burst overshoot to one packet");
            assert!(packets.len() >= 2);
            let next_edt = sh.slots[server.idx()].conn.as_ref().unwrap().next_send_time;
            assert_eq!(sh.pacing.peek().map(|x| x.0), Some(next_edt), "remaining backlog must stay on the full-quantum deadline");
            assert!(next_edt > edt + ahead);
        }
    }

    #[test]
    fn small_burst_boundary_counts_inflight_bytes_and_uses_full_quantum() {
        for (inflight, extra, clipped_round) in [(false, 0, false), (false, 1, false), (true, 1, false), (false, 0, true)] {
            let rate = 505;
            let (mut sim, server, _) = connected(rate, 10);
            let now = sim.now;
            let sh = &mut sim.a.shard;
            let mss = sh.info(server).unwrap().mss as usize;
            let quantum = quantum_for(rate, mss, sh.cfg.max_quantum);
            let queued = if inflight {
                assert_eq!(sh.write(server, &vec![5; quantum - 1]), WriteResult::Written(quantum - 1));
                let (_, sent) = collect(sh, now);
                assert_eq!(sent.iter().map(|(_, payload)| payload.len()).sum::<usize>(), quantum - 1);
                assert_eq!(sh.info(server).unwrap().pipe, (quantum - 1) as u64);
                2
            } else {
                quantum + extra
            };
            if clipped_round {
                sh.cfg.round_bytes_cap = mss;
            }
            let eligible = now + Duration::from_millis(20);
            let edt = eligible + pacing_ahead(rate, mss, sh.cfg.max_quantum);
            sh.slots[server.idx()].conn.as_mut().unwrap().next_send_time = edt;
            assert_eq!(sh.write(server, &vec![5; queued]), WriteResult::Written(queued));
            assert!(collect(sh, now).1.is_empty());
            assert_eq!(sh.pacing.peek().map(|x| x.0), Some(if extra == 0 { eligible } else { edt }));
        }
    }

    #[cfg(feature = "tokio")]
    #[test]
    fn small_burst_boundary_counts_adapter_bytes() {
        for extra in [0, 1] {
            let rate = 505;
            let (mut sim, server, _) = connected(rate, 10);
            let now = sim.now;
            let sh = &mut sim.a.shard;
            let mss = sh.info(server).unwrap().mss as usize;
            let quantum = quantum_for(rate, mss, sh.cfg.max_quantum);
            sh.set_adapter_tx(server, (quantum - 256 + extra) as u32);
            let eligible = now + Duration::from_millis(20);
            let edt = eligible + pacing_ahead(rate, mss, sh.cfg.max_quantum);
            sh.slots[server.idx()].conn.as_mut().unwrap().next_send_time = edt;
            assert_eq!(sh.write(server, &[5; 256]), WriteResult::Written(256));
            assert!(collect(sh, now).1.is_empty());
            assert_eq!(sh.pacing.peek().map(|x| x.0), Some(if extra == 0 { eligible } else { edt }));
        }
    }

    #[test]
    fn debt_rate_changes_conserve_remaining_bytes_and_round_up() {
        for (old_rate, new_rate, remaining_ns) in [(100, 200, 1_000_000_000), (200, 100, 1_000_000_000), (505, 505, 7), (2, 3, 1), (3, 2, 1)] {
            let (mut sim, server, _) = connected(505, 10);
            let now = sim.now;
            let c = sim.a.shard.slots[server.idx()].conn.as_mut().unwrap();
            c.sync_pacing_debt(now, Some(old_rate));
            c.next_send_time = now + Duration::from_nanos(remaining_ns);
            c.sync_pacing_debt(now, Some(new_rate));
            let owed = remaining_ns as u128 * old_rate as u128;
            let expected = owed.div_ceil(new_rate as u128) as u64;
            assert_eq!(c.next_send_time, now + Duration::from_nanos(expected));
            assert!((c.next_send_time - now).as_nanos() * new_rate as u128 >= owed, "no forgiven byte debt");
            let t = c.next_send_time;
            c.sync_pacing_debt(now, Some(new_rate));
            assert_eq!(c.next_send_time, t, "same-rate synchronization must be idempotent");
        }
    }
    #[test]
    fn debt_none_transition_past_credit_and_saturation_are_explicit() {
        let (mut sim, server, _) = connected(505, 10);
        let now = sim.now;
        let c = sim.a.shard.slots[server.idx()].conn.as_mut().unwrap();
        c.sync_pacing_debt(now, Some(505));
        c.next_send_time = now + Duration::from_secs(2);
        c.sync_pacing_debt(now, None);
        assert_eq!(c.next_send_time, now + Duration::from_secs(2));
        c.sync_pacing_debt(now, Some(1010));
        assert_eq!(c.next_send_time, now + Duration::from_secs(1), "unpaced transition cannot erase positive debt");
        c.next_send_time = now - Duration::from_millis(1);
        c.sync_pacing_debt(now, Some(505));
        assert_eq!(c.next_send_time, now - Duration::from_millis(1), "past credit must not expand");
        c.sync_pacing_debt(now, Some(u64::MAX));
        c.next_send_time = Instant::MAX;
        c.sync_pacing_debt(now, Some(1));
        assert_eq!(c.next_send_time, Instant::MAX, "unrepresentable future debt must saturate conservatively");
        c.sync_pacing_debt(now, Some(0));
        assert_eq!(c.next_send_time, Instant::MAX, "zero external rate uses existing minimum-one semantics");
    }
    #[test]
    fn accepted_retransmission_retains_full_wire_charge() {
        let rate = 505;
        let (mut sim, server, _) = connected(rate, 10);
        let now = sim.now;
        let sh = &mut sim.a.shard;
        assert_eq!(sh.write(server, &[7; 256]), WriteResult::Written(256));
        let (_, first) = collect(sh, now);
        assert_eq!(first.len(), 1);
        let before = sh.slots[server.idx()].conn.as_ref().unwrap().next_send_time;
        let rto = sh.info(server).unwrap().rto;
        let (_, resent) = collect(sh, now + rto);
        assert!(!resent.is_empty());
        assert!(sh.info(server).unwrap().stats.bytes_retrans >= 256);
        let bytes = resent.iter().map(|(n, _)| *n).sum::<usize>();
        assert_eq!(
            sh.slots[server.idx()].conn.as_ref().unwrap().next_send_time,
            before + Duration::from_nanos((bytes as u128 * 1_000_000_000 / rate as u128) as u64)
        );
    }
    #[test]
    fn rate_change_repositions_armed_heap_without_control_work() {
        for (old_rate, new_rate) in [(10_000, 100_000), (100_000, 10_000)] {
            let (mut sim, server, _) = connected(new_rate, 10);
            let now = sim.now;
            let sh = &mut sim.a.shard;
            let idx = server.idx() as u32;
            {
                let c = sh.slots[server.idx()].conn.as_mut().unwrap();
                c.sync_pacing_debt(now, Some(new_rate));
                c.next_send_time = now + Duration::from_secs(1);
            }
            assert_eq!(sh.write(server, &[3; 256]), WriteResult::Written(256));
            assert!(collect(sh, now).1.is_empty());
            // An old-rate debt and armed timer, while CC now reports new_rate.
            // No ACK/RST/probe is pending to wake this heap entry.
            let c = sh.slots[server.idx()].conn.as_mut().unwrap();
            c.sync_pacing_debt(now, Some(old_rate));
            c.next_send_time = now + Duration::from_secs(1);
            let old_wake = c.next_send_time - pacing_ahead(old_rate, c.info().mss as usize, sh.cfg.max_quantum);
            sh.pacing.set(idx, old_wake);
            sh.after_conn_change(idx);
            assert!(sh.slots[server.idx()].sched.in_ready);
            assert!(collect(sh, now).1.is_empty());
            let c = sh.slots[server.idx()].conn.as_ref().unwrap();
            let expected = c.next_send_time - pacing_ahead(new_rate, c.info().mss as usize, sh.cfg.max_quantum);
            assert_eq!(old_wake > expected, old_rate < new_rate);
            assert_eq!(sh.pacing.peek().unwrap().0, expected);
            assert!(collect(sh, expected - Duration::from_nanos(1)).1.is_empty());
            assert_eq!(collect(sh, expected).1.len(), 1);
        }
    }
    #[test]
    fn idle_rate_change_reprices_debt_when_the_segment_arrives() {
        // (old, new, debt): a slowdown must not forgive debt and a speedup
        // must not keep charging the old rate until the next write.
        for (old_rate, new_rate, debt) in [(10_000, 500, Duration::from_millis(100)), (500, 10_000, Duration::from_secs(2))] {
            let (mut sim, server, client) = connected(new_rate, 10);
            let t0 = sim.now;
            {
                let c = sim.a.shard.slots[server.idx()].conn.as_mut().unwrap();
                assert!(!c.wants_tx(), "the server must be idle");
                c.sync_pacing_debt(t0, Some(old_rate));
                c.next_send_time = t0 + debt;
            }
            assert_eq!(sim.b.shard.write(client, &[1]), WriteResult::Written(1));
            let deadline = t0 + Duration::from_millis(5);
            while sim.a.conns[&server].received < 1 && sim.now < deadline {
                sim.run_until(sim.now + Duration::from_micros(100));
            }
            let arrived = sim.now;
            assert!(arrived < deadline && sim.a.conns[&server].received == 1);
            let c = sim.a.shard.slots[server.idx()].conn.as_ref().unwrap();
            assert!(!c.pacing_rate_changed(Some(new_rate)), "the segment path must reprice");
            let edt = c.next_send_time;
            // Repricing happened within the last 100 µs step; units are byte-ns.
            let step = Duration::from_micros(100).as_nanos();
            let owed_before = (t0 + debt - (arrived - Duration::from_micros(100))).as_nanos() * old_rate as u128;
            let owed_after = (t0 + debt - arrived).as_nanos() * old_rate as u128;
            let remaining = edt.saturating_since(arrived).as_nanos() * new_rate as u128;
            assert!(remaining + step * new_rate as u128 >= owed_after, "no forgiven byte debt: {remaining} < {owed_after}");
            assert!(remaining <= owed_before + new_rate as u128, "no stale-rate charge: {remaining} > {owed_before}");
        }
    }
    #[test]
    fn unchanged_rate_does_not_repeatedly_requeue_paced_flow() {
        let (mut sim, server, _) = connected(10_000, 10);
        let now = sim.now;
        let sh = &mut sim.a.shard;
        let c = sh.slots[server.idx()].conn.as_mut().unwrap();
        c.sync_pacing_debt(now, Some(10_000));
        c.next_send_time = now + Duration::from_secs(1);
        assert_eq!(sh.write(server, &[3; 256]), WriteResult::Written(256));
        assert!(collect(sh, now).1.is_empty());
        let before = sh.pacing.peek().unwrap();
        for _ in 0..100 {
            sh.after_conn_change(server.idx() as u32);
            assert!(!sh.slots[server.idx()].sched.in_ready);
            assert_eq!(sh.pacing.peek().unwrap(), before);
        }
    }
}
#[cfg(test)]
mod pacing_rate_recovery_tests {
    use super::*;
    use crate::cc::CcAlgo;
    use crate::sim::{pattern_byte, LinkParams, Sim};

    fn replies_across_rate_recovery(slow_upstream: bool) {
        let cfg = StackConfig { cc: CcAlgo::Bbr, ..Default::default() };
        let fast = LinkParams { rate_bps: 100_000_000, delay: Duration::from_millis(1), queue_bytes: 1 << 20, ..Default::default() };
        let mut sim = Sim::new(4405, cfg, StackConfig::default(), fast.clone(), fast);
        sim.gap_a = Duration::from_millis(10); // 100 Hz server driver.
        let client = sim.connect(44005, Default::default());
        sim.run_until(sim.now + Duration::from_secs(2));
        let server = sim.a.accepted[0];
        let origin = sim.now;
        let end = origin + Duration::from_secs(32);
        let mut next_request = origin;
        let mut request: Option<(Instant, bool)> = None;
        let mut seq = 0u64;
        let mut recovery_completed = 0;
        let mut max_recovery_latency = Duration::ZERO;
        // The same 8/16/8 s windows as the queue rig: inject 500 ms from
        // 8 s through 23.25 s, then restore fast links before recovery at 24 s.
        // Model either delayed upstream requests or an application waiting
        // 500 ms after each loaded-phase reply. BBR evolves without overrides.
        while sim.now < end || request.is_some() {
            let loaded = sim.now >= origin + Duration::from_secs(8) && sim.now < origin + Duration::from_millis(23_250);
            sim.ab.p.delay = if loaded { Duration::from_millis(500) } else { Duration::from_millis(1) };
            sim.ba.p.delay = if loaded && slow_upstream { Duration::from_millis(500) } else { Duration::from_millis(1) };
            if request.is_none() && sim.now >= next_request && sim.now < end {
                let payload: Vec<_> = (seq * 256..(seq + 1) * 256).map(pattern_byte).collect();
                assert_eq!(sim.b.shard.write(client, &payload), WriteResult::Written(256));
                request = Some((sim.now, false));
            }
            if let Some((started, ref mut replied)) = request.as_mut() {
                if !*replied && sim.a.conns[&server].received >= (seq + 1) * 256 {
                    let payload: Vec<_> = (seq * 256..(seq + 1) * 256).map(pattern_byte).collect();
                    assert_eq!(sim.a.shard.write(server, &payload), WriteResult::Written(256));
                    *replied = true;
                }
                if sim.b.conns[&client].received >= (seq + 1) * 256 {
                    let latency = sim.now - *started;
                    assert!(latency < Duration::from_secs(3));
                    if *started >= origin + Duration::from_secs(24) {
                        recovery_completed += 1;
                        max_recovery_latency = max_recovery_latency.max(latency);
                    }
                    next_request =
                        if !slow_upstream && loaded { sim.now + Duration::from_millis(500) } else { (*started + Duration::from_millis(10)).max(sim.now) };
                    seq += 1;
                    request = None;
                } else {
                    let conn = sim.a.shard.slots[server.idx()].conn.as_ref().unwrap();
                    assert!(
                        sim.now < *started + Duration::from_secs(3),
                        "reply {seq}, slow_upstream={slow_upstream}, started {:?}, elapsed {:?}, server EDT {:?}, server {:?}, client {:?}",
                        *started - origin,
                        sim.now - *started,
                        conn.next_send_time.saturating_since(sim.now),
                        sim.a.shard.info(server),
                        sim.b.shard.info(client)
                    );
                }
            }
            sim.run_until(sim.now + Duration::from_millis(1));
        }
        assert!(recovery_completed > 0, "recovery must execute requests, not pass with no observations");
        assert!(!sim.a.conns[&server].corrupt && !sim.b.conns[&client].corrupt);
        assert_eq!(sim.ab.stats.queue_drops + sim.ab.stats.random_drops + sim.ba.stats.queue_drops + sim.ba.stats.random_drops, 0);
        assert!(sim.a.shard.info(server).unwrap().stats.bytes_retrans > 0, "natural delay must exercise retransmission debt");
        eprintln!("BBR recovery: slow_upstream={slow_upstream}, completed={seq}, recovery_completed={recovery_completed}, max_recovery_latency={max_recovery_latency:?}");
    }

    #[test]
    fn bbr_recovers_after_delayed_requests_and_replies() {
        replies_across_rate_recovery(true);
    }

    #[test]
    fn bbr_recovers_after_spaced_requests_and_delayed_replies() {
        replies_across_rate_recovery(false);
    }
}

#[cfg(test)]
mod drr_reuse_tests {
    use super::{Drr, DRR_SPARE_CAP, DRR_SPARE_Q};

    fn peer(n: u64) -> crate::PeerId {
        crate::PeerId(n)
    }

    /// Thread-local allocation counters for the reuse tests. `Drr` is only
    /// reachable here, so this binary-level hook is the only way to assert that
    /// a reuse path allocates nothing. Tests run in parallel, hence per-thread.
    mod alloc_probe {
        use std::alloc::{GlobalAlloc, Layout, System};
        use std::cell::Cell;

        thread_local! {
            static ALLOCS: Cell<u64> = const { Cell::new(0) };
        }

        struct Counting;
        unsafe impl GlobalAlloc for Counting {
            unsafe fn alloc(&self, l: Layout) -> *mut u8 {
                ALLOCS.with(|c| c.set(c.get() + 1));
                System.alloc(l)
            }
            unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
                System.dealloc(p, l)
            }
            unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
                ALLOCS.with(|c| c.set(c.get() + 1));
                System.realloc(p, l, n)
            }
        }
        #[global_allocator]
        static PROBE_ALLOC: Counting = Counting;

        pub(super) fn allocs() -> u64 {
            ALLOCS.with(|c| c.get())
        }
    }

    /// Drain a peer's queue the way the serve loop does, so the next `recycle`
    /// sees an empty queue.
    fn drain(drr: &mut Drr, peer: crate::PeerId) {
        let q = drr.peer_q.get_mut(&peer).expect("queued peer");
        while q.pop_front().is_some() {}
    }

    /// A genuinely drained per-peer queue must be recycled so the next ready
    /// burst of any peer reuses the backing with no allocation.
    #[test]
    fn drained_queue_backing_is_reused_without_allocating() {
        let mut drr = Drr::default();
        drr.push(peer(1), (0, 0));
        assert_eq!(drr.spare_q.len(), 0);
        assert!(drr.peer_q[&peer(1)].capacity() > 0, "push must have allocated backing");

        // The serve loop pops the peer from `ready_peers` before draining.
        assert_eq!(drr.ready_peers.pop_front(), Some(peer(1)));
        drain(&mut drr, peer(1));
        drr.recycle(peer(1));
        assert!(drr.is_empty());
        assert!(drr.peer_q.is_empty());
        assert_eq!(drr.spare_q.len(), 1, "drained queue must be retained");

        // The warm map and recycled queue mean the reuse push allocates nothing.
        let before = alloc_probe::allocs();
        drr.push(peer(2), (7, 3));
        let after = alloc_probe::allocs();
        assert_eq!(after - before, 0, "queue reuse must not allocate");
        assert_eq!(drr.spare_q.len(), 0, "reuse must consume the spare queue");
        assert_eq!(drr.peer_q[&peer(2)][0], (7, 3));
        assert_eq!(drr.ready_peers.len(), 1);
    }

    /// Reuse after the deque head has moved must preserve FIFO order and not
    /// leave stale tuples behind.
    #[test]
    fn reuse_preserves_fifo_after_head_movement() {
        let mut drr = Drr::default();
        for i in 0..3u32 {
            drr.push(peer(1), (i, 0));
        }
        assert_eq!(drr.ready_peers.pop_front(), Some(peer(1)));
        drain(&mut drr, peer(1));
        drr.recycle(peer(1));

        // The reused ring must serve the new peer's entries in order.
        drr.push(peer(2), (10, 0));
        drr.push(peer(2), (11, 0));
        assert_eq!(drr.ready_peers.len(), 1);
        assert_eq!(drr.peer_q[&peer(2)].len(), 2);
        let q = drr.peer_q.get_mut(&peer(2)).unwrap();
        assert_eq!(q.pop_front(), Some((10, 0)));
        assert_eq!(q.pop_front(), Some((11, 0)));
        assert!(q.is_empty());
    }

    /// The reuse pool is bounded: an oversized queue is dropped, and no more
    /// than `DRR_SPARE_Q` small queues are ever retained.
    #[test]
    fn reuse_pool_is_bounded_and_drops_oversized_backings() {
        let mut drr = Drr::default();
        // One oversized queue: pushed beyond DRR_SPARE_CAP then truly drained.
        for i in 0..=(DRR_SPARE_CAP + 4) {
            drr.push(peer(1), (i as u32, 0));
        }
        assert!(drr.peer_q[&peer(1)].capacity() > DRR_SPARE_CAP);
        assert_eq!(drr.ready_peers.pop_front(), Some(peer(1)));
        drain(&mut drr, peer(1));
        drr.recycle(peer(1));
        assert!(drr.spare_q.is_empty(), "oversized backing must be released, not cached");

        // Many small queues, each genuinely drained: the spare pool never
        // exceeds its hard cap.
        for p in 0..(DRR_SPARE_Q as u64 * 2) {
            drr.push(peer(p + 10), (p as u32, 0));
        }
        while let Some(p) = drr.ready_peers.pop_front() {
            drain(&mut drr, p);
            drr.recycle(p);
        }
        assert_eq!(drr.spare_q.len(), DRR_SPARE_Q);
    }

    /// Recycling a peer that is not present is a no-op, and `ready_peers`
    /// keeps at most one entry per peer across a full recycle-and-reuse cycle.
    #[test]
    fn ready_peers_stays_unique_across_push_and_recycle() {
        let mut drr = Drr::default();
        drr.recycle(peer(9));
        assert!(drr.spare_q.is_empty());
        drr.push(peer(1), (0, 0));
        drr.push(peer(1), (1, 0));
        assert_eq!(drr.ready_peers.len(), 1, "a peer is queued once");
        assert_eq!(drr.peer_q[&peer(1)].len(), 2);

        // Drain, recycle, then reuse for the same peer: still one ready entry.
        assert_eq!(drr.ready_peers.pop_front(), Some(peer(1)));
        drain(&mut drr, peer(1));
        drr.recycle(peer(1));
        drr.push(peer(1), (2, 0));
        drr.push(peer(1), (3, 0));
        assert_eq!(drr.ready_peers.len(), 1);
        assert_eq!(drr.peer_q[&peer(1)].len(), 2);
    }
}
