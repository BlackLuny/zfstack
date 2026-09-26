//! Memory accounting and quotas (§6.2, §6.3, §6.7).
//!
//! * [`GlobalBudget`] charges physical backing before allocation and is shared
//!   across shards; its `high` is a hard managed-memory limit.
//! * [`Budget`] keeps separate TCP payload/window quotas per port and peer.

use crate::PeerId;
use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, Weak};
use std::task::Waker;

/// Conservative per-slot reservation for the compact state and the growing
/// slot, tuple, timer and pending-reply containers (including growth slack).
/// Full connection state and payload allocations require separate accounting.
pub const TIME_WAIT_SLOT_BYTES: u64 = 640;
/// The portion kept after a tuple expires while its slot and indexed vectors
/// remain allocated. The rest of its TIME_WAIT charge is released at expiry.
pub(crate) const RETAINED_SLOT_BYTES: u64 = 384;
/// Default headroom for one host egress packet (docs/design/0005 §2). A host
/// whose largest charged egress packet differs sets it with
/// [`GlobalBudget::ensure_egress_reserve`].
pub const DEFAULT_EGRESS_RESERVE: u64 = 8 * 1024;
/// Default room bulk buffering leaves for admission: the core state of four
/// connections plus slack. It only has to hold a burst of admissions, since
/// it is free again whenever bulk refills to its own limit. A host adapter
/// raises it with its own stream state ([`GlobalBudget::ensure_admit_reserve`]).
pub const ADMIT_BURST: u64 = 4;
pub const DEFAULT_ADMIT_RESERVE: u64 = ADMIT_BURST * (crate::shard::ACTIVE_STATE_BYTES + 1024);
/// Smallest growth that lets a drained connection record its next segment, or
/// lets the adapter take one more RX chunk from the core.
const META_RESERVE: u64 = {
    let rx_index = 8 * std::mem::size_of::<bytes::Bytes>() as u64 * 2;
    if crate::scoreboard::MIN_RECORD_CHARGE > rx_index {
        crate::scoreboard::MIN_RECORD_CHARGE
    } else {
        rx_index
    }
};

#[derive(Debug)]
pub struct GlobalBudget {
    high: u64,
    low: u64,
    pressure: u64,
    reserved: AtomicU64,
    /// A failed hierarchical allocation rolls back its temporary global
    /// reservation without a release notification. Serialize admission so
    /// another allocator cannot park on that uncommitted reservation.
    allocation_lock: Mutex<()>,
    active_limit: u64,
    time_wait_limit: u64,
    time_wait_bytes_limit: u64,
    active: AtomicU64,
    time_wait_reserved: AtomicU64,
    time_wait_bytes_reserved: AtomicU64,
    cached_bytes: AtomicU64,
    cache_limit: u64,
    release_epoch: AtomicU64,
    next_waiter: AtomicU64,
    waiting: AtomicUsize,
    waiters: Mutex<HashMap<u64, Waker>>,
    /// Physical allocation waiters are served one at a time. Broadcasting a
    /// single freed TX block to every stream of a peer causes a retry storm.
    physical_waiters: Mutex<VecDeque<PhysicalWaiter>>,
    /// Length of `physical_waiters`, published under its lock. Every lease
    /// drop and logical release calls `wake_waiters`; with no waiter parked
    /// that path must stay lock-free instead of taking the process-wide
    /// allocation lock on every packet.
    physical_waiting: AtomicUsize,
    egress_reserve: AtomicU64,
    admit_reserve: AtomicU64,
    /// Bumped when an allocation fails while idle TX blocks are cached. Each
    /// shard compares it on its next round and drops its idle blocks.
    cache_reclaim_epoch: AtomicU64,
    /// One waker per shard owner task, so a reclaim request reaches shards
    /// that would otherwise sleep until their own next packet or timer.
    cache_holders: Mutex<HashMap<u64, Waker>>,
}

#[derive(Debug)]
struct PhysicalWaiter {
    id: u64,
    port: Weak<AtomicU64>,
    port_limit: u64,
    peer: Weak<AtomicU64>,
    peer_limit: u64,
    bytes: u64,
    kind: AllocationKind,
    waker: Waker,
}

impl PhysicalWaiter {
    fn counters(&self) -> Option<(Arc<AtomicU64>, Arc<AtomicU64>)> {
        self.port.upgrade().zip(self.peer.upgrade())
    }
}

impl GlobalBudget {
    /// `high` is the hard limit; low/pressure are 3/8 and 5/8 of it (3%/5%/8% of memory, §6.3).
    pub fn new(high: u64) -> Arc<Self> {
        Self::with_connection_limits(high, u64::MAX, u64::MAX)
    }

    /// Share connection admission across every shard using this budget. Each
    /// active connection reserves a future TIME_WAIT entry at admission, so a
    /// close never has to discard a valid TIME_WAIT state for lack of a slot.
    pub fn with_connection_limits(high: u64, active_limit: u64, time_wait_limit: u64) -> Arc<Self> {
        Self::with_resource_limits(high, active_limit, time_wait_limit, high)
    }

    pub fn with_resource_limits(high: u64, active_limit: u64, time_wait_limit: u64, time_wait_bytes_limit: u64) -> Arc<Self> {
        assert!(time_wait_bytes_limit <= high);
        Arc::new(GlobalBudget {
            high,
            low: ((high as u128 * 3) / 8) as u64,
            pressure: ((high as u128 * 5) / 8) as u64,
            reserved: AtomicU64::new(0),
            active_limit,
            time_wait_limit,
            time_wait_bytes_limit,
            allocation_lock: Mutex::new(()),
            active: AtomicU64::new(0),
            time_wait_reserved: AtomicU64::new(0),
            time_wait_bytes_reserved: AtomicU64::new(0),
            cached_bytes: AtomicU64::new(0),
            cache_limit: (high / 8).max(128 * 1024).min(high).min(16 << 20),
            release_epoch: AtomicU64::new(0),
            next_waiter: AtomicU64::new(1),
            waiting: AtomicUsize::new(0),
            waiters: Mutex::new(HashMap::new()),
            physical_waiters: Mutex::new(VecDeque::new()),
            physical_waiting: AtomicUsize::new(0),
            egress_reserve: AtomicU64::new(DEFAULT_EGRESS_RESERVE),
            admit_reserve: AtomicU64::new(DEFAULT_ADMIT_RESERVE),
            cache_reclaim_epoch: AtomicU64::new(0),
            cache_holders: Mutex::new(HashMap::new()),
        })
    }

    pub fn connection_limits(&self) -> (u64, u64) {
        (self.active_limit, self.time_wait_limit)
    }

    pub fn time_wait_memory_limit(&self) -> u64 {
        self.time_wait_bytes_limit
    }

    pub fn connection_counts(&self) -> (u64, u64) {
        (self.active.load(Ordering::Relaxed), self.time_wait_reserved.load(Ordering::Relaxed))
    }

    pub fn time_wait_bytes_reserved(&self) -> u64 {
        self.time_wait_bytes_reserved.load(Ordering::Relaxed)
    }

    pub fn cached_bytes(&self) -> u64 {
        self.cached_bytes.load(Ordering::Relaxed)
    }

    fn try_cache(&self, bytes: u64) -> bool {
        reserve_amount(&self.cached_bytes, self.cache_limit, bytes)
    }

    fn release_cache(&self, bytes: u64) {
        self.cached_bytes.fetch_sub(bytes, Ordering::AcqRel);
    }

    /// Raise the egress headroom to at least the largest charge of one host
    /// egress packet. Ports sharing this budget may call it with their own
    /// packet size; the reserve only grows.
    pub fn ensure_egress_reserve(&self, bytes: u64) {
        self.egress_reserve.fetch_max(bytes, Ordering::Relaxed);
    }

    /// Raise the room bulk buffering leaves for admitting new connections
    /// (their core and host state); the reserve only grows.
    pub fn ensure_admit_reserve(&self, bytes: u64) {
        self.admit_reserve.fetch_max(bytes, Ordering::Relaxed);
    }

    /// Bytes a `tier` allocation must leave free below a level's `limit`
    /// (docs/design/0005 §2). A level too small to hold the reserves twice
    /// runs without them; [`Self::progress_reserve_active`] reports that.
    pub fn headroom(&self, limit: u64, tier: Tier) -> u64 {
        let egress = self.egress_reserve.load(Ordering::Relaxed);
        let drain = egress.saturating_add(META_RESERVE);
        let block = drain.saturating_add(crate::buf::TX_BLOCK_CHARGE);
        let admit = block.saturating_add(self.admit_reserve.load(Ordering::Relaxed));
        if limit / 2 < admit {
            return 0;
        }
        match tier {
            Tier::Egress => 0,
            Tier::Drain => egress,
            Tier::Block => drain,
            Tier::Admit => block,
            Tier::Bulk => admit,
        }
    }

