//! Tokio adapter (feature `tokio`, §4.4): `TcpStream` implementing
//! `AsyncRead`/`AsyncWrite` on top of a [`Shard`] driven by a Tokio task
//! ("driver A", §7.5).
//!
//! Each stream owns a pair of byte-bounded queues shared with the driver:
//! * rx: chunks moved out of the shard (`read_chunk`, zero-copy slices of ingress
//!   packets) up to `rx_cap` bytes; the reader copies them into its buffer.
//! * tx: bytes accepted by `poll_write` up to `tx_cap`; the driver moves them into
//!   the shard as send space allows.
//!
//! Lost-wakeup protection follows "check → register waker → re-check" (§4.4).
//! Writes are partial: `poll_write` accepts `min(len, free)` once at least the low
//! watermark is free; below it the task is parked until the driver drains the
//! queue past the watermark. Wakeups only happen on watermark crossings.

use crate::budget::{AllocationKind, GlobalBudget, KindCounts, MemoryHandle, MemoryLease};
use crate::shard::{EgressSinks, IfaceConfig, OutPacket, SendResult, Shard, ShardStats};
use crate::{CloseReason, ConnId, Event, IfaceId, PeerId, ReadResult, StackConfig, WriteResult};
use bytes::Bytes;
use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::hash::Hash;
use std::io;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Wake, Waker};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::sync::{mpsc, oneshot, Notify};

/// Queue sizes for the async handles.
#[derive(Clone, Debug)]
pub struct StreamConfig {
    pub rx_cap: usize,
    pub tx_cap: usize,
    /// Wake writers / accept partial writes once this much tx space is free.
    pub tx_low_watermark: usize,
}

impl Default for StreamConfig {
    fn default() -> Self {
        StreamConfig { rx_cap: 256 * 1024, tx_cap: 256 * 1024, tx_low_watermark: 16 * 1024 }
    }
}

/// Egress callback: hand one IP packet to the WG encrypt/send path.
pub trait Egress: Send + 'static {
    fn send(&mut self, iface: IfaceId, peer: PeerId, pkt: &OutPacket<'_>) -> SendResult;
}

impl<F> Egress for F
where
    F: FnMut(IfaceId, PeerId, &OutPacket<'_>) -> SendResult + Send + 'static,
{
    fn send(&mut self, iface: IfaceId, peer: PeerId, pkt: &OutPacket<'_>) -> SendResult {
        self(iface, peer, pkt)
    }
}

#[derive(Clone, Copy)]
enum QueueGrowError {
    Quota(u64),
    Allocation,
}

/// Account descriptor backing separately from the payload owned by each
/// entry. Grow into a fresh deque while both old and new backing are charged;
/// a failed reservation leaves every queued byte in the old deque.
struct ChargedDeque<T> {
    items: VecDeque<T>,
    _capacity: Option<MemoryLease>,
}

impl<T> Default for ChargedDeque<T> {
    fn default() -> Self {
        Self { items: VecDeque::new(), _capacity: None }
    }
}

impl<T> ChargedDeque<T> {
    fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
    fn front(&self) -> Option<&T> {
        self.items.front()
    }
    fn front_mut(&mut self) -> Option<&mut T> {
        self.items.front_mut()
    }
    fn back(&self) -> Option<&T> {
        self.items.back()
    }
    fn back_mut(&mut self) -> Option<&mut T> {
        self.items.back_mut()
    }

    fn reserve_one(&mut self, memory: &MemoryHandle, kind: AllocationKind) -> Result<(), QueueGrowError> {
        if self.items.len() < self.items.capacity() {
            return Ok(());
        }
        let target = self.items.capacity().max(4).checked_mul(2).ok_or(QueueGrowError::Allocation)?;
        let bytes = target
            .checked_mul(std::mem::size_of::<T>())
            .and_then(|n| n.checked_mul(2))
            .and_then(|n| u64::try_from(n).ok())
            .ok_or(QueueGrowError::Allocation)?;
        let lease = memory.try_allocate_kind(bytes, kind).ok_or(QueueGrowError::Quota(bytes))?;
        let mut next = VecDeque::new();
        next.try_reserve(target).map_err(|_| QueueGrowError::Allocation)?;
        if (next.capacity() as u64).saturating_mul(std::mem::size_of::<T>() as u64) > bytes {
            return Err(QueueGrowError::Allocation);
        }
        next.extend(self.items.drain(..));
        self.items = next;
        self._capacity = Some(lease);
        Ok(())
    }

    fn push_back_reserved(&mut self, item: T) {
        debug_assert!(self.items.len() < self.items.capacity());
        self.items.push_back(item);
    }

    fn pop_front(&mut self) -> Option<T> {
        let item = self.items.pop_front();
        if self.items.is_empty() && self.items.capacity() > 8 {
            self.items = VecDeque::new();
            self._capacity = None;
        }
        item
    }

    fn clear(&mut self) {
        self.items.clear();
        if self.items.capacity() > 8 {
            self.items = VecDeque::new();
            self._capacity = None;
        }
    }

    #[cfg(test)]
    fn capacity_bytes(&self) -> u64 {
        (self.items.capacity() * std::mem::size_of::<T>()) as u64
    }
}

#[derive(Default)]
struct Queues {
    rx: ChargedDeque<Bytes>,
    rx_len: usize,
    rx_consumed_pending: usize,
    rx_eof: bool,
    tx: TxQueue,
    tx_shutdown: bool,
    /// Set by the driver when the connection is gone.
    error: Option<CloseReason>,
    closed_by_app: bool,
    /// Exact deadline so a completed stream can remove its pending orphan node.
    orphan_deadline: Option<crate::Instant>,
    read_waker: Option<Waker>,
    write_waker: Option<Waker>,
    flush_waker: Option<Waker>,
    /// Writer parked below the low watermark.
    write_parked: bool,
    /// Pending request's watermark before clamping to the current share.
    write_need: usize,
    /// Attainable capacity, independent of the currently free room.
    send_limit: usize,
    /// Additional bytes this stream may buffer across the adapter queue and
    /// the core send buffer under its send share; published by the driver on
    /// each pump (docs/design/0007 §2.4). Writes consume it synchronously.
    send_room: usize,
    /// `TcpStream::splice` hand-over, taken by the driver's next pump.
    splice: Option<SpliceRequest>,
}

struct SpliceRequest {
    upstream: tokio::net::TcpStream,
    done: oneshot::Sender<io::Result<SpliceStats>>,
}

/// Bytes moved by a finished [`TcpStream::splice`].
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct SpliceStats {
    /// Stack stream → upstream socket (what the TUN-side peer sent).
    pub uploaded: u64,
    /// Upstream socket → stack stream.
    pub downloaded: u64,
}

/// Completes when a spliced connection has finished in both directions, or
/// failed; see [`TcpStream::splice`].
pub struct Splice {
    rx: oneshot::Receiver<io::Result<SpliceStats>>,
}

impl std::future::Future for Splice {
    type Output = io::Result<SpliceStats>;
    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        Pin::new(&mut self.rx).poll(cx).map(|r| r.unwrap_or_else(|_| Err(io::Error::new(io::ErrorKind::BrokenPipe, "stack driver stopped"))))
    }
}

/// `data[head..]` is queued; `data.capacity()` is the charged backing, so a
/// chunk is filled by appending instead of zeroing it first.
struct TxChunk {
    data: Vec<u8>,
    head: usize,
    _lease: MemoryLease,
}

#[derive(Default)]
struct TxQueue {
    chunks: ChargedDeque<TxChunk>,
    len: usize,
    last_failure: Option<(QueueGrowError, u64)>,
}

impl TxQueue {
    fn len(&self) -> usize {
        self.len
    }
    fn is_empty(&self) -> bool {
        self.len == 0
    }
    fn clear(&mut self) {
        self.chunks.clear();
        self.len = 0;
    }
    fn front(&self) -> &[u8] {
        self.chunks.front().map_or(&[], |c| &c.data[c.head..])
    }
    fn push(&mut self, mut src: &[u8], memory: &MemoryHandle) -> usize {
        let wanted = src.len();
        self.last_failure = None;
        while !src.is_empty() {
            if self.chunks.back().is_none_or(|c| c.data.len() == c.data.capacity()) {
                let cap = src.len().min(64 * 1024).next_power_of_two().max(64);
                let observed = memory.global().release_epoch();
                if let Err(error) = self.chunks.reserve_one(memory, AllocationKind::AdapterTx) {
                    self.last_failure = Some((error, observed));
                    break;
                }
                let observed = memory.global().release_epoch();
                let Some(lease) = memory.try_allocate_kind(cap as u64 + 64, AllocationKind::AdapterTx) else {
                    self.last_failure = Some((QueueGrowError::Quota(cap as u64 + 64), observed));
                    break;
                };
                let mut data = Vec::new();
                if data.try_reserve_exact(cap).is_err() || data.capacity() > cap {
                    self.last_failure = Some((QueueGrowError::Allocation, observed));
                    break;
                }
                self.chunks.push_back_reserved(TxChunk { data, head: 0, _lease: lease });
            }
            let chunk = self.chunks.back_mut().unwrap();
            let n = src.len().min(chunk.data.capacity() - chunk.data.len());
            chunk.data.extend_from_slice(&src[..n]);
            self.len += n;
            src = &src[n..];
        }
        wanted - src.len()
    }
    fn consume(&mut self, mut n: usize) {
        while n > 0 {
            let chunk = self.chunks.front_mut().expect("TX bytes present");
            let take = n.min(chunk.data.len() - chunk.head);
            chunk.head += take;
            self.len -= take;
            n -= take;
            if chunk.head == chunk.data.len() {
                self.chunks.pop_front();
            }
        }
    }
}

struct Shared {
    id: ConnId,
    peer: PeerId,
    q: Mutex<Queues>,
    ctl: Arc<Ctl>,
    cfg: StreamConfig,
    memory: MemoryHandle,
    /// The app may retain this handle after the core releases its Conn.
    _state_memory: MemoryLease,
    memory_waiter: u64,
    driver_waiter: u64,
    rx_index_waiter: u64,
    memory_waiting: AtomicBool,
    driver_tx_wait: AtomicU8,
    rx_index_waiting: AtomicBool,
    driver_wake: Waker,
}

const TX_WAIT_NONE: u8 = 0;
const TX_WAIT_QUOTA: u8 = 1;
const TX_WAIT_MEMORY: u8 = 2;

const STREAM_STATE_BYTES: u64 = (std::mem::size_of::<Shared>() + std::mem::size_of::<TcpStream>() + 256) as u64;

impl Drop for Shared {
    fn drop(&mut self) {
        if self.memory_waiting.swap(false, Ordering::AcqRel) {
            self.memory.global().remove_waiter(self.memory_waiter);
        }
        clear_tx_wait(self);
        if self.rx_index_waiting.swap(false, Ordering::AcqRel) {
            self.memory.global().remove_waiter(self.rx_index_waiter);
        }
    }
}

fn clear_tx_wait(sh: &Shared) {
    if sh.driver_tx_wait.load(Ordering::Relaxed) != TX_WAIT_NONE && sh.driver_tx_wait.swap(TX_WAIT_NONE, Ordering::AcqRel) != TX_WAIT_NONE {
        sh.memory.global().remove_waiter(sh.driver_waiter);
    }
}

/// The release epoch must be captured BEFORE the write attempt. Allocation
/// failure can itself request another owner's cache reclaim; that owner may
/// release memory before we get here. Both queue pumps and readv use this
/// protocol so an empty core buffer never waits for an ACK that cannot arrive.
fn track_tx_wait(shard: &mut Shard, sh: &Shared, result: WriteResult, observed: u64, wanted: usize) {
    match result {
        WriteResult::QuotaBlocked if shard.send_space(sh.id) == 0 => {
            // The connection filled its own send share (`space == 0 &&
            // send_share < sndbuf_limit`). `Shard` registered it in
            // `share_blocked`, so own ACKs or `sender_del` share growth will
            // emit `Event::Writable`; unrelated logical/physical releases
            // cannot increase `send_space` and must not wake this stream.
            clear_tx_wait(sh);
        }
        WriteResult::QuotaBlocked => {
            let prev = sh.driver_tx_wait.swap(TX_WAIT_QUOTA, Ordering::AcqRel);
            if prev != TX_WAIT_NONE && prev != TX_WAIT_QUOTA {
                sh.memory.global().remove_waiter(sh.driver_waiter);
            }
            sh.memory.global().register_waiter(sh.driver_waiter, observed, &sh.driver_wake);
        }
        WriteResult::MemoryBlocked => {
            let prev = sh.driver_tx_wait.swap(TX_WAIT_MEMORY, Ordering::AcqRel);
            if prev != TX_WAIT_NONE && prev != TX_WAIT_MEMORY {
                sh.memory.global().remove_waiter(sh.driver_waiter);
            }
            let charge = shard.write_allocation_charge(sh.id, wanted);
            sh.memory.global().register_physical_waiter(sh.driver_waiter, observed, &sh.driver_wake, &sh.memory, charge, AllocationKind::TxBlock);
        }
        _ => clear_tx_wait(sh),
    }
}

struct DriverWake {
    ctl: Arc<Ctl>,
    id: ConnId,
}

impl Wake for DriverWake {
    fn wake(self: Arc<Self>) {
        self.ctl.mark(self.id);
    }
    fn wake_by_ref(self: &Arc<Self>) {
        self.ctl.mark(self.id);
    }
}

struct BudgetWake {
    ctl: Arc<Ctl>,
}

impl Wake for BudgetWake {
    fn wake(self: Arc<Self>) {
        self.ctl.notify.notify_one();
    }
    fn wake_by_ref(self: &Arc<Self>) {
        self.ctl.notify.notify_one();
    }
}

/// Driver-side control: dirty stream list + wakeup.
struct Ctl {
    dirty: Mutex<HashSet<ConnId>>,
    pending: Mutex<PendingControl>,
    notify: Notify,
}

#[derive(Default)]
struct PendingControl {
    egress_released: HashSet<IfaceId>,
    mtu: HashMap<IfaceId, u16>,
    tx_csum: HashMap<IfaceId, bool>,
    tso: HashMap<IfaceId, u32>,
}

impl Ctl {
    fn mark(&self, id: ConnId) {
        self.dirty.lock().unwrap().insert(id);
        self.notify.notify_one();
    }
}

/// Keep a short-connection burst from pinning a mostly empty driver index.
/// Build the replacement before moving entries so allocation failure leaves
/// the original map intact.
fn compact_sparse_map<K: Eq + Hash, V>(map: &mut HashMap<K, V>) {
    if map.is_empty() {
        *map = HashMap::new();
        return;
    }
    if map.capacity() <= 512 || map.len() > map.capacity() / 8 {
        return;
    }
    let mut compact = HashMap::new();
    if compact.try_reserve(map.len()).is_err() {
        return;
    }
    compact.extend(map.drain());
    *map = compact;
}

/// Metadata of an accepted connection.
#[derive(Clone, Debug)]
pub struct ConnMeta {
    pub iface: IfaceId,
    pub peer: PeerId,
    pub local: SocketAddr,
    pub remote: SocketAddr,
}

pub struct TcpStream {
    sh: Arc<Shared>,
    meta: ConnMeta,
}

impl TcpStream {
    pub fn meta(&self) -> &ConnMeta {
        &self.meta
    }
    pub fn peer_addr(&self) -> SocketAddr {
        self.meta.remote
    }
    pub fn local_addr(&self) -> SocketAddr {
        self.meta.local
    }
}

impl TcpStream {
    /// Relay this stream to `upstream` inside the driver task, without a
    /// relay task or copy buffers (the proxy-client "direct" outbound):
    /// upstream bytes are read straight into the connection's send buffer
    /// (`Shard::write_with`), and received segments are written to the socket
    /// from the stack's own chunks with `writev`. Readiness of both sides
    /// wakes the driver; nothing is polled.
    ///
    /// Bytes already buffered in this handle are kept in order. Each FIN is
    /// forwarded as a half-close; the stream ends when both directions have
    /// ended. A reset or socket error on either side resets the other.
    ///
    /// The socket is re-registered with the driver's runtime, so a host that
    /// gives the driver its own current-thread runtime keeps every spliced
    /// byte on that thread.
    pub fn splice(self, upstream: tokio::net::TcpStream) -> Splice {
        let this = std::mem::ManuallyDrop::new(self);
        // SAFETY: `this` is never used or dropped again; `meta` owns no heap
        // memory, so moving `sh` out is the only ownership transfer.
        let sh = unsafe { std::ptr::read(&this.sh) };
        let (done, rx) = oneshot::channel();
        sh.q.lock().unwrap().splice = Some(SpliceRequest { upstream, done });
        sh.ctl.mark(sh.id);
        Splice { rx }
    }
}

fn register_waker(slot: &mut Option<Waker>, waker: &Waker) {
    match slot {
        Some(stored) => stored.clone_from(waker),
        None => *slot = Some(waker.clone()),
    }
}

impl AsyncRead for TcpStream {
    fn poll_read(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &mut ReadBuf<'_>) -> Poll<io::Result<()>> {
        if buf.remaining() == 0 {
            return Poll::Ready(Ok(()));
        }
        let sh = &self.sh;
        let mut q = sh.q.lock().unwrap();
        // check → register → re-check happens under the same lock the driver uses.
        if q.rx.is_empty() {
            if let Some(r) = q.error {
                if r != CloseReason::Normal {
                    return Poll::Ready(Err(io::Error::new(io::ErrorKind::ConnectionReset, format!("{r:?}"))));
                }
                return Poll::Ready(Ok(()));
            }
            if q.rx_eof {
                return Poll::Ready(Ok(()));
            }
            register_waker(&mut q.read_waker, cx.waker());
            return Poll::Pending;
        }
        let mut consumed = 0;
        while buf.remaining() > 0 {
            let Some(front) = q.rx.front_mut() else { break };
            let n = front.len().min(buf.remaining());
            buf.put_slice(&front[..n]);
            if n == front.len() {
                q.rx.pop_front();
            } else {
                let _ = front.split_to(n);
            }
            q.rx_len -= n;
            consumed += n;
        }
        // The shard owns TCP window accounting. Report only bytes the
        // application actually copied, not chunks moved into this queue.
        q.rx_consumed_pending += consumed;
        drop(q);
        if consumed > 0 {
            sh.ctl.mark(sh.id);
        }
        Poll::Ready(Ok(()))
    }
}

impl AsyncWrite for TcpStream {
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, src: &[u8]) -> Poll<io::Result<usize>> {
        if src.is_empty() {
            return Poll::Ready(Ok(0));
        }
        let sh = &self.sh;
        let mut q = sh.q.lock().unwrap();
        if let Some(r) = q.error {
            return Poll::Ready(Err(io::Error::new(io::ErrorKind::BrokenPipe, format!("{r:?}"))));
        }
        if q.tx_shutdown {
            return Poll::Ready(Err(io::Error::new(io::ErrorKind::BrokenPipe, "write side shut down")));
        }
        // The fixed queue bound and the connection's send share both cap how
        // much the app may buffer (docs/design/0007 §2.4).
        let free = sh.cfg.tx_cap.saturating_sub(q.tx.len()).min(q.send_room);
        let write_need = sh.cfg.tx_low_watermark.max(1).min(src.len());
        let min_free = write_need.min(q.send_limit).max(1);
        if free < min_free {
            register_waker(&mut q.write_waker, cx.waker());
            q.write_parked = true;
            q.write_need = write_need;
            // Republish the room promptly: the core buffer may have drained
            // since the last pump, and an empty queue gives no other wake
            // (docs/design/0007 §2.4).
            drop(q);
            sh.ctl.mark(sh.id);
            return Poll::Pending;
        }
        let n = free.min(src.len());
        let was_empty = q.tx.is_empty();
        let written = q.tx.push(&src[..n], &sh.memory);
        q.send_room = q.send_room.saturating_sub(written);
        if written == 0 {
            let failure = q.tx.last_failure.take();
            if matches!(failure, Some((QueueGrowError::Allocation, _))) {
                return Poll::Ready(Err(io::Error::new(io::ErrorKind::OutOfMemory, "TX queue descriptor allocation failed")));
            }
            register_waker(&mut q.write_waker, cx.waker());
            q.write_parked = true;
            q.write_need = write_need;
            let needed = match failure {
                Some((QueueGrowError::Quota(bytes), _)) => bytes,
                _ => src.len().min(64 * 1024).next_power_of_two().max(64) as u64 + 64,
            };
            let observed = failure.map_or_else(|| sh.memory.global().release_epoch(), |(_, observed)| observed);
            sh.memory_waiting.store(true, Ordering::Release);
            sh.memory.global().register_physical_waiter(sh.memory_waiter, observed, cx.waker(), &sh.memory, needed, AllocationKind::AdapterTx);
            return Poll::Pending;
        }
        if sh.memory_waiting.load(Ordering::Relaxed) && sh.memory_waiting.swap(false, Ordering::AcqRel) {
            sh.memory.global().remove_waiter(sh.memory_waiter);
        }
        q.write_parked = false;
        drop(q);
        if was_empty {
            sh.ctl.mark(sh.id);
        }
        Poll::Ready(Ok(written))
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let mut q = self.sh.q.lock().unwrap();
        if let Some(r) = q.error {
            return Poll::Ready(Err(io::Error::new(io::ErrorKind::BrokenPipe, format!("{r:?}"))));
        }
        if q.tx.is_empty() {
            Poll::Ready(Ok(()))
        } else {
            register_waker(&mut q.flush_waker, cx.waker());
            Poll::Pending
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let sh = &self.sh;
        let mut q = sh.q.lock().unwrap();
        if !q.tx_shutdown {
            q.tx_shutdown = true;
            sh.ctl.mark(sh.id);
        }
        if q.tx.is_empty() {
            Poll::Ready(Ok(()))
        } else {
            register_waker(&mut q.flush_waker, cx.waker());
            Poll::Pending
        }
    }
}

impl Drop for TcpStream {
    fn drop(&mut self) {
        let mut q = self.sh.q.lock().unwrap();
        q.closed_by_app = true;
        drop(q);
        self.sh.ctl.mark(self.sh.id);
    }
}

enum Cmd {
    Packets(IfaceId, Vec<(PeerId, Bytes)>),
    Snapshot(oneshot::Sender<StackSnapshot>),
}

