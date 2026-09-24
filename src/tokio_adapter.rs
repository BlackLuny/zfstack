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

use crate::shard::{EgressSinks, IfaceConfig, OutPacket, SendResult, Shard};
use crate::{CloseReason, ConnId, Event, IfaceId, PeerId, ReadResult, StackConfig, WriteResult};
use bytes::{Bytes, BytesMut};
use std::collections::{BTreeSet, HashMap, VecDeque};
use std::io;
use std::net::SocketAddr;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};
use tokio::io::{AsyncRead, AsyncWrite, ReadBuf};
use tokio::sync::{mpsc, Notify};

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

#[derive(Default)]
struct Queues {
    rx: VecDeque<Bytes>,
    rx_len: usize,
    rx_eof: bool,
    tx: BytesMut,
    tx_shutdown: bool,
    /// Set by the driver when the connection is gone.
    error: Option<CloseReason>,
    closed_by_app: bool,
    /// The driver armed the orphan deadline for this dropped stream.
    orphan_timer: bool,
    read_waker: Option<Waker>,
    write_waker: Option<Waker>,
    /// Writer parked below the low watermark.
    write_parked: bool,
}

struct Shared {
    id: ConnId,
    q: Mutex<Queues>,
    ctl: Arc<Ctl>,
    cfg: StreamConfig,
}

/// Driver-side control: dirty stream list + wakeup.
struct Ctl {
    dirty: Mutex<Vec<ConnId>>,
    notify: Notify,
}

impl Ctl {
    fn mark(&self, id: ConnId) {
        self.dirty.lock().unwrap().push(id);
        self.notify.notify_one();
    }
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
        let was_full = q.rx_len >= sh.cfg.rx_cap / 2;
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
        }
        // Crossing below half capacity: let the driver pull more from the shard.
        let now_low = q.rx_len < sh.cfg.rx_cap / 2;
        drop(q);
        if was_full && now_low {
            sh.ctl.mark(sh.id);
        }
        Poll::Ready(Ok(()))
    }
}

impl AsyncWrite for TcpStream {
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, src: &[u8]) -> Poll<io::Result<usize>> {
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
        q.tx.extend_from_slice(&src[..n]);
        drop(q);
        if was_empty {
            sh.ctl.mark(sh.id);
        }
        Poll::Ready(Ok(n))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        // Data is handed to the stack asynchronously; TCP has no flush boundary.
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let sh = &self.sh;
        let mut q = sh.q.lock().unwrap();
        if !q.tx_shutdown {
            q.tx_shutdown = true;
            drop(q);
            sh.ctl.mark(sh.id);
        }
        Poll::Ready(Ok(()))
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
    EgressReleased(IfaceId),
    SetMtu(IfaceId, u16),
}

/// Handle used by the WG side to feed decrypted packets.
#[derive(Clone)]
pub struct StackHandle {
    tx: mpsc::Sender<Cmd>,
    /// Control messages must never be dropped (a lost `EgressReleased` would freeze
    /// the iface forever), so they use their own unbounded channel.
    ctl_tx: mpsc::UnboundedSender<Cmd>,
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
    /// The egress path for `iface` drained (state ③, §4.3).
    pub fn egress_released(&self, iface: IfaceId) {
        let _ = self.ctl_tx.send(Cmd::EgressReleased(iface));
    }
    pub fn set_iface_mtu(&self, iface: IfaceId, mtu: u16) {
        let _ = self.ctl_tx.send(Cmd::SetMtu(iface, mtu));
    }
}