    pub fn progress_reserve_active(&self, limit: u64) -> bool {
        self.headroom(limit, Tier::Bulk) != 0
    }

    fn tier_limit(&self, limit: u64, tier: Tier) -> u64 {
        limit - self.headroom(limit, tier)
    }

    fn has_waiters(&self) -> bool {
        self.waiting.load(Ordering::SeqCst) != 0 || self.physical_waiting.load(Ordering::SeqCst) != 0
    }

    pub fn cache_reclaim_epoch(&self) -> u64 {
        self.cache_reclaim_epoch.load(Ordering::Acquire)
    }

    /// Ask every shard to drop its idle TX blocks. Called after a failed
    /// allocation; a no-op while nothing is cached.
    pub fn request_cache_reclaim(&self) {
        if self.cached_bytes.load(Ordering::Acquire) == 0 {
            return;
        }
        self.cache_reclaim_epoch.fetch_add(1, Ordering::AcqRel);
        let holders: Vec<Waker> = self.cache_holders.lock().unwrap().values().cloned().collect();
        for waker in holders {
            waker.wake();
        }
    }

    pub fn register_cache_holder(&self, id: u64, waker: &Waker) {
        self.cache_holders.lock().unwrap().insert(id, waker.clone());
    }

    pub fn remove_cache_holder(&self, id: u64) {
        self.cache_holders.lock().unwrap().remove(&id);
    }

    pub fn try_acquire_connection(self: &Arc<Self>) -> Option<ConnectionPermit> {
        let _allocation = self.allocation_lock.lock().unwrap();
        if !self.try_reserve(TIME_WAIT_SLOT_BYTES, Tier::Admit) {
            return None;
        }
        if !reserve_amount(&self.time_wait_bytes_reserved, self.time_wait_bytes_limit, TIME_WAIT_SLOT_BYTES) {
            self.release_uncommitted(TIME_WAIT_SLOT_BYTES);
            return None;
        }
        if !reserve_count(&self.time_wait_reserved, self.time_wait_limit) {
            self.time_wait_bytes_reserved.fetch_sub(TIME_WAIT_SLOT_BYTES, Ordering::AcqRel);
            self.release_uncommitted(TIME_WAIT_SLOT_BYTES);
            return None;
        }
        if !reserve_count(&self.active, self.active_limit) {
            self.time_wait_reserved.fetch_sub(1, Ordering::AcqRel);
            self.time_wait_bytes_reserved.fetch_sub(TIME_WAIT_SLOT_BYTES, Ordering::AcqRel);
            self.release_uncommitted(TIME_WAIT_SLOT_BYTES);
            return None;
        }
        Some(ConnectionPermit { global: Arc::clone(self), active: true, retired: false })
    }

    /// Default: 8% of min(cgroup memory.max, physical memory).
    pub fn from_system() -> Arc<Self> {
        let mem = system_memory().unwrap_or(1 << 30);
        Self::new(((mem as u128 * 8) / 100) as u64)
    }

    pub fn reserved(&self) -> u64 {
        self.reserved.load(Ordering::Relaxed)
    }
    pub fn high(&self) -> u64 {
        self.high
    }

    fn try_reserve(&self, n: u64, tier: Tier) -> bool {
        let limit = self.tier_limit(self.high, tier);
        let mut cur = self.reserved.load(Ordering::Relaxed);
        loop {
            if n > limit.saturating_sub(cur) {
                return false;
            }
            match self.reserved.compare_exchange_weak(cur, cur + n, Ordering::AcqRel, Ordering::Relaxed) {
                Ok(_) => return true,
                Err(v) => cur = v,
            }
        }
    }

    fn release_uncommitted(&self, n: u64) {
        self.reserved.fetch_sub(n, Ordering::AcqRel);
    }

    fn release(&self, n: u64) {
        self.release_uncommitted(n);
        self.wake_waiters();
    }

    pub fn release_epoch(&self) -> u64 {
        self.release_epoch.load(Ordering::Acquire)
    }

    pub fn new_waiter_id(&self) -> u64 {
        self.next_waiter.fetch_add(1, Ordering::Relaxed)
    }

    /// If a release raced the failed allocation, the caller's task is woken
    /// immediately so it can retry without a lost notification.
    pub fn register_waiter(&self, id: u64, observed: u64, waker: &Waker) {
        let mut waiters = self.waiters.lock().unwrap();
        // Publish before reading the epoch (both SeqCst, pairs with
        // `wake_waiters`). Checking first let a release bump the epoch and
        // read `waiting == 0` between the check and the insert, losing it.
        if waiters.insert(id, waker.clone()).is_none() {
            self.waiting.fetch_add(1, Ordering::SeqCst);
        }
        if self.release_epoch.load(Ordering::SeqCst) != observed {
            if waiters.remove(&id).is_some() {
                self.waiting.fetch_sub(1, Ordering::SeqCst);
            }
            drop(waiters);
            waker.wake_by_ref();
        }
    }

    /// Park a stream that needs one more physical allocation. A release wakes
    /// only the waiters that fit the available global, port, and peer shares;
    /// each must still retry the real reservation if another allocator wins.
    ///
    /// `kind` sets the waiter's tier for the fit check. A `TxBlock` waiter can
    /// also reuse an already charged cache entry without new global or port
    /// capacity, so one such waiter may retry when a cache exists.
    pub fn register_physical_waiter(&self, id: u64, observed: u64, waker: &Waker, memory: &MemoryHandle, bytes: u64, kind: AllocationKind) {
        let raced = {
            let mut waiters = self.physical_waiters.lock().unwrap();
            if let Some(entry) = waiters.iter_mut().find(|entry| entry.id == id) {
                entry.port = Arc::downgrade(&memory.port);
                entry.port_limit = memory.port_limit;
                entry.peer = Arc::downgrade(&memory.peer);
                entry.peer_limit = memory.peer_limit;
                entry.waker.clone_from(waker);
                entry.bytes = bytes;
                entry.kind = kind;
            } else {
                waiters.push_back(PhysicalWaiter {
                    id,
                    port: Arc::downgrade(&memory.port),
                    port_limit: memory.port_limit,
                    peer: Arc::downgrade(&memory.peer),
                    peer_limit: memory.peer_limit,
                    bytes,
                    kind,
                    waker: waker.clone(),
                });
            }
            // Pairs with `wake_waiters`: publish the waiter, then read the
            // epoch (both SeqCst). A releaser that skipped the scan because it
            // saw no waiter must have bumped the epoch first, so we see it.
            self.physical_waiting.store(waiters.len(), Ordering::SeqCst);
            self.release_epoch.load(Ordering::SeqCst) != observed
        };
        if raced {
            self.wake_eligible_physical_waiters();
        }
    }

    fn wake_eligible_physical_waiters(&self) {
        let ready = {
            // Admission may temporarily reserve the global share before a
            // port/peer rejection rolls it back without a release event.
            // Snapshot all three limits outside such an uncommitted window.
            let _allocation = self.allocation_lock.lock().unwrap();
            let mut waiters = self.physical_waiters.lock().unwrap();
            let reserved = self.reserved.load(Ordering::Acquire);
            let mut global_selected = 0u64;
            let mut selected_ports: HashMap<*const AtomicU64, u64> = HashMap::new();
            let mut selected_peers: HashMap<*const AtomicU64, u64> = HashMap::new();
            let cache_available = self.cached_bytes.load(Ordering::Acquire) != 0;
            let mut cache_retry_selected = false;
            let len = waiters.len();
            let mut ready = Vec::new();
            for _ in 0..len {
                let entry = waiters.pop_front().unwrap();
                let Some((port, peer)) = entry.counters() else {
                    continue;
                };
                let port_key = Arc::as_ptr(&port);
                let peer_key = Arc::as_ptr(&peer);
                let port_selected = selected_ports.get(&port_key).copied().unwrap_or(0);
                let peer_selected = selected_peers.get(&peer_key).copied().unwrap_or(0);
                let tier = entry.kind.tier();
                let peer_limit = self.tier_limit(entry.peer_limit, tier);
                let port_limit = self.tier_limit(entry.port_limit, tier);
                let global_limit = self.tier_limit(self.high, tier);
                let peer_fits = entry.bytes <= peer_limit.saturating_sub(peer.load(Ordering::Acquire)).saturating_sub(peer_selected);
                let new_allocation_fits = entry.bytes <= global_limit.saturating_sub(reserved).saturating_sub(global_selected)
                    && entry.bytes <= port_limit.saturating_sub(port.load(Ordering::Acquire)).saturating_sub(port_selected);
                let cacheable = matches!(entry.kind, AllocationKind::TxBlock);
                let cache_retry = !new_allocation_fits && cacheable && cache_available && !cache_retry_selected;
                if peer_fits && (new_allocation_fits || cache_retry) {
                    if new_allocation_fits {
                        global_selected += entry.bytes;
                        selected_ports.insert(port_key, port_selected + entry.bytes);
                    } else {
                        cache_retry_selected = true;
                    }
                    selected_peers.insert(peer_key, peer_selected + entry.bytes);
                    // Retain the counters for this scan so their pointer
                    // identities cannot be reused by a different peer.
                    ready.push((entry.waker, port, peer));
                } else {
                    waiters.push_back(entry);
                }
            }
            if waiters.is_empty() && waiters.capacity() > 1024 {
                *waiters = VecDeque::new();
            }
            self.physical_waiting.store(waiters.len(), Ordering::SeqCst);
            ready
        };
        for (waker, _port, _peer) in ready {
            waker.wake();
        }
    }