/// One accepted stream's core and adapter backlog, sampled for tail diagnosis.
#[derive(Debug, Clone)]
pub struct TailConnection {
    pub id: ConnId,
    pub core: crate::ConnInfo,
    pub adapter_tx_queued: usize,
    pub adapter_rx_queued: usize,
    pub write_parked: bool,
    /// Share-aware room left for app writes (docs/design/0007 §2.4).
    pub send_room: usize,
}

/// Point-in-time counters from the shard owner task. Only when at most four
/// accepted streams remain, include their TCP state for tail diagnostics.
/// Sampling never retains packet data.
#[derive(Debug, Clone)]
pub struct StackSnapshot {
    pub stats: ShardStats,
    pub port_physical_bytes: u64,
    /// All charged global bytes, including free and borrowed PacketPool buffers.
    pub global_reserved_bytes: u64,
    /// TX payload/index cache ledger; PacketPool free buffers are not included.
    pub global_cached_bytes: u64,
    pub tx_cache: crate::buf::TxCacheSnapshot,
    pub core_reserve_failures: u64,
    pub failures_by_kind: crate::budget::ReserveFailures,
    /// Physical failures rejected by the global, port and peer share.
    pub failures_by_level: [u64; 3],
    /// Physical waiters parked on this port, by allocation kind. `tx_block`
    /// counts streams whose adapter bytes wait for a core TX block.
    pub waiters: KindCounts,
    /// Core connections waiting for a send-record allocation.
    pub record_waiters: usize,
    /// Whether global, port and peer all keep the progress reserves.
    pub progress_reserve: bool,
    /// Admission state owed by the global, port and peer levels after
    /// refused connections (docs/design/0006 §2).
    pub admission_debt: [u64; 3],
    /// Optional prepaid RX bytes: global, port, and this port's peers combined.
    /// `failures_by_kind.rx_progress` counts skipped credits, not refused SYNs.
    pub rx_progress_bytes: [u64; 3],
    pub active_connections: usize,
    pub adapter_streams: usize,
    /// Connections currently holding send-side bytes on this port, and how
    /// often a write was rejected by the per-connection send share
    /// (docs/design/0007 §5).
    pub senders: u32,
    pub share_blocked: u64,
    /// Core send-record or send-share wait; adapter and host waiters are in
    /// `waiters`.
    pub budget_waiting: bool,
    pub closes: CloseCounts,
    pub aborts: AbortCounts,
    pub tail_connections: Vec<TailConnection>,
}

/// Connection close events seen by the adapter, by reason.
#[derive(Debug, Clone, Copy, Default)]
pub struct CloseCounts {
    pub normal: u64,
    pub reset: u64,
    pub timeout: u64,
    pub aborted: u64,
    pub refused: u64,
    pub desync: u64,
}

impl CloseCounts {
    fn note(&mut self, reason: CloseReason) {
        let n = match reason {
            CloseReason::Normal => &mut self.normal,
            CloseReason::Reset => &mut self.reset,
            CloseReason::Timeout => &mut self.timeout,
            CloseReason::Aborted => &mut self.aborted,
            CloseReason::Refused => &mut self.refused,
            CloseReason::Desync => &mut self.desync,
        };
        *n += 1;
    }
}

/// Connections the adapter aborted itself (each sends an RST), by cause.
#[derive(Debug, Clone, Copy, Default)]
pub struct AbortCounts {
    /// No memory for the stream state of an accepted connection.
    pub stream_state: u64,
    /// The accept backlog was full.
    pub accept_backlog: u64,
    /// A dropped stream could not hand its bytes to the core in time.
    pub orphan_timeout: u64,
    /// The app dropped the stream with unread data.
    pub unread_drop: u64,
    /// The RX descriptor allocation itself failed (not a quota wait).
    pub rx_index_alloc: u64,
}

/// Handle used by the WG side to feed decrypted packets.
#[derive(Clone)]
pub struct StackHandle {
    tx: mpsc::Sender<Cmd>,
    ctl: Arc<Ctl>,
}

impl StackHandle {
    /// Feed a batch of decrypted IP packets (with their WG peer) for `iface`.
    pub async fn ingress(&self, iface: IfaceId, pkts: Vec<(PeerId, Bytes)>) -> bool {
        self.tx.send(Cmd::Packets(iface, pkts)).await.is_ok()
    }
    /// Non-blocking variant; returns false if the driver queue is full or gone.
    pub fn try_ingress(&self, iface: IfaceId, pkts: Vec<(PeerId, Bytes)>) -> bool {
        self.tx.try_send(Cmd::Packets(iface, pkts)).is_ok()
    }
    /// Request a cheap diagnostic snapshot. Returns None after shutdown.
    pub async fn snapshot(&self) -> Option<StackSnapshot> {
        let (tx, rx) = oneshot::channel();
        self.tx.send(Cmd::Snapshot(tx)).await.ok()?;
        rx.await.ok()
    }
    /// The egress path for `iface` drained (state ③, §4.3).
    pub fn egress_released(&self, iface: IfaceId) {
        self.ctl.pending.lock().unwrap().egress_released.insert(iface);
        self.ctl.notify.notify_one();
    }
    pub fn set_iface_mtu(&self, iface: IfaceId, mtu: u16) {
        self.ctl.pending.lock().unwrap().mtu.insert(iface, mtu);
        self.ctl.notify.notify_one();
    }
    /// See `Shard::set_iface_tx_checksum_offload`.
    pub fn set_iface_tx_checksum_offload(&self, iface: IfaceId, on: bool) {
        self.ctl.pending.lock().unwrap().tx_csum.insert(iface, on);
        self.ctl.notify.notify_one();
    }
    /// See `Shard::set_iface_tso`.
    pub fn set_iface_tso(&self, iface: IfaceId, max_payload: u32) {
        self.ctl.pending.lock().unwrap().tso.insert(iface, max_payload);
        self.ctl.notify.notify_one();
    }
}

/// Receives accepted connections.
pub struct Acceptor {
    rx: mpsc::Receiver<TcpStream>,
}

/// Owns a production driver task. Dropping it cancels the driver and wakes all
/// outstanding stream readers and writers through `Driver::drop`.
pub struct DriverTask {
    stop: Arc<AtomicBool>,
    ctl: Arc<Ctl>,
    join: Option<tokio::task::JoinHandle<()>>,
}

impl DriverTask {
    pub fn shutdown(&self) {
        self.stop.store(true, Ordering::Release);
        self.ctl.notify.notify_one();
    }

    /// Wait for the driver without requesting shutdown. Cancellation of this
    /// future leaves the task owned by `DriverTask`, so callers can monitor it
    /// inside `select!` and still join it during normal teardown.
    pub async fn wait_finished(&mut self) -> Result<(), tokio::task::JoinError> {
        let Some(join) = self.join.as_mut() else { return Ok(()) };
        let result = join.await;
        self.join.take();
        result
    }

    pub async fn shutdown_and_join(mut self) -> Result<(), tokio::task::JoinError> {
        self.shutdown();
        self.wait_finished().await
    }
}

impl Drop for DriverTask {
    fn drop(&mut self) {
        if let Some(join) = self.join.take() {
            join.abort();
        }
    }
}

impl Acceptor {
    pub async fn accept(&mut self) -> Option<TcpStream> {
        self.rx.recv().await
    }
}

struct Sink<'a, E: Egress> {
    egress: &'a mut E,
}

impl<E: Egress> EgressSinks for Sink<'_, E> {
    fn send(&mut self, iface: IfaceId, pkt: &OutPacket<'_>) -> SendResult {
        self.egress.send(iface, pkt.peer, pkt)
    }
}

/// Spawn the driver task on the current Tokio runtime. Returns the ingress handle,
/// the acceptor and the iface ids (in the order of `ifaces`).
pub fn spawn<E: Egress>(cfg: StackConfig, stream_cfg: StreamConfig, ifaces: Vec<IfaceConfig>, egress: E) -> (StackHandle, Acceptor, Vec<IfaceId>) {
    let (handle, acceptor, ids, _join, _stop) = spawn_inner(Shard::new(cfg), stream_cfg, ifaces, egress, None::<InputSource<(), NoInput>>);
    (handle, acceptor, ids)
}

/// Production entry point: all port shards can share a process-wide budget and
/// the caller owns the driver lifetime.
pub fn spawn_with_budget<E: Egress>(
    cfg: StackConfig,
    stream_cfg: StreamConfig,
    ifaces: Vec<IfaceConfig>,
    egress: E,
    global: Arc<GlobalBudget>,
) -> (StackHandle, Acceptor, Vec<IfaceId>, DriverTask) {
    let (handle, acceptor, ids, join, stop) = spawn_inner(Shard::with_budget(cfg, global), stream_cfg, ifaces, egress, None::<InputSource<(), NoInput>>);
    let task = DriverTask { stop, ctl: handle.ctl.clone(), join: Some(join) };
    (handle, acceptor, ids, task)
}

type NoInput = fn(&mut Shard, crate::Instant, ());

#[derive(Clone, Copy, Debug)]
pub struct ResourceLimits {
    pub port_bytes: u64,
    pub peer_bytes: u64,
    pub peer_max_connections: u32,
}

struct InputSource<I, F> {
    rx: mpsc::Receiver<I>,
    handle: F,
}

/// Production entry point with a caller-owned bounded packet channel. The
/// handler runs synchronously on the shard owner task, so it can borrow an
/// ingress buffer from a host pool, call `Shard::ingress_borrowed`, and return
/// that buffer immediately. It can also demultiplex UDP without another bridge
/// channel or a second TCP packet allocation.
pub fn spawn_with_source<E, I, F>(
    cfg: StackConfig,
    stream_cfg: StreamConfig,
    ifaces: Vec<IfaceConfig>,
    egress: E,
    global: Arc<GlobalBudget>,
    limits: ResourceLimits,
    ingress_rx: mpsc::Receiver<I>,
    on_ingress: F,
) -> (StackHandle, Acceptor, Vec<IfaceId>, DriverTask)
where
    E: Egress,
    I: Send + 'static,
    F: FnMut(&mut Shard, crate::Instant, I) + Send + 'static,
{
    let (handle, acceptor, ids, task, ()) = spawn_with_source_factory(cfg, stream_cfg, ifaces, move |_| (egress, ()), global, limits, ingress_rx, on_ingress);
    (handle, acceptor, ids, task)
}

/// As above, but construct the sink after configuring the shard. This lets a
/// host cache per-peer memory handles for packet backing retained by its
/// bounded egress queue, sharing the same shard limits as core allocations.
pub fn spawn_with_source_factory<E, I, F, G, A>(
    cfg: StackConfig,
    stream_cfg: StreamConfig,
    ifaces: Vec<IfaceConfig>,
    make_egress: G,
    global: Arc<GlobalBudget>,
    limits: ResourceLimits,
    ingress_rx: mpsc::Receiver<I>,
    on_ingress: F,
) -> (StackHandle, Acceptor, Vec<IfaceId>, DriverTask, A)
where
    E: Egress,
    I: Send + 'static,
    F: FnMut(&mut Shard, crate::Instant, I) + Send + 'static,
    G: FnOnce(&mut Shard) -> (E, A),
{
    let input = InputSource { rx: ingress_rx, handle: on_ingress };
    let mut shard = Shard::with_budget(cfg, global);
    shard.set_budget_limits(limits.port_bytes, limits.peer_bytes, limits.peer_max_connections);
    let (egress, extra) = make_egress(&mut shard);
    let (handle, acceptor, ids, join, stop) = spawn_inner(shard, stream_cfg, ifaces, egress, Some(input));
    let task = DriverTask { stop, ctl: handle.ctl.clone(), join: Some(join) };
    (handle, acceptor, ids, task, extra)
}

fn spawn_inner<E, I, F>(
    mut shard: Shard,
    stream_cfg: StreamConfig,
    ifaces: Vec<IfaceConfig>,
    egress: E,
    input: Option<InputSource<I, F>>,
) -> (StackHandle, Acceptor, Vec<IfaceId>, tokio::task::JoinHandle<()>, Arc<AtomicBool>)
where
    E: Egress,
    I: Send + 'static,
    F: FnMut(&mut Shard, crate::Instant, I) + Send + 'static,
{
    shard.reserve_stream_state(STREAM_STATE_BYTES);
    shard.budget().global().ensure_admit_reserve(crate::budget::ADMIT_BURST * (crate::shard::ACTIVE_STATE_BYTES + STREAM_STATE_BYTES));
    let ids: Vec<IfaceId> = ifaces.into_iter().map(|c| shard.add_iface(c)).collect();
    let (tx, rx) = mpsc::channel(1024);
    let (acc_tx, acc_rx) = mpsc::channel(shard.config().accept_backlog.max(1));
    let ctl = Arc::new(Ctl { dirty: Mutex::new(HashSet::new()), pending: Mutex::new(PendingControl::default()), notify: Notify::new() });
    let stop = Arc::new(AtomicBool::new(false));
    let budget_waiter = shard.budget().global().new_waiter_id();
    let budget_wake = Waker::from(Arc::new(BudgetWake { ctl: ctl.clone() }));
    let driver = Driver {
        shard,
        egress,
        input,
        cmd_rx: rx,
        accept_tx: acc_tx,
        ctl: ctl.clone(),
        stop: stop.clone(),
        budget_waiter,
        budget_waiting: false,
        budget_wake,
        streams: HashMap::new(),
        orphans: BTreeSet::new(),
        stream_cfg,
        epoch: tokio::time::Instant::now(),
        closes: CloseCounts::default(),
        aborts: AbortCounts::default(),
        relays: HashMap::new(),
    };
    driver.shard.budget().global().register_cache_holder(driver.budget_waiter, &driver.budget_wake);
    let join = tokio::spawn(driver.run());
    (StackHandle { tx, ctl }, Acceptor { rx: acc_rx }, ids, join, stop)
}

struct Driver<E: Egress, I, F> {
    shard: Shard,
    egress: E,
    input: Option<InputSource<I, F>>,
    cmd_rx: mpsc::Receiver<Cmd>,
    accept_tx: mpsc::Sender<TcpStream>,
    ctl: Arc<Ctl>,
    stop: Arc<AtomicBool>,
    budget_waiter: u64,
    budget_waiting: bool,
    budget_wake: Waker,
    streams: HashMap<ConnId, Arc<Shared>>,
    /// Dropped streams whose tx queue has not drained into the shard yet, by
    /// deadline (the shard's orphan timeout only starts once `close` is called).
    /// Entries of streams released meanwhile are skipped when they come due.
    orphans: BTreeSet<(crate::Instant, ConnId)>,
    stream_cfg: StreamConfig,
    epoch: tokio::time::Instant,
    closes: CloseCounts,
    aborts: AbortCounts,
    /// Spliced streams: the driver moves their bytes itself.
    relays: HashMap<ConnId, Relay>,
}

/// Driver-owned state of a spliced stream.
struct Relay {
    sock: tokio::net::TcpStream,
    /// Chunks taken from the core (not yet consumed) waiting for the socket.
    pending: VecDeque<Bytes>,
    pending_len: usize,
    /// The core delivered EOF; shut the socket's write side once drained.
    core_eof: bool,
    sock_write_shut: bool,
    /// The socket hit EOF; the core's write side is shut.
    sock_eof: bool,
    stats: SpliceStats,
    done: Option<oneshot::Sender<io::Result<SpliceStats>>>,
}

/// Up to this many bytes are taken from the core per writev batch.
const RELAY_UP_BATCH: usize = 512 * 1024;
const RELAY_IOV: usize = 64;
/// Upper bound of one in-place socket read (two TX block slices at most).
const RELAY_DOWN_READ: usize = 256 * 1024;