/// Receives accepted connections.
pub struct Acceptor {
    rx: mpsc::UnboundedReceiver<TcpStream>,
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
pub fn spawn<E: Egress>(
    cfg: StackConfig,
    stream_cfg: StreamConfig,
    ifaces: Vec<IfaceConfig>,
    egress: E,
) -> (StackHandle, Acceptor, Vec<IfaceId>) {
    let mut shard = Shard::new(cfg);
    let ids: Vec<IfaceId> = ifaces.into_iter().map(|c| shard.add_iface(c)).collect();
    let (tx, rx) = mpsc::channel(1024);
    let (ctl_tx, ctl_rx) = mpsc::unbounded_channel();
    let (acc_tx, acc_rx) = mpsc::unbounded_channel();
    let driver = Driver {
        shard,
        egress,
        cmd_rx: rx,
        ctl_rx,
        accept_tx: acc_tx,
        ctl: Arc::new(Ctl { dirty: Mutex::new(Vec::new()), notify: Notify::new() }),
        streams: HashMap::new(),
        orphans: BTreeSet::new(),
        stream_cfg,
        epoch: tokio::time::Instant::now(),
        buf: vec![0u8; 64 * 1024],
    };
    tokio::spawn(driver.run());
    (StackHandle { tx, ctl_tx }, Acceptor { rx: acc_rx }, ids)
}

struct Driver<E: Egress> {
    shard: Shard,
    egress: E,
    cmd_rx: mpsc::Receiver<Cmd>,
    ctl_rx: mpsc::UnboundedReceiver<Cmd>,
    accept_tx: mpsc::UnboundedSender<TcpStream>,
    ctl: Arc<Ctl>,
    streams: HashMap<ConnId, Arc<Shared>>,
    /// Dropped streams whose tx queue has not drained into the shard yet, by
    /// deadline (the shard's orphan timeout only starts once `close` is called).
    /// Entries of streams released meanwhile are skipped when they come due.
    orphans: BTreeSet<(crate::Instant, ConnId)>,
    stream_cfg: StreamConfig,
    epoch: tokio::time::Instant,
    buf: Vec<u8>,
}

impl<E: Egress> Driver<E> {
    fn now(&self) -> crate::Instant {
        crate::Instant::from_nanos(self.epoch.elapsed().as_nanos() as u64 + 1)
    }

    fn to_tokio(&self, t: crate::Instant) -> tokio::time::Instant {
        self.epoch + std::time::Duration::from_nanos(t.as_nanos().saturating_sub(1))
    }

    async fn run(mut self) {
        loop {
            let orphan = self.orphans.first().map(|x| x.0);
            let deadline = match (self.shard.next_deadline(), orphan) {
                (Some(a), Some(b)) => Some(a.min(b)),
                (a, b) => a.or(b),
            }
            .map(|d| self.to_tokio(d));
            let ctl = self.ctl.clone();
            tokio::select! {
                biased;
                cmd = self.ctl_rx.recv() => {
                    if let Some(cmd) = cmd {
                        self.handle_cmd(cmd);
                    }
                }
                cmd = self.cmd_rx.recv() => {
                    let Some(cmd) = cmd else { break };
                    self.handle_cmd(cmd);
                    // Drain whatever else is queued without blocking.
                    while let Ok(cmd) = self.cmd_rx.try_recv() {
                        self.handle_cmd(cmd);
                    }
                    while let Ok(cmd) = self.ctl_rx.try_recv() {
                        self.handle_cmd(cmd);
                    }
                }
                _ = ctl.notify.notified() => {}
                _ = async {
                    match deadline {
                        Some(d) => tokio::time::sleep_until(d).await,
                        None => std::future::pending::<()>().await,
                    }
                } => {}
            }
            self.service();
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
            Cmd::EgressReleased(i) => self.shard.egress_released(i),
            Cmd::SetMtu(i, m) => self.shard.set_iface_mtu(i, m),
        }
    }

    /// Move data between stream queues and the shard, run the shard, dispatch events.
    fn service(&mut self) {
        self.expire_orphans();
        for _ in 0..16 {
            let dirty: Vec<ConnId> = std::mem::take(&mut *self.ctl.dirty.lock().unwrap());
            for id in dirty {
                self.pump(id);
            }
            let now = self.now();
            let mut sink = Sink { egress: &mut self.egress };
            let out = self.shard.run(now, &mut sink);
            let mut any = false;
            while let Some(ev) = self.shard.poll_event() {
                any = true;
                self.on_event(ev);
            }
            if !any && !out.more && self.ctl.dirty.lock().unwrap().is_empty() {
                break;
            }
        }
    }