    /// Physical waiters parked on one port's share, by allocation kind.
    pub fn physical_waiters_on(&self, port: &Arc<AtomicU64>) -> KindCounts {
        let mut counts = KindCounts::default();
        if self.physical_waiting.load(Ordering::SeqCst) == 0 {
            return counts;
        }
        let waiters = self.physical_waiters.lock().unwrap();
        for entry in waiters.iter().filter(|entry| std::ptr::eq(entry.port.as_ptr(), Arc::as_ptr(port))) {
            *counts.get_mut(entry.kind) += 1;
        }
        counts
    }

    /// Called on every successful send/write. A waiter id is registered and
    /// removed by its single owner, so an owner that sees an empty set cannot
    /// have an entry of its own there; skip both locks in the common case.
    pub fn remove_waiter(&self, id: u64) {
        if self.waiting.load(Ordering::Acquire) != 0 {
            let mut waiters = self.waiters.lock().unwrap();
            if waiters.remove(&id).is_some() {
                self.waiting.fetch_sub(1, Ordering::Release);
                if waiters.is_empty() && waiters.capacity() > 1024 {
                    *waiters = HashMap::new();
                }
            }
        }
        if self.physical_waiting.load(Ordering::SeqCst) != 0 {
            let mut physical = self.physical_waiters.lock().unwrap();
            physical.retain(|entry| entry.id != id);
            if physical.is_empty() && physical.capacity() > 1024 {
                *physical = VecDeque::new();
            }
            self.physical_waiting.store(physical.len(), Ordering::SeqCst);
        }
    }

    fn wake_waiters(&self) {
        self.release_epoch.fetch_add(1, Ordering::SeqCst);
        if self.waiting.load(Ordering::SeqCst) != 0 {
            let waiters = {
                let mut waiters = self.waiters.lock().unwrap();
                self.waiting.store(0, Ordering::Release);
                std::mem::take(&mut *waiters)
            };
            for waker in waiters.into_values() {
                waker.wake();
            }
        }
        if self.physical_waiting.load(Ordering::SeqCst) != 0 {
            self.wake_eligible_physical_waiters();
        }
    }

    pub fn level(&self) -> Pressure {
        let r = self.reserved();
        if r >= self.high {
            Pressure::High
        } else if r >= self.pressure {
            Pressure::Pressure
        } else if r >= self.low {
            Pressure::Low
        } else {
            Pressure::Free
        }
    }
}

fn reserve_count(count: &AtomicU64, limit: u64) -> bool {
    reserve_amount(count, limit, 1)
}

fn reserve_amount(count: &AtomicU64, limit: u64, amount: u64) -> bool {
    let mut cur = count.load(Ordering::Relaxed);
    loop {
        if amount > limit.saturating_sub(cur) {
            return false;
        }
        match count.compare_exchange_weak(cur, cur + amount, Ordering::AcqRel, Ordering::Relaxed) {
            Ok(_) => return true,
            Err(v) => cur = v,
        }
    }
}

/// Reservation for one live TCP state. Its TIME_WAIT share remains reserved
/// until the state is removed; a later compact tombstone can retain that share
/// while releasing the active share.
pub struct ConnectionPermit {
    global: Arc<GlobalBudget>,
    active: bool,
    retired: bool,
}

impl ConnectionPermit {
    /// Reactivate the same tuple after a safe TIME_WAIT reuse decision. This
    /// keeps its reserved future tombstone slot and only reacquires an active
    /// connection share; failure leaves the old tombstone untouched.
    pub fn try_reactivate(&mut self) -> bool {
        if self.active {
            return true;
        }
        if !reserve_count(&self.global.active, self.global.active_limit) {
            return false;
        }
        self.active = true;
        true
    }

    /// Call only after the full connection has been replaced with its compact
    /// TIME_WAIT state. The reserved TIME_WAIT entry remains owned by this permit.
    pub fn to_time_wait(&mut self) {
        if self.active {
            self.active = false;
            self.global.active.fetch_sub(1, Ordering::AcqRel);
        }
    }

    /// A removed tuple no longer occupies a TIME_WAIT entry, but its stable
    /// slot may still retain vector capacity until the shard compacts. Move
    /// only that physical share to the shard's retained-metadata account.
    fn retire_to_slot(mut self) {
        if self.active {
            self.global.active.fetch_sub(1, Ordering::AcqRel);
            self.active = false;
        }
        self.global.time_wait_reserved.fetch_sub(1, Ordering::AcqRel);
        self.global.time_wait_bytes_reserved.fetch_sub(TIME_WAIT_SLOT_BYTES, Ordering::AcqRel);
        self.retired = true;
        self.global.release(TIME_WAIT_SLOT_BYTES - RETAINED_SLOT_BYTES);
    }
}

impl Drop for ConnectionPermit {
    fn drop(&mut self) {
        if self.retired {
            return;
        }
        if self.active {
            self.global.active.fetch_sub(1, Ordering::AcqRel);
        }
        self.global.time_wait_reserved.fetch_sub(1, Ordering::AcqRel);
        self.global.time_wait_bytes_reserved.fetch_sub(TIME_WAIT_SLOT_BYTES, Ordering::AcqRel);
        self.global.release(TIME_WAIT_SLOT_BYTES);
    }
}

/// Shard-owned physical charge for free slot capacity and compact generations.
/// Kept after a connection permit expires, then released when its backing is
/// reused or deallocated. This is global metadata rather than peer payload.
pub(crate) struct RetainedMetadata {
    global: Arc<GlobalBudget>,
    free_slots: u64,
    generations_bytes: u64,
}

impl RetainedMetadata {
    pub(crate) fn new(global: Arc<GlobalBudget>) -> Self {
        Self { global, free_slots: 0, generations_bytes: 0 }
    }

    pub(crate) fn retain_slot(&mut self, permit: ConnectionPermit) {
        self.free_slots += 1;
        permit.retire_to_slot();
    }

    pub(crate) fn reuse_slot(&mut self) {
        self.free_slots -= 1;
        self.global.release(RETAINED_SLOT_BYTES);
    }

    pub(crate) fn free_slot_bytes(&self) -> u64 {
        self.free_slots * RETAINED_SLOT_BYTES
    }

    pub(crate) fn total_bytes(&self) -> u64 {
        self.free_slot_bytes() + self.generations_bytes
    }

    /// Once all tuples leave, the large slot/index containers are replaced by
    /// a compact generation array. Transfer the retained share before waking
    /// waiters; no allocation can observe freed credit while the old backing
    /// is still live.
    pub(crate) fn reclaim_to_generations(&mut self, generations_bytes: u64) {
        let previous = self.total_bytes();
        assert!(generations_bytes <= previous, "generation table exceeds retained metadata credit");
        self.free_slots = 0;
        self.generations_bytes = generations_bytes;
        if previous > generations_bytes {
            self.global.release(previous - generations_bytes);
        }
    }
}

impl Drop for RetainedMetadata {
    fn drop(&mut self) {
        let bytes = self.total_bytes();
        if bytes != 0 {
            self.global.release(bytes);
        }
    }
}

/// Effective memory available to this process. A nested cgroup can be more
/// restrictive than the mount root, so inspect the current cgroup and every
/// ancestor up to its memory-controller mount.
pub fn system_memory() -> Option<u64> {
    let phys = std::fs::read_to_string("/proc/meminfo").ok().and_then(|s| {
        s.lines()
            .find(|l| l.starts_with("MemTotal:"))
            .and_then(|l| l.split_whitespace().nth(1).and_then(|v| v.parse::<u64>().ok()))
            .and_then(|kb| kb.checked_mul(1024))
    });
    let cg = cgroup_memory_limit();
    match (phys, cg) {
        (Some(p), Some(c)) => Some(p.min(c)),
        (p, c) => p.or(c),
    }
}

fn cgroup_memory_limit() -> Option<u64> {
    let membership = std::fs::read_to_string("/proc/self/cgroup").ok()?;
    let mounts = std::fs::read_to_string("/proc/self/mountinfo").ok()?;
    memory_limit_from_mounts(&membership, &mounts)
}