impl<E, I, F> Driver<E, I, F>
where
    E: Egress,
    I: Send + 'static,
    F: FnMut(&mut Shard, crate::Instant, I) + Send + 'static,
{
    fn now(&self) -> crate::Instant {
        crate::Instant::from_nanos(self.epoch.elapsed().as_nanos() as u64 + 1)
    }

    fn to_tokio(&self, t: crate::Instant) -> tokio::time::Instant {
        self.epoch + std::time::Duration::from_nanos(t.as_nanos().saturating_sub(1))
    }

    async fn run(mut self) {
        let mut more_work = false;
        let mut ingress_batch = Vec::with_capacity(16);
        loop {
            if self.stop.load(Ordering::Acquire) {
                break;
            }
            if more_work {
                // `service` is synchronous, and a ready ingress/notify branch
                // can win the select indefinitely. Give the WG encrypt task a
                // turn before the next capped round on current-thread runtimes.
                tokio::task::yield_now().await;
            }
            if let Some(epoch) = self.shard.record_wait_epoch() {
                self.budget_waiting = true;
                self.shard.budget().global().register_waiter(self.budget_waiter, epoch, &self.budget_wake);
            } else if std::mem::replace(&mut self.budget_waiting, false) {
                self.shard.budget().global().remove_waiter(self.budget_waiter);
            }
            let orphan = self.orphans.first().map(|x| x.0);
            let deadline = match (self.shard.next_deadline(), orphan) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (a, b) => a.or(b),
            }
            .map(|d| self.to_tokio(d));
            let ctl = self.ctl.clone();
            tokio::select! {
                biased;
                cmd = self.cmd_rx.recv() => {
                    let Some(cmd) = cmd else { break };
                    self.handle_cmd(cmd);
                }
                count = async {
                    match self.input.as_mut() {
                        Some(source) => source.rx.recv_many(&mut ingress_batch, 16).await,
                        None => std::future::pending().await,
                    }
                } => {
                    if count == 0 {
                        self.input = None;
                    } else {
                        // Bound the batch so control commands and egress still
                        // get a service round under sustained ingress.
                        for pkt in ingress_batch.drain(..) {
                            let now = self.now();
                            let source = self.input.as_mut().unwrap();
                            (source.handle)(&mut self.shard, now, pkt);
                        }
                    }
                }
                _ = ctl.notify.notified() => {}
                _ = async {
                    match deadline {
                        Some(d) => tokio::time::sleep_until(d).await,
                        None => std::future::pending::<()>().await,
                    }
                } => {}
                // A capped service round must resume without another packet or
                // timer, but it must still drain ingress ACKs and control commands.
                _ = tokio::task::yield_now(), if more_work => {}
            }
            self.drain_control();
            more_work = self.service();
        }
    }

    fn drain_control(&mut self) {
        let pending = std::mem::take(&mut *self.ctl.pending.lock().unwrap());
        for iface in pending.egress_released {
            self.shard.egress_released(iface);
        }
        for (iface, mtu) in pending.mtu {
            self.shard.set_iface_mtu(iface, mtu);
        }
        for (iface, on) in pending.tx_csum {
            self.shard.set_iface_tx_checksum_offload(iface, on);
        }
        for (iface, max) in pending.tso {
            self.shard.set_iface_tso(iface, max);
        }
    }

    fn handle_cmd(&mut self, cmd: Cmd) {
        let now = self.now();
        match cmd {
            Cmd::Packets(iface, pkts) => {
                for (peer, p) in pkts {
                    self.shard.ingress(now, iface, peer, p);
                }
            }
            Cmd::Snapshot(reply) => {
                let tail_connections = if self.streams.len() <= 8 {
                    self.streams
                        .iter()
                        .filter_map(|(&id, shared)| {
                            let core = self.shard.info(id)?;
                            let q = shared.q.lock().unwrap();
                            Some(TailConnection {
                                id,
                                core,
                                adapter_tx_queued: q.tx.len(),
                                adapter_rx_queued: q.rx_len,
                                write_parked: q.write_parked,
                                send_room: q.send_room,
                            })
                        })
                        .collect()
                } else {
                    Vec::new()
                };
                let budget = self.shard.budget();
                let _ = reply.send(StackSnapshot {
                    stats: self.shard.stats().clone(),
                    port_physical_bytes: budget.physical_used(),
                    global_reserved_bytes: budget.global().reserved(),
                    global_cached_bytes: budget.global().cached_bytes(),
                    tx_cache: self.shard.tx_cache_snapshot(),
                    core_reserve_failures: budget.reserve_failures(),
                    failures_by_kind: budget.stats().failures(),
                    failures_by_level: budget.stats().failures_by_level(),
                    waiters: budget.physical_waiters(),
                    record_waiters: self.shard.record_waiters(),
                    progress_reserve: budget.progress_reserve_active(),
                    admission_debt: budget.admission_debt(),
                    rx_progress_bytes: budget.rx_progress_bytes(),
                    closes: self.closes,
                    aborts: self.aborts,
                    active_connections: self.shard.conn_count(),
                    adapter_streams: self.streams.len(),
                    senders: budget.senders(),
                    share_blocked: budget.share_blocked(),
                    budget_waiting: self.shard.budget_wait_epoch().is_some(),
                    tail_connections,
                });
            }
        }
    }

    /// Move data between stream queues and the shard, run the shard, dispatch events.
    fn service(&mut self) -> bool {
        self.expire_orphans();
        for round in 0..16 {
            // Take packets that arrived meanwhile before producing more: a
            // driver kept busy by bulk egress would otherwise leave ACKs and
            // other flows' requests queued in the device (RR latency).
            if round > 0 {
                self.ingest_ready(16);
            }
            let dirty = std::mem::take(&mut *self.ctl.dirty.lock().unwrap());
            for id in dirty {
                self.pump(id);
            }
            let now = self.now();
            let mut sink = Sink { egress: &mut self.egress };
            let out = self.shard.run(now, &mut sink);
            let mut any = false;
            for _ in 0..128 {
                let Some(ev) = self.shard.poll_event() else { break };
                any = true;
                self.on_event(ev);
            }
            if !any && !out.more && self.ctl.dirty.lock().unwrap().is_empty() {
                return false;
            }
        }
        true
    }

    /// Non-blocking: up to `max` queued ingress items (host source and
    /// `StackHandle::ingress` batches).
    fn ingest_ready(&mut self, max: usize) {
        let now = self.now();
        for _ in 0..max {
            let Some(source) = self.input.as_mut() else { break };
            match source.rx.try_recv() {
                Ok(item) => (source.handle)(&mut self.shard, now, item),
                Err(_) => break,
            }
        }
        for _ in 0..max {
            match self.cmd_rx.try_recv() {
                Ok(Cmd::Packets(iface, pkts)) => {
                    for (peer, p) in pkts {
                        self.shard.ingress(now, iface, peer, p);
                    }
                }
                Ok(cmd) => self.handle_cmd(cmd),
                Err(_) => break,
            }
        }
    }

    fn on_event(&mut self, ev: Event) {
        match ev {
            Event::Accepted(id) => {
                let Some(info) = self.shard.info(id) else { return };
                let memory = self.shard.memory_handle(info.peer);
                // Reserved with the core state at SYN time; allocating here is
                // only a fallback for a shard configured without it.
                let reserved = self.shard.take_stream_memory(id);
                let Some(state_memory) = reserved.or_else(|| memory.try_allocate_kind(STREAM_STATE_BYTES, AllocationKind::StreamState)) else {
                    self.aborts.stream_state += 1;
                    self.shard.abort(id);
                    return;
                };
                let memory_waiter = memory.global().new_waiter_id();
                let driver_waiter = memory.global().new_waiter_id();
                let rx_index_waiter = memory.global().new_waiter_id();
                let driver_wake = Waker::from(Arc::new(DriverWake { ctl: self.ctl.clone(), id }));
                // Start the share-aware room at the first pump's value so an
                // immediately-writing app does not park on a stale zero.
                let send_room = self.shard.send_share(info.peer).min(self.stream_cfg.tx_cap as u64) as usize;
                let sh = Arc::new(Shared {
                    id,
                    peer: info.peer,
                    q: Mutex::new(Queues { send_room, send_limit: send_room, ..Queues::default() }),
                    ctl: self.ctl.clone(),
                    cfg: self.stream_cfg.clone(),
                    memory,
                    _state_memory: state_memory,
                    memory_waiter,
                    driver_waiter,
                    rx_index_waiter,
                    memory_waiting: AtomicBool::new(false),
                    driver_tx_wait: AtomicU8::new(TX_WAIT_NONE),
                    rx_index_waiting: AtomicBool::new(false),
                    driver_wake,
                });
                self.streams.insert(id, sh.clone());
                let meta = ConnMeta { iface: info.iface, peer: info.peer, local: info.local, remote: info.remote };
                if self.accept_tx.try_send(TcpStream { sh, meta }).is_err() {
                    self.aborts.accept_backlog += 1;
                    self.shard.abort(id);
                    self.remove_stream(id);
                    return;
                }
                self.pump(id);
            }
            Event::Readable(id) | Event::Writable(id) => self.pump(id),
            Event::Closed(id, reason) => {
                self.closes.note(reason);
                self.pump(id);
                if self.relays.contains_key(&id) {
                    // An orderly close leaves received data readable: the
                    // relay drains it upstream and finishes on its own.
                    if reason != CloseReason::Normal {
                        self.finish_relay(id, Err(io::Error::new(io::ErrorKind::ConnectionReset, format!("{reason:?}"))));
                    }
                    return;
                }
                if let Some(sh) = self.streams.get(&id) {
                    let mut q = sh.q.lock().unwrap();
                    q.error = Some(reason);
                    let (r, w, f) = (q.read_waker.take(), q.write_waker.take(), q.flush_waker.take());
                    drop(q);
                    r.map(|w| w.wake());
                    w.map(|w| w.wake());
                    f.map(|w| w.wake());
                }
                self.release_if_done(id);
            }
            Event::Connected(_) => {}
        }
    }

    /// Abort dropped streams that could not hand their remaining bytes to the shard
    /// within the orphan timeout (e.g. the peer keeps a zero window forever).
    fn expire_orphans(&mut self) {
        let now = self.now();
        while let Some(&(t, id)) = self.orphans.first() {
            if t > now {
                break;
            }
            self.orphans.pop_first();
            if let Some(sh) = self.remove_stream(id) {
                sh.q.lock().unwrap().tx.clear();
                self.aborts.orphan_timeout += 1;
                self.shard.abort(id);
            }
        }
    }

    fn remove_stream(&mut self, id: ConnId) -> Option<Arc<Shared>> {
        let removed = self.streams.remove(&id);
        if let Some(sh) = &removed {
            if sh.memory_waiting.swap(false, Ordering::AcqRel) {
                sh.memory.global().remove_waiter(sh.memory_waiter);
            }
            clear_tx_wait(sh);
            if sh.rx_index_waiting.swap(false, Ordering::AcqRel) {
                sh.memory.global().remove_waiter(sh.rx_index_waiter);
            }
            // The queue is gone; stop charging it to the core connection.
            self.shard.set_adapter_tx(id, 0);
        }
        compact_sparse_map(&mut self.streams);
        removed
    }

    fn release_if_done(&mut self, id: ConnId) {
        let now = self.now();
        let timeout = self.shard.config().orphan_timeout;
        let state = self.streams.get(&id).map(|sh| {
            let mut q = sh.q.lock().unwrap();
            let done = q.closed_by_app && (q.error.is_some() || q.tx.is_empty());
            let start_orphan = q.closed_by_app && !done && q.orphan_deadline.is_none();
            if start_orphan {
                q.orphan_deadline = Some(now + timeout);
            }
            let orphan_deadline = if done { q.orphan_deadline.take() } else { None };
            (done, q.rx_len > 0, start_orphan, orphan_deadline)
        });
        if let Some((false, _, true, _)) = state {
            self.orphans.insert((now + timeout, id));
        }
        if let Some((true, unread, _, orphan_deadline)) = state {
            if let Some(deadline) = orphan_deadline {
                self.orphans.remove(&(deadline, id));
            }
            if unread {
                // Dropped with unread data: the bytes are lost, tell the peer (§10.3).
                self.aborts.unread_drop += 1;
                self.shard.abort(id);
            } else {
                let now = self.now();
                self.shard.close(now, id);
            }
            self.remove_stream(id);
        }
    }

    /// Exchange data for one stream.
    fn pump(&mut self, id: ConnId) {
        let Some(sh) = self.streams.get(&id).cloned() else { return };
        let now = self.now();
        if !self.relays.is_empty() && self.relay_pump(id, &sh, now) {
            return;
        }
        let cfg = &self.stream_cfg;
        let consumed = {
            let mut q = sh.q.lock().unwrap();
            if q.splice.is_some() {
                drop(q);
                if self.start_relay(id) {
                    self.relay_pump(id, &sh, self.now());
                }
                return;
            }
            std::mem::take(&mut q.rx_consumed_pending)
        };
        if consumed > 0 {
            self.shard.consume_adapter(now, id, consumed);
        }
        // rx: shard → stream queue (zero-copy chunks), bounded by rx_cap.
        let mut wake_reader = None;
        let mut abort_rx = false;
        {
            let mut q = sh.q.lock().unwrap();
            let was_empty = q.rx.is_empty() && !q.rx_eof;
            while q.rx_len < cfg.rx_cap && !q.rx_eof && q.error.is_none() {
                let observed = sh.memory.global().release_epoch();
                match q.rx.reserve_one(&sh.memory, AllocationKind::AdapterRx) {
                    Ok(()) => {
                        if sh.rx_index_waiting.load(Ordering::Relaxed) && sh.rx_index_waiting.swap(false, Ordering::AcqRel) {
                            sh.memory.global().remove_waiter(sh.rx_index_waiter);
                        }
                    }
                    Err(QueueGrowError::Quota(bytes)) => {
                        sh.rx_index_waiting.store(true, Ordering::Release);
                        sh.memory.global().register_physical_waiter(
                            sh.rx_index_waiter,
                            observed,
                            &sh.driver_wake,
                            &sh.memory,
                            bytes,
                            AllocationKind::AdapterRx,
                        );
                        break;
                    }
                    Err(QueueGrowError::Allocation) => {
                        q.error = Some(CloseReason::Aborted);
                        abort_rx = true;
                        break;
                    }
                }
                match self.shard.read_chunk_for_adapter(now, id, cfg.rx_cap - q.rx_len) {
                    Ok(b) => {
                        q.rx_len += b.len();
                        q.rx.push_back_reserved(b);
                    }
                    Err(ReadResult::Eof) => q.rx_eof = true,
                    Err(ReadResult::Closed(r)) => q.error = Some(r),
                    Err(_) => break,
                }
            }
            if was_empty && (!q.rx.is_empty() || q.rx_eof || q.error.is_some()) {
                wake_reader = q.read_waker.take();
            }
        }
        if abort_rx {
            self.aborts.rx_index_alloc += 1;
            self.shard.abort(id);
        }
        // tx: stream queue → shard.
        let mut wake_writer = None;
        let mut wake_flush = None;
        let mut shutdown = false;
        let app_closed;
        {
            let mut q = sh.q.lock().unwrap();
            while !q.tx.is_empty() {
                let n = q.tx.front().len().min(64 * 1024);
                let observed = sh.memory.global().release_epoch();
                let result = self.shard.write_for_adapter(id, &q.tx.front()[..n]);
                track_tx_wait(&mut self.shard, &sh, result, observed, n);
                match result {
                    WriteResult::Written(w) => {
                        q.tx.consume(w);
                        if w < n {
                            break;
                        }
                    }
                    WriteResult::WouldBlock | WriteResult::QuotaBlocked | WriteResult::MemoryBlocked => break,
                    WriteResult::Closed => {
                        q.error.get_or_insert(CloseReason::Aborted);
                        q.tx.clear();
                        break;
                    }
                }
            }
            if q.tx.is_empty() {
                clear_tx_wait(&sh);
            }
            // Publish the share-aware room: one accounting across the adapter
            // queue and the core send buffer (docs/design/0007 §2.1, §2.4).
            self.shard.set_adapter_tx(id, q.tx.len() as u32);
            let share = self.shard.send_share(sh.peer);
            let core_tx = self.shard.tx_queued(id).unwrap_or(0) as u64;
            q.send_limit = share.min(cfg.tx_cap as u64) as usize;
            q.send_room = share.saturating_sub(core_tx + q.tx.len() as u64).min(usize::MAX as u64) as usize;
            let min_free = q.write_need.min(q.send_limit).max(1);
            let wakeable = q.write_parked && cfg.tx_cap.saturating_sub(q.tx.len()).min(q.send_room) >= min_free;
            if wakeable {
                q.write_parked = false;
                wake_writer = q.write_waker.take();
            }
            // A writer parked on the share may have an empty queue, so no
            // core write will block to set want_write: mark it here, or a
            // later drain of the core buffer never re-runs this pump
            // (docs/design/0007 §2.4).
            if q.write_parked {
                self.shard.note_write_parked(id);
            }
            if q.tx.is_empty() {
                wake_flush = q.flush_waker.take();
            }
            if q.tx.is_empty() && q.tx_shutdown {
                shutdown = true;
            }
            app_closed = q.closed_by_app;
        }
        if shutdown {
            self.shard.shutdown_write(id);
        }
        if let Some(w) = wake_reader {
            w.wake();
        }
        if let Some(w) = wake_writer {
            w.wake();
        }
        if let Some(w) = wake_flush {
            w.wake();
        }
        if app_closed {
            self.release_if_done(id);
        }
    }
}