    fn on_event(&mut self, ev: Event) {
        match ev {
            Event::Accepted(id) => {
                let Some(info) = self.shard.info(id) else { return };
                let sh = Arc::new(Shared { id, q: Mutex::new(Queues::default()), ctl: self.ctl.clone(), cfg: self.stream_cfg.clone() });
                self.streams.insert(id, sh.clone());
                let meta = ConnMeta { iface: info.iface, peer: info.peer, local: info.local, remote: info.remote };
                if self.accept_tx.send(TcpStream { sh, meta }).is_err() {
                    self.shard.abort(id);
                    self.streams.remove(&id);
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
                    let (r, w) = (q.read_waker.take(), q.write_waker.take());
                    drop(q);
                    r.map(|w| w.wake());
                    w.map(|w| w.wake());
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
            if let Some(sh) = self.streams.remove(&id) {
                sh.q.lock().unwrap().tx.clear();
                self.shard.abort(id);
            }
        }
    }

    fn release_if_done(&mut self, id: ConnId) {
        let state = self.streams.get(&id).map(|sh| {
            let mut q = sh.q.lock().unwrap();
            let done = q.closed_by_app && (q.error.is_some() || q.tx.is_empty());
            let start_orphan = q.closed_by_app && !done && !q.orphan_timer;
            q.orphan_timer |= start_orphan;
            (done, q.rx_len > 0, start_orphan)
        });
        if let Some((false, _, true)) = state {
            let t = self.now() + self.shard.config().orphan_timeout;
            self.orphans.insert((t, id));
        }
        if let Some((true, unread, _)) = state {
            if unread {
                // Dropped with unread data: the bytes are lost, tell the peer (§10.3).
                self.shard.abort(id);
            } else {
                let now = self.now();
                self.shard.close(now, id);
            }
            self.streams.remove(&id);
        }
    }

    /// Exchange data for one stream.
    fn pump(&mut self, id: ConnId) {
        let Some(sh) = self.streams.get(&id).cloned() else { return };
        let now = self.now();
        let cfg = &self.stream_cfg;
        // rx: shard → stream queue (zero-copy chunks), bounded by rx_cap.
        let mut wake_reader = None;
        {
            let mut q = sh.q.lock().unwrap();
            let was_empty = q.rx.is_empty() && !q.rx_eof;
            while q.rx_len < cfg.rx_cap && !q.rx_eof && q.error.is_none() {
                match self.shard.read_chunk(now, id, cfg.rx_cap - q.rx_len) {
                    Ok(b) => {
                        q.rx_len += b.len();
                        q.rx.push_back(b);
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
        // tx: stream queue → shard.
        let mut wake_writer = None;
        let mut shutdown = false;
        let app_closed;
        {
            let mut q = sh.q.lock().unwrap();
            while !q.tx.is_empty() {
                let n = q.tx.len().min(self.buf.len());
                self.buf[..n].copy_from_slice(&q.tx[..n]);
                match self.shard.write(id, &self.buf[..n]) {
                    WriteResult::Written(w) => {
                        let _ = q.tx.split_to(w);
                        if w < n {
                            break;
                        }
                    }
                    WriteResult::WouldBlock => break,
                    WriteResult::Closed => {
                        q.error.get_or_insert(CloseReason::Aborted);
                        q.tx.clear();
                        break;
                    }
                }
            }
            if q.write_parked && cfg.tx_cap - q.tx.len() >= cfg.tx_low_watermark {
                q.write_parked = false;
                wake_writer = q.write_waker.take();
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
        if app_closed {
            self.release_if_done(id);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sim::pattern_byte;
    use tokio::io::AsyncWriteExt;

    #[tokio::test(flavor = "current_thread")]
    async fn echo_roundtrip_through_async_handles() {
        let (to_client_tx, mut to_client_rx) = mpsc::unbounded_channel::<Vec<u8>>();
        let egress = move |_i: IfaceId, _p: PeerId, pkt: &OutPacket<'_>| {
            let _ = to_client_tx.send(pkt.to_vec());
            SendResult::Accepted
        };
        let (h, mut acc, ids) = spawn(StackConfig::default(), StreamConfig::default(), vec![IfaceConfig::default()], egress);
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
                c.ingress(now(), ci, PeerId(0), Bytes::from(p));
                while let Ok(p) = to_client_rx.try_recv() {
                    c.ingress(now(), ci, PeerId(0), Bytes::from(p));
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
        let (h, mut acc, ids) = spawn(cfg, StreamConfig::default(), vec![IfaceConfig::default()], egress);
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
                c.ingress(now(), ci, PeerId(0), Bytes::from(p));
                while let Ok(p) = to_client_rx.try_recv() {
                    c.ingress(now(), ci, PeerId(0), Bytes::from(p));
                }
            }
        };
        assert_eq!(reason, CloseReason::Reset);
        assert!(max_rx >= 64 * 1024, "client window never filled ({max_rx})");
    }
}