fn memory_limit_from_mounts(membership: &str, mounts: &str) -> Option<u64> {
    let mut smallest = None;
    for (kind, path) in cgroup_memberships(membership) {
        for (root, mount) in memory_mounts(mounts, kind) {
            let Some(current) = cgroup_mount_path(&root, &mount, &path) else { continue };
            let file = if kind == "cgroup2" { "memory.max" } else { "memory.limit_in_bytes" };
            let mut dir = current.as_path();
            loop {
                if let Ok(value) = std::fs::read_to_string(dir.join(file)) {
                    if let Ok(bytes) = value.trim().parse::<u64>() {
                        smallest = Some(smallest.map_or(bytes, |old: u64| old.min(bytes)));
                    }
                }
                if dir == mount {
                    break;
                }
                let Some(parent) = dir.parent() else { break };
                if !parent.starts_with(&mount) {
                    break;
                }
                dir = parent;
            }
        }
    }
    smallest
}

fn cgroup_memberships(data: &str) -> Vec<(&'static str, String)> {
    data.lines()
        .filter_map(|line| {
            let mut fields = line.splitn(3, ':');
            let _hierarchy = fields.next()?;
            let controllers = fields.next()?;
            let path = fields.next()?;
            if controllers.is_empty() {
                Some(("cgroup2", path.to_owned()))
            } else if controllers.split(',').any(|name| name == "memory") {
                Some(("cgroup", path.to_owned()))
            } else {
                None
            }
        })
        .collect()
}

fn memory_mounts(data: &str, kind: &str) -> Vec<(std::path::PathBuf, std::path::PathBuf)> {
    data.lines()
        .filter_map(|line| {
            let (left, right) = line.split_once(" - ")?;
            let mut fields = left.split_whitespace();
            let root = fields.nth(3)?;
            let mount = fields.next()?;
            let mut after = right.split_whitespace();
            if after.next()? != kind {
                return None;
            }
            let _source = after.next()?;
            if kind == "cgroup" && !after.next()?.split(',').any(|name| name == "memory") {
                return None;
            }
            Some((root.into(), mount.into()))
        })
        .collect()
}

fn cgroup_mount_path(root: &std::path::Path, mount: &std::path::Path, path: &str) -> Option<std::path::PathBuf> {
    use std::path::Component;
    let group = std::path::Path::new(path);
    // A cgroup namespace can expose its root as `/`, even when mountinfo
    // names the corresponding host-side subtree.
    let relative = group.strip_prefix(root).ok().or_else(|| group.strip_prefix("/").ok())?;
    if relative.components().any(|c| !matches!(c, Component::Normal(_))) {
        return None;
    }
    Some(mount.join(relative))
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Pressure {
    Free,
    Low,
    Pressure,
    High,
}

#[derive(Default, Debug, Clone, Copy)]
pub struct PeerUsage {
    pub bytes: u64,
    pub conns: u32,
}

/// Progress rank of a physical allocation (docs/design/0005 §2). A lower rank
/// moves bytes that are already buffered toward the exit; a higher rank admits
/// new bytes or state and must leave the lower ranks their headroom.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Tier {
    /// Host egress packets: released by the host without another allocation.
    Egress,
    /// Send records and adapter RX descriptors: released by ACK or app read.
    Drain,
    /// Core TX blocks taking bytes the adapter already holds.
    Block,
    /// New connections and replies: their state, not their bytes.
    Admit,
    /// New bytes: adapter TX buffers and core RX/OOO chunks. They leave the
    /// admission reserve free so a busy port still admits connections.
    Bulk,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AllocationKind {
    TxBlock,
    RxChunk,
    Ooo,
    SendRecord,
    ActiveState,
    Logical,
    Other,
    AdapterTx,
    AdapterRx,
    StreamState,
    Egress,
    Stateless,
}

impl AllocationKind {
    pub fn tier(self) -> Tier {
        match self {
            AllocationKind::Egress => Tier::Egress,
            AllocationKind::SendRecord | AllocationKind::AdapterRx => Tier::Drain,
            AllocationKind::TxBlock => Tier::Block,
            AllocationKind::ActiveState | AllocationKind::Logical | AllocationKind::Other | AllocationKind::StreamState | AllocationKind::Stateless => {
                Tier::Admit
            }
            AllocationKind::RxChunk | AllocationKind::Ooo | AllocationKind::AdapterTx => Tier::Bulk,
        }
    }
}

/// Counts per allocation kind (failures or parked waiters).
#[derive(Default, Clone, Copy, Debug)]
pub struct KindCounts {
    pub tx_block: u64,
    pub rx_chunk: u64,
    pub ooo: u64,
    pub send_record: u64,
    pub active_state: u64,
    pub logical: u64,
    pub other: u64,
    pub adapter_tx: u64,
    pub adapter_rx: u64,
    pub stream_state: u64,
    pub egress: u64,
    pub stateless: u64,
}

pub type ReserveFailures = KindCounts;

impl KindCounts {
    fn get_mut(&mut self, kind: AllocationKind) -> &mut u64 {
        match kind {
            AllocationKind::TxBlock => &mut self.tx_block,
            AllocationKind::RxChunk => &mut self.rx_chunk,
            AllocationKind::Ooo => &mut self.ooo,
            AllocationKind::SendRecord => &mut self.send_record,
            AllocationKind::ActiveState => &mut self.active_state,
            AllocationKind::Logical => &mut self.logical,
            AllocationKind::Other => &mut self.other,
            AllocationKind::AdapterTx => &mut self.adapter_tx,
            AllocationKind::AdapterRx => &mut self.adapter_rx,
            AllocationKind::StreamState => &mut self.stream_state,
            AllocationKind::Egress => &mut self.egress,
            AllocationKind::Stateless => &mut self.stateless,
        }
    }
}

/// The budget level whose share rejected an allocation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Level {
    Global,
    Port,
    Peer,
}

/// Failure counters of one port, shared by its core, adapter and host handles.
#[derive(Default, Debug)]
pub struct PortStats {
    failures: Mutex<KindCounts>,
    by_level: [AtomicU64; 3],
}

impl PortStats {
    fn note(&self, kind: AllocationKind, level: Option<Level>) {
        *self.failures.lock().unwrap().get_mut(kind) += 1;
        if let Some(level) = level {
            self.by_level[level as usize].fetch_add(1, Ordering::Relaxed);
        }
    }

    pub fn failures(&self) -> KindCounts {
        *self.failures.lock().unwrap()
    }

    /// Physical failures rejected by the global, port and peer share.
    pub fn failures_by_level(&self) -> [u64; 3] {
        self.by_level.each_ref().map(|n| n.load(Ordering::Relaxed))
    }
}

/// Shard-local accounting.
pub struct Budget {
    global: Arc<GlobalBudget>,
    physical_port: Arc<AtomicU64>,
    physical_peers: HashMap<PeerId, Arc<AtomicU64>>,
    /// Logical TCP payload retained by the core, distinct from physical backing.
    pub used: u64,
    pub port_limit: u64,
    pub peers: HashMap<PeerId, PeerUsage>,
    pub peer_limit: u64,
    pub peer_max_conns: u32,
    stats: Arc<PortStats>,
}

impl Budget {
    pub fn new(global: Arc<GlobalBudget>) -> Self {
        let peer_limit = global.high / 4;
        let port_limit = global.high;
        Budget {
            global,
            physical_port: Arc::new(AtomicU64::new(0)),
            physical_peers: HashMap::new(),
            used: 0,
            port_limit,
            peers: HashMap::new(),
            peer_limit,
            peer_max_conns: 4096,
            stats: Arc::default(),
        }
    }

    /// Configure hard shard/peer shares before admitting any connection.
    /// Shares are ceilings, not preallocations or guaranteed reservations.
    pub fn set_limits(&mut self, port_bytes: u64, peer_bytes: u64, peer_max_conns: u32) {
        assert_eq!(self.used, 0, "budget limits must be set before ingress");
        assert_eq!(self.physical_used(), 0, "physical limits must be set before allocation");
        assert!(peer_bytes <= port_bytes && port_bytes <= self.global.high);
        self.port_limit = port_bytes;
        self.peer_limit = peer_bytes;
        self.peer_max_conns = peer_max_conns;
    }

    pub fn global(&self) -> &Arc<GlobalBudget> {
        &self.global
    }

    /// Reserve actual backing capacity before allocation. The lease follows
    /// the allocation across the core/adapter boundary and releases on drop.
    pub fn try_allocate(&mut self, peer: PeerId, bytes: u64) -> Option<MemoryLease> {
        self.try_allocate_kind(peer, bytes, AllocationKind::Other)
    }

    pub fn try_allocate_kind(&mut self, peer: PeerId, bytes: u64, kind: AllocationKind) -> Option<MemoryLease> {
        self.memory_handle(peer).try_allocate_kind(bytes, kind)
    }

    pub fn note_reserve_failure(&self, kind: AllocationKind) {
        self.stats.note(kind, None);
    }

    pub fn stats(&self) -> &Arc<PortStats> {
        &self.stats
    }

