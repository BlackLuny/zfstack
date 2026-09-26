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

use crate::budget::{GlobalBudget, MemoryHandle, MemoryLease};
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
    fn default() -> Self { Self { items: VecDeque::new(), _capacity: None } }
}

impl<T> ChargedDeque<T> {
    fn is_empty(&self) -> bool { self.items.is_empty() }
    fn front(&self) -> Option<&T> { self.items.front() }
    fn front_mut(&mut self) -> Option<&mut T> { self.items.front_mut() }
    fn back(&self) -> Option<&T> { self.items.back() }
    fn back_mut(&mut self) -> Option<&mut T> { self.items.back_mut() }

    fn reserve_one(&mut self, memory: &MemoryHandle) -> Result<(), QueueGrowError> {
        if self.items.len() < self.items.capacity() { return Ok(()) }
        let target = self.items.capacity().max(4).checked_mul(2).ok_or(QueueGrowError::Allocation)?;
        let bytes = target.checked_mul(std::mem::size_of::<T>())
            .and_then(|n| n.checked_mul(2))
            .and_then(|n| u64::try_from(n).ok())
            .ok_or(QueueGrowError::Allocation)?;
        let lease = memory.try_allocate(bytes).ok_or(QueueGrowError::Quota(bytes))?;
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
    fn len(&self) -> usize { self.len }
    fn is_empty(&self) -> bool { self.len == 0 }
    fn clear(&mut self) { self.chunks.clear(); self.len = 0; }
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
                if let Err(error) = self.chunks.reserve_one(memory) {
                    self.last_failure = Some((error, observed));
                    break;
                }
                let observed = memory.global().release_epoch();
                let Some(lease) = memory.try_allocate(cap as u64 + 64) else {
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
            if chunk.head == chunk.len { self.chunks.pop_front(); }
        }
    }
}

struct Shared {
    id: ConnId,
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
    fn wake(self: Arc<Self>) { self.ctl.mark(self.id); }
    fn wake_by_ref(self: &Arc<Self>) { self.ctl.mark(self.id); }
}

struct BudgetWake {
    ctl: Arc<Ctl>,
}

impl Wake for BudgetWake {
    fn wake(self: Arc<Self>) { self.ctl.notify.notify_one(); }
    fn wake_by_ref(self: &Arc<Self>) { self.ctl.notify.notify_one(); }
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
    if map.capacity() <= 512 || map.len() > map.capacity() / 8 { return; }
    let mut compact = HashMap::new();
    if compact.try_reserve(map.len()).is_err() { return; }
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
        let free = sh.cfg.tx_cap.saturating_sub(q.tx.len());
        let min_free = sh.cfg.tx_low_watermark.min(src.len().max(1));
        if free < min_free {
            q.write_waker = Some(cx.waker().clone());
            q.write_parked = true;
            return Poll::Pending;
        }
        let n = free.min(src.len());
        let was_empty = q.tx.is_empty();
        let written = q.tx.push(&src[..n], &sh.memory);
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
            sh.memory.global().register_physical_waiter(sh.memory_waiter, observed, cx.waker(), &sh.memory, needed);
            return Poll::Pending;
        }
        sh.memory.global().remove_waiter(sh.memory_waiter);
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
    pub active_connections: usize,
    pub adapter_streams: usize,
    pub budget_waiting: bool,
    pub tail_connections: Vec<TailConnection>,
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
    let (handle, acceptor, ids, task, ()) = spawn_with_source_factory(
        cfg, stream_cfg, ifaces, move |_| (egress, ()), global, limits,
        ingress_rx, on_ingress,
    );
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

fn spawn_inner<E, I, F>(mut shard: Shard, stream_cfg: StreamConfig, ifaces: Vec<IfaceConfig>, egress: E, input: Option<InputSource<I, F>>)
    -> (StackHandle, Acceptor, Vec<IfaceId>, tokio::task::JoinHandle<()>, Arc<AtomicBool>)