impl<E, I, F> Driver<E, I, F>
where
    E: Egress,
    I: Send + 'static,
    F: FnMut(&mut Shard, crate::Instant, I) + Send + 'static,
{
    /// Take a pending `TcpStream::splice` request for `id`.
    fn start_relay(&mut self, id: ConnId) -> bool {
        let Some(sh) = self.streams.get(&id).cloned() else { return false };
        let mut q = sh.q.lock().unwrap();
        let Some(req) = q.splice.take() else { return false };
        // Chunks the adapter already took from the core go out first; they
        // are consumed (window reopened) only once the socket accepts them.
        let mut pending = VecDeque::new();
        while let Some(b) = q.rx.pop_front() {
            pending.push_back(b);
        }
        let pending_len = std::mem::take(&mut q.rx_len);
        let consumed = std::mem::take(&mut q.rx_consumed_pending);
        let core_eof = q.rx_eof;
        let error = q.error;
        drop(q);
        let now = self.now();
        if consumed > 0 {
            self.shard.consume_adapter(now, id, consumed);
        }
        // Move the socket to the driver's own reactor. A host that runs the
        // driver on a dedicated current-thread runtime then does all relay
        // I/O on one thread: no readiness wakeups to other workers.
        let sock = match req.upstream.into_std().and_then(|s| tokio::net::TcpStream::from_std(s)) {
            Ok(s) => s,
            Err(e) => {
                let _ = req.done.send(Err(e));
                self.shard.abort(id);
                self.remove_stream(id);
                return false;
            }
        };
        self.relays.insert(
            id,
            Relay { sock, pending, pending_len, core_eof, sock_write_shut: false, sock_eof: false, stats: SpliceStats::default(), done: Some(req.done) },
        );
        if let Some(r) = error.filter(|&r| r != CloseReason::Normal) {
            self.finish_relay(id, Err(io::Error::new(io::ErrorKind::ConnectionReset, format!("{r:?}"))));
            return false;
        }
        true
    }

    /// Move bytes between a spliced stream and its socket until both sides
    /// would block; readiness and core events call this again.
    /// Returns whether this was a relay, including one finished by this call.
    fn relay_pump(&mut self, id: ConnId, sh: &Shared, now: crate::Instant) -> bool {
        let Some(relay) = self.relays.get_mut(&id) else { return false };
        let mut cx = Context::from_waker(&sh.driver_wake);
        let mut failure: Option<io::Error> = None;

        // Core → socket: zero-copy chunks out with writev.
        'up: loop {
            while relay.pending_len < RELAY_UP_BATCH && !relay.core_eof {
                match self.shard.read_chunk_for_adapter(now, id, RELAY_UP_BATCH - relay.pending_len) {
                    Ok(b) => {
                        relay.pending_len += b.len();
                        relay.pending.push_back(b);
                    }
                    Err(ReadResult::Eof) => relay.core_eof = true,
                    Err(ReadResult::Closed(CloseReason::Normal)) => relay.core_eof = true,
                    Err(ReadResult::Closed(r)) => {
                        failure = Some(io::Error::new(io::ErrorKind::ConnectionReset, format!("{r:?}")));
                        break 'up;
                    }
                    Err(_) => break,
                }
            }
            if relay.pending.is_empty() {
                if relay.core_eof && !relay.sock_write_shut {
                    // shutdown(SHUT_WR); Pending retries on the next pump.
                    match Pin::new(&mut relay.sock).poll_shutdown(&mut cx) {
                        Poll::Ready(Ok(())) => relay.sock_write_shut = true,
                        Poll::Ready(Err(e)) => failure = Some(e),
                        Poll::Pending => {}
                    }
                }
                break;
            }
            let mut iov = [std::io::IoSlice::new(&[]); RELAY_IOV];
            let mut n_iov = 0;
            for b in relay.pending.iter().take(RELAY_IOV) {
                iov[n_iov] = std::io::IoSlice::new(b);
                n_iov += 1;
            }
            match relay.sock.try_write_vectored(&iov[..n_iov]) {
                Ok(mut n) => {
                    relay.stats.uploaded += n as u64;
                    relay.pending_len -= n;
                    self.shard.consume_adapter(now, id, n);
                    while n > 0 {
                        let front = relay.pending.front_mut().unwrap();
                        if n >= front.len() {
                            n -= front.len();
                            relay.pending.pop_front();
                        } else {
                            let _ = front.split_to(n);
                            n = 0;
                        }
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    if relay.sock.poll_write_ready(&mut cx).is_pending() {
                        break;
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => {
                    failure = Some(e);
                    break;
                }
            }
        }

        // Socket → core: read straight into the send buffer. Bytes the app
        // wrote into this handle before splicing go first.
        let mut q = sh.q.lock().unwrap();
        while failure.is_none() && !q.tx.is_empty() {
            let n = q.tx.front().len().min(64 * 1024);
            let observed = sh.memory.global().release_epoch();
            let result = self.shard.write_for_adapter(id, &q.tx.front()[..n]);
            track_tx_wait(&mut self.shard, sh, result, observed, n);
            match result {
                WriteResult::Written(w) => q.tx.consume(w),
                WriteResult::WouldBlock | WriteResult::QuotaBlocked | WriteResult::MemoryBlocked => break,
                WriteResult::Closed => failure = Some(io::Error::new(io::ErrorKind::ConnectionReset, "core closed while draining splice backlog")),
            }
        }
        self.shard.set_adapter_tx(id, q.tx.len() as u32);
        let backlog = !q.tx.is_empty();
        drop(q);
        while failure.is_none() && !backlog && !relay.sock_eof {
            let sock = &relay.sock;
            let observed = sh.memory.global().release_epoch();
            let r = self.shard.write_with_for_adapter(id, RELAY_DOWN_READ, |[a, b]| {
                let mut iov = [std::io::IoSliceMut::new(a), std::io::IoSliceMut::new(b)];
                sock.try_read_vectored(&mut iov)
            });
            match &r {
                Ok(result) => track_tx_wait(&mut self.shard, sh, *result, observed, RELAY_DOWN_READ),
                Err(_) => clear_tx_wait(sh),
            }
            match r {
                Ok(WriteResult::Written(0)) => {
                    relay.sock_eof = true;
                    self.shard.shutdown_write(id);
                }
                Ok(WriteResult::Written(n)) => relay.stats.downloaded += n as u64,
                // The core emits Writable once space frees up.
                Ok(WriteResult::WouldBlock) => break,
                Ok(WriteResult::QuotaBlocked) | Ok(WriteResult::MemoryBlocked) => break,
                // Closed in order: nothing more can go to the peer; stop
                // reading (unread upstream bytes reset it when we close).
                Ok(WriteResult::Closed) => relay.sock_eof = true,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    if relay.sock.poll_read_ready(&mut cx).is_pending() {
                        break;
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(e) => failure = Some(e),
            }
        }

        if let Some(e) = failure {
            self.finish_relay(id, Err(e));
        } else if relay.sock_eof && relay.sock_write_shut {
            self.finish_relay(id, Ok(()));
        }
        true
    }

    /// End a spliced stream: an orderly finish closes the core connection
    /// (pending bytes and FIN still go out); a failure resets both sides.
    fn finish_relay(&mut self, id: ConnId, result: io::Result<()>) {
        let Some(mut relay) = self.relays.remove(&id) else { return };
        let now = self.now();
        let unconsumed = relay.pending_len;
        relay.pending.clear();
        if unconsumed > 0 {
            self.shard.consume_adapter(now, id, unconsumed);
        }
        let ok = result.is_ok();
        if ok {
            self.shard.close(now, id);
        } else {
            #[allow(deprecated)]
            let _ = relay.sock.set_linger(Some(std::time::Duration::ZERO));
            self.shard.abort(id);
        }
        if let Some(sh) = self.streams.get(&id) {
            let mut q = sh.q.lock().unwrap();
            q.closed_by_app = true;
            q.tx.clear();
            q.error.get_or_insert(if ok { CloseReason::Normal } else { CloseReason::Aborted });
        }
        self.remove_stream(id);
        let stats = relay.stats;
        drop(relay.sock);
        if let Some(done) = relay.done.take() {
            let _ = done.send(result.map(|()| stats));
        }
    }
}

impl<E: Egress, I, F> Drop for Driver<E, I, F> {
    fn drop(&mut self) {
        if std::mem::replace(&mut self.budget_waiting, false) {
            self.shard.budget().global().remove_waiter(self.budget_waiter);
        }
        self.shard.budget().global().remove_cache_holder(self.budget_waiter);
        for sh in self.streams.values() {
            let mut q = sh.q.lock().unwrap();
            q.error = Some(CloseReason::Aborted);
            q.rx.clear();
            q.rx_len = 0;
            q.tx.clear();
            let (r, w, f) = (q.read_waker.take(), q.write_waker.take(), q.flush_waker.take());
            drop(q);
            if let Some(w) = r {
                w.wake();
            }
            if let Some(w) = w {
                w.wake();
            }
            if let Some(w) = f {
                w.wake();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sim::pattern_byte;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;
    use std::task::Wake;
    use tokio::io::AsyncWriteExt;

    #[tokio::test(flavor = "current_thread")]
    async fn driver_panic_is_observable_and_join_is_idempotent() {
        let (ingress_tx, ingress_rx) = mpsc::channel(1);
        let (handle, _acceptor, _ids, mut task) = spawn_with_source(
            StackConfig::default(),
            StreamConfig::default(),
            vec![IfaceConfig::default()],
            |_iface: IfaceId, _peer: PeerId, _packet: &OutPacket<'_>| SendResult::Accepted,
            GlobalBudget::new(1 << 20),
            ResourceLimits { port_bytes: 1 << 20, peer_bytes: 1 << 20, peer_max_connections: 1 },
            ingress_rx,
            |_shard: &mut Shard, _now: crate::Instant, _packet: ()| panic!("injected driver failure"),
        );
        // The owner remains live when a select branch stops waiting for it.
        assert!(tokio::time::timeout(std::time::Duration::from_millis(1), task.wait_finished()).await.is_err());
        ingress_tx.send(()).await.unwrap();
        let error = tokio::time::timeout(std::time::Duration::from_secs(1), task.wait_finished()).await.unwrap().unwrap_err();
        assert!(error.is_panic());
        assert!(handle.snapshot().await.is_none());
        task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn source_batch_preserves_order_and_closed_source_keeps_driver_alive() {
        let (ingress_tx, ingress_rx) = mpsc::channel(64);
        let seen = Arc::new(Mutex::new(Vec::new()));
        let seen_by_driver = Arc::clone(&seen);
        let (handle, _acceptor, _ids, task) = spawn_with_source(
            StackConfig::default(),
            StreamConfig::default(),
            vec![IfaceConfig::default()],
            |_iface: IfaceId, _peer: PeerId, _packet: &OutPacket<'_>| SendResult::Accepted,
            GlobalBudget::new(1 << 20),
            ResourceLimits { port_bytes: 1 << 20, peer_bytes: 1 << 20, peer_max_connections: 1 },
            ingress_rx,
            move |_shard: &mut Shard, _now: crate::Instant, packet: usize| {
                seen_by_driver.lock().unwrap().push(packet);
            },
        );
        for packet in 0..33 {
            ingress_tx.try_send(packet).unwrap();
        }
        drop(ingress_tx);
        tokio::time::timeout(std::time::Duration::from_secs(1), async {
            loop {
                if seen.lock().unwrap().len() == 33 {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
        assert_eq!(*seen.lock().unwrap(), (0..33).collect::<Vec<_>>());
        assert!(handle.snapshot().await.is_some());
        task.shutdown_and_join().await.unwrap();
    }

    struct CountWake(AtomicUsize);
    impl Wake for CountWake {
        fn wake(self: Arc<Self>) {
            self.0.fetch_add(1, Ordering::Relaxed);
        }
        fn wake_by_ref(self: &Arc<Self>) {
            self.0.fetch_add(1, Ordering::Relaxed);
        }
    }

    type TestRelayDriver = Driver<fn(IfaceId, PeerId, &OutPacket<'_>) -> SendResult, (), NoInput>;

    /// Use a real handshake and socket, but drive the owner synchronously so
    /// the exact allocation/release ordering is reproducible without sleeps.
    async fn parked_relay(prefix: &[u8]) -> (TestRelayDriver, TcpStream, tokio::net::TcpStream, crate::sim::Side) {
        let mut sim = crate::sim::Sim::new(3740, StackConfig::default(), StackConfig::default(), Default::default(), Default::default());
        sim.a.shard.set_budget_limits(1 << 20, 1 << 20, 16);
        sim.a.shard.reserve_stream_state(STREAM_STATE_BYTES);
        sim.connect(43000, Default::default());
        sim.run_until(sim.now + std::time::Duration::from_millis(100));
        let id = sim.a.accepted[0];
        let global = sim.a.shard.budget().global().clone();
        let (_, cmd_rx) = mpsc::channel(1);
        let (accept_tx, mut accept_rx) = mpsc::channel(1);
        let ctl = Arc::new(Ctl { dirty: Mutex::new(HashSet::new()), pending: Mutex::new(PendingControl::default()), notify: Notify::new() });
        let mut driver = Driver {
            shard: sim.a.shard,
            egress: (|_, _, _| SendResult::Accepted) as fn(IfaceId, PeerId, &OutPacket<'_>) -> SendResult,
            input: None::<InputSource<(), NoInput>>,
            cmd_rx,
            accept_tx,
            ctl: ctl.clone(),
            stop: Arc::new(AtomicBool::new(false)),
            budget_waiter: global.new_waiter_id(),
            budget_waiting: false,
            budget_wake: Waker::from(Arc::new(BudgetWake { ctl })),
            streams: HashMap::new(),
            orphans: BTreeSet::new(),
            stream_cfg: StreamConfig::default(),
            epoch: tokio::time::Instant::now() - std::time::Duration::from_nanos(sim.now.as_nanos()),
            closes: CloseCounts::default(),
            aborts: AbortCounts::default(),
            relays: HashMap::new(),
        };
        driver.on_event(Event::Accepted(id));
        let mut stream = accept_rx.try_recv().unwrap();
        stream.write_all(prefix).await.unwrap();
        // The ordinary pump has published these bytes but has not yet been
        // able to move them into core TX when the app requests splice.
        driver.shard.set_adapter_tx(id, prefix.len() as u32);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let (up, sock) = tokio::join!(tokio::net::TcpStream::connect(listener.local_addr().unwrap()), listener.accept());
        let mut up = up.unwrap();
        let (done, _) = oneshot::channel();
        stream.sh.q.lock().unwrap().splice = Some(SpliceRequest { upstream: sock.unwrap().0, done });
        assert!(driver.start_relay(id));
        up.write_all(b"socket tail").await.unwrap();
        driver.relays[&id].sock.readable().await.unwrap();
        driver.ctl.dirty.lock().unwrap().clear();
        assert_eq!(driver.shard.tx_queued(id), Some(0));
        (driver, stream, up, sim.b)
    }

    struct ReleaseOnReclaim(Mutex<Option<MemoryLease>>);

    impl Wake for ReleaseOnReclaim {
        fn wake(self: Arc<Self>) {
            self.wake_by_ref();
        }
        fn wake_by_ref(self: &Arc<Self>) {
            let lease = self.0.lock().unwrap().take();
            drop(lease);
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn issue37_relay_does_not_miss_release_during_failed_allocation() {
        let (mut driver, stream, up, _peer) = parked_relay(&[]).await;
        let sh = &stream.sh;
        let global = sh.memory.global().clone();
        let mut cache = sh.memory.try_allocate_kind(65536, AllocationKind::TxBlock).unwrap();
        assert!(cache.park_cached());
        let ceiling = (1 << 20) - global.headroom(1 << 20, crate::budget::Tier::Block);
        let held = sh.memory.try_allocate_kind(ceiling - driver.shard.budget().physical_used(), AllocationKind::TxBlock).unwrap();
        // Model a different owner reclaiming memory while this failed
        // allocator requests cache reclaim, before relay_pump can park.
        let reclaim = Arc::new(ReleaseOnReclaim(Mutex::new(Some(held))));
        let holder = global.new_waiter_id();
        global.register_cache_holder(holder, &Waker::from(reclaim.clone()));
        driver.pump(sh.id);
        assert!(reclaim.0.lock().unwrap().is_none(), "the release must happen inside the failing attempt");
        assert_eq!(driver.shard.tx_queued(sh.id), Some(0));
        assert!(driver.shard.budget().can_allocate(sh.peer, crate::buf::TX_FIRST_WRITE_CHARGE, AllocationKind::TxBlock));
        assert!(driver.ctl.dirty.lock().unwrap().remove(&sh.id), "relay missed the only memory release and has no ACK to wake it");
        driver.pump(sh.id);
        assert_eq!(driver.shard.tx_queued(sh.id), Some(b"socket tail".len()));
        assert_eq!(driver.shard.budget().physical_waiters().tx_block, 0);
        global.remove_cache_holder(holder);
        drop((cache, stream, driver, up));
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn issue37_splice_backlog_waits_for_memory_and_preserves_order() {
        let prefix = b"queued prefix:";
        let (mut driver, stream, up, mut peer) = parked_relay(prefix).await;
        let sh = &stream.sh;
        let global = sh.memory.global().clone();
        let ceiling = (1 << 20) - global.headroom(1 << 20, crate::budget::Tier::Block);
        let held = sh.memory.try_allocate_kind(ceiling - driver.shard.budget().physical_used(), AllocationKind::TxBlock).unwrap();
        driver.pump(sh.id);
        assert_eq!(driver.shard.tx_queued(sh.id), Some(0));
        assert_eq!(sh.q.lock().unwrap().tx.len(), prefix.len());
        assert_eq!(driver.shard.budget().physical_waiters().tx_block, 1, "splice must park for its pre-existing adapter backlog");
        drop(held);
        assert!(driver.ctl.dirty.lock().unwrap().remove(&sh.id));
        driver.pump(sh.id);
        assert!(sh.q.lock().unwrap().tx.is_empty());
        let mut bytes = Vec::new();
        let now = driver.now();
        driver.shard.run(now, &mut |_: IfaceId, pkt: &OutPacket<'_>| {
            bytes.extend_from_slice(pkt.payload[0]);
            bytes.extend_from_slice(pkt.payload[1]);
            peer.shard.ingress(now, peer.iface, sh.peer, Bytes::from(pkt.to_vec()));
            SendResult::Accepted
        });
        assert_eq!(bytes, b"queued prefix:socket tail");
        let ack_at = now + std::time::Duration::from_millis(50);
        peer.shard.run(ack_at, &mut |_: IfaceId, pkt: &OutPacket<'_>| {
            driver.shard.ingress(ack_at, stream.meta.iface, sh.peer, Bytes::from(pkt.to_vec()));
            SendResult::Accepted
        });
        assert_eq!(driver.shard.tx_queued(sh.id), Some(0));
        assert_eq!(driver.shard.budget().senders(), 0, "drained splice backlog must stop counting as adapter TX");
        drop((stream, driver, up));
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn tx_wait_state_switches_without_stale_queue_entries_and_clears_on_success() {
        let (mut driver, stream, _up, _peer) = parked_relay(b"prefix").await;
        let sh = &stream.sh;
        let global = sh.memory.global().clone();
        let observed = global.release_epoch();
        // QuotaBlocked -> MemoryBlocked must clear the broadcast waiter before
        // parking in the physical waiter queue.
        track_tx_wait(&mut driver.shard, sh, WriteResult::QuotaBlocked, observed, 64);
        assert_eq!(sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_QUOTA);
        track_tx_wait(&mut driver.shard, sh, WriteResult::MemoryBlocked, observed, 64);
        assert_eq!(sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_MEMORY);
        assert_eq!(driver.shard.budget().physical_waiters().tx_block, 1);
        // MemoryBlocked -> QuotaBlocked must remove the physical waiter before
        // parking in the broadcast waiter set.
        track_tx_wait(&mut driver.shard, sh, WriteResult::QuotaBlocked, observed, 64);
        assert_eq!(sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_QUOTA);
        assert_eq!(driver.shard.budget().physical_waiters().tx_block, 0);
        // QuotaBlocked with send_space == 0 (share-bound) must clear the
        // broadcast waiter instead of keeping a stale entry in GlobalBudget.
        let room = driver.shard.send_space(sh.id);
        assert_eq!(driver.shard.write(sh.id, &vec![3u8; room]), WriteResult::Written(room));
        assert_eq!(driver.shard.send_space(sh.id), 0);
        let observed = global.release_epoch();
        driver.ctl.dirty.lock().unwrap().clear();
        global.register_waiter(sh.driver_waiter, observed, &sh.driver_wake);
        sh.driver_tx_wait.store(TX_WAIT_QUOTA, Ordering::Release);
        track_tx_wait(&mut driver.shard, sh, WriteResult::QuotaBlocked, observed, 64);
        assert_eq!(sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_NONE);
        let held = sh.memory.try_allocate(64).unwrap();
        drop(held);
        assert!(!driver.ctl.dirty.lock().unwrap().contains(&sh.id), "stale waiter fired after share-bound transition");
        drop((stream, driver));
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn share_bound_relay_ignores_unrelated_releases_and_recovers_on_ack_or_sender_exit() {
        let mut cfg = StackConfig::default();
        cfg.min_prefetch = 1 << 20;
        cfg.prefetch_max = 1 << 20;
        let mut sim = crate::sim::Sim::new(4101, cfg, StackConfig::default(), Default::default(), Default::default());
        sim.a.shard.set_budget_limits(819_200, 819_200, 16);
        sim.a.shard.reserve_stream_state(STREAM_STATE_BYTES);
        sim.connect(43100, Default::default());
        sim.connect(43101, Default::default());
        sim.run_until(sim.now + std::time::Duration::from_millis(100));
        let id = sim.a.accepted[0];
        let sibling = sim.a.accepted[1];
        let mut peer_side = sim.b;
        let global = sim.a.shard.budget().global().clone();
        let (_, cmd_rx) = mpsc::channel(1);
        let (accept_tx, mut accept_rx) = mpsc::channel(2);
        let ctl = Arc::new(Ctl { dirty: Mutex::new(HashSet::new()), pending: Mutex::new(PendingControl::default()), notify: Notify::new() });
        let mut driver = Driver {
            shard: sim.a.shard,
            egress: (|_, _, _| SendResult::Accepted) as fn(IfaceId, PeerId, &OutPacket<'_>) -> SendResult,
            input: None::<InputSource<(), NoInput>>,
            cmd_rx,
            accept_tx,
            ctl: ctl.clone(),
            stop: Arc::new(AtomicBool::new(false)),
            budget_waiter: global.new_waiter_id(),
            budget_waiting: false,
            budget_wake: Waker::from(Arc::new(BudgetWake { ctl })),
            streams: HashMap::new(),
            orphans: BTreeSet::new(),
            stream_cfg: StreamConfig::default(),
            epoch: tokio::time::Instant::now() - std::time::Duration::from_nanos(sim.now.as_nanos()),
            closes: CloseCounts::default(),
            aborts: AbortCounts::default(),
            relays: HashMap::new(),
        };
        driver.on_event(Event::Accepted(id));
        driver.on_event(Event::Accepted(sibling));
        let stream = accept_rx.try_recv().unwrap();
        let sibling_stream = accept_rx.try_recv().unwrap();
        // Activate both senders first so `id` fills the 2-sender share (192 KiB),
        // which can then grow to the 1-sender share (320 KiB) when `sibling` exits.
        assert_eq!(driver.shard.write(sibling, &[7u8; 16 << 10]), WriteResult::Written(16 << 10));
        assert_eq!(driver.shard.write(id, &[5u8; 16 << 10]), WriteResult::Written(16 << 10));
        let rest = driver.shard.send_space(id);
        assert!(rest > 0);
        assert_eq!(driver.shard.write(id, &vec![5u8; rest]), WriteResult::Written(rest));
        let room = (16 << 10) + rest;
        assert_eq!(driver.shard.send_space(id), 0);
        while driver.shard.poll_event().is_some() {}

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let (up, sock) = tokio::join!(tokio::net::TcpStream::connect(listener.local_addr().unwrap()), listener.accept());
        let mut up = up.unwrap();
        let (done, _) = oneshot::channel();
        stream.sh.q.lock().unwrap().splice = Some(SpliceRequest { upstream: sock.unwrap().0, done });
        assert!(driver.start_relay(id));
        up.write_all(b"after own ack").await.unwrap();
        driver.relays[&id].sock.readable().await.unwrap();
        driver.ctl.dirty.lock().unwrap().clear();

        // Pumping the share-exhausted relay records one share block and arms
        // the shard-level share waiter without registering a global waiter.
        driver.pump(id);
        assert_eq!(stream.sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_NONE);
        assert!(driver.shard.record_wait_epoch().is_none());
        let blocked_before = driver.shard.budget().share_blocked();
        assert_eq!(blocked_before, 1);

        // 200 unrelated physical/logical budget releases must neither mark the
        // stream dirty nor trigger redundant `share_blocked` retries.
        let mut other_budget = crate::budget::Budget::new(global.clone());
        other_budget.set_limits(819_200, 819_200, 4);
        for _ in 0..200 {
            let lease = stream.sh.memory.try_allocate(256).unwrap();
            drop(lease);
            assert!(other_budget.try_reserve(PeerId(99), 1024, false));
            other_budget.release(PeerId(99), 1024);
        }
        let now = driver.now();
        driver.shard.run(now, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Full);
        assert!(driver.ctl.dirty.lock().unwrap().is_empty(), "unrelated releases woke a share-bound relay");
        assert!(driver.shard.poll_event().is_none(), "unrelated releases emitted a false event");
        assert_eq!(driver.shard.budget().share_blocked(), blocked_before);
        assert_eq!(driver.shard.tx_queued(id), Some(room));

        // Recovery path 1: own ACKs freeing >= write_low_watermark (16 KiB;
        // two initial flights at 1448 B MSS) emit Event::Writable and wake the
        // relay while `sibling` still holds its share.
        driver.shard.egress_released(stream.meta.iface);
        for rtt in 0..2 {
            let now = driver.now() + std::time::Duration::from_millis(rtt * 100);
            let mut to_peer = Vec::new();
            driver.shard.run(now, &mut |_: IfaceId, pkt: &OutPacket<'_>| {
                if pkt.header.len() >= 24 {
                    let dst_port = u16::from_be_bytes([pkt.header[22], pkt.header[23]]);
                    if dst_port == 43100 {
                        to_peer.push(Bytes::from(pkt.to_vec()));
                    }
                }
                SendResult::Accepted
            });
            for pkt in to_peer {
                peer_side.shard.ingress(now, peer_side.iface, stream.sh.peer, pkt);
            }
            let ack_at = now + std::time::Duration::from_millis(50);
            let mut acks = Vec::new();
            peer_side.shard.run(ack_at, &mut |_: IfaceId, pkt: &OutPacket<'_>| {
                acks.push(Bytes::from(pkt.to_vec()));
                SendResult::Accepted
            });
            for ack in acks {
                driver.shard.ingress(ack_at, stream.meta.iface, stream.sh.peer, ack);
            }
        }
        driver.service();
        assert_eq!(driver.relays[&id].stats.downloaded, b"after own ack".len() as u64);

        // Fill `id`'s remaining 2-sender share again and queue more upstream bytes.
        let rest = driver.shard.send_space(id);
        assert!(rest > 0);
        assert_eq!(driver.shard.write(id, &vec![5u8; rest]), WriteResult::Written(rest));
        let queued_before_exit = driver.shard.tx_queued(id).unwrap();
        assert_eq!(driver.shard.send_space(id), 0);
        while driver.shard.poll_event().is_some() {}
        up.write_all(b"after share recovery").await.unwrap();
        driver.relays[&id].sock.readable().await.unwrap();
        driver.ctl.dirty.lock().unwrap().clear();
        driver.pump(id);
        let blocked_second = driver.shard.budget().share_blocked();
        assert_eq!(blocked_second, blocked_before + 1);

        // Recovery path 2: when the sibling sender exits (`sender_del`), the
        // shard's `share_epoch` advances and `service()` wakes the relay
        // without needing an own ACK.
        driver.shard.abort(sibling);
        driver.service();
        assert_eq!(driver.shard.tx_queued(id), Some(queued_before_exit + b"after share recovery".len()));
        assert_eq!(driver.shard.budget().share_blocked(), blocked_second);

        drop((stream, sibling_stream, driver, up));
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn quota_bound_relay_with_send_space_wakes_on_logical_release() {
        let (mut driver, stream, up, _peer) = parked_relay(&[]).await;
        let sh = &stream.sh;
        let global = sh.memory.global().clone();
        // Exhaust the port's logical byte ceiling while leaving `send_space(id) > 0`,
        // so `admit_write` passes the `send_space` check and fails at `try_reserve`.
        assert!(driver.shard.budget_mut_for_test().try_reserve(sh.peer, 1 << 20, false));
        assert!(driver.shard.send_space(sh.id) > 0);

        driver.pump(sh.id);
        assert_eq!(driver.shard.tx_queued(sh.id), Some(0));
        assert_eq!(sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_QUOTA);

        // Releasing the logical quota must wake `driver_waiter` and mark the stream dirty.
        driver.shard.budget_mut_for_test().release(sh.peer, 1 << 20);
        assert!(driver.ctl.dirty.lock().unwrap().remove(&sh.id), "logical quota waiter missed release");
        driver.pump(sh.id);
        assert_eq!(driver.shard.tx_queued(sh.id), Some(b"socket tail".len()));
        assert_eq!(sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_NONE);

        drop((stream, driver, up));
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn pending_polls_keep_the_latest_task_waker() {
        let (h, mut acc, iface, mut peer) = splice_stack(StackConfig::default(), 1500);
        peer.c.connect(peer.now(), peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        let mut stream = tokio::time::timeout(std::time::Duration::from_secs(5), async {
            loop {
                peer.step(&h, iface).await;
                if let Ok(stream) = acc.rx.try_recv() {
                    break stream;
                }
            }
        })
        .await
        .unwrap();
        let first = Arc::new(CountWake(AtomicUsize::new(0)));
        let latest = Arc::new(CountWake(AtomicUsize::new(0)));
        let wakers = [Waker::from(first.clone()), Waker::from(latest.clone())];
        // No await while polling: the driver cannot drain our queues between
        // registrations. Exercise read, full write, flush and shutdown slots.
        for operation in 0..4 {
            {
                let mut q = stream.sh.q.lock().unwrap();
                q.send_room = 0;
                if operation == 2 {
                    assert_eq!(q.tx.push(b"queued", &stream.sh.memory), 6);
                }
            }
            for waker in [&wakers[0], &wakers[0], &wakers[1], &wakers[1]] {
                let mut cx = Context::from_waker(waker);
                let mut data = [0u8; 1];
                let mut buf = ReadBuf::new(&mut data);
                let pending = match operation {
                    0 => Pin::new(&mut stream).poll_read(&mut cx, &mut buf).is_pending(),
                    1 => Pin::new(&mut stream).poll_write(&mut cx, b"x").is_pending(),
                    2 => Pin::new(&mut stream).poll_flush(&mut cx).is_pending(),
                    _ => Pin::new(&mut stream).poll_shutdown(&mut cx).is_pending(),
                };
                assert!(pending);
            }
            let registered = {
                let mut q = stream.sh.q.lock().unwrap();
                match operation {
                    0 => q.read_waker.take(),
                    1 => q.write_waker.take(),
                    _ => q.flush_waker.take(),
                }
                .unwrap()
            };
            registered.wake();
            assert_eq!(first.0.load(Ordering::Relaxed), 0);
            assert_eq!(latest.0.load(Ordering::Relaxed), operation + 1);
        }
    }

    #[test]
    fn short_connection_index_releases_sparse_capacity() {
        let mut streams = HashMap::new();
        for id in 0..8192 {
            streams.insert(id, id);
        }
        let peak = streams.capacity();
        for id in 0..8191 {
            streams.remove(&id);
        }
        compact_sparse_map(&mut streams);
        assert_eq!(streams.get(&8191), Some(&8191));
        assert!(streams.capacity() < peak / 4);
        streams.remove(&8191);
        compact_sparse_map(&mut streams);
        assert_eq!(streams.capacity(), 0);
    }

    #[test]
    fn tx_queue_capacity_and_waiter_release() {
        let one_queue = 128 + (8 * std::mem::size_of::<TxChunk>() * 2) as u64;
        let global = GlobalBudget::new(one_queue * 2);
        let mut budget = crate::budget::Budget::new(global.clone());
        budget.set_limits(one_queue * 2, one_queue * 2, 4);
        let memory = budget.memory_handle(PeerId(1));
        let mut first = TxQueue::default();
        let mut second = TxQueue::default();
        assert_eq!(first.push(&[1], &memory), 1);
        assert_eq!(second.push(&[2], &memory), 1);
        assert_eq!(global.reserved(), one_queue * 2);
        let observed = global.release_epoch();
        let mut blocked = TxQueue::default();
        assert_eq!(blocked.push(&[3], &memory), 0);
        let counter = Arc::new(CountWake(AtomicUsize::new(0)));
        let waker = Waker::from(counter.clone());
        let waiter_id = global.new_waiter_id();
        global.register_waiter(waiter_id, observed, &waker);
        first.consume(1);
        assert_eq!(counter.0.load(Ordering::Relaxed), 1);
        assert_eq!(blocked.push(&[3], &memory), 0, "resident descriptor capacity still owns its credit");
        let observed = global.release_epoch();
        global.register_waiter(waiter_id, observed, &waker);
        drop(first);
        assert_eq!(counter.0.load(Ordering::Relaxed), 2);
        assert_eq!(blocked.push(&[3], &memory), 1);
        global.remove_waiter(waiter_id);
        drop((second, blocked));
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn queue_descriptor_growth_keeps_bytes_and_releases_backing() {
        let global = GlobalBudget::new(2048);
        let mut budget = crate::budget::Budget::new(Arc::clone(&global));
        budget.set_limits(2048, 2048, 1);
        let memory = budget.memory_handle(PeerId(1));
        let mut queue = ChargedDeque::<Bytes>::default();
        let mut inserted = 0u8;
        while queue.reserve_one(&memory, AllocationKind::AdapterRx).is_ok() {
            queue.push_back_reserved(Bytes::from(vec![inserted]));
            inserted += 1;
            assert!(queue.capacity_bytes() <= global.reserved());
        }
        assert!(inserted >= 8);
        assert_eq!(queue.items.len(), inserted as usize, "failed growth discarded queued data");
        for expected in 0..inserted - 1 {
            assert_eq!(queue.pop_front().unwrap()[0], expected);
        }
        assert!(queue.capacity_bytes() <= global.reserved(), "one remaining item still pins deque backing");
        assert_eq!(queue.pop_front().unwrap()[0], inserted - 1);
        assert!(queue.is_empty());
        drop(queue);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn small_writes_stop_before_unfunded_tx_descriptor_growth() {
        let global = GlobalBudget::new(4096);
        let mut budget = crate::budget::Budget::new(Arc::clone(&global));
        budget.set_limits(4096, 4096, 1);
        let memory = budget.memory_handle(PeerId(1));
        let mut q = TxQueue::default();
        let mut accepted = 0u8;
        for i in 0..64u8 {
            let n = q.push(&[i; 64], &memory);
            if n == 0 {
                break;
            }
            assert_eq!(n, 64);
            accepted += 1;
            assert!(q.chunks.capacity_bytes() <= global.reserved());
        }
        assert!(accepted >= 8 && accepted < 64);
        assert!(matches!(q.last_failure, Some((QueueGrowError::Quota(_), _))));
        assert_eq!(q.len(), accepted as usize * 64);
        for i in 0..accepted {
            assert_eq!(q.front(), &[i; 64]);
            q.consume(64);
        }
        assert!(q.is_empty());
        drop(q);
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn tx_cache_complete_idle_is_reclaimed_by_real_driver_deadline() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let global = GlobalBudget::new(16 << 20);
        let packets = Arc::new(AtomicUsize::new(0));
        let sent = packets.clone();
        // Keep the empty source open. There are no ingress messages, connections,
        // stream/control notifications, snapshots or sampling tasks before reclaim.
        let (_input_tx, input_rx) = mpsc::channel::<()>(1);
        let high = global.high();
        let (handle, _acceptor, _, task, ()) = spawn_with_source_factory(
            StackConfig::default(),
            StreamConfig::default(),
            vec![IfaceConfig::default()],
            move |shard| {
                shard
                    .set_tx_cache_trim(Some(crate::TxCacheTrimPolicy {
                        check_interval: std::time::Duration::from_millis(20),
                        warm_after: std::time::Duration::from_millis(60),
                        cold_after: std::time::Duration::from_millis(140),
                        warm_large: 1,
                        warm_small: 0,
                    }))
                    .unwrap();
                shard.prime_tx_cache_for_test(4);
                let egress = move |_: IfaceId, _: PeerId, _: &OutPacket<'_>| {
                    sent.fetch_add(1, Ordering::Relaxed);
                    SendResult::Accepted
                };
                (egress, ())
            },
            global.clone(),
            ResourceLimits { port_bytes: high, peer_bytes: high, peer_max_connections: 8 },
            input_rx,
            |_, _, ()| {},
        );
        let cached_before = global.cached_bytes();
        let reserved_before = global.reserved();
        assert!(cached_before >= 4 * crate::buf::TX_BLOCK_CHARGE);
        let wall_start = std::time::Instant::now();
        // This is a real-time test: no pause(), advance() or direct Shard::run().
        // Reading an atomic ledger does not notify the driver. If next_deadline
        // omits maintenance, the driver stays pending and this timeout fails.
        tokio::time::timeout(std::time::Duration::from_secs(3), async {
            while global.cached_bytes() != 0 {
                tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            }
        })
        .await
        .expect("complete idle must reclaim without input or snapshot-driven service");
        assert!(wall_start.elapsed() >= std::time::Duration::from_millis(140));
        assert_eq!(global.reserved(), reserved_before - cached_before);
        assert_eq!(packets.load(Ordering::Relaxed), 0);
        // The first and only control request follows the independently observed release.
        let snap = handle.snapshot().await.unwrap();
        assert_eq!(snap.tx_cache.large_cached, 0);
        assert_eq!(snap.tx_cache.small_cached, 0);
        assert_eq!(snap.tx_cache.descriptor_bytes, 0);
        assert_eq!(snap.global_cached_bytes, 0);
        assert_eq!(snap.stats.tx_cache_idle_trims, 2);
        assert_eq!(snap.stats.tx_cache_idle_trimmed_blocks, 4);
        assert_eq!(snap.stats.tx_cache_idle_trimmed_bytes, cached_before);
        assert!(snap.stats.tx_cache_maintenance_checks >= 2);
        assert_eq!(snap.stats.cache_reclaims, 0);
        task.shutdown_and_join().await.unwrap();
        drop(handle);
        drop(_acceptor);
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn send_record_budget_release_notifies_driver() {
        let global = GlobalBudget::new(1024);
        let mut budget = crate::budget::Budget::new(global.clone());
        budget.set_limits(1024, 1024, 1);
        let lease = budget.memory_handle(PeerId(1)).try_allocate(512).unwrap();
        let ctl = Arc::new(Ctl { dirty: Mutex::new(HashSet::new()), pending: Mutex::new(PendingControl::default()), notify: Notify::new() });
        let waker = Waker::from(Arc::new(BudgetWake { ctl: ctl.clone() }));
        let waiter = global.new_waiter_id();
        global.register_waiter(waiter, global.release_epoch(), &waker);
        drop(lease);
        tokio::time::timeout(std::time::Duration::from_millis(100), ctl.notify.notified()).await.unwrap();
        global.remove_waiter(waiter);
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn echo_roundtrip_through_async_handles() {
        echo_roundtrip_with_tx_capacity(u64::MAX).await;
    }

    #[tokio::test(flavor = "current_thread")]
    async fn tx_capacity_async_writer_resumes_below_low_watermark() {
        echo_roundtrip_with_tx_capacity(4097).await;
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ordinary_writer_watermark_is_attainable_with_small_queue_or_share() {
        for (tx_cap, watermark, core_watermark, limit) in [
            (4096, 16 << 10, 16 << 10, 8 << 20),
            (256 << 10, 128 << 10, 16 << 10, 256 << 10),
            (256 << 10, 128 << 10, 128 << 10, 256 << 10),
            (4096, 0, 1, 8 << 20),
        ] {
            let global = GlobalBudget::new(16 << 20);
            let mut cfg = StackConfig::default();
            cfg.write_low_watermark = core_watermark;
            let mut shard = Shard::with_budget(cfg, global.clone());
            shard.set_budget_limits(limit, limit, 16);
            let (tx, rx) = mpsc::unbounded_channel();
            let egress = move |_: IfaceId, _: PeerId, pkt: &OutPacket<'_>| {
                tx.send(pkt.to_vec()).unwrap();
                SendResult::Accepted
            };
            let stream_cfg = StreamConfig { tx_cap, tx_low_watermark: watermark, ..StreamConfig::default() };
            let (h, mut acc, ids, join, stop) = spawn_inner(shard, stream_cfg, vec![IfaceConfig::default()], egress, None::<InputSource<(), NoInput>>);
            let driver = DriverTask { stop, ctl: h.ctl.clone(), join: Some(join) };
            let mut peer = TunPeer::new(1500, rx);
            let id = peer.c.connect(peer.now(), peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
            const TOTAL: usize = 512 << 10;
            let app = tokio::spawn(async move {
                let mut stream = acc.accept().await.unwrap();
                let data: Vec<_> = (0..TOTAL).map(|i| pattern_byte(i as u64)).collect();
                stream.write_all(&data).await.unwrap();
                stream.shutdown().await.unwrap();
            });
            let mut received = 0;
            let result = tokio::time::timeout(std::time::Duration::from_secs(3), async {
                let mut buf = [0; 8192];
                loop {
                    peer.step(&h, ids[0]).await;
                    loop {
                        match peer.c.read(peer.now(), id, &mut buf) {
                            ReadResult::Data(n) => {
                                for (i, &b) in buf[..n].iter().enumerate() {
                                    assert_eq!(b, pattern_byte((received + i) as u64));
                                }
                                received += n;
                            }
                            ReadResult::Eof => return,
                            ReadResult::WouldBlock => break,
                            other => panic!("unexpected read: {other:?}"),
                        }
                    }
                }
            })
            .await;
            app.abort();
            let _ = app.await;
            driver.shutdown_and_join().await.unwrap();
            drop((h, peer));
            assert!(result.is_ok(), "writer stalled: cap={tx_cap} watermark={watermark} core={core_watermark} limit={limit} received={received}");
            assert_eq!(received, TOTAL);
            assert_eq!(global.reserved(), 0);
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ordinary_writer_with_empty_queue_recovers_on_ack_sender_exit_or_close() {
        #[derive(Clone, Copy, Debug)]
        enum Recovery {
            Ack,
            SenderExit,
            Close,
        }
        for recovery in [Recovery::Ack, Recovery::SenderExit, Recovery::Close] {
            let mut cfg = StackConfig::default();
            cfg.min_prefetch = 1 << 20;
            cfg.prefetch_max = 1 << 20;
            cfg.write_low_watermark = 4096;
            let mut sim = crate::sim::Sim::new(43044, cfg, StackConfig::default(), Default::default(), Default::default());
            sim.a.shard.set_budget_limits(819_200, 819_200, 16);
            sim.a.shard.reserve_stream_state(STREAM_STATE_BYTES);
            let client_id = sim.connect(43100, Default::default());
            sim.connect(43101, Default::default());
            sim.run_until(sim.now + std::time::Duration::from_millis(100));
            let id = sim.a.accepted[0];
            let sibling = sim.a.accepted[1];
            assert_eq!(sim.a.shard.write(sibling, &[7; 16 << 10]), WriteResult::Written(16 << 10));
            assert_eq!(sim.a.shard.write(id, &[5; 16 << 10]), WriteResult::Written(16 << 10));
            let room = sim.a.shard.send_space(id);
            assert_eq!(sim.a.shard.write(id, &vec![5; room]), WriteResult::Written(room));
            // Sender activation can lower the share; fill the stable remainder.
            let rest = sim.a.shard.send_space(id);
            if rest > 0 {
                assert_eq!(sim.a.shard.write(id, &vec![5; rest]), WriteResult::Written(rest));
            }
            assert_eq!(sim.a.shard.send_space(id), 0);
            let mut packets = Vec::new();
            sim.a.shard.run(sim.now, &mut |_: IfaceId, pkt: &OutPacket<'_>| {
                if u16::from_be_bytes([pkt.header[22], pkt.header[23]]) == 43100 {
                    packets.push(Bytes::from(pkt.to_vec()));
                }
                SendResult::Accepted
            });
            for packet in packets {
                sim.b.shard.ingress(sim.now, sim.b.iface, PeerId(1), packet);
            }
            let mut acks = Vec::new();
            sim.b.shard.run(sim.now + std::time::Duration::from_millis(50), &mut |_: IfaceId, pkt: &OutPacket<'_>| {
                acks.push((PeerId(1), Bytes::from(pkt.to_vec())));
                SendResult::Accepted
            });
            assert!(!acks.is_empty());
            while sim.a.shard.poll_event().is_some() {}
            let iface = sim.a.iface;
            let global = sim.a.shard.budget().global().clone();
            let (cmd_tx, cmd_rx) = mpsc::channel(4);
            let (exit_tx, exit_rx) = mpsc::channel(1);
            let (accept_tx, mut accept_rx) = mpsc::channel(2);
            let ctl = Arc::new(Ctl { dirty: Mutex::new(HashSet::new()), pending: Mutex::new(PendingControl::default()), notify: Notify::new() });
            let stop = Arc::new(AtomicBool::new(false));
            let mut driver = Driver {
                shard: sim.a.shard,
                egress: (|_, _, _| SendResult::Accepted) as fn(IfaceId, PeerId, &OutPacket<'_>) -> SendResult,
                input: Some(InputSource { rx: exit_rx, handle: |shard: &mut Shard, _: crate::Instant, id: ConnId| shard.abort(id) }),
                cmd_rx,
                accept_tx,
                ctl: ctl.clone(),
                stop: stop.clone(),
                budget_waiter: global.new_waiter_id(),
                budget_waiting: false,
                budget_wake: Waker::from(Arc::new(BudgetWake { ctl: ctl.clone() })),
                streams: HashMap::new(),
                orphans: BTreeSet::new(),
                stream_cfg: StreamConfig::default(),
                epoch: tokio::time::Instant::now() - std::time::Duration::from_nanos(sim.now.as_nanos()),
                closes: CloseCounts::default(),
                aborts: AbortCounts::default(),
                relays: HashMap::new(),
            };
            driver.on_event(Event::Accepted(id));
            driver.on_event(Event::Accepted(sibling));
            let mut stream = accept_rx.try_recv().unwrap();
            let sibling_stream = accept_rx.try_recv().unwrap();
            let shared = stream.sh.clone();
            let h = StackHandle { tx: cmd_tx, ctl: ctl.clone() };
            let task = DriverTask { stop, ctl, join: Some(tokio::spawn(driver.run())) };
            let mut writer = tokio::spawn(async move {
                let result = stream.write_all(b"small request").await;
                if matches!(recovery, Recovery::Close) {
                    assert_eq!(result.unwrap_err().kind(), io::ErrorKind::BrokenPipe);
                } else {
                    result.unwrap();
                }
                stream
            });
            tokio::time::timeout(std::time::Duration::from_millis(100), async {
                while !shared.q.lock().unwrap().write_parked {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            assert!(shared.q.lock().unwrap().tx.is_empty());
            // Unrelated budget releases must not be needed to restore progress.
            for _ in 0..200 {
                drop(shared.memory.try_allocate(64).unwrap());
            }
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
            assert!(!writer.is_finished());
            assert_eq!(shared.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_NONE);
            match recovery {
                Recovery::SenderExit => exit_tx.send(sibling).await.unwrap(),
                Recovery::Close => {
                    // A local Shard::abort intentionally emits no Closed
                    // event. Use the real peer's RST to close the app stream.
                    sim.b.shard.abort(client_id);
                    let mut resets = Vec::new();
                    sim.b.shard.run(sim.now, &mut |_: IfaceId, pkt: &OutPacket<'_>| {
                        resets.push((PeerId(1), Bytes::from(pkt.to_vec())));
                        SendResult::Accepted
                    });
                    assert!(h.ingress(iface, resets).await);
                }
                Recovery::Ack => assert!(h.ingress(iface, acks).await),
            }
            // No snapshot command, manual pump, extra ACK, or polling of the
            // writer future can manufacture the wake being tested here.
            let result = tokio::time::timeout(std::time::Duration::from_millis(100), &mut writer).await;
            let completed = matches!(&result, Ok(Ok(_)));
            if result.is_err() {
                writer.abort();
                let _ = writer.await;
            }
            drop(result);
            task.shutdown_and_join().await.unwrap();
            drop((shared, sibling_stream, accept_rx, h, exit_tx, sim.b));
            assert!(completed, "missed ordinary writer wake: {recovery:?}");
            assert_eq!(global.reserved(), 0);
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ordinary_writer_recovers_from_the_only_memory_release() {
        for during_attempt in [false, true] {
            let global = GlobalBudget::new(16 << 20);
            let (tx, rx) = mpsc::unbounded_channel();
            let (_input_tx, input_rx) = mpsc::channel::<()>(1);
            let (h, mut acc, ids, task, memory) = spawn_with_source_factory(
                StackConfig::default(),
                StreamConfig::default(),
                vec![IfaceConfig::default()],
                move |shard| {
                    let memory = shard.memory_handle(PeerId(9));
                    let egress = move |_: IfaceId, _: PeerId, pkt: &OutPacket<'_>| {
                        tx.send(pkt.to_vec()).unwrap();
                        SendResult::Accepted
                    };
                    (egress, memory)
                },
                global.clone(),
                ResourceLimits { port_bytes: 1 << 20, peer_bytes: 1 << 20, peer_max_connections: 16 },
                input_rx,
                |_, _, ()| {},
            );
            let mut peer = TunPeer::new(1500, rx);
            peer.c.connect(peer.now(), peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
            let mut stream = tokio::time::timeout(std::time::Duration::from_secs(1), async {
                loop {
                    peer.step(&h, ids[0]).await;
                    if let Ok(stream) = acc.rx.try_recv() {
                        break stream;
                    }
                }
            })
            .await
            .unwrap();
            let shared = stream.sh.clone();
            let mut cache = memory.try_allocate_kind(65536, AllocationKind::TxBlock).unwrap();
            assert!(cache.park_cached());
            let ceiling = (1 << 20) - global.headroom(1 << 20, crate::budget::Tier::Block);
            let used = h.snapshot().await.unwrap().port_physical_bytes;
            let held = memory.try_allocate_kind(ceiling - used, AllocationKind::TxBlock).unwrap();
            let reclaim = Arc::new(ReleaseOnReclaim(Mutex::new(Some(held))));
            let holder = global.new_waiter_id();
            if during_attempt {
                global.register_cache_holder(holder, &Waker::from(reclaim.clone()));
            }
            let mut writer = tokio::spawn(async move {
                stream.write_all(&[7; 65536]).await.unwrap();
                stream
            });
            if !during_attempt {
                tokio::time::timeout(std::time::Duration::from_millis(100), async {
                    while !shared.memory_waiting.load(Ordering::Acquire) {
                        tokio::task::yield_now().await;
                    }
                })
                .await
                .unwrap();
                assert!(shared.q.lock().unwrap().tx.is_empty());
                drop(reclaim.0.lock().unwrap().take());
            }
            let result = tokio::time::timeout(std::time::Duration::from_millis(100), &mut writer).await;
            let completed = matches!(&result, Ok(Ok(_)));
            if result.is_err() {
                writer.abort();
                let _ = writer.await;
            }
            drop(result);
            assert!(!shared.memory_waiting.load(Ordering::Acquire));
            assert!(reclaim.0.lock().unwrap().is_none(), "release must occur in the tested interval");
            global.remove_cache_holder(holder);
            task.shutdown_and_join().await.unwrap();
            drop((shared, memory, cache, reclaim, h, acc, peer));
            assert!(completed, "only memory release missed: during_attempt={during_attempt}");
            assert_eq!(global.reserved(), 0);
        }
    }

    async fn echo_roundtrip_with_tx_capacity(cap: u64) {
        let (to_client_tx, mut to_client_rx) = mpsc::unbounded_channel::<Vec<u8>>();
        let egress = move |_i: IfaceId, _p: PeerId, pkt: &OutPacket<'_>| {
            let _ = to_client_tx.send(pkt.to_vec());
            SendResult::Accepted
        };
        // Force many capped rounds so the driver must ingest ACKs while it
        // still has egress work; otherwise the two directions deadlock.
        let mut server_cfg = StackConfig::default();
        server_cfg.round_bytes_cap = 1500;
        server_cfg.max_snd_buf = cap;
        let (h, mut acc, ids) = spawn(server_cfg, StreamConfig::default(), vec![IfaceConfig::default()], egress);
        let initial = h.snapshot().await.unwrap();
        assert_eq!(initial.active_connections, 0);
        assert_eq!(initial.adapter_streams, 0);
        // Server app: echo every accepted stream.
        tokio::spawn(async move {
            while let Some(s) = acc.accept().await {
                tokio::spawn(async move {
                    let (mut r, mut w) = tokio::io::split(s);
                    let _ = tokio::io::copy(&mut r, &mut w).await;
                    let _ = w.shutdown().await;
                });
            }
        });

        // Client: a test-peer shard driven by this task.
        let epoch = tokio::time::Instant::now();
        let now = || crate::Instant::from_nanos(epoch.elapsed().as_nanos() as u64 + 1);
        let mut c = Shard::with_budget(StackConfig::default(), crate::budget::GlobalBudget::new(1 << 30));
        let ci = c.add_iface(IfaceConfig::default());
        let id = c.connect(now(), ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        const TOTAL: u64 = 2 << 20;
        let (mut sent, mut recvd, mut eof, mut shut) = (0u64, 0u64, false, false);
        let mut buf = vec![0u8; 64 * 1024];
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(30);
        while !eof {
            assert!(tokio::time::Instant::now() < deadline, "timed out: sent {sent} recvd {recvd}");
            let mut out: Vec<(PeerId, Bytes)> = Vec::new();
            let mut sink = |_: IfaceId, p: &OutPacket<'_>| {
                out.push((PeerId(9), Bytes::from(p.to_vec())));
                SendResult::Accepted
            };
            c.run(now(), &mut sink);
            if !out.is_empty() {
                assert!(h.ingress(ids[0], out).await);
            }
            while c.poll_event().is_some() {}
            // Write the pattern, read the echo.
            while sent < TOTAL {
                let n = ((TOTAL - sent) as usize).min(buf.len());
                for (k, b) in buf[..n].iter_mut().enumerate() {
                    *b = pattern_byte(sent + k as u64);
                }
                match c.write(id, &buf[..n]) {
                    WriteResult::Written(w) => sent += w as u64,
                    _ => break,
                }
            }
            if sent == TOTAL && !shut {
                c.shutdown_write(id);
                shut = true;
            }
            loop {
                match c.read(now(), id, &mut buf) {
                    ReadResult::Data(n) => {
                        for (k, &b) in buf[..n].iter().enumerate() {
                            assert_eq!(b, pattern_byte(recvd + k as u64), "echo corrupted at {}", recvd + k as u64);
                        }
                        recvd += n as u64;
                    }
                    ReadResult::Eof => {
                        eof = true;
                        break;
                    }
                    ReadResult::WouldBlock => break,
                    ReadResult::Closed(r) => panic!("client closed: {r:?}"),
                }
            }
            // Wait for server packets or the client's next timer.
            let wait = c.next_deadline().map_or(std::time::Duration::from_millis(5), |d| {
                std::time::Duration::from_nanos(d.as_nanos().saturating_sub(now().as_nanos())).min(std::time::Duration::from_millis(5))
            });
            if let Ok(Some(p)) = tokio::time::timeout(wait, to_client_rx.recv()).await {
                c.ingress(now(), ci, PeerId(9), Bytes::from(p));
                while let Ok(p) = to_client_rx.try_recv() {
                    c.ingress(now(), ci, PeerId(9), Bytes::from(p));
                }
            }
        }
        assert_eq!(recvd, TOTAL);
    }

    /// The OS side of a TUN: a test-peer shard exchanging packets with a
    /// spawned stack.
    struct TunPeer {
        c: Shard,
        ci: IfaceId,
        epoch: tokio::time::Instant,
        rx: mpsc::UnboundedReceiver<Vec<u8>>,
    }

    impl TunPeer {
        fn new(mtu: u16, rx: mpsc::UnboundedReceiver<Vec<u8>>) -> Self {
            let mut c = Shard::with_budget(StackConfig::default(), GlobalBudget::new(1 << 30));
            let ci = c.add_iface(IfaceConfig { mtu });
            TunPeer { c, ci, epoch: tokio::time::Instant::now(), rx }
        }
        fn now(&self) -> crate::Instant {
            crate::Instant::from_nanos(self.epoch.elapsed().as_nanos() as u64 + 1)
        }
        /// Send what the peer has to the stack, then take the stack's replies
        /// (waiting up to 5 ms or the peer's next timer).
        async fn step(&mut self, h: &StackHandle, iface: IfaceId) {
            let now = self.now();
            let mut out: Vec<(PeerId, Bytes)> = Vec::new();
            let mut sink = |_: IfaceId, p: &OutPacket<'_>| {
                out.push((PeerId(9), Bytes::from(p.to_vec())));
                SendResult::Accepted
            };
            self.c.run(now, &mut sink);
            if !out.is_empty() {
                assert!(h.ingress(iface, out).await);
            }
            let wait = self.c.next_deadline().map_or(std::time::Duration::from_millis(5), |d| {
                std::time::Duration::from_nanos(d.as_nanos().saturating_sub(self.now().as_nanos())).min(std::time::Duration::from_millis(5))
            });
            if let Ok(Some(p)) = tokio::time::timeout(wait, self.rx.recv()).await {
                let (now, ci) = (self.now(), self.ci);
                self.c.ingress(now, ci, PeerId(9), Bytes::from(p));
                while let Ok(p) = self.rx.try_recv() {
                    self.c.ingress(now, ci, PeerId(9), Bytes::from(p));
                }
            }
        }
    }

    fn splice_stack(cfg: StackConfig, mtu: u16) -> (StackHandle, Acceptor, IfaceId, TunPeer) {
        splice_stack_with(cfg, mtu, 0).0
    }

    /// `tso > 0` turns on TX checksum and segmentation offload; the egress
    /// then completes the partial checksum like a device. The test peer takes
    /// a super-segment whole, as the Linux stack takes a GSO packet.
    fn splice_stack_with(cfg: StackConfig, mtu: u16, tso: u32) -> ((StackHandle, Acceptor, IfaceId, TunPeer), Arc<AtomicUsize>) {
        let (to_client_tx, to_client_rx) = mpsc::unbounded_channel::<Vec<u8>>();
        let super_segments = Arc::new(AtomicUsize::new(0));
        let counter = super_segments.clone();
        let egress = move |_i: IfaceId, _p: PeerId, pkt: &OutPacket<'_>| {
            if pkt.gso_size > 0 {
                counter.fetch_add(1, Ordering::Relaxed);
            }
            let mut v = pkt.to_vec();
            if pkt.csum_partial {
                let h = crate::offload::VirtioNetHdr::for_packet(pkt);
                let start = h.csum_start as usize;
                let c = crate::wire::checksum_fold(crate::wire::sum_bytes(0, &v[start..]));
                v[start + 16..start + 18].copy_from_slice(&c.to_be_bytes());
                assert!(pkt.gso_size == 0 || pkt.len() > pkt.header.len() + pkt.gso_size as usize);
            }
            let _ = to_client_tx.send(v);
            SendResult::Accepted
        };
        let (h, acc, ids) = spawn(cfg, StreamConfig::default(), vec![IfaceConfig { mtu }], egress);
        if tso > 0 {
            h.set_iface_tso(ids[0], tso);
        }
        ((h, acc, ids[0], TunPeer::new(mtu, to_client_rx)), super_segments)
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ordinary_stream_progresses_while_a_relay_is_active() {
        let up = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = up.local_addr().unwrap();
        let release = Arc::new(Notify::new());
        let upstream_release = release.clone();
        let upstream = tokio::spawn(async move {
            let (mut socket, _) = up.accept().await.unwrap();
            socket.write_all(b"relay").await.unwrap();
            upstream_release.notified().await;
            socket.shutdown().await.unwrap();
        });
        let (h, mut acc, iface, mut peer) = splice_stack(StackConfig::default(), 1500);
        let server = tokio::spawn(async move {
            let stream = acc.accept().await.unwrap();
            let relay = tokio::spawn(stream.splice(tokio::net::TcpStream::connect(addr).await.unwrap()));
            let mut ordinary = acc.accept().await.unwrap();
            ordinary.write_all(b"plain").await.unwrap();
            ordinary.shutdown().await.unwrap();
            relay.await.unwrap().unwrap()
        });
        let relay_id = peer.c.connect(peer.now(), peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        tokio::time::timeout(std::time::Duration::from_secs(10), async {
            // Receiving these bytes proves the relay map is populated before
            // the ordinary connection takes pump's missing-relay fallback.
            let mut received = Vec::new();
            let mut buf = [0; 16];
            while received.len() < 5 {
                peer.step(&h, iface).await;
                if let ReadResult::Data(n) = peer.c.read(peer.now(), relay_id, &mut buf) {
                    received.extend_from_slice(&buf[..n]);
                }
            }
            assert_eq!(received, b"relay");
            let ordinary_id = peer.c.connect(peer.now(), peer.ci, PeerId(9), "10.0.0.2:5556".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
            received.clear();
            loop {
                peer.step(&h, iface).await;
                match peer.c.read(peer.now(), ordinary_id, &mut buf) {
                    ReadResult::Data(n) => received.extend_from_slice(&buf[..n]),
                    ReadResult::Eof => break,
                    ReadResult::WouldBlock => {}
                    other => panic!("ordinary connection failed: {other:?}"),
                }
            }
            assert_eq!(received, b"plain");
            peer.c.shutdown_write(ordinary_id);
            peer.c.shutdown_write(relay_id);
            release.notify_one();
            while !server.is_finished() {
                peer.step(&h, iface).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(server.await.unwrap(), SpliceStats { uploaded: 0, downloaded: 5 });
        upstream.await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn splice_relays_both_directions_and_half_closes() {
        splice_roundtrips(vec![(StackConfig::default(), 1420u16, 0u32), (StackConfig::client(), 65535, 0), (StackConfig::client(), 1500, 65_000)]).await;
    }

    #[tokio::test(flavor = "current_thread")]
    async fn tx_capacity_splice_roundtrips_and_half_closes() {
        let cases = [(4097, 1500, 0), (65537, 65535, 0), (256 * 1024, 1500, 65_000)]
            .into_iter()
            .map(|(cap, mtu, tso)| (StackConfig { max_snd_buf: cap, ..StackConfig::client() }, mtu, tso))
            .collect();
        splice_roundtrips(cases).await;
    }

    async fn splice_roundtrips(cases: Vec<(StackConfig, u16, u32)>) {
        for (cfg, mtu, tso) in cases {
            // Upstream: echo until EOF, then close.
            let up = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let up_addr = up.local_addr().unwrap();
            tokio::spawn(async move {
                let (s, _) = up.accept().await.unwrap();
                let (mut r, mut w) = s.into_split();
                tokio::io::copy(&mut r, &mut w).await.unwrap();
                w.shutdown().await.unwrap();
            });
            let ((h, mut acc, iface, mut peer), super_segments) = splice_stack_with(cfg, mtu, tso);
            let (stats_tx, stats_rx) = oneshot::channel();
            tokio::spawn(async move {
                let mut s = acc.accept().await.unwrap();
                // Bytes the app writes before splicing go out first; bytes the
                // peer sends meanwhile wait in the handle and are relayed.
                s.write_all(b"hi").await.unwrap();
                tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                let upstream = tokio::net::TcpStream::connect(up_addr).await.unwrap();
                let _ = stats_tx.send(s.splice(upstream).await);
            });
            let now = peer.now();
            let id = peer.c.connect(now, peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
            const TOTAL: u64 = 3 << 20;
            let (mut sent, mut got, mut eof, mut shut) = (0u64, Vec::new(), false, false);
            let mut buf = vec![0u8; 64 * 1024];
            let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(30);
            while !eof {
                assert!(tokio::time::Instant::now() < deadline, "timed out: mtu {mtu} sent {sent} got {}", got.len());
                peer.step(&h, iface).await;
                while peer.c.poll_event().is_some() {}
                while sent < TOTAL {
                    let n = ((TOTAL - sent) as usize).min(buf.len());
                    for (k, b) in buf[..n].iter_mut().enumerate() {
                        *b = pattern_byte(sent + k as u64);
                    }
                    match peer.c.write(id, &buf[..n]) {
                        WriteResult::Written(w) => sent += w as u64,
                        _ => break,
                    }
                }
                if sent == TOTAL && !shut {
                    peer.c.shutdown_write(id);
                    shut = true;
                }
                loop {
                    let now = peer.now();
                    match peer.c.read(now, id, &mut buf) {
                        ReadResult::Data(n) => got.extend_from_slice(&buf[..n]),
                        ReadResult::Eof => {
                            eof = true;
                            break;
                        }
                        ReadResult::WouldBlock => break,
                        ReadResult::Closed(r) => panic!("peer closed: {r:?}"),
                    }
                }
            }
            assert_eq!(&got[..2], b"hi");
            assert_eq!(got.len() as u64, TOTAL + 2, "mtu {mtu}");
            for (k, &b) in got[2..].iter().enumerate() {
                assert_eq!(b, pattern_byte(k as u64), "echo corrupted at {k} (mtu {mtu})");
            }
            let stats = stats_rx.await.unwrap().unwrap();
            assert_eq!(stats, SpliceStats { uploaded: TOTAL, downloaded: TOTAL });
            assert_eq!(super_segments.load(Ordering::Relaxed) > 0, tso > 0, "mtu {mtu} tso {tso}");
            // The stream is released once the orderly close completes.
            let t = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
            loop {
                peer.step(&h, iface).await;
                let snap = h.snapshot().await.unwrap();
                if snap.adapter_streams == 0 {
                    break;
                }
                assert!(tokio::time::Instant::now() < t, "spliced stream not released");
            }
        }
    }

    #[tokio::test(flavor = "current_thread")]
    async fn splice_upstream_fin_first_then_peer_fin() {
        const RESP: usize = 700_000;
        let up = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let up_addr = up.local_addr().unwrap();
        let upstream_task = tokio::spawn(async move {
            let (s, _) = up.accept().await.unwrap();
            let (mut r, mut w) = s.into_split();
            let resp: Vec<u8> = (0..RESP as u64).map(pattern_byte).collect();
            w.write_all(&resp).await.unwrap();
            w.shutdown().await.unwrap();
            let mut all = Vec::new();
            tokio::io::AsyncReadExt::read_to_end(&mut r, &mut all).await.unwrap();
            all
        });
        let (h, mut acc, iface, mut peer) = splice_stack(StackConfig::client(), 9000);
        tokio::spawn(async move {
            let s = acc.accept().await.unwrap();
            let upstream = tokio::net::TcpStream::connect(up_addr).await.unwrap();
            s.splice(upstream).await.unwrap();
        });
        let now = peer.now();
        let id = peer.c.connect(now, peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        let (mut got, mut eof, mut sent) = (Vec::new(), false, false);
        let mut buf = vec![0u8; 64 * 1024];
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
        // Read the whole response (and its FIN) before sending anything.
        while !(eof && sent) {
            assert!(tokio::time::Instant::now() < deadline, "timed out: got {}", got.len());
            peer.step(&h, iface).await;
            while peer.c.poll_event().is_some() {}
            loop {
                let now = peer.now();
                match peer.c.read(now, id, &mut buf) {
                    ReadResult::Data(n) => got.extend_from_slice(&buf[..n]),
                    ReadResult::Eof => {
                        eof = true;
                        break;
                    }
                    ReadResult::WouldBlock => break,
                    ReadResult::Closed(r) => panic!("peer closed: {r:?}"),
                }
            }
            if eof && !sent {
                assert!(matches!(peer.c.write(id, b"bye"), WriteResult::Written(3)));
                peer.c.shutdown_write(id);
                sent = true;
            }
        }
        assert_eq!(got.len(), RESP);
        assert!(got.iter().enumerate().all(|(k, &b)| b == pattern_byte(k as u64)));
        // The peer's FIN reaches upstream as EOF after its last bytes.
        while !upstream_task.is_finished() {
            assert!(tokio::time::Instant::now() < deadline, "upstream never saw EOF");
            peer.step(&h, iface).await;
        }
        assert_eq!(upstream_task.await.unwrap(), b"bye");
    }

    #[tokio::test(flavor = "current_thread")]
    async fn splice_delivers_upload_tail_after_the_connection_closes() {
        // The peer uploads and FINs at once; upstream half-closes only later
        // and reads later still. Our FIN then completes the close (LAST_ACK →
        // Closed(Normal)) while the relay still holds bytes the socket has
        // not taken. None may be lost. Also with upstream FIN first.
        for (fin_after, read_after, up_len) in [(300u64, 600u64, 200usize << 10), (0, 400, 6 << 20)] {
            splice_upload_tail(fin_after, read_after, up_len).await;
        }
    }

    async fn splice_upload_tail(fin_after_ms: u64, read_after_ms: u64, up_len: usize) {
        // Small socket buffers on both ends of the upstream hop keep most of
        // the upload in the relay until upstream starts reading.
        let ls = tokio::net::TcpSocket::new_v4().unwrap();
        ls.set_recv_buffer_size(4096).unwrap();
        ls.bind("127.0.0.1:0".parse().unwrap()).unwrap();
        let up = ls.listen(16).unwrap();
        let up_addr = up.local_addr().unwrap();
        let upstream_task = tokio::spawn(async move {
            let (s, _) = up.accept().await.unwrap();
            let _ = s.set_nodelay(true);
            let (mut r, mut w) = s.into_split();
            tokio::time::sleep(std::time::Duration::from_millis(fin_after_ms)).await;
            w.shutdown().await.unwrap();
            tokio::time::sleep(std::time::Duration::from_millis(read_after_ms - fin_after_ms)).await;
            let mut all = Vec::new();
            tokio::io::AsyncReadExt::read_to_end(&mut r, &mut all).await.unwrap();
            all
        });
        let (h, mut acc, iface, mut peer) = splice_stack(StackConfig::client(), 65535);
        tokio::spawn(async move {
            let s = acc.accept().await.unwrap();
            let cs = tokio::net::TcpSocket::new_v4().unwrap();
            cs.set_send_buffer_size(4096).unwrap();
            let upstream = cs.connect(up_addr).await.unwrap();
            let _ = s.splice(upstream).await;
        });
        let now = peer.now();
        let id = peer.c.connect(now, peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        let mut buf = vec![0u8; 64 * 1024];
        let (mut sent, mut shut) = (0usize, false);
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
        while !upstream_task.is_finished() {
            assert!(tokio::time::Instant::now() < deadline, "upstream never got EOF: sent {sent}");
            peer.step(&h, iface).await;
            while peer.c.poll_event().is_some() {}
            let now = peer.now();
            while let ReadResult::Data(_) = peer.c.read(now, id, &mut buf) {}
            while sent < up_len {
                let n = (up_len - sent).min(buf.len());
                for (k, b) in buf[..n].iter_mut().enumerate() {
                    *b = pattern_byte((sent + k) as u64);
                }
                match peer.c.write(id, &buf[..n]) {
                    WriteResult::Written(w) => sent += w,
                    _ => break,
                }
            }
            if sent == up_len && !shut {
                peer.c.shutdown_write(id);
                shut = true;
            }
        }
        let got = upstream_task.await.unwrap();
        assert_eq!(got.len(), up_len, "fin after {fin_after_ms} ms");
        assert!(got.iter().enumerate().all(|(k, &b)| b == pattern_byte(k as u64)));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn splice_upstream_reset_resets_the_peer() {
        let up = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let up_addr = up.local_addr().unwrap();
        tokio::spawn(async move {
            let (mut s, _) = up.accept().await.unwrap();
            let mut b = [0u8; 4];
            tokio::io::AsyncReadExt::read_exact(&mut s, &mut b).await.unwrap();
            #[allow(deprecated)]
            s.set_linger(Some(std::time::Duration::ZERO)).unwrap();
            drop(s);
        });
        let (h, mut acc, iface, mut peer) = splice_stack(StackConfig::client(), 65535);
        let (res_tx, res_rx) = oneshot::channel();
        tokio::spawn(async move {
            let s = acc.accept().await.unwrap();
            let upstream = tokio::net::TcpStream::connect(up_addr).await.unwrap();
            let _ = res_tx.send(s.splice(upstream).await);
        });
        let now = peer.now();
        let id = peer.c.connect(now, peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        let mut wrote = false;
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(10);
        let reason = loop {
            assert!(tokio::time::Instant::now() < deadline, "peer was never reset");
            peer.step(&h, iface).await;
            let mut closed = None;
            while let Some(ev) = peer.c.poll_event() {
                if let Event::Closed(_, r) = ev {
                    closed = Some(r);
                }
            }
            if let Some(r) = closed {
                break r;
            }
            if !wrote && matches!(peer.c.write(id, b"ping"), WriteResult::Written(4)) {
                wrote = true;
            }
        };
        assert_eq!(reason, CloseReason::Reset);
        assert!(res_rx.await.unwrap().is_err());
    }

    #[tokio::test(flavor = "current_thread")]
    async fn dropped_stream_facing_zero_window_is_aborted() {
        // The server app writes more than fits and drops the stream; the client never
        // reads, so its window stays at zero and the adapter's tx queue never drains.
        // The orphan timeout must still end the connection (RST to the client).
        let (to_client_tx, mut to_client_rx) = mpsc::unbounded_channel::<Vec<u8>>();
        let egress = move |_i: IfaceId, _p: PeerId, pkt: &OutPacket<'_>| {
            let _ = to_client_tx.send(pkt.to_vec());
            SendResult::Accepted
        };
        let mut cfg = StackConfig::default();
        cfg.orphan_timeout = std::time::Duration::from_millis(500);
        let server_global = GlobalBudget::new(1 << 30);
        let (h, mut acc, ids, task) = spawn_with_budget(cfg, StreamConfig::default(), vec![IfaceConfig::default()], egress, server_global.clone());
        tokio::spawn(async move {
            let mut s = acc.accept().await.unwrap();
            let data = vec![7u8; 32 << 20];
            // More than the shard's send buffer takes, so the adapter's queue backs up.
            let r = tokio::time::timeout(std::time::Duration::from_millis(300), s.write_all(&data)).await;
            assert!(r.is_err(), "write should still be blocked by the zero window");
            drop(s);
        });

        let epoch = tokio::time::Instant::now();
        let now = || crate::Instant::from_nanos(epoch.elapsed().as_nanos() as u64 + 1);
        let mut c = Shard::with_budget(StackConfig::default(), crate::budget::GlobalBudget::new(1 << 30));
        let ci = c.add_iface(IfaceConfig::default());
        let id = c.connect(now(), ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
        let mut max_rx = 0;
        let reason = loop {
            assert!(tokio::time::Instant::now() < deadline, "orphan never torn down");
            max_rx = max_rx.max(c.info(id).map_or(0, |i| i.rx_queued));
            let mut out: Vec<(PeerId, Bytes)> = Vec::new();
            let mut sink = |_: IfaceId, p: &OutPacket<'_>| {
                out.push((PeerId(9), Bytes::from(p.to_vec())));
                SendResult::Accepted
            };
            c.run(now(), &mut sink);
            if !out.is_empty() {
                assert!(h.ingress(ids[0], out).await);
            }
            let mut closed = None;
            while let Some(ev) = c.poll_event() {
                if let Event::Closed(i, r) = ev {
                    if i == id {
                        closed = Some(r);
                    }
                }
            }
            if let Some(r) = closed {
                break r;
            }
            let wait = c.next_deadline().map_or(std::time::Duration::from_millis(20), |d| {
                std::time::Duration::from_nanos(d.as_nanos().saturating_sub(now().as_nanos())).min(std::time::Duration::from_millis(20))
            });
            if let Ok(Some(p)) = tokio::time::timeout(wait, to_client_rx.recv()).await {
                c.ingress(now(), ci, PeerId(9), Bytes::from(p));
                while let Ok(p) = to_client_rx.try_recv() {
                    c.ingress(now(), ci, PeerId(9), Bytes::from(p));
                }
            }
        };
        assert_eq!(reason, CloseReason::Reset);
        assert!(max_rx >= 64 * 1024, "client window never filled ({max_rx})");
        task.shutdown_and_join().await.unwrap();
        assert_eq!(server_global.connection_counts(), (0, 0));
        assert_eq!(server_global.cached_bytes(), 0);
        assert_eq!(server_global.reserved(), 0, "adapter/core backing survived cancellation");
    }

    // #664 (zfc): adapter queues, buffered core data, idle TX cache and host
    // egress packets share global/port/peer budgets; none may stop progress.

    type Ingress = (IfaceId, Vec<(PeerId, Bytes)>);
    type ToClient = (PeerId, Vec<u8>, Option<MemoryLease>);

    struct EgressWake(std::sync::OnceLock<(StackHandle, IfaceId)>);

    impl Wake for EgressWake {
        fn wake(self: Arc<Self>) {
            self.wake_by_ref();
        }
        fn wake_by_ref(self: &Arc<Self>) {
            if let Some((handle, iface)) = self.0.get() {
                handle.egress_released(*iface);
            }
        }
    }

    /// Host egress in the zfc shape: each queued packet keeps a lease on the
    /// port/peer share until the WG side has consumed it.
    struct ChargedEgress {
        to_client: mpsc::UnboundedSender<ToClient>,
        memory: HashMap<PeerId, MemoryHandle>,
        global: Arc<GlobalBudget>,
        waiter: u64,
        waker: Waker,
    }

    impl Egress for ChargedEgress {
        fn send(&mut self, _iface: IfaceId, peer: PeerId, pkt: &OutPacket<'_>) -> SendResult {
            let lease = match self.memory.get(&peer) {
                None => None,
                Some(memory) => {
                    let bytes = pkt.len().max(2048) as u64;
                    let observed = self.global.release_epoch();
                    match memory.try_allocate_kind(bytes, AllocationKind::Egress) {
                        Some(lease) => Some(lease),
                        None => {
                            self.global.register_physical_waiter(self.waiter, observed, &self.waker, memory, bytes, AllocationKind::Egress);
                            return SendResult::Full;
                        }
                    }
                }
            };
            self.global.remove_waiter(self.waiter);
            let _ = self.to_client.send((peer, pkt.to_vec(), lease));
            SendResult::Accepted
        }
    }

    #[derive(Clone, Copy)]
    struct Profile {
        port_bytes: u64,
        peer_bytes: u64,
        peers: u64,
        charge_egress: bool,
        per_conn: u64,
    }

    impl Profile {
        /// zfc repro: 10% of an 8 MiB global budget, one peer.
        fn zfc_repro(charge_egress: bool, per_conn: u64) -> Self {
            Profile { port_bytes: 819_200, peer_bytes: 819_200, peers: 1, charge_egress, per_conn }
        }
    }

    struct LimitedServer {
        handle: StackHandle,
        iface: IfaceId,
        ingress: mpsc::Sender<Ingress>,
        to_client: mpsc::UnboundedReceiver<ToClient>,
        task: DriverTask,
        global: Arc<GlobalBudget>,
        profile: Profile,
        /// Peer 9's handle on the port share, for tests that hold part of it.
        memory: MemoryHandle,
        refilling: Arc<std::sync::atomic::AtomicBool>,
        refill_rounds: Arc<std::sync::atomic::AtomicU64>,
        stop_refilling: Arc<std::sync::atomic::AtomicBool>,
        load_ready: Arc<tokio::sync::Notify>,
        ack_delay: std::time::Duration,
    }

    /// Accepted streams each send `per_conn` bytes over 64 KiB stream queues.
    fn limited_server(global: Arc<GlobalBudget>, profile: Profile) -> LimitedServer {
        limited_server_with_forced_refusal(global, profile, None)
    }

    /// Test-only pressure gate: fill the quota in the same owner turn that
    /// processes the selected SYN, then require that SYN to create debt.
    fn limited_server_with_forced_refusal(global: Arc<GlobalBudget>, profile: Profile, refuse_port: Option<u16>) -> LimitedServer {
        limited_server_mode(global, profile, refuse_port, None)
    }

    fn limited_server_mode(global: Arc<GlobalBudget>, profile: Profile, refuse_port: Option<u16>, upstream: Option<SocketAddr>) -> LimitedServer {
        let mut refusal_observed = false;
        let refilling = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let ingress_refilling = refilling.clone();
        let refill_rounds = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let ingress_refill_rounds = refill_rounds.clone();
        global.ensure_egress_reserve(2 * 2048);
        let limits =
            ResourceLimits { port_bytes: profile.port_bytes, peer_bytes: profile.peer_bytes, peer_max_connections: if upstream.is_some() { 512 } else { 128 } };
        let stream_cfg = StreamConfig { rx_cap: 64 * 1024, tx_cap: 64 * 1024, tx_low_watermark: 16 * 1024 };
        let (to_client_tx, to_client) = mpsc::unbounded_channel();
        let (ingress, ingress_rx) = mpsc::channel::<Ingress>(1024);
        let wake = Arc::new(EgressWake(std::sync::OnceLock::new()));
        let egress_global = global.clone();
        let egress_wake = wake.clone();
        let (handle, mut acc, ids, task, memory) = spawn_with_source_factory(
            StackConfig::default(),
            stream_cfg,
            vec![IfaceConfig::default()],
            move |shard: &mut Shard| {
                let memory = if profile.charge_egress {
                    (0..profile.peers).map(|k| (PeerId(9 + k), shard.memory_handle(PeerId(9 + k)))).collect()
                } else {
                    HashMap::new()
                };
                let egress = ChargedEgress {
                    to_client: to_client_tx,
                    memory,
                    waiter: egress_global.new_waiter_id(),
                    global: egress_global,
                    waker: Waker::from(egress_wake),
                };
                (egress, shard.memory_handle(PeerId(9)))
            },
            global.clone(),
            limits,
            ingress_rx,
            move |shard: &mut Shard, now: crate::Instant, (iface, pkts): Ingress| {
                for (peer, p) in pkts {
                    let gate = !refusal_observed
                        && refuse_port.is_some_and(|port| {
                            crate::wire::parse_ip(&p)
                                .ok()
                                .and_then(|ip| crate::wire::parse_tcp(&p, &ip).ok())
                                .is_some_and(|tcp| tcp.src_port == port && tcp.has(crate::wire::SYN) && !tcp.has(crate::wire::ACK))
                        });
                    if gate {
                        let memory = shard.memory_handle(peer);
                        let mut held = Vec::new();
                        for quantum in [crate::buf::TX_BLOCK_CHARGE, 256] {
                            while let Some(lease) = memory.try_allocate_kind(quantum, AllocationKind::TxBlock) {
                                held.push(lease);
                            }
                        }
                        let before = shard.stats().admission_debts;
                        let conns = shard.conn_count();
                        // No driver/ACK task can interleave between filling the
                        // quota and this SYN's admission attempt.
                        shard.ingress(now, iface, peer, p);
                        let after = shard.stats().admission_debts;
                        let debt = shard.budget().admission_debt();
                        assert_eq!(after, before + 1, "selected late SYN did not create a new debt");
                        assert!(debt.iter().any(|&n| n > 0));
                        assert_eq!(shard.conn_count(), conns, "refused SYN was admitted");
                        refusal_observed = true;
                        drop(held);
                        // Start competing refills at the observed refusal,
                        // not before the initial six handshakes complete.
                        ingress_refilling.store(true, std::sync::atomic::Ordering::SeqCst);
                        let active = ingress_refilling.clone();
                        let rounds = ingress_refill_rounds.clone();
                        tokio::spawn(async move {
                            spawn_refilling_senders_counted(memory, 3500, Some(rounds)).await.unwrap();
                            active.store(false, std::sync::atomic::Ordering::SeqCst);
                        });
                    } else {
                        shard.ingress(now, iface, peer, p);
                    }
                }
            },
        );
        let _ = wake.0.set((handle.clone(), ids[0]));
        let per_conn = profile.per_conn;
        tokio::spawn(async move {
            while let Some(mut s) = acc.accept().await {
                tokio::spawn(async move {
                    if let Some(addr) = upstream {
                        let socket = tokio::net::TcpStream::connect(addr).await.unwrap();
                        let _ = s.splice(socket).await;
                        return;
                    }
                    let mut buf = vec![0u8; 16 * 1024];
                    let mut off = 0u64;
                    while off < per_conn {
                        let n = ((per_conn - off) as usize).min(buf.len());
                        for (k, b) in buf[..n].iter_mut().enumerate() {
                            *b = pattern_byte(off + k as u64);
                        }
                        if s.write_all(&buf[..n]).await.is_err() {
                            return;
                        }
                        off += n as u64;
                    }
                    let _ = s.shutdown().await;
                });
            }
        });
        LimitedServer {
            handle,
            iface: ids[0],
            ingress,
            to_client,
            task,
            global,
            profile,
            memory,
            refilling,
            refill_rounds,
            stop_refilling: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            load_ready: Arc::new(tokio::sync::Notify::new()),
            ack_delay: std::time::Duration::ZERO,
        }
    }

    /// Open `conns` client connections, spread over the profile's peers, and
    /// read `per_conn` bytes from each. Samples the physical bounds meanwhile.
    async fn download(server: &mut LimitedServer, first_port: u16, conns: u16, limit: std::time::Duration) -> Result<(), String> {
        download_with(server, first_port, conns, limit, Load::default()).await.map(|_| ())
    }

    #[derive(Default, Clone, Copy)]
    struct Load {
        /// Bytes per second each load stream reads; 0 reads everything.
        read_rate: u64,
        /// Open one more connection once every load stream has read this many
        /// bytes, and read it unthrottled.
        late_after: Option<u64>,
        /// Open the late connection only once the port holds more than this
        /// many physical bytes.
        late_above: u64,
        /// Start ordinary refills after the established-load threshold, and
        /// require them to be running before the late pressure trigger.
        require_refiller: bool,
    }

    #[derive(Debug, Default)]
    struct LoadReport {
        /// Time from the late connect to its handshake, and how many load
        /// streams were still running then.
        late_connected: Option<(std::time::Duration, usize)>,
        /// Load streams still running when the late connection finished.
        late_done_while_loading: Option<usize>,
        /// Bytes each load stream read between the late connect and its
        /// handshake, while the late connection was being refused.
        load_progress: Vec<u64>,
        refills_active_at_late_handshake: bool,
        refill_rounds_at_late_handshake: u64,
        late_trigger_physical_bytes: u64,
        loading_at_late_trigger: usize,
        refills_active_at_late_trigger: bool,
        refill_rounds_at_late_trigger: u64,
        /// A fixed progress window also applies when the late SYN succeeds
        /// immediately, leaving no meaningful refusal interval to measure.
        progress_after_one_second: Option<Vec<u64>>,
        refills_active_after_one_second: bool,
        refill_rounds_after_one_second: u64,
    }

    async fn download_with(server: &mut LimitedServer, first_port: u16, conns: u16, limit: std::time::Duration, load: Load) -> Result<LoadReport, String> {
        let per_conn = server.profile.per_conn;
        let peers = server.profile.peers;
        let epoch = tokio::time::Instant::now();
        let now = || crate::Instant::from_nanos(epoch.elapsed().as_nanos() as u64 + 1);
        let mut c = Shard::with_budget(StackConfig::default(), GlobalBudget::new(1 << 30));
        let ci = c.add_iface(IfaceConfig::default());
        let ids: Vec<ConnId> = (0..conns)
            .map(|k| {
                let peer = PeerId(9 + u64::from(k) % peers);
                c.connect(now(), ci, peer, SocketAddr::from(([10, 0, 0, 2], first_port + k)), "10.0.0.1:80".parse().unwrap())
            })
            .collect();
        let mut ids = ids;
        let load_n = ids.len();
        let mut recvd = vec![0u64; ids.len()];
        let mut eof = vec![false; ids.len()];
        let mut buf = vec![0u8; 64 * 1024];
        let deadline = tokio::time::Instant::now() + limit;
        let mut rounds = 0u64;
        let mut report = LoadReport::default();
        let mut late_started: Option<tokio::time::Instant> = None;
        let mut late_start_bytes = Vec::new();
        let mut load_ready_signaled = false;
        loop {
            assert!(server.global.reserved() <= server.global.high(), "global budget exceeded");
            rounds += 1;
            if rounds.is_multiple_of(64) {
                if let Some(snap) = server.handle.snapshot().await {
                    assert!(snap.port_physical_bytes <= server.profile.port_bytes, "port share exceeded");
                }
            }
            if tokio::time::Instant::now() > deadline {
                let snap = server.handle.snapshot().await;
                return Err(format!(
                    "stalled: recvd={recvd:?} cached={} reserved={} snapshot={:?}",
                    server.global.cached_bytes(),
                    server.global.reserved(),
                    snap.map(|s| (
                        s.port_physical_bytes,
                        s.failures_by_kind,
                        s.failures_by_level,
                        s.waiters,
                        s.record_waiters,
                        s.stats.sink_full,
                        s.active_connections
                    ))
                ));
            }
            let mut out = Vec::new();
            let mut sink = |_: IfaceId, p: &OutPacket<'_>| {
                out.push((p.peer, Bytes::from(p.to_vec())));
                SendResult::Accepted
            };
            c.run(now(), &mut sink);
            if !out.is_empty() && server.ingress.send((server.iface, out)).await.is_err() {
                return Err("server ingress closed".into());
            }
            let loading = |eof: &[bool]| eof[..load_n].iter().filter(|&&e| !e).count();
            while let Some(ev) = c.poll_event() {
                match ev {
                    Event::Closed(id, reason) => {
                        if let Some(k) = ids.iter().position(|&x| x == id) {
                            if reason != CloseReason::Normal && !eof[k] {
                                return Err(format!("conn {k} closed with {reason:?} after {} bytes", recvd[k]));
                            }
                        }
                    }
                    Event::Connected(id) if ids.len() > load_n && id == ids[load_n] => {
                        report.late_connected = Some((late_started.unwrap().elapsed(), loading(&eof)));
                        report.refills_active_at_late_handshake = server.refilling.load(std::sync::atomic::Ordering::SeqCst);
                        report.refill_rounds_at_late_handshake = server.refill_rounds.load(std::sync::atomic::Ordering::SeqCst);
                        for (k, p) in report.load_progress.iter_mut().enumerate() {
                            *p = recvd[k] - *p;
                        }
                    }
                    _ => {}
                }
            }
            if let Some(after) = load.late_after {
                let ready = recvd[..load_n].iter().all(|&n| n >= after);
                if ready && !load_ready_signaled {
                    server.load_ready.notify_one();
                    load_ready_signaled = true;
                }
                let refiller_ready = !load.require_refiller
                    || (server.refilling.load(std::sync::atomic::Ordering::SeqCst) && server.refill_rounds.load(std::sync::atomic::Ordering::SeqCst) >= 2);
                let physical = if load.late_above > 0 { server.handle.snapshot().await.map_or(0, |s| s.port_physical_bytes) } else { 0 };
                let full = load.late_above == 0 || physical > load.late_above;
                if late_started.is_none() && ready && refiller_ready && full {
                    report.late_trigger_physical_bytes = physical;
                    report.loading_at_late_trigger = loading(&eof);
                    report.refills_active_at_late_trigger = server.refilling.load(std::sync::atomic::Ordering::SeqCst);
                    report.refill_rounds_at_late_trigger = server.refill_rounds.load(std::sync::atomic::Ordering::SeqCst);
                    let port = first_port + conns;
                    ids.push(c.connect(now(), ci, PeerId(9), SocketAddr::from(([10, 0, 0, 2], port)), "10.0.0.1:80".parse().unwrap()));
                    recvd.push(0);
                    eof.push(false);
                    late_started = Some(tokio::time::Instant::now());
                    late_start_bytes = recvd[..load_n].to_vec();
                    report.load_progress = late_start_bytes.clone();
                }
            }
            for (k, &id) in ids.iter().enumerate() {
                let mut budget = if k < load_n && load.read_rate > 0 {
                    let allowed = (load.read_rate as u128 * epoch.elapsed().as_nanos() / 1_000_000_000) as u64;
                    allowed.saturating_sub(recvd[k]) as usize
                } else {
                    usize::MAX
                };
                while !eof[k] && budget > 0 {
                    let want = buf.len().min(budget);
                    match c.read(now(), id, &mut buf[..want]) {
                        ReadResult::Data(n) => {
                            for (j, &b) in buf[..n].iter().enumerate() {
                                assert_eq!(b, pattern_byte(recvd[k] + j as u64), "conn {k} corrupted");
                            }
                            recvd[k] += n as u64;
                            budget -= n.min(budget);
                        }
                        ReadResult::Eof => {
                            eof[k] = true;
                            if k == load_n {
                                report.late_done_while_loading = Some(loading(&eof));
                            }
                        }
                        ReadResult::WouldBlock => break,
                        ReadResult::Closed(r) => return Err(format!("conn {k} closed with {r:?} after {} bytes", recvd[k])),
                    }
                }
            }
            if report.progress_after_one_second.is_none() && late_started.is_some_and(|at| at.elapsed() >= std::time::Duration::from_secs(1)) {
                report.progress_after_one_second = Some(recvd[..load_n].iter().zip(&late_start_bytes).map(|(n, start)| n - start).collect());
                report.refills_active_after_one_second = server.refilling.load(std::sync::atomic::Ordering::SeqCst);
                report.refill_rounds_after_one_second = server.refill_rounds.load(std::sync::atomic::Ordering::SeqCst);
            }
            if report.progress_after_one_second.is_some() && report.late_connected.is_some() {
                server.stop_refilling.store(true, std::sync::atomic::Ordering::SeqCst);
            }
            if eof.iter().all(|&e| e) && (load.late_after.is_none() || late_started.is_some()) {
                assert!(recvd.iter().all(|&n| n == per_conn), "short stream: {recvd:?}");
                return Ok(report);
            }
            let wait = c.next_deadline().map_or(std::time::Duration::from_millis(5), |d| {
                std::time::Duration::from_nanos(d.as_nanos().saturating_sub(now().as_nanos())).min(std::time::Duration::from_millis(5))
            });
            // The lease of each packet is dropped once the client ingested it.
            if let Ok(Some((peer, p, _lease))) = tokio::time::timeout(wait, server.to_client.recv()).await {
                c.ingress(now(), ci, peer, Bytes::from(p));
                while let Ok((peer, p, _lease)) = server.to_client.try_recv() {
                    c.ingress(now(), ci, peer, Bytes::from(p));
                }
            }
        }
    }

    /// Client whose receive window closes while the app is paused (#674).
    fn pausable_client() -> Shard {
        let mut cfg = StackConfig::default();
        cfg.max_rcv_buf = 64 * 1024;
        cfg.init_rcv_wnd = 64 * 1024;
        Shard::with_budget(cfg, GlobalBudget::new(1 << 30))
    }

    /// Download with per-connection read pauses and one optional late
    /// connection. Returns per-connection 250 ms progress samples and finish
    /// times. `pauses`: (conn index, start, end) from the download start.
    async fn download_pauses(
        server: &mut LimitedServer,
        first_port: u16,
        conns: u16,
        read_rate: u64,
        pauses: &[(u16, std::time::Duration, std::time::Duration)],
        late_at: Option<std::time::Duration>,
        limit: std::time::Duration,
    ) -> Result<(Vec<Vec<u64>>, Vec<Option<std::time::Duration>>), String> {
        let per_conn = server.profile.per_conn;
        let peers = server.profile.peers;
        let epoch = tokio::time::Instant::now();
        let now = || crate::Instant::from_nanos(epoch.elapsed().as_nanos() as u64 + 1);
        let mut c = pausable_client();
        let ci = c.add_iface(IfaceConfig::default());
        let total = conns + u16::from(late_at.is_some());
        let mut ids: Vec<ConnId> = (0..conns)
            .map(|k| {
                let peer = PeerId(9 + u64::from(k) % peers);
                c.connect(now(), ci, peer, SocketAddr::from(([10, 0, 0, 2], first_port + k)), "10.0.0.1:80".parse().unwrap())
            })
            .collect();
        let mut recvd = vec![0u64; total as usize];
        let mut eof = vec![false; total as usize];
        let mut finished: Vec<Option<std::time::Duration>> = vec![None; total as usize];
        let mut samples: Vec<Vec<u64>> = (0..total).map(|_| vec![0]).collect();
        /// Sample index at which each connection was opened (the late
        /// connection's earlier samples are placeholders, not stalls).
        let mut conn_since: Vec<usize> = vec![0; total as usize];
        let mut next_sample = std::time::Duration::from_millis(250);
        let mut buf = vec![0u8; 64 * 1024];
        let deadline = tokio::time::Instant::now() + limit;
        let mut rounds = 0u64;
        let mut delayed = VecDeque::new();
        loop {
            assert!(server.global.reserved() <= server.global.high(), "global budget exceeded");
            rounds += 1;
            if rounds.is_multiple_of(64) {
                if let Some(snap) = server.handle.snapshot().await {
                    assert!(snap.port_physical_bytes <= server.profile.port_bytes, "port share exceeded");
                }
            }
            let elapsed = epoch.elapsed();
            if tokio::time::Instant::now() > deadline {
                return Err(format!("stalled: recvd={recvd:?}"));
            }
            while elapsed >= next_sample {
                for (k, s) in samples.iter_mut().enumerate() {
                    s.push(recvd[k]);
                    // Diagnostic: a non-paused, already-connected stream flat
                    // for >= 8 samples (2 s) violates the design's progress
                    // bound; dump the stack state right then. A flat run that
                    // overlaps a pause window (including its final instant,
                    // before the resumed client has read) is expected.
                    let flat_from = elapsed.saturating_sub(std::time::Duration::from_millis(250 * 7));
                    let overlaps_pause = pauses.iter().any(|&(conn, _start, end)| conn as usize == k && flat_from < end);
                    let live = s.len() - conn_since[k];
                    if !overlaps_pause && k < ids.len() && recvd[k] > 0 && !eof[k] && live >= 8 && s[s.len() - 8..].iter().all(|&v| v == s[s.len() - 1]) {
                        let snap = server.handle.snapshot().await;
                        let tail = snap.as_ref().and_then(|sn| {
                            sn.tail_connections.iter().find(|t| ids.get(k) == Some(&t.id)).map(|t| {
                                format!(
                                    "state={:?} cwnd={} snd_wnd={} txq={} unsent={} pipe={} pace={:?} atxq={} room={} parked={} want_write={}",
                                    t.core.state,
                                    t.core.cwnd,
                                    t.core.snd_wnd,
                                    t.core.tx_queued,
                                    t.core.tx_unsent,
                                    t.core.pipe,
                                    t.core.pacing_rate,
                                    t.adapter_tx_queued,
                                    t.send_room,
                                    t.write_parked,
                                    t.core.want_write
                                )
                            })
                        });
                        panic!("conn {k} flat at {} around {elapsed:?}: {tail:?}", s[s.len() - 1]);
                    }
                }
                next_sample += std::time::Duration::from_millis(250);
            }
            if late_at.is_some_and(|at| elapsed >= at) && ids.len() < total as usize {
                let k = (total - 1) as usize;
                let peer = PeerId(9 + k as u64 % peers);
                let port = first_port + total - 1;
                conn_since[k] = samples[k].len();
                ids.push(c.connect(now(), ci, peer, SocketAddr::from(([10, 0, 0, 2], port)), "10.0.0.1:80".parse().unwrap()));
            }
            let mut out = Vec::new();
            let mut sink = |_: IfaceId, p: &OutPacket<'_>| {
                out.push((p.peer, Bytes::from(p.to_vec())));
                SendResult::Accepted
            };
            c.run(now(), &mut sink);
            if !out.is_empty() {
                delayed.push_back((tokio::time::Instant::now() + server.ack_delay, out));
            }
            while delayed.front().is_some_and(|(at, _)| *at <= tokio::time::Instant::now()) {
                let (_, out) = delayed.pop_front().unwrap();
                if server.ingress.send((server.iface, out)).await.is_err() {
                    return Err("server ingress closed".into());
                }
            }
            while let Some(ev) = c.poll_event() {
                if let Event::Closed(id, reason) = ev {
                    if let Some(k) = ids.iter().position(|&x| x == id) {
                        if reason != CloseReason::Normal && !eof[k] {
                            return Err(format!("conn {k} closed with {reason:?} after {} bytes", recvd[k]));
                        }
                    }
                }
            }
            for (k, &id) in ids.iter().enumerate() {
                let paused = pauses.iter().any(|&(conn, start, end)| conn as usize == k && elapsed >= start && elapsed < end);
                if paused || eof[k] {
                    continue;
                }
                let mut budget = if read_rate > 0 {
                    let allowed = (read_rate as u128 * elapsed.as_nanos() / 1_000_000_000) as u64;
                    allowed.saturating_sub(recvd[k]) as usize
                } else {
                    usize::MAX
                };
                while !eof[k] && budget > 0 {
                    let want = buf.len().min(budget);
                    match c.read(now(), id, &mut buf[..want]) {
                        ReadResult::Data(n) => {
                            for (j, &b) in buf[..n].iter().enumerate() {
                                assert_eq!(b, pattern_byte(recvd[k] + j as u64), "conn {k} corrupted");
                            }
                            recvd[k] += n as u64;
                            budget -= n.min(budget);
                        }
                        ReadResult::Eof => {
                            eof[k] = true;
                            finished[k] = Some(elapsed);
                        }
                        ReadResult::WouldBlock => break,
                        ReadResult::Closed(r) => return Err(format!("conn {k} closed with {r:?} after {} bytes", recvd[k])),
                    }
                }
            }
            if eof.iter().all(|&e| e) && ids.len() == total as usize {
                assert!(recvd.iter().all(|&n| n == per_conn), "short stream: {recvd:?}");
                return Ok((samples, finished));
            }
            let wait = c.next_deadline().map_or(std::time::Duration::from_millis(5), |d| {
                std::time::Duration::from_nanos(d.as_nanos().saturating_sub(now().as_nanos())).min(std::time::Duration::from_millis(5))
            });
            if let Ok(Some((peer, p, _lease))) = tokio::time::timeout(wait, server.to_client.recv()).await {
                c.ingress(now(), ci, peer, Bytes::from(p));
                while let Ok((peer, p, _lease)) = server.to_client.try_recv() {
                    c.ingress(now(), ci, peer, Bytes::from(p));
                }
            }
        }
    }

    /// Sample indices covering [start, end) of the 250 ms progress samples.
    fn window_samples(samples: &[u64], start: std::time::Duration, end: std::time::Duration) -> &[u64] {
        let lo = (start.as_millis() / 250) as usize;
        let hi = ((end.as_millis() / 250) as usize).min(samples.len());
        samples.get(lo..hi).unwrap_or(&[])
    }

    /// Longest run of equal 250 ms progress samples: the stream made no
    /// progress for (run − 1) × 250 ms.
    fn longest_flat(samples: &[u64]) -> usize {
        let mut best = 1;
        let mut run = 1;
        for pair in samples.windows(2) {
            run = if pair[0] == pair[1] { run + 1 } else { 1 };
            best = best.max(run);
        }
        best
    }

    /// The design guarantees progress well inside the 5 s evidence threshold
    /// of #674 even when the port is saturated at the per-connection floor
    /// (docs/design/0007 §3); 8 samples = 1.75 s without progress.
    const MAX_FLAT_SAMPLES: usize = 8;

    #[tokio::test(flavor = "current_thread")]
    async fn issue37_splice_streams_keep_progressing_with_delayed_acks() {
        issue37_splice_delayed(64).await;
    }

    #[tokio::test(flavor = "current_thread")]
    #[ignore = "full 256-stream splice comparison; 64 streams run in CI"]
    async fn issue37_splice_256_streams_keep_progressing_with_delayed_acks() {
        issue37_splice_delayed(256).await;
    }

    #[cfg(target_os = "linux")]
    #[tokio::test(flavor = "current_thread")]
    #[ignore = "repeat CPU/byte evidence; run alone with --test-threads=1"]
    async fn issue43_repeated_splice_cpu_evidence() {
        fn cpu_ticks() -> (u64, u64) {
            let stat = std::fs::read_to_string("/proc/self/stat").unwrap();
            let fields: Vec<_> = stat.rsplit_once(')').unwrap().1.split_whitespace().collect();
            (fields[11].parse().unwrap(), fields[12].parse().unwrap())
        }
        let hz = std::process::Command::new("getconf").arg("CLK_TCK").output().unwrap();
        assert!(hz.status.success());
        let hz = std::str::from_utf8(&hz.stdout).unwrap().trim();
        for repeat in 0..5 {
            for conns in if repeat % 2 == 0 { [64, 256] } else { [256, 64] } {
                let before = cpu_ticks();
                let begin = std::time::Instant::now();
                issue37_splice_delayed(conns).await;
                let wall_ns = begin.elapsed().as_nanos();
                let after = cpu_ticks();
                eprintln!(
                    "issue43 cpu: repeat={repeat} conns={conns} bytes={} wall_ns={wall_ns} user_ticks={} sys_ticks={} hz={hz}",
                    u64::from(conns) * (4 << 20),
                    after.0 - before.0,
                    after.1 - before.1
                );
            }
        }
    }

    async fn issue37_splice_delayed(conns: u16) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let per_conn = 4 << 20;
        let upstream = tokio::spawn(async move {
            let data: Arc<[u8]> = (0..per_conn).map(pattern_byte).collect();
            let mut writers = tokio::task::JoinSet::new();
            for _ in 0..conns {
                let (mut socket, _) = listener.accept().await.unwrap();
                let data = data.clone();
                writers.spawn(async move {
                    socket.write_all(&data).await.unwrap();
                    socket.shutdown().await.unwrap();
                });
            }
            while let Some(result) = writers.join_next().await {
                result.unwrap();
            }
        });
        let port_bytes = u64::from(conns) * (128 << 10);
        let profile = Profile { port_bytes, peer_bytes: port_bytes, peers: 1, charge_egress: true, per_conn };
        let mut server = limited_server_mode(GlobalBudget::new(2 * port_bytes), profile, None, Some(addr));
        server.ack_delay = std::time::Duration::from_millis(50);
        let (samples, finished) = download_pauses(&mut server, 20000, conns, 0, &[], None, std::time::Duration::from_secs(40)).await.unwrap();
        assert!(finished.iter().all(Option::is_some));
        // Measure mid-transfer stalls, excluding connection setup and EOF.
        // All streams must still finish within the overall deadline above.
        let max_flat = samples
            .iter()
            .zip(&finished)
            .map(|(s, end)| {
                let live = window_samples(s, std::time::Duration::ZERO, end.unwrap());
                let first = live.iter().position(|&bytes| bytes > 0).unwrap();
                longest_flat(&live[first..])
            })
            .max()
            .unwrap();
        assert!(max_flat <= MAX_FLAT_SAMPLES);
        let snap = server.handle.snapshot().await.unwrap();
        eprintln!(
            "issue37 splice: conns={conns} max_flat_samples={max_flat} last_eof={:?} share_blocked={}",
            finished.iter().max().unwrap(),
            snap.share_blocked
        );
        upstream.await.unwrap();
        let global = server.global.clone();
        server.task.shutdown_and_join().await.unwrap();
        drop(server.to_client);
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn stopped_readers_do_not_starve_healthy_streams() {
        // zfc #674: three clients stop reading from t=1s to t=7s. Pre-fix the
        // healthy streams made zero progress for the whole window.
        let mut server = limited_server(GlobalBudget::new(8 << 20), Profile::zfc_repro(false, 1 << 20));
        let pauses: Vec<_> = (0..3u16).map(|k| (k, std::time::Duration::from_secs(1), std::time::Duration::from_secs(7))).collect();
        let (samples, finished) =
            download_pauses(&mut server, 20_000, 6, 128 << 10, &pauses, None, std::time::Duration::from_secs(30)).await.expect("six streams with three paused");
        for k in 3..6 {
            let w = window_samples(&samples[k], std::time::Duration::from_secs(1), std::time::Duration::from_secs(7));
            assert!(longest_flat(w) <= MAX_FLAT_SAMPLES, "healthy conn {k} stalled: {w:?}");
        }
        // The paused streams recover once they resume (zero-window recovery).
        for k in 0..6 {
            assert!(finished[k].is_some(), "conn {k} never finished");
        }
        let snap = server.handle.snapshot().await.unwrap();
        assert!(snap.share_blocked > 0, "the send share never bound");
        assert_eq!(snap.senders, 0, "sender tally leaked: {}", snap.senders);
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn late_stream_gets_a_share_while_stopped_readers_hold_theirs() {
        // Three paused streams fill their shares first; the connection joining
        // at t=2s must still complete while they remain paused (#674 §3).
        let mut server = limited_server(GlobalBudget::new(8 << 20), Profile::zfc_repro(false, 1 << 20));
        let pauses: Vec<_> = (0..3u16).map(|k| (k, std::time::Duration::ZERO, std::time::Duration::from_secs(12))).collect();
        let (_samples, finished) =
            download_pauses(&mut server, 20_000, 3, 0, &pauses, Some(std::time::Duration::from_secs(2)), std::time::Duration::from_secs(30))
                .await
                .expect("late stream next to stopped readers");
        let late = finished[3].expect("late stream never finished");
        assert!(late < std::time::Duration::from_secs(12), "late stream finished at {late:?}, after the pause ended");
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn healthy_stream_keeps_its_share_next_to_a_stopped_peer_mate() {
        // Peer 9: two stopped readers and one healthy stream; peer 10: three
        // healthy streams. Both levels constrain; every healthy stream must
        // progress through the pause window.
        let profile = Profile { port_bytes: 819_200, peer_bytes: 409_600, peers: 2, charge_egress: false, per_conn: 1 << 20 };
        let mut server = limited_server(GlobalBudget::new(8 << 20), profile);
        let pauses: Vec<_> = [0u16, 2].into_iter().map(|k| (k, std::time::Duration::from_secs(1), std::time::Duration::from_secs(7))).collect();
        let (samples, finished) =
            download_pauses(&mut server, 20_000, 6, 128 << 10, &pauses, None, std::time::Duration::from_secs(30)).await.expect("peers with stopped readers");
        for k in [1usize, 3, 4, 5] {
            let w = window_samples(&samples[k], std::time::Duration::from_secs(1), std::time::Duration::from_secs(7));
            assert!(longest_flat(w) <= MAX_FLAT_SAMPLES, "healthy conn {k} stalled: {w:?}");
        }
        assert!(finished.iter().all(|f| f.is_some()));
        server.task.shutdown_and_join().await.unwrap();
    }

    const SECS: std::time::Duration = std::time::Duration::from_secs(30);

    #[tokio::test(flavor = "current_thread")]
    async fn adapter_queues_do_not_starve_core_tx_blocks() {
        // 16 full 64 KiB adapter queues exceed an 819 KiB port share. Queued
        // bytes can only drain into core TX blocks from that same share.
        let mut server = limited_server(GlobalBudget::new(8 << 20), Profile::zfc_repro(false, 1 << 20));
        download(&mut server, 20_000, 16, SECS).await.expect("16 concurrent streams");
        let snap = server.handle.snapshot().await.unwrap();
        assert!(snap.progress_reserve);
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn idle_tx_cache_does_not_block_the_next_stream_on_the_same_port() {
        let mut server = limited_server(GlobalBudget::new(8 << 20), Profile::zfc_repro(false, 4 << 20));
        download(&mut server, 20_000, 1, SECS).await.expect("first stream");
        download(&mut server, 21_000, 1, std::time::Duration::from_secs(10)).await.expect("next stream");
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn charged_egress_makes_progress_when_port_share_is_full() {
        // Few streams, so adapter queues fit; buffered core data then fills
        // the share that zfc also charges for each queued egress packet.
        let mut server = limited_server(GlobalBudget::new(8 << 20), Profile::zfc_repro(true, 8 << 20));
        download(&mut server, 20_000, 4, SECS).await.expect("4 streams with charged egress");
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn small_peer_shares_each_make_progress() {
        // Four peers with a quarter of the port share each, two streams per
        // peer and charged egress: the peer level binds first.
        let profile = Profile { port_bytes: 819_200, peer_bytes: 204_800, peers: 4, charge_egress: true, per_conn: 1 << 20 };
        let mut server = limited_server(GlobalBudget::new(8 << 20), profile);
        download(&mut server, 20_000, 8, SECS).await.expect("4 peers x 2 streams");
        let snap = server.handle.snapshot().await.unwrap();
        assert!(snap.progress_reserve);
        assert!(snap.failures_by_level[2] > 0, "peer shares were never under pressure");
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn ports_sharing_a_small_global_budget_all_make_progress() {
        // Each port may use 800 KiB, but together they only get 1 MiB, so
        // the global level binds and each port waits on the other's releases.
        let global = GlobalBudget::new(1 << 20);
        let profile = Profile { port_bytes: 819_200, peer_bytes: 819_200, peers: 2, charge_egress: true, per_conn: 1 << 20 };
        let mut a = limited_server(global.clone(), profile);
        let mut b = limited_server(global.clone(), profile);
        let (ra, rb) = tokio::join!(download(&mut a, 20_000, 8, SECS), download(&mut b, 21_000, 8, SECS));
        ra.expect("port a");
        rb.expect("port b");
        let (sa, sb) = (a.handle.snapshot().await.unwrap(), b.handle.snapshot().await.unwrap());
        assert!(sa.failures_by_level[0] + sb.failures_by_level[0] > 0, "global budget was never under pressure");
        a.task.shutdown_and_join().await.unwrap();
        b.task.shutdown_and_join().await.unwrap();
        assert_eq!(global.reserved(), 0);
    }

    #[tokio::test(flavor = "current_thread")]
    async fn saturate_release_and_single_stream_rounds_recover_on_the_same_port() {
        let mut server = limited_server(GlobalBudget::new(8 << 20), Profile::zfc_repro(true, 512 << 10));
        for round in 0..3u16 {
            download(&mut server, 20_000 + round * 100, 16, SECS).await.unwrap_or_else(|e| panic!("round {round} load: {e}"));
            download(&mut server, 20_050 + round * 100, 1, std::time::Duration::from_secs(10))
                .await
                .unwrap_or_else(|e| panic!("round {round} single stream after load: {e}"));
        }
        let snap = server.handle.snapshot().await.unwrap();
        assert_eq!(snap.closes.reset + snap.closes.aborted, 0, "{:?}", snap.closes);
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn new_connection_is_admitted_while_senders_keep_refilling_tx_blocks() {
        // Busy senders take a TX block back as soon as they free one, which
        // keeps the port above the admission line (TX blocks may use one block
        // more than admission). A new connection must still get in on an
        // early SYN retransmit instead of waiting for the senders to stop.
        let global = GlobalBudget::new(8 << 20);
        let mut server = limited_server(global.clone(), Profile::zfc_repro(false, 256 << 10));
        let churn = spawn_refilling_senders(server.memory.clone(), 3500);
        let load = Load { read_rate: 0, late_after: Some(0), late_above: 0, ..Load::default() };
        let report = download_with(&mut server, 20_000, 0, std::time::Duration::from_secs(20), load).await.expect("new connection");
        churn.await.unwrap();
        let (connect, _) = report.late_connected.expect("never connected");
        let snap = server.handle.snapshot().await.unwrap();
        assert!(connect < std::time::Duration::from_millis(2500), "handshake took {connect:?}; active_state failures {}", snap.failures_by_kind.active_state);
        assert_eq!(snap.closes.reset + snap.closes.aborted, 0, "{:?}", snap.closes);
        server.task.shutdown_and_join().await.unwrap();
    }

    /// Busy senders refill every freed TX block (docs/design/0006 §1) until
    /// `ms`, next to real load streams on the same port.
    fn spawn_refilling_senders(memory: MemoryHandle, ms: u64) -> tokio::task::JoinHandle<()> {
        spawn_refilling_senders_counted(memory, ms, None)
    }

    fn spawn_refilling_senders_counted(memory: MemoryHandle, ms: u64, rounds: Option<Arc<std::sync::atomic::AtomicU64>>) -> tokio::task::JoinHandle<()> {
        spawn_refilling_senders_until(memory, ms, rounds, None)
    }

    fn spawn_refilling_senders_until(
        memory: MemoryHandle,
        ms: u64,
        rounds: Option<Arc<std::sync::atomic::AtomicU64>>,
        stop: Option<Arc<std::sync::atomic::AtomicBool>>,
    ) -> tokio::task::JoinHandle<()> {
        tokio::spawn(async move {
            let until = tokio::time::Instant::now() + std::time::Duration::from_millis(ms);
            let mut held: std::collections::VecDeque<MemoryLease> = std::collections::VecDeque::new();
            while tokio::time::Instant::now() < until && !stop.as_ref().is_some_and(|stop| stop.load(std::sync::atomic::Ordering::SeqCst)) {
                if held.len() > 1 {
                    held.pop_front();
                }
                while let Some(lease) = memory.try_allocate_kind(crate::buf::TX_BLOCK_CHARGE, AllocationKind::TxBlock) {
                    held.push_back(lease);
                }
                if let Some(counter) = &rounds {
                    counter.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                }
                tokio::time::sleep(std::time::Duration::from_millis(5)).await;
            }
        })
    }

    #[tokio::test(flavor = "current_thread")]
    async fn forced_refusal_debt_does_not_stop_established_streams() {
        // Force one selected late SYN to create debt, then release the filler
        // and start competing refills. This is an explicit refusal witness;
        // natural mixed-load admission pressure is covered separately below.
        let first_port = 20_000;
        let conns = 6;
        let global = GlobalBudget::new(8 << 20);
        let mut server = limited_server_with_forced_refusal(global, Profile::zfc_repro(false, 1 << 20), Some(first_port + conns));
        let load = Load { read_rate: 128 << 10, late_after: Some(64 << 10), late_above: 0, ..Load::default() };
        let report = download_with(&mut server, first_port, conns, std::time::Duration::from_secs(40), load).await.expect("load and late stream");
        let (connect, loading) = report.late_connected.expect("never connected");
        let snap = server.handle.snapshot().await.unwrap();
        assert!(report.refill_rounds_at_late_handshake >= 2, "refiller did not actually execute multiple rounds");
        assert!(report.refills_active_at_late_handshake, "refilling competition ended before the late handshake");
        assert!(snap.stats.admission_debts > 0, "the late SYN was never refused");
        assert!(connect < std::time::Duration::from_millis(2500), "handshake took {connect:?}");
        assert!(loading > 0);
        assert!(report.load_progress.iter().all(|&n| n >= 64 << 10), "a load stream stalled while admission was owed: {:?}", report.load_progress);
        assert_eq!(snap.closes.reset + snap.closes.aborted, 0, "{:?}", snap.closes);
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn mixed_load_above_admission_line_keeps_streams_progressing() {
        let first_port = 20_000;
        let conns = 6;
        let global = GlobalBudget::new(8 << 20);
        let profile = Profile::zfc_repro(false, 1 << 20);
        let admit_line = profile.port_bytes - global.headroom(profile.port_bytes, crate::budget::Tier::Admit);
        // No SYN hook or injected filler: first let all six real streams
        // read 64 KiB, then start ordinary competing refills. Observe the
        // admission line only while that preexisting refiller is active.
        let mut server = limited_server(global, profile);
        let memory = server.memory.clone();
        let active = server.refilling.clone();
        let rounds = server.refill_rounds.clone();
        let stop = server.stop_refilling.clone();
        let load_ready = server.load_ready.clone();
        let churn = tokio::spawn(async move {
            load_ready.notified().await;
            active.store(true, std::sync::atomic::Ordering::SeqCst);
            // Stop on observed progress and handshake, not elapsed startup
            // time; 40s remains a guard if the scenario cannot make progress.
            spawn_refilling_senders_until(memory, 40_000, Some(rounds), Some(stop)).await.unwrap();
            active.store(false, std::sync::atomic::Ordering::SeqCst);
        });
        let load = Load { read_rate: 128 << 10, late_after: Some(64 << 10), late_above: admit_line, require_refiller: true };
        let report = download_with(&mut server, first_port, conns, std::time::Duration::from_secs(40), load).await.expect("mixed load and late stream");
        churn.await.unwrap();
        let (connect, loading) = report.late_connected.expect("never connected");
        assert!(report.late_trigger_physical_bytes > admit_line);
        assert_eq!(report.loading_at_late_trigger, usize::from(conns));
        assert!(report.refills_active_at_late_trigger && report.refill_rounds_at_late_trigger >= 2);
        assert!(connect < std::time::Duration::from_millis(2500), "handshake took {connect:?}");
        assert!(loading > 0);
        // A snapshot above the admission line does not guarantee refusal:
        // actual ACK releases may admit the SYN immediately. Progress is
        // therefore measured over a fixed, unconditional one-second window.
        let progress = report.progress_after_one_second.expect("progress window was not observed");
        assert!(report.refills_active_after_one_second, "refills ended before the progress window");
        assert!(report.refill_rounds_after_one_second > report.refill_rounds_at_late_trigger);
        assert!(progress.iter().all(|&n| n >= 64 << 10), "a mixed-load stream stalled: {progress:?}");
        let snap = server.handle.snapshot().await.unwrap();
        assert_eq!(snap.closes.reset + snap.closes.aborted, 0, "{:?}", snap.closes);
        server.task.shutdown_and_join().await.unwrap();
    }

    #[tokio::test(flavor = "current_thread")]
    async fn established_connection_is_not_reset_when_stream_state_is_short() {
        // Admission room for the core state of one connection, but not for its
        // adapter stream state too: the handshake must not end in a reset.
        let global = GlobalBudget::new(8 << 20);
        let mut server = limited_server(global.clone(), Profile::zfc_repro(false, 256 << 10));
        let admit_limit = 819_200 - global.headroom(819_200, crate::budget::Tier::Admit);
        let used = server.handle.snapshot().await.unwrap().port_physical_bytes;
        let room = crate::shard::ACTIVE_STATE_BYTES + STREAM_STATE_BYTES / 2;
        let blocker = server.memory.try_allocate_kind(admit_limit - used - room, AllocationKind::Other).unwrap();
        tokio::spawn(async move {
            tokio::time::sleep(std::time::Duration::from_millis(300)).await;
            drop(blocker);
        });
        download(&mut server, 20_000, 1, std::time::Duration::from_secs(10)).await.expect("connection after admission shortage");
        let snap = server.handle.snapshot().await.unwrap();
        assert_eq!(snap.aborts.stream_state, 0);
        server.task.shutdown_and_join().await.unwrap();
    }
}