    /// Core, adapter and host allocation failures on this port.
    pub fn reserve_failures(&self) -> u64 {
        let f = self.stats.failures();
        f.tx_block
            + f.rx_chunk
            + f.ooo
            + f.send_record
            + f.active_state
            + f.logical
            + f.other
            + f.adapter_tx
            + f.adapter_rx
            + f.stream_state
            + f.egress
            + f.stateless
    }

    /// Whether every level of this port keeps the progress reserves.
    pub fn progress_reserve_active(&self) -> bool {
        [self.global.high, self.port_limit, self.peer_limit].into_iter().all(|limit| self.global.progress_reserve_active(limit))
    }

    pub fn physical_waiters(&self) -> KindCounts {
        self.global.physical_waiters_on(&self.physical_port)
    }

    pub fn memory_handle(&mut self, peer: PeerId) -> MemoryHandle {
        let counter = self.physical_peers.entry(peer).or_insert_with(|| Arc::new(AtomicU64::new(0))).clone();
        MemoryHandle {
            global: Arc::clone(&self.global),
            port: Arc::clone(&self.physical_port),
            peer: counter,
            port_limit: self.port_limit,
            peer_limit: self.peer_limit,
            stats: Arc::clone(&self.stats),
        }
    }

    pub fn physical_used(&self) -> u64 {
        self.physical_port.load(Ordering::Relaxed)
    }

    /// A read-only admission hint for a sender parked after an allocation
    /// failure. The actual allocation still performs atomic reservations.
    pub fn can_allocate(&self, peer: PeerId, bytes: u64, kind: AllocationKind) -> bool {
        // A failed allocator may briefly hold the global share before its
        // port/peer check rolls it back. Do not mistake that transient share
        // for durable pressure and park without a future release notification.
        let _allocation = self.global.allocation_lock.lock().unwrap();
        let tier = kind.tier();
        let peer_used = self.physical_peers.get(&peer).map_or(0, |counter| counter.load(Ordering::Relaxed));
        self.global.reserved().checked_add(bytes).is_some_and(|n| n <= self.global.tier_limit(self.global.high(), tier))
            && self.physical_used().checked_add(bytes).is_some_and(|n| n <= self.global.tier_limit(self.port_limit, tier))
            && peer_used.checked_add(bytes).is_some_and(|n| n <= self.global.tier_limit(self.peer_limit, tier))
    }

    pub fn level(&self) -> Pressure {
        self.global.level()
    }

    /// Charge TCP payload for flow-control and per-peer byte quotas. Actual
    /// backing is separately charged by `try_allocate` before allocation.
    /// `force` never bypasses either limit.
    pub fn try_reserve(&mut self, peer: PeerId, n: u64, _force: bool) -> bool {
        if n == 0 {
            return true;
        }
        let pu = self.peers.get(&peer).copied().unwrap_or_default();
        let Some(port_used) = self.used.checked_add(n) else {
            self.note_reserve_failure(AllocationKind::Logical);
            return false;
        };
        let Some(peer_used) = pu.bytes.checked_add(n) else {
            self.note_reserve_failure(AllocationKind::Logical);
            return false;
        };
        if port_used > self.port_limit || peer_used > self.peer_limit {
            self.note_reserve_failure(AllocationKind::Logical);
            return false;
        }
        self.used = port_used;
        self.peers.entry(peer).or_default().bytes = peer_used;
        true
    }

    /// Undo a reservation made in the same synchronous operation. No other
    /// shard work can observe it, so this must not wake memory waiters.
    pub fn cancel_reserve(&mut self, peer: PeerId, n: u64) {
        if n == 0 {
            return;
        }
        self.used -= n;
        if let Some(p) = self.peers.get_mut(&peer) {
            p.bytes -= n;
            if p.bytes == 0 && p.conns == 0 {
                self.peers.remove(&peer);
            }
        }
    }

    pub fn release(&mut self, peer: PeerId, n: u64) {
        if n == 0 {
            return;
        }
        self.cancel_reserve(peer, n);
        self.global.wake_waiters();
    }

    pub fn peer_conn_add(&mut self, peer: PeerId) -> bool {
        let e = self.peers.entry(peer).or_default();
        if e.conns >= self.peer_max_conns {
            return false;
        }
        e.conns += 1;
        true
    }

    pub fn peer_conn_del(&mut self, peer: PeerId) {
        if let Some(p) = self.peers.get_mut(&peer) {
            p.conns = p.conns.saturating_sub(1);
            if p.bytes == 0 && p.conns == 0 {
                self.peers.remove(&peer);
            }
        }
    }
}

#[derive(Clone)]
pub struct MemoryHandle {
    global: Arc<GlobalBudget>,
    port: Arc<AtomicU64>,
    peer: Arc<AtomicU64>,
    port_limit: u64,
    peer_limit: u64,
    stats: Arc<PortStats>,
}

impl MemoryHandle {
    /// Allocate at the admission tier. Paths that drain buffered bytes use
    /// [`Self::try_allocate_kind`] with their kind.
    pub fn try_allocate(&self, bytes: u64) -> Option<MemoryLease> {
        self.try_allocate_kind(bytes, AllocationKind::Other)
    }

    pub fn try_allocate_kind(&self, bytes: u64, kind: AllocationKind) -> Option<MemoryLease> {
        let rejected = self.reserve(bytes, kind.tier());
        if let Some(level) = rejected {
            self.stats.note(kind, Some(level));
            // Idle cached blocks may hold the share this allocation needs.
            self.global.request_cache_reclaim();
            return None;
        }
        Some(MemoryLease {
            global: Arc::clone(&self.global),
            port: Arc::clone(&self.port),
            peer: Some(Arc::clone(&self.peer)),
            bytes,
            peer_limit: self.peer_limit,
            cached: false,
        })
    }

    fn reserve(&self, bytes: u64, tier: Tier) -> Option<Level> {
        let global = &self.global;
        let _allocation = global.allocation_lock.lock().unwrap();
        if !global.try_reserve(bytes, tier) {
            return Some(Level::Global);
        }
        if !reserve_amount(&self.port, global.tier_limit(self.port_limit, tier), bytes) {
            global.release_uncommitted(bytes);
            return Some(Level::Port);
        }
        if !reserve_amount(&self.peer, global.tier_limit(self.peer_limit, tier), bytes) {
            self.port.fetch_sub(bytes, Ordering::AcqRel);
            global.release_uncommitted(bytes);
            return Some(Level::Peer);
        }
        None
    }

    pub fn global(&self) -> &Arc<GlobalBudget> {
        &self.global
    }
}

/// One allocation's global/port reservation. Cached blocks can temporarily
/// release their peer share without releasing the still-resident backing.
pub struct MemoryLease {
    global: Arc<GlobalBudget>,
    port: Arc<AtomicU64>,
    peer: Option<Arc<AtomicU64>>,
    bytes: u64,
    peer_limit: u64,
    cached: bool,
}

impl MemoryLease {
    /// Keep an idle block for reuse. While any allocator is parked, the block
    /// is released instead: a cached block would pin the share it waits for
    /// without a failure left to request its reclaim.
    pub fn park_cached(&mut self) -> bool {
        if self.global.has_waiters() || !self.global.try_cache(self.bytes) {
            return false;
        }
        if let Some(peer) = self.peer.take() {
            peer.fetch_sub(self.bytes, Ordering::AcqRel);
            self.global.wake_waiters();
        }
        self.cached = true;
        true
    }

    pub fn assign(&mut self, budget: &mut Budget, peer: PeerId) -> bool {
        debug_assert!(self.peer.is_none());
        let counter = budget.physical_peers.entry(peer).or_insert_with(|| Arc::new(AtomicU64::new(0))).clone();
        if !reserve_amount(&counter, self.global.tier_limit(self.peer_limit, Tier::Block), self.bytes) {
            budget.stats.note(AllocationKind::TxBlock, Some(Level::Peer));
            return false;
        }
        self.peer = Some(counter);
        if self.cached {
            self.cached = false;
            self.global.release_cache(self.bytes);
        }
        true
    }
}