where
    E: Egress,
    I: Send + 'static,
    F: FnMut(&mut Shard, crate::Instant, I) + Send + 'static,
{
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
    };
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
                pkt = async {
                    match self.input.as_mut() {
                        Some(source) => source.rx.recv().await,
                        None => std::future::pending().await,
                    }
                } => {
                    match pkt {
                        Some(pkt) => {
                            let now = self.now();
                            let source = self.input.as_mut().unwrap();
                            (source.handle)(&mut self.shard, now, pkt);
                        }
                        None => self.input = None,
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
                let tail_connections = if self.streams.len() <= 4 {
                    self.streams.iter().filter_map(|(&id, shared)| {
                        let core = self.shard.info(id)?;
                        let q = shared.q.lock().unwrap();
                        Some(TailConnection {
                            id,
                            core,
                            adapter_tx_queued: q.tx.len(),
                            adapter_rx_queued: q.rx_len,
                            write_parked: q.write_parked,
                        })
                    }).collect()
                } else {
                    Vec::new()
                };
                let budget = self.shard.budget();
                let _ = reply.send(StackSnapshot {
                    stats: self.shard.stats().clone(),
                    port_physical_bytes: budget.physical_used(),
                    core_reserve_failures: budget.reserve_failures,
                    failures_by_kind: budget.failures_by_kind,
                    active_connections: self.shard.conn_count(),
                    adapter_streams: self.streams.len(),
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
                let Some(state_memory) = memory.try_allocate(STREAM_STATE_BYTES) else {
                    self.shard.abort(id);
                    return;
                };
                let memory_waiter = memory.global().new_waiter_id();
                let driver_waiter = memory.global().new_waiter_id();
                let rx_index_waiter = memory.global().new_waiter_id();
                let driver_wake = Waker::from(Arc::new(DriverWake { ctl: self.ctl.clone(), id }));
                let sh = Arc::new(Shared { id, q: Mutex::new(Queues::default()), ctl: self.ctl.clone(), cfg: self.stream_cfg.clone(), memory, _state_memory: state_memory, memory_waiter, driver_waiter, rx_index_waiter, driver_wake });
                self.streams.insert(id, sh.clone());
                let meta = ConnMeta { iface: info.iface, peer: info.peer, local: info.local, remote: info.remote };
                if self.accept_tx.try_send(TcpStream { sh, meta }).is_err() {
                    self.shard.abort(id);
                    self.remove_stream(id);
                    return;
                }
                self.pump(id);
            }
            Event::Readable(id) | Event::Writable(id) => self.pump(id),
            Event::Closed(id, reason) => {
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
                self.shard.abort(id);
            }
        }
    }

    fn remove_stream(&mut self, id: ConnId) -> Option<Arc<Shared>> {
        let removed = self.streams.remove(&id);
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
            if start_orphan { q.orphan_deadline = Some(now + timeout); }
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
                match q.rx.reserve_one(&sh.memory) {
                    Ok(()) => sh.memory.global().remove_waiter(sh.rx_index_waiter),
                    Err(QueueGrowError::Quota(bytes)) => {
                        sh.memory.global().register_physical_waiter(sh.rx_index_waiter, observed, &sh.driver_wake, &sh.memory, bytes);
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
        if abort_rx { self.shard.abort(id); }
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
                match self.shard.write(id, &q.tx.front()[..n]) {
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
                        sh.memory.global().register_cacheable_physical_waiter(sh.driver_waiter, observed, &sh.driver_wake, &sh.memory, crate::buf::TX_BLOCK_CHARGE);
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
            if q.tx.is_empty() { sh.memory.global().remove_waiter(sh.driver_waiter); }
            if q.write_parked && cfg.tx_cap - q.tx.len() >= cfg.tx_low_watermark {
                q.write_parked = false;
                wake_writer = q.write_waker.take();
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
        for sh in self.streams.values() {
            let mut q = sh.q.lock().unwrap();
            q.error = Some(CloseReason::Aborted);
            q.rx.clear();
            q.rx_len = 0;
            q.tx.clear();
            let (r, w, f) = (q.read_waker.take(), q.write_waker.take(), q.flush_waker.take());
            drop(q);
            if let Some(w) = r { w.wake(); }
            if let Some(w) = w { w.wake(); }
            if let Some(w) = f { w.wake(); }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sim::pattern_byte;
    use std::sync::atomic::{AtomicUsize, Ordering};
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
        let error = tokio::time::timeout(std::time::Duration::from_secs(1), task.wait_finished())
            .await.unwrap().unwrap_err();
        assert!(error.is_panic());
        assert!(handle.snapshot().await.is_none());
        task.shutdown_and_join().await.unwrap();
    }

    struct CountWake(AtomicUsize);
    impl Wake for CountWake {
        fn wake(self: Arc<Self>) { self.0.fetch_add(1, Ordering::Relaxed); }
        fn wake_by_ref(self: &Arc<Self>) { self.0.fetch_add(1, Ordering::Relaxed); }
    }

    #[test]
    fn short_connection_index_releases_sparse_capacity() {
        let mut streams = HashMap::new();
        for id in 0..8192 { streams.insert(id, id); }
        let peak = streams.capacity();
        for id in 0..8191 { streams.remove(&id); }
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
        while queue.reserve_one(&memory).is_ok() {
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
            if n == 0 { break }
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
}
