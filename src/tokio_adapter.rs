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
use std::sync::atomic::{AtomicBool, Ordering};
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
    /// Additional bytes this stream may buffer across the adapter queue and
    /// the core send buffer under its send share; published by the driver on
    /// each pump (docs/design/0007 §2.4). Writes consume it synchronously.
    send_room: usize,
}

struct TxChunk {
    data: Box<[u8]>,
    head: usize,
    len: usize,
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
        self.chunks.front().map_or(&[], |c| &c.data[c.head..c.len])
    }
    fn push(&mut self, mut src: &[u8], memory: &MemoryHandle) -> usize {
        let wanted = src.len();
        self.last_failure = None;
        while !src.is_empty() {
            if self.chunks.back().is_none_or(|c| c.len == c.data.len()) {
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
                self.chunks.push_back_reserved(TxChunk { data: vec![0; cap].into_boxed_slice(), head: 0, len: 0, _lease: lease });
            }
            let chunk = self.chunks.back_mut().unwrap();
            let n = src.len().min(chunk.data.len() - chunk.len);
            chunk.data[chunk.len..chunk.len + n].copy_from_slice(&src[..n]);
            chunk.len += n;
            self.len += n;
            src = &src[n..];
        }
        wanted - src.len()
    }
    fn consume(&mut self, mut n: usize) {
        while n > 0 {
            let chunk = self.chunks.front_mut().expect("TX bytes present");
            let take = n.min(chunk.len - chunk.head);
            chunk.head += take;
            self.len -= take;
            n -= take;
            if chunk.head == chunk.len {
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
    driver_wake: Waker,
}

const STREAM_STATE_BYTES: u64 = (std::mem::size_of::<Shared>() + std::mem::size_of::<TcpStream>() + 256) as u64;

impl Drop for Shared {
    fn drop(&mut self) {
        self.memory.global().remove_waiter(self.memory_waiter);
        self.memory.global().remove_waiter(self.driver_waiter);
        self.memory.global().remove_waiter(self.rx_index_waiter);
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
            q.read_waker = Some(cx.waker().clone());
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
        let min_free = sh.cfg.tx_low_watermark.min(src.len().max(1));
        if free < min_free {
            q.write_waker = Some(cx.waker().clone());
            q.write_parked = true;
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
            q.write_waker = Some(cx.waker().clone());
            q.write_parked = true;
            let needed = match failure {
                Some((QueueGrowError::Quota(bytes), _)) => bytes,
                _ => src.len().min(64 * 1024).next_power_of_two().max(64) as u64 + 64,
            };
            let observed = failure.map_or_else(|| sh.memory.global().release_epoch(), |(_, observed)| observed);
            sh.memory.global().register_physical_waiter(sh.memory_waiter, observed, cx.waker(), &sh.memory, needed, AllocationKind::AdapterTx);
            return Poll::Pending;
        }
        sh.memory.global().remove_waiter(sh.memory_waiter);
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
            q.flush_waker = Some(cx.waker().clone());
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
            q.flush_waker = Some(cx.waker().clone());
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
        budget_wake,
        streams: HashMap::new(),
        orphans: BTreeSet::new(),
        stream_cfg,
        epoch: tokio::time::Instant::now(),
        closes: CloseCounts::default(),
        aborts: AbortCounts::default(),
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
}

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
            if let Some(epoch) = self.shard.budget_wait_epoch() {
                self.shard.budget().global().register_waiter(self.budget_waiter, epoch, &self.budget_wake);
            } else {
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
                    core_reserve_failures: budget.reserve_failures(),
                    failures_by_kind: budget.stats().failures(),
                    failures_by_level: budget.stats().failures_by_level(),
                    waiters: budget.physical_waiters(),
                    record_waiters: self.shard.record_waiters(),
                    progress_reserve: budget.progress_reserve_active(),
                    admission_debt: budget.admission_debt(),
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
        for _ in 0..16 {
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
                    q: Mutex::new(Queues { send_room, ..Queues::default() }),
                    ctl: self.ctl.clone(),
                    cfg: self.stream_cfg.clone(),
                    memory,
                    _state_memory: state_memory,
                    memory_waiter,
                    driver_waiter,
                    rx_index_waiter,
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
        if removed.is_some() {
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
        let cfg = &self.stream_cfg;
        let consumed = {
            let mut q = sh.q.lock().unwrap();
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
                    Ok(()) => sh.memory.global().remove_waiter(sh.rx_index_waiter),
                    Err(QueueGrowError::Quota(bytes)) => {
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
                match self.shard.write_for_adapter(id, &q.tx.front()[..n]) {
                    WriteResult::Written(w) => {
                        sh.memory.global().remove_waiter(sh.driver_waiter);
                        q.tx.consume(w);
                        if w < n {
                            break;
                        }
                    }
                    WriteResult::WouldBlock => {
                        sh.memory.global().remove_waiter(sh.driver_waiter);
                        break;
                    }
                    WriteResult::QuotaBlocked => {
                        sh.memory.global().register_waiter(sh.driver_waiter, observed, &sh.driver_wake);
                        break;
                    }
                    WriteResult::MemoryBlocked => {
                        sh.memory.global().register_physical_waiter(
                            sh.driver_waiter,
                            observed,
                            &sh.driver_wake,
                            &sh.memory,
                            self.shard.write_allocation_charge(id, n),
                            AllocationKind::TxBlock,
                        );
                        break;
                    }
                    WriteResult::Closed => {
                        sh.memory.global().remove_waiter(sh.driver_waiter);
                        q.error.get_or_insert(CloseReason::Aborted);
                        q.tx.clear();
                        break;
                    }
                }
            }
            if q.tx.is_empty() {
                sh.memory.global().remove_waiter(sh.driver_waiter);
            }
            // Publish the share-aware room: one accounting across the adapter
            // queue and the core send buffer (docs/design/0007 §2.1, §2.4).
            self.shard.set_adapter_tx(id, q.tx.len() as u32);
            let share = self.shard.send_share(sh.peer);
            let core_tx = self.shard.tx_queued(id).unwrap_or(0) as u64;
            q.send_room = share.saturating_sub(core_tx + q.tx.len() as u64).min(usize::MAX as u64) as usize;
            let wakeable = q.write_parked && cfg.tx_cap.saturating_sub(q.tx.len()).min(q.send_room) >= cfg.tx_low_watermark;
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

impl<E: Egress, I, F> Drop for Driver<E, I, F> {
    fn drop(&mut self) {
        self.shard.budget().global().remove_waiter(self.budget_waiter);
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
        let (to_client_tx, mut to_client_rx) = mpsc::unbounded_channel::<Vec<u8>>();
        let egress = move |_i: IfaceId, _p: PeerId, pkt: &OutPacket<'_>| {
            let _ = to_client_tx.send(pkt.to_vec());
            SendResult::Accepted
        };
        // Force many capped rounds so the driver must ingest ACKs while it
        // still has egress work; otherwise the two directions deadlock.
        let mut server_cfg = StackConfig::default();
        server_cfg.round_bytes_cap = 1500;
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
    }

    /// Accepted streams each send `per_conn` bytes over 64 KiB stream queues.
    fn limited_server(global: Arc<GlobalBudget>, profile: Profile) -> LimitedServer {
        limited_server_with_refused_syn(global, profile, None)
    }

    /// Test-only pressure gate: fill the quota in the same owner turn that
    /// processes the selected SYN, then require that SYN to create debt.
    fn limited_server_with_refused_syn(global: Arc<GlobalBudget>, profile: Profile, refuse_port: Option<u16>) -> LimitedServer {
        let mut refusal_observed = false;
        let refilling = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let ingress_refilling = refilling.clone();
        let refill_rounds = Arc::new(std::sync::atomic::AtomicU64::new(0));
        let ingress_refill_rounds = refill_rounds.clone();
        global.ensure_egress_reserve(2 * 2048);
        let limits = ResourceLimits { port_bytes: profile.port_bytes, peer_bytes: profile.peer_bytes, peer_max_connections: 128 };
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
                        let mut injected_bytes = 0;
                        for quantum in [crate::buf::TX_BLOCK_CHARGE, 256] {
                            while let Some(lease) = memory.try_allocate_kind(quantum, AllocationKind::TxBlock) {
                                injected_bytes += quantum;
                                held.push(lease);
                            }
                        }
                        let before = shard.stats().admission_debts;
                        let conns = shard.conn_count();
                        let physical = shard.budget().physical_used();
                        // No driver/ACK task can interleave between filling the
                        // quota and this SYN's admission attempt.
                        shard.ingress(now, iface, peer, p);
                        let after = shard.stats().admission_debts;
                        let debt = shard.budget().admission_debt();
                        assert_eq!(after, before + 1, "selected late SYN did not create a new debt");
                        assert!(debt.iter().any(|&n| n > 0));
                        assert_eq!(shard.conn_count(), conns, "refused SYN was admitted");
                        refusal_observed = true;
                        eprintln!(
                            "ADMISSION_GATE before={before} after={after} debt={debt:?} held_leases={} physical={physical} injected_bytes={injected_bytes}",
                            held.len()
                        );
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
        LimitedServer { handle, iface: ids[0], ingress, to_client, task, global, profile, memory, refilling, refill_rounds }
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
                let full = load.late_above == 0 || server.handle.snapshot().await.is_some_and(|s| s.port_physical_bytes > load.late_above);
                if late_started.is_none() && recvd[..load_n].iter().all(|&n| n >= after) && full {
                    let port = first_port + conns;
                    ids.push(c.connect(now(), ci, PeerId(9), SocketAddr::from(([10, 0, 0, 2], port)), "10.0.0.1:80".parse().unwrap()));
                    recvd.push(0);
                    eof.push(false);
                    late_started = Some(tokio::time::Instant::now());
                    report.load_progress = recvd[..load_n].to_vec();
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
                    if !overlaps_pause && k < ids.len() && !eof[k] && live >= 8 && s[s.len() - 8..].iter().all(|&v| v == s[s.len() - 1]) {
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
            if !out.is_empty() && server.ingress.send((server.iface, out)).await.is_err() {
                return Err("server ingress closed".into());
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
        let load = Load { read_rate: 0, late_after: Some(0), late_above: 0 };
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
        tokio::spawn(async move {
            let until = tokio::time::Instant::now() + std::time::Duration::from_millis(ms);
            let mut held: std::collections::VecDeque<MemoryLease> = std::collections::VecDeque::new();
            while tokio::time::Instant::now() < until {
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
    async fn owed_admission_room_does_not_stop_established_streams() {
        // Six real streams read at 128 KiB/s while refilling senders pin the
        // share. The debt left by the refused SYN holds refills back; the
        // real streams must keep moving through that window and all finish.
        let global = GlobalBudget::new(8 << 20);
        let mut server = limited_server_with_refused_syn(global, Profile::zfc_repro(false, 1 << 20), Some(20_006));
        let load = Load { read_rate: 128 << 10, late_after: Some(64 << 10), late_above: 0 };
        let report = download_with(&mut server, 20_000, 6, std::time::Duration::from_secs(40), load).await.expect("load and late stream");
        let (connect, loading) = report.late_connected.expect("never connected");
        let snap = server.handle.snapshot().await.unwrap();
        eprintln!(
            "ADMISSION_PROGRESS connect={connect:?} loading={loading} bytes={:?} refills_active={}",
            report.load_progress, report.refills_active_at_late_handshake
        );
        eprintln!("ADMISSION_REFILLS rounds_at_handshake={}", report.refill_rounds_at_late_handshake);
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