impl Drop for MemoryLease {
    fn drop(&mut self) {
        if self.cached {
            self.global.release_cache(self.bytes);
        }
        if let Some(peer) = self.peer.take() {
            peer.fetch_sub(self.bytes, Ordering::AcqRel);
        }
        self.port.fetch_sub(self.bytes, Ordering::AcqRel);
        self.global.release(self.bytes);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicUsize;
    use std::task::Wake;

    struct CountWake(AtomicUsize);

    impl Wake for CountWake {
        fn wake(self: Arc<Self>) {
            self.0.fetch_add(1, Ordering::Relaxed);
        }
        fn wake_by_ref(self: &Arc<Self>) {
            self.0.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[test]
    fn pressure_thresholds_do_not_wrap_for_large_explicit_budget() {
        let global = GlobalBudget::with_resource_limits(u64::MAX, 1, 1, u64::MAX);
        assert_eq!(global.low, ((u64::MAX as u128 * 3) / 8) as u64);
        assert_eq!(global.pressure, ((u64::MAX as u128 * 5) / 8) as u64);
        assert!(global.low < global.pressure && global.pressure < global.high);
    }

    #[test]
    fn logical_reservation_rejects_counter_overflow() {
        let global = GlobalBudget::new(u64::MAX);
        let mut budget = Budget::new(global);
        budget.set_limits(u64::MAX, u64::MAX, 1);
        let peer = PeerId(1);
        assert!(budget.try_reserve(peer, u64::MAX, false));
        assert!(!budget.try_reserve(peer, 1, false));
        assert_eq!(budget.used, u64::MAX);
        budget.release(peer, u64::MAX);
        assert_eq!(budget.used, 0);
    }

    #[test]
    fn connection_reservations_are_shared_and_released() {
        let global = GlobalBudget::with_connection_limits(1 << 20, 2, 3);
        let a = global.try_acquire_connection().unwrap();
        let b = Arc::clone(&global).try_acquire_connection().unwrap();
        assert!(global.try_acquire_connection().is_none());
        assert_eq!(global.connection_counts(), (2, 2));
        drop(a);
        let c = global.try_acquire_connection().unwrap();
        assert_eq!(global.connection_counts(), (2, 2));
        drop((b, c));
        assert_eq!(global.connection_counts(), (0, 0));

        let global = GlobalBudget::with_connection_limits(1 << 20, 3, 1);
        let mut a = global.try_acquire_connection().unwrap();
        assert!(global.try_acquire_connection().is_none());
        a.to_time_wait();
        assert_eq!(global.connection_counts(), (0, 1));
        assert!(global.try_acquire_connection().is_none());
        drop(a);
        assert_eq!(global.connection_counts(), (0, 0));

        let global = GlobalBudget::with_resource_limits(1 << 20, 3, 3, TIME_WAIT_SLOT_BYTES);
        let a = global.try_acquire_connection().unwrap();
        assert!(global.try_acquire_connection().is_none());
        assert_eq!(global.time_wait_bytes_reserved(), TIME_WAIT_SLOT_BYTES);
        drop(a);
        assert_eq!(global.time_wait_bytes_reserved(), 0);
    }

    #[test]
    fn time_wait_capacity_supports_two_thousand_per_second_for_a_minute() {
        let global = GlobalBudget::with_resource_limits(256 << 20, 16_384, 262_144, 128 << 20);
        let mut tombstones = Vec::with_capacity(120_000);
        for _ in 0..120_000 {
            let mut permit = global.try_acquire_connection().expect("120k TIME_WAIT capacity");
            permit.to_time_wait();
            tombstones.push(permit);
        }
        assert_eq!(global.connection_counts(), (0, 120_000));
        assert_eq!(global.time_wait_bytes_reserved(), 120_000 * TIME_WAIT_SLOT_BYTES);
        assert_eq!(global.reserved(), 120_000 * TIME_WAIT_SLOT_BYTES);
        drop(tombstones);
        assert_eq!(global.connection_counts(), (0, 0));
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn physical_leases_enforce_each_share_and_release_on_last_owner() {
        let global = GlobalBudget::new(2048);
        let mut port_a = Budget::new(global.clone());
        let mut port_b = Budget::new(global.clone());
        port_a.set_limits(1024, 512, 4);
        port_b.set_limits(1024, 512, 4);
        let handle = port_a.memory_handle(PeerId(1));
        let first = handle.try_allocate(400).unwrap();
        assert!(handle.try_allocate(200).is_none());
        let second = port_a.try_allocate(PeerId(2), 400).unwrap();
        assert!(port_a.try_allocate(PeerId(3), 400).is_none());
        let third = port_b.try_allocate(PeerId(1), 400).unwrap();
        assert_eq!(global.reserved(), 1200);
        drop(first);
        assert_eq!(port_a.physical_used(), 400);
        assert!(handle.try_allocate(200).is_some());
        drop((second, third));
        assert_eq!(port_a.physical_used(), 0);
        assert_eq!(port_b.physical_used(), 0);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn release_between_failed_reservation_and_waiter_registration_wakes() {
        let global = GlobalBudget::new(512);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(512, 512, 4);
        let handle = budget.memory_handle(PeerId(1));
        let lease = handle.try_allocate(512).unwrap();
        let observed = global.release_epoch();
        assert!(handle.try_allocate(1).is_none());
        drop(lease);
        let count = Arc::new(CountWake(AtomicUsize::new(0)));
        let waker = Waker::from(count.clone());
        let id = global.new_waiter_id();
        global.register_waiter(id, observed, &waker);
        assert_eq!(count.0.load(Ordering::Relaxed), 1);
        assert!(handle.try_allocate(1).is_some());
        global.remove_waiter(id);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn an_uncommitted_hierarchical_reservation_cannot_reject_another_allocator() {
        let global = GlobalBudget::new(512);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(512, 512, 4);
        let handle = budget.memory_handle(PeerId(1));
        let transaction = global.allocation_lock.lock().unwrap();
        assert!(global.try_reserve(512, Tier::Admit));

        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let (result_tx, result_rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            started_tx.send(()).unwrap();
            result_tx.send(handle.try_allocate(512).is_some()).unwrap();
        });
        started_rx.recv().unwrap();
        assert!(result_rx.recv_timeout(std::time::Duration::from_millis(30)).is_err());
        global.release_uncommitted(512);
        drop(transaction);
        assert!(result_rx.recv_timeout(std::time::Duration::from_secs(1)).unwrap());
        worker.join().unwrap();
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn physical_release_wakes_only_waiters_that_fit_peer_share() {
        let global = GlobalBudget::new(4096);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(4096, 512, 8);
        let peer_a = budget.memory_handle(PeerId(1));
        let peer_b = budget.memory_handle(PeerId(2));
        let held = peer_a.try_allocate(512).unwrap();
        let wakes: Vec<_> = (0..3).map(|_| Arc::new(CountWake(AtomicUsize::new(0)))).collect();
        let ids: Vec<_> = (0..3).map(|_| global.new_waiter_id()).collect();
        let observed = global.release_epoch();
        for (id, wake) in ids.iter().zip(&wakes) {
            global.register_physical_waiter(*id, observed, &Waker::from(wake.clone()), &peer_a, 256, AllocationKind::Other);
        }
        // A release by another peer must skip peer A while its share is full.
        let other = peer_b.try_allocate(256).unwrap();
        drop(other);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 0);
        drop(held);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 2);
        let first = peer_a.try_allocate(256).unwrap();
        let second = peer_a.try_allocate(256).unwrap();
        let other = peer_b.try_allocate(1).unwrap();
        drop(other);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 2);
        drop(first);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 3);
        drop(second);
        for id in ids {
            global.remove_waiter(id);
        }
    }

    #[test]
    fn physical_release_wakes_only_waiters_that_fit_global_share() {
        let global = GlobalBudget::new(1024);
        let mut port = Budget::new(global.clone());
        port.set_limits(1024, 1024, 4);
        let peers: Vec<_> = (1..=3).map(|id| port.memory_handle(PeerId(id))).collect();
        let mut other_port = Budget::new(global.clone());
        other_port.set_limits(1024, 1024, 1);
        let other = other_port.memory_handle(PeerId(4));
        let held_a = other.try_allocate(512).unwrap();
        let held_b = other.try_allocate(512).unwrap();
        let wakes: Vec<_> = (0..3).map(|_| Arc::new(CountWake(AtomicUsize::new(0)))).collect();
        let ids: Vec<_> = (0..3).map(|_| global.new_waiter_id()).collect();
        let observed = global.release_epoch();
        for ((id, wake), peer) in ids.iter().zip(&wakes).zip(&peers) {
            global.register_physical_waiter(*id, observed, &Waker::from(wake.clone()), peer, 256, AllocationKind::Other);
        }
        drop(held_a);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 2);
        let first = peers[0].try_allocate(256).unwrap();
        let second = peers[1].try_allocate(256).unwrap();
        drop(held_b);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 3);
        drop((first, second));
        for id in ids {
            global.remove_waiter(id);
        }
    }

    #[test]
    fn physical_release_wakes_only_waiters_that_fit_port_share() {
        let global = GlobalBudget::new(2048);
        let mut port = Budget::new(global.clone());
        port.set_limits(512, 512, 4);
        let held_peer = port.memory_handle(PeerId(0));
        let held = held_peer.try_allocate(512).unwrap();
        let peers: Vec<_> = (1..=3).map(|id| port.memory_handle(PeerId(id))).collect();
        let wakes: Vec<_> = (0..3).map(|_| Arc::new(CountWake(AtomicUsize::new(0)))).collect();
        let ids: Vec<_> = (0..3).map(|_| global.new_waiter_id()).collect();
        let observed = global.release_epoch();
        for ((id, wake), peer) in ids.iter().zip(&wakes).zip(&peers) {
            global.register_physical_waiter(*id, observed, &Waker::from(wake.clone()), peer, 256, AllocationKind::Other);
        }
        drop(held);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 2);
        let first = peers[0].try_allocate(256).unwrap();
        let second = peers[1].try_allocate(256).unwrap();
        let mut other_port = Budget::new(global.clone());
        let unrelated = other_port.memory_handle(PeerId(4)).try_allocate(1).unwrap();
        drop(unrelated);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 2);
        drop(first);
        assert_eq!(wakes.iter().map(|w| w.0.load(Ordering::Relaxed)).sum::<usize>(), 3);
        drop(second);
        for id in ids {
            global.remove_waiter(id);
        }
    }

    #[test]
    fn cached_tx_block_wakes_one_peer_even_when_global_and_port_are_full() {
        let global = GlobalBudget::new(2048);
        let mut port = Budget::new(global.clone());
        port.set_limits(2048, 2048, 3);
        let original = port.memory_handle(PeerId(1));
        let replacement = port.memory_handle(PeerId(2));
        let other = port.memory_handle(PeerId(3));
        // The block was cached before anyone waited; the rest of the share is held.
        let mut cached = original.try_allocate(1024).unwrap();
        assert!(cached.park_cached());
        let held = other.try_allocate(1000).unwrap();
        let tiny = other.try_allocate(24).unwrap();
        let ordinary_wake = Arc::new(CountWake(AtomicUsize::new(0)));
        let cache_wake = Arc::new(CountWake(AtomicUsize::new(0)));
        let ordinary_id = global.new_waiter_id();
        let cache_id = global.new_waiter_id();
        let observed = global.release_epoch();
        global.register_physical_waiter(ordinary_id, observed, &Waker::from(ordinary_wake.clone()), &replacement, 1024, AllocationKind::Other);
        global.register_physical_waiter(cache_id, observed, &Waker::from(cache_wake.clone()), &replacement, 1024, AllocationKind::TxBlock);

        // A release too small for a fresh block still lets one TX waiter try the cache.
        drop(tiny);
        assert_eq!(ordinary_wake.0.load(Ordering::Relaxed), 0);
        assert_eq!(cache_wake.0.load(Ordering::Relaxed), 1);
        assert!(cached.assign(&mut port, PeerId(2)));
        global.remove_waiter(ordinary_id);
        global.remove_waiter(cache_id);
        drop((cached, held));
        assert_eq!(global.reserved(), 0);
    }

    /// A freed block is not cached while an allocator waits: parked, it would
    /// pin the share with no failure left to request its reclaim (#664).
    #[test]
    fn freed_block_is_released_instead_of_cached_while_allocators_wait() {
        let global = GlobalBudget::new(1024);
        let mut port = Budget::new(global.clone());
        port.set_limits(1024, 1024, 2);
        let original = port.memory_handle(PeerId(1));
        let replacement = port.memory_handle(PeerId(2));
        let mut block = original.try_allocate(1024).unwrap();
        let wake = Arc::new(CountWake(AtomicUsize::new(0)));
        let id = global.new_waiter_id();
        global.register_physical_waiter(id, global.release_epoch(), &Waker::from(wake.clone()), &replacement, 1024, AllocationKind::AdapterTx);
        assert!(!block.park_cached());
        assert_eq!(global.cached_bytes(), 0);
        drop(block);
        assert_eq!(wake.0.load(Ordering::Relaxed), 1);
        let lease = replacement.try_allocate_kind(1024, AllocationKind::AdapterTx).unwrap();
        global.remove_waiter(id);
        drop(lease);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn temporary_reply_lease_release_does_not_retry_while_global_still_full() {
        let global = GlobalBudget::new(2048);
        let mut port_a = Budget::new(global.clone());
        port_a.set_limits(2048, 2048, 1);
        let mut port_b = Budget::new(global.clone());
        port_b.set_limits(2048, 2048, 1);
        let peer_a = port_a.memory_handle(PeerId(1));
        let peer_b = port_b.memory_handle(PeerId(2));
        let held_b = peer_b.try_allocate(1024).unwrap();
        let observed = global.release_epoch();
        let base = peer_a.try_allocate(1024).unwrap();
        assert!(peer_a.try_allocate(512).is_none());
        drop(base);

        let wake = Arc::new(CountWake(AtomicUsize::new(0)));
        let id = global.new_waiter_id();
        global.register_physical_waiter(id, observed, &Waker::from(wake.clone()), &peer_a, 1536, AllocationKind::Other);
        assert_eq!(wake.0.load(Ordering::Relaxed), 0);
        drop(held_b);
        assert_eq!(wake.0.load(Ordering::Relaxed), 1);
        global.remove_waiter(id);
    }

    #[test]
    fn temporary_reply_lease_release_does_not_retry_while_port_still_full() {
        let global = GlobalBudget::new(4096);
        let mut port = Budget::new(global.clone());
        port.set_limits(2048, 2048, 1);
        let peer_a = port.memory_handle(PeerId(1));
        let peer_b = port.memory_handle(PeerId(2));
        let held_b = peer_b.try_allocate(1024).unwrap();
        let observed = global.release_epoch();
        let base = peer_a.try_allocate(1024).unwrap();
        assert!(peer_a.try_allocate(512).is_none());
        drop(base);

        let wake = Arc::new(CountWake(AtomicUsize::new(0)));
        let id = global.new_waiter_id();
        global.register_physical_waiter(id, observed, &Waker::from(wake.clone()), &peer_a, 1536, AllocationKind::Other);
        assert_eq!(wake.0.load(Ordering::Relaxed), 0);
        drop(held_b);
        assert_eq!(wake.0.load(Ordering::Relaxed), 1);
        global.remove_waiter(id);
    }

    #[test]
    fn reused_waiter_id_tracks_the_latest_peer() {
        let global = GlobalBudget::new(4096);
        let mut port = Budget::new(global.clone());
        port.set_limits(4096, 512, 2);
        let peer_a = port.memory_handle(PeerId(1));
        let peer_b = port.memory_handle(PeerId(2));
        let held_a = peer_a.try_allocate(512).unwrap();
        let held_b = peer_b.try_allocate(512).unwrap();
        let wake = Arc::new(CountWake(AtomicUsize::new(0)));
        let id = global.new_waiter_id();
        let observed = global.release_epoch();
        global.register_physical_waiter(id, observed, &Waker::from(wake.clone()), &peer_a, 256, AllocationKind::Other);
        global.register_physical_waiter(id, observed, &Waker::from(wake.clone()), &peer_b, 256, AllocationKind::Other);
        drop(held_b);
        assert_eq!(wake.0.load(Ordering::Relaxed), 1);
        drop(held_a);
        global.remove_waiter(id);
    }

    #[test]
    fn waiter_counters_track_parked_entries_for_lock_free_release() {
        let global = GlobalBudget::new(1024);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(1024, 512, 1);
        let peer = budget.memory_handle(PeerId(1));
        let held = peer.try_allocate(512).unwrap();
        let count = Arc::new(CountWake(AtomicUsize::new(0)));
        let waker = Waker::from(count.clone());
        let (physical, broadcast) = (global.new_waiter_id(), global.new_waiter_id());
        let observed = global.release_epoch();
        global.register_physical_waiter(physical, observed, &waker, &peer, 256, AllocationKind::Other);
        global.register_waiter(broadcast, observed, &waker);
        assert_eq!(global.physical_waiting.load(Ordering::SeqCst), 1);
        assert_eq!(global.waiting.load(Ordering::SeqCst), 1);
        // Removing an unrelated id keeps both entries parked.
        global.remove_waiter(global.new_waiter_id());
        assert_eq!(global.physical_waiting.load(Ordering::SeqCst), 1);
        drop(held);
        assert_eq!(count.0.load(Ordering::Relaxed), 2);
        assert_eq!(global.physical_waiting.load(Ordering::SeqCst), 0);
        assert_eq!(global.waiting.load(Ordering::SeqCst), 0);
        // A stale registration after the release wakes at once and is not kept.
        global.register_waiter(broadcast, observed, &waker);
        assert_eq!(count.0.load(Ordering::Relaxed), 3);
        assert_eq!(global.waiting.load(Ordering::SeqCst), 0);
        global.remove_waiter(physical);
        global.remove_waiter(broadcast);
    }

    #[test]
    fn physical_waiter_registration_after_release_does_not_miss_it() {
        let global = GlobalBudget::new(1024);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(1024, 512, 1);
        let peer = budget.memory_handle(PeerId(1));
        let held = peer.try_allocate(512).unwrap();
        let observed = global.release_epoch();
        drop(held);
        let count = Arc::new(CountWake(AtomicUsize::new(0)));
        let id = global.new_waiter_id();
        global.register_physical_waiter(id, observed, &Waker::from(count.clone()), &peer, 512, AllocationKind::Other);
        assert_eq!(count.0.load(Ordering::Relaxed), 1);
        global.remove_waiter(id);
    }

    #[test]
    fn physical_waiter_burst_releases_empty_queue_capacity() {
        let global = GlobalBudget::new(1 << 20);
        let mut budget = Budget::new(global.clone());
        let peer = budget.memory_handle(PeerId(1));
        let wake = Waker::from(Arc::new(CountWake(AtomicUsize::new(0))));
        let observed = global.release_epoch();
        let ids: Vec<_> = (0..1500).map(|_| global.new_waiter_id()).collect();
        for &id in &ids {
            global.register_physical_waiter(id, observed, &wake, &peer, 64, AllocationKind::Other);
        }
        assert!(global.physical_waiters.lock().unwrap().capacity() > 1024);
        for id in ids {
            global.remove_waiter(id);
        }
        assert_eq!(global.physical_waiters.lock().unwrap().capacity(), 0);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn canceled_logical_reservation_does_not_wake_physical_waiters() {
        let global = GlobalBudget::new(1024);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(1024, 512, 1);
        let peer = budget.memory_handle(PeerId(1));
        let count = Arc::new(CountWake(AtomicUsize::new(0)));
        let id = global.new_waiter_id();
        let observed = global.release_epoch();
        global.register_physical_waiter(id, observed, &Waker::from(count.clone()), &peer, 512, AllocationKind::Other);
        assert!(budget.try_reserve(PeerId(1), 256, false));
        budget.cancel_reserve(PeerId(1), 256);
        assert_eq!(global.release_epoch(), observed);
        assert_eq!(count.0.load(Ordering::Relaxed), 0);
        global.remove_waiter(id);
    }

    #[test]
    fn memory_controller_paths_respect_mount_root_and_namespace() {
        let membership = "0::/tenant/a\n3:cpu,memory:/legacy/b\n4:cpu:/ignored\n";
        assert_eq!(cgroup_memberships(membership), vec![("cgroup2", "/tenant/a".to_owned()), ("cgroup", "/legacy/b".to_owned())]);
        let mounts = "36 25 0:32 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n\
                      37 25 0:33 /legacy /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n\
                      38 25 0:34 / /sys/fs/cgroup/cpu rw - cgroup cgroup rw,cpu\n";
        let v2 = memory_mounts(mounts, "cgroup2");
        let v1 = memory_mounts(mounts, "cgroup");
        assert_eq!(v2.len(), 1);
        assert_eq!(v1.len(), 1);
        assert_eq!(cgroup_mount_path(&v2[0].0, &v2[0].1, "/tenant/a").unwrap(), std::path::Path::new("/sys/fs/cgroup/a"));
        assert_eq!(cgroup_mount_path(&v2[0].0, &v2[0].1, "/a").unwrap(), std::path::Path::new("/sys/fs/cgroup/a"));
        assert_eq!(cgroup_mount_path(&v1[0].0, &v1[0].1, "/legacy/b").unwrap(), std::path::Path::new("/sys/fs/cgroup/memory/b"));
        assert_eq!(cgroup_mount_path(&v2[0].0, &v2[0].1, "/").unwrap(), std::path::Path::new("/sys/fs/cgroup"));
        assert!(cgroup_mount_path(&v2[0].0, &v2[0].1, "/tenant/../escape").is_none());
    }

    #[test]
    fn nested_cgroup_uses_smallest_parent_or_child_limit() {
        let unique = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos();
        let root = std::env::temp_dir().join(format!("zfstack-cgroup-{}-{unique}", std::process::id()));
        std::fs::create_dir_all(root.join("child")).unwrap();
        std::fs::write(root.join("memory.max"), "500\n").unwrap();
        std::fs::write(root.join("child/memory.max"), "300\n").unwrap();
        let mounts = format!("36 25 0:32 /tenant {} rw - cgroup2 cgroup rw\n", root.display());
        assert_eq!(memory_limit_from_mounts("0::/tenant/child\n", &mounts), Some(300));
        std::fs::write(root.join("child/memory.max"), "max\n").unwrap();
        assert_eq!(memory_limit_from_mounts("0::/tenant/child\n", &mounts), Some(500));
        std::fs::remove_dir_all(root).unwrap();
    }

    /// docs/design/0005 §2: at each of global, port and peer, an admission
    /// allocation stops at `limit - H_3`; a TX block may use up to `H_2`,
    /// a send record up to `H_1`, and only egress reaches the limit.
    #[test]
    fn progress_tiers_keep_headroom_at_every_level() {
        const L: u64 = 512 << 10;
        for (level, (high, port, peer)) in [(Level::Global, (L, L, L)), (Level::Port, (2 * L, L, L)), (Level::Peer, (2 * L, 2 * L, L))] {
            let global = GlobalBudget::new(high);
            global.ensure_egress_reserve(8192);
            let mut budget = Budget::new(global.clone());
            budget.set_limits(port, peer, 4);
            assert!(budget.progress_reserve_active());
            let h1 = global.headroom(L, Tier::Drain);
            let h2 = global.headroom(L, Tier::Block);
            let h3 = global.headroom(L, Tier::Admit);
            let h4 = global.headroom(L, Tier::Bulk);
            assert_eq!(h1, 8192);
            assert_eq!(h2, h1 + META_RESERVE);
            assert_eq!(h3, h2 + crate::buf::TX_BLOCK_CHARGE);
            assert_eq!(h4, h3 + DEFAULT_ADMIT_RESERVE);
            let memory = budget.memory_handle(PeerId(1));
            let bulk = memory.try_allocate_kind(L - h4, AllocationKind::AdapterTx).unwrap();
            assert!(memory.try_allocate_kind(1, AllocationKind::RxChunk).is_none(), "{level:?}");
            let admitted = memory.try_allocate_kind(h4 - h3, AllocationKind::StreamState).unwrap();
            assert!(memory.try_allocate_kind(1, AllocationKind::ActiveState).is_none(), "{level:?}");
            assert_eq!(budget.stats().failures_by_level()[level as usize], 2, "{level:?}");
            let block = memory.try_allocate_kind(h3 - h2, AllocationKind::TxBlock).unwrap();
            assert!(memory.try_allocate_kind(1, AllocationKind::TxBlock).is_none(), "{level:?}");
            let record = memory.try_allocate_kind(h2 - h1, AllocationKind::SendRecord).unwrap();
            assert!(memory.try_allocate_kind(1, AllocationKind::AdapterRx).is_none(), "{level:?}");
            let egress = memory.try_allocate_kind(h1, AllocationKind::Egress).unwrap();
            assert!(memory.try_allocate_kind(1, AllocationKind::Egress).is_none(), "{level:?}");
            assert_eq!(budget.stats().failures_by_level()[level as usize], 5, "{level:?}");
            let failures = budget.stats().failures();
            assert_eq!((failures.rx_chunk, failures.active_state, failures.tx_block, failures.adapter_rx, failures.egress), (1, 1, 1, 1, 1));
            drop((bulk, admitted, block, record, egress));
            assert_eq!(global.reserved(), 0);
        }
    }

    #[test]
    fn small_levels_run_without_progress_reserves() {
        let global = GlobalBudget::new(64 << 10);
        assert!(!global.progress_reserve_active(64 << 10));
        assert_eq!(global.headroom(64 << 10, Tier::Admit), 0);
        let mut budget = Budget::new(global.clone());
        assert!(!budget.progress_reserve_active());
        let lease = budget.memory_handle(PeerId(1)).try_allocate(16 << 10).unwrap();
        drop(lease);
    }

    #[test]
    fn physical_waiter_fit_respects_its_tier() {
        let global = GlobalBudget::new(1 << 20);
        global.ensure_egress_reserve(8192);
        let h3 = global.headroom(1 << 20, Tier::Admit);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(1 << 20, 1 << 20, 4);
        let memory = budget.memory_handle(PeerId(1));
        let held = memory.try_allocate_kind((1 << 20) - h3, AllocationKind::StreamState).unwrap();
        let small = memory.try_allocate_kind(1024, AllocationKind::Egress).unwrap();
        let admit = Arc::new(CountWake(AtomicUsize::new(0)));
        let block = Arc::new(CountWake(AtomicUsize::new(0)));
        let (admit_id, block_id) = (global.new_waiter_id(), global.new_waiter_id());
        let observed = global.release_epoch();
        global.register_physical_waiter(admit_id, observed, &Waker::from(admit.clone()), &memory, 1024, AllocationKind::StreamState);
        global.register_physical_waiter(block_id, observed, &Waker::from(block.clone()), &memory, 1024, AllocationKind::TxBlock);
        drop(small);
        assert_eq!(admit.0.load(Ordering::Relaxed), 0, "admission may not enter the progress headroom");
        assert_eq!(block.0.load(Ordering::Relaxed), 1);
        global.remove_waiter(admit_id);
        global.remove_waiter(block_id);
        drop(held);
    }
}
