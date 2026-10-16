// Diagnostic only: two production Tokio drivers on separate OS threads share
// one GlobalBudget, with real kernel TCP clients over an owned nonpersistent TUN.
// Run ONLY in a fresh network namespace (see pareto/shared_budget_case.py).
// Host reader queues are bounded but outside the stack ledger. No WG/relay.
use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use clap::Parser;
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::sync::{mpsc, oneshot};
use zfstack::budget::{AllocationKind, GlobalBudget, MemoryHandle, MemoryLease};
use zfstack::tokio_adapter::{self, ResourceLimits, StackHandle, StackSnapshot, StreamConfig};
use zfstack::{IfaceConfig, IfaceId, OutPacket, PeerId, SendResult, StackConfig, TxCacheTrimPolicy};

#[derive(Parser, Clone, Debug)]
struct Opts {
    #[arg(long, default_value_t = 1500, value_parser = clap::value_parser!(u16).range(512..=1500))]
    mtu: u16,
    #[arg(long, default_value_t = 1, value_parser = clap::value_parser!(u64).range(1..=8))]
    global_mib: u64,
    #[arg(long, default_value_t = 3, value_parser = clap::value_parser!(u16).range(1..=10))]
    rounds: u16,
    #[arg(long, default_value_t = 8, value_parser = clap::value_parser!(u16).range(2..=32))]
    flows: u16,
    #[arg(long)]
    trim: bool,
    /// Bounded host packets per port, outside the stack ledger.
    #[arg(long, default_value_t = 4096, value_parser = clap::value_parser!(u32).range(1..=65536))]
    ingress_capacity: u32,
    /// Kernel TUN queue bound; zero preserves the kernel default for controls.
    #[arg(long, default_value_t = 4096, value_parser = clap::value_parser!(u32).range(0..=65536))]
    tun_queue_len: u32,
}
const DEVICE: &str = "zfmp0";
const BYTES: usize = 1 << 20;
const PEER: PeerId = PeerId(7); // Deliberately reused across distinct port instances.

fn emit(value: serde_json::Value) {
    println!("{value}");
}
fn command(args: &[&str]) -> io::Result<()> {
    let out = std::process::Command::new("ip").args(args).output()?;
    if out.status.success() {
        Ok(())
    } else {
        Err(io::Error::other(String::from_utf8_lossy(&out.stderr).into_owned()))
    }
}
fn open_tun(mtu: u16, queue_len: u32) -> io::Result<Arc<OwnedFd>> {
    #[repr(C)]
    struct IfReq {
        name: [u8; libc::IFNAMSIZ],
        flags: libc::c_short,
        pad: [u8; 22],
    }
    let raw = unsafe { libc::open(c"/dev/net/tun".as_ptr(), libc::O_RDWR | libc::O_NONBLOCK | libc::O_CLOEXEC) };
    if raw < 0 {
        return Err(io::Error::last_os_error());
    }
    let fd = Arc::new(unsafe { OwnedFd::from_raw_fd(raw) });
    let mut req = IfReq { name: [0; libc::IFNAMSIZ], flags: 0x0001 | 0x1000, pad: [0; 22] };
    req.name[..DEVICE.len()].copy_from_slice(DEVICE.as_bytes());
    if unsafe { libc::ioctl(raw, 0x4004_54ca as libc::c_ulong, &mut req) } < 0 {
        return Err(io::Error::last_os_error());
    }
    command(&["addr", "add", "10.222.0.1/32", "dev", DEVICE])?;
    command(&["link", "set", DEVICE, "mtu", &mtu.to_string(), "up"])?;
    command(&["route", "add", "10.223.0.0/16", "dev", DEVICE])?;
    if queue_len != 0 {
        command(&["link", "set", DEVICE, "txqueuelen", &queue_len.to_string()])?;
    }
    Ok(fd)
}
fn classify(packet: &[u8]) -> Option<usize> {
    let ip = zfstack::wire::parse_ip(packet).ok()?;
    let std::net::IpAddr::V4(dst) = ip.dst else { return None };
    let dst = dst.octets();
    (dst[0..2] == [10, 223] && dst[2] < 2 && dst[3] == 2).then_some(dst[2] as usize)
}
fn send(fd: &OwnedFd, packet: &OutPacket<'_>, errors: &AtomicU64) -> SendResult {
    let parts = [packet.header, packet.payload[0], packet.payload[1]];
    let iov = parts.map(|p| libc::iovec { iov_base: p.as_ptr() as *mut _, iov_len: p.len() });
    loop {
        let n = unsafe { libc::writev(fd.as_raw_fd(), iov.as_ptr(), iov.len() as _) };
        if n == packet.len() as isize {
            return SendResult::Accepted;
        }
        if n < 0 && io::Error::last_os_error().kind() == io::ErrorKind::Interrupted {
            continue;
        }
        // A failed device write is recorded and fails this diagnostic run.
        errors.fetch_add(1, Relaxed);
        return SendResult::Full;
    }
}

#[derive(Default)]
struct Apps {
    completed: AtomicU64,
    cancelled: AtomicU64,
    stopping: AtomicBool,
    errors: Mutex<Vec<String>>,
}
struct Port {
    handle: StackHandle,
    memory: MemoryHandle,
    input: mpsc::Sender<Vec<u8>>,
    stop: Option<oneshot::Sender<()>>,
    join: Option<std::thread::JoinHandle<io::Result<()>>>,
    apps: Arc<Apps>,
}
impl Port {
    fn start(fd: Arc<OwnedFd>, global: Arc<GlobalBudget>, opts: &Opts, index: usize, device_errors: Arc<AtomicU64>) -> io::Result<Self> {
        let opts = opts.clone();
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let (stop, stopped) = oneshot::channel();
        let apps = Arc::new(Apps::default());
        let thread_apps = apps.clone();
        let join = std::thread::Builder::new().name(format!("budget-port-{index}")).spawn(move || {
            let rt = tokio::runtime::Builder::new_current_thread().enable_all().build()?;
            rt.block_on(async move {
                let (input, rx) = mpsc::channel::<Vec<u8>>(opts.ingress_capacity as usize);
                let limits = ResourceLimits { port_bytes: global.high(), peer_bytes: global.high(), peer_max_connections: 128 };
                let (handle, mut acceptor, _, task, memory) = tokio_adapter::spawn_with_source_factory(
                    StackConfig::default(),
                    StreamConfig { rx_cap: 65536, tx_cap: 65536, tx_low_watermark: 16384 },
                    vec![IfaceConfig { mtu: opts.mtu }],
                    move |shard| {
                        if opts.trim {
                            shard
                                .set_tx_cache_trim(Some(TxCacheTrimPolicy {
                                    check_interval: Duration::from_secs(1),
                                    warm_after: Duration::from_secs(3),
                                    cold_after: Duration::from_secs(10),
                                    warm_large: 2,
                                    warm_small: 2,
                                }))
                                .unwrap();
                        }
                        let sink = move |_: IfaceId, peer: PeerId, packet: &OutPacket<'_>| {
                            assert_eq!(peer, PEER);
                            send(&fd, packet, &device_errors)
                        };
                        (sink, shard.memory_handle(PEER))
                    },
                    global,
                    limits,
                    rx,
                    |shard, now, packet| shard.ingress_borrowed(now, IfaceId(0), PEER, &packet),
                );
                ready_tx.send((handle, memory, input)).map_err(|_| io::Error::other("controller gone"))?;
                let mut workers = tokio::task::JoinSet::new();
                let accept = tokio::spawn(async move {
                    while let Some(mut stream) = acceptor.accept().await {
                        let apps = thread_apps.clone();
                        workers.spawn(async move {
                            let result = async {
                                let mut salt = [0];
                                stream.read_exact(&mut salt).await?;
                                stream.write_all(b"R").await?;
                                stream.flush().await?;
                                let mut go = [0];
                                stream.read_exact(&mut go).await?;
                                if go != *b"G" {
                                    return Err(io::Error::other("bad request"));
                                }
                                let mut buf = [0; 16384];
                                for offset in (0..BYTES).step_by(buf.len()) {
                                    for (i, byte) in buf.iter_mut().enumerate() {
                                        *byte = ((offset + i + salt[0] as usize) % 251) as u8;
                                    }
                                    stream.write_all(&buf).await?;
                                }
                                stream.shutdown().await
                            }
                            .await;
                            match result {
                                Ok(()) => {
                                    apps.completed.fetch_add(1, Relaxed);
                                }
                                Err(_) if apps.stopping.load(Relaxed) => {
                                    apps.cancelled.fetch_add(1, Relaxed);
                                }
                                Err(error) => apps.errors.lock().unwrap().push(error.to_string()),
                            }
                        });
                    }
                    while let Some(result) = workers.join_next().await {
                        result.map_err(io::Error::other)?;
                    }
                    Ok::<_, io::Error>(())
                });
                let _ = stopped.await;
                task.shutdown_and_join().await.map_err(io::Error::other)?;
                tokio::time::timeout(Duration::from_secs(3), accept).await.map_err(io::Error::other)??
            })
        })?;
        let (handle, memory, input) = ready_rx.recv_timeout(Duration::from_secs(5)).map_err(io::Error::other)?;
        Ok(Self { handle, memory, input, stop: Some(stop), join: Some(join), apps })
    }
    fn stop(&mut self) -> io::Result<()> {
        self.apps.stopping.store(true, Relaxed);
        if let Some(stop) = self.stop.take() {
            let _ = stop.send(());
        }
        if let Some(join) = self.join.take() {
            join.join().map_err(|_| io::Error::other("port thread panicked"))??;
        }
        let errors = self.apps.errors.lock().unwrap();
        if !errors.is_empty() {
            return Err(io::Error::other(format!("application errors: {errors:?}")));
        }
        Ok(())
    }
}
impl Drop for Port {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}
#[derive(Default)]
struct ReaderStats {
    received: [u64; 2],
    drops: [u64; 2],
    ack_only_drops: [u64; 2],
    dropped_bytes: [u64; 2],
    closed: [u64; 2],
    high_water: [usize; 2],
}
impl ReaderStats {
    fn record_drop(&mut self, port: usize, packet: &[u8]) {
        self.drops[port] += 1;
        self.dropped_bytes[port] += packet.len() as u64;
        if let Ok(ip) = zfstack::wire::parse_ip(packet) {
            if let Ok(tcp) = zfstack::wire::parse_tcp(packet, &ip) {
                if tcp.flags == 0x10 && tcp.data_off == ip.l4_len {
                    self.ack_only_drops[port] += 1;
                }
            }
        }
    }
}
struct Reader {
    stop: Arc<AtomicBool>,
    join: Option<std::thread::JoinHandle<ReaderStats>>,
}
impl Reader {
    fn start(fd: Arc<OwnedFd>, ports: &[Port; 2]) -> Self {
        let senders = [ports[0].input.clone(), ports[1].input.clone()];
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let join = std::thread::spawn(move || {
            let mut stats = ReaderStats::default();
            let mut buf = [0; 2048];
            while !stopping.load(Relaxed) {
                let mut pfd = libc::pollfd { fd: fd.as_raw_fd(), events: libc::POLLIN, revents: 0 };
                if unsafe { libc::poll(&mut pfd, 1, 20) } <= 0 {
                    continue;
                }
                let count = unsafe { libc::read(fd.as_raw_fd(), buf.as_mut_ptr() as *mut _, buf.len()) };
                if count <= 0 {
                    continue;
                }
                if let Some(port) = classify(&buf[..count as usize]) {
                    stats.received[port] += 1;
                    // Never block both ports behind one saturated/closed driver.
                    // Overload is measured and rejected by the acceptance runner.
                    match senders[port].try_send(buf[..count as usize].to_vec()) {
                        Ok(()) => {
                            let occupied = senders[port].max_capacity() - senders[port].capacity();
                            stats.high_water[port] = stats.high_water[port].max(occupied);
                        }
                        Err(mpsc::error::TrySendError::Full(packet)) => {
                            stats.high_water[port] = senders[port].max_capacity();
                            stats.record_drop(port, &packet);
                        }
                        Err(mpsc::error::TrySendError::Closed(_)) => stats.closed[port] += 1,
                    }
                }
            }
            stats
        });
        Self { stop, join: Some(join) }
    }
    fn stop(&mut self) -> ReaderStats {
        self.stop.store(true, Relaxed);
        self.join.take().map(|join| join.join().expect("reader panic")).unwrap_or_default()
    }
}
impl Drop for Reader {
    fn drop(&mut self) {
        self.stop();
    }
}
fn tun_tx_dropped() -> io::Result<u64> {
    std::fs::read_to_string(format!("/sys/class/net/{DEVICE}/statistics/tx_dropped"))?.trim().parse().map_err(io::Error::other)
}
struct Client {
    bytes: Arc<AtomicU64>,
    socket: TcpStream,
    join: Option<std::thread::JoinHandle<io::Result<()>>>,
}
impl Client {
    fn prepare(port: usize, salt: u8) -> io::Result<(TcpStream, u8)> {
        let address = format!("10.223.{port}.2:5201").parse().unwrap();
        let mut socket = TcpStream::connect_timeout(&address, Duration::from_secs(5))?;
        socket.set_nodelay(true)?;
        socket.set_read_timeout(Some(Duration::from_secs(5)))?;
        socket.set_write_timeout(Some(Duration::from_secs(5)))?;
        socket.write_all(&[salt])?;
        let mut ready = [0];
        socket.read_exact(&mut ready)?;
        assert_eq!(ready, *b"R");
        Ok((socket, salt))
    }
    fn start((mut socket, salt): (TcpStream, u8)) -> io::Result<Self> {
        let close = socket.try_clone()?;
        let bytes = Arc::new(AtomicU64::new(0));
        let progress = bytes.clone();
        let join = std::thread::spawn(move || {
            socket.write_all(b"G")?;
            let mut buf = [0; 32768];
            let mut offset = 0;
            loop {
                let count = socket.read(&mut buf)?;
                if count == 0 {
                    break;
                }
                for (i, byte) in buf[..count].iter().enumerate() {
                    if *byte != ((offset + i + salt as usize) % 251) as u8 {
                        return Err(io::Error::other("payload corruption"));
                    }
                }
                offset += count;
                progress.store(offset as u64, Relaxed);
            }
            if offset != BYTES {
                return Err(io::Error::other(format!("short response: {offset}")));
            }
            Ok(())
        });
        Ok(Self { bytes, socket: close, join: Some(join) })
    }
    fn finish(mut self) -> io::Result<()> {
        self.join.take().unwrap().join().map_err(|_| io::Error::other("client panic"))?
    }
}
impl Drop for Client {
    fn drop(&mut self) {
        let _ = self.socket.shutdown(Shutdown::Both);
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
    }
}
async fn sample(port: &Port, round: u16, index: usize, phase: &str) -> StackSnapshot {
    let snap = tokio::time::timeout(Duration::from_secs(2), port.handle.snapshot()).await.unwrap().unwrap();
    emit(json!({"type":"sample","round":round,"port":index,"phase":phase,
        "port_bytes":snap.port_physical_bytes,"peer_bytes":port.memory.peer_physical_used(),
        "global_bytes":snap.global_reserved_bytes,"global_cache_bytes":snap.global_cached_bytes,
        "failures":snap.failures_by_level,"waiters":format!("{:?}",snap.waiters),
        "tx_waiters":snap.waiters.tx_block + snap.waiters.adapter_tx,
        "core_waiting":snap.budget_waiting,"record_waiters":snap.record_waiters,
        "active":snap.active_connections,"streams":snap.adapter_streams,"driver_runs":snap.stats.runs}));
    snap
}
// Explicit fault-injection host buffers, not claimed as TCP payload traffic.
// Each buffer is backed by a real allocation and a port-A TxBlock-tier lease.
async fn fill(memory: &MemoryHandle) -> Vec<(Box<[u8]>, MemoryLease)> {
    let mut held = Vec::new();
    for _ in 0..3 {
        for size in [65536, 4096, 256] {
            while let Some(lease) = memory.try_allocate_kind(size, AllocationKind::TxBlock) {
                held.push((vec![0u8; size as usize].into_boxed_slice(), lease));
            }
        }
        tokio::time::sleep(Duration::from_millis(30)).await;
    }
    held
}
async fn parked(port: &Port, client: &Client, round: u16, phase: &str) -> u64 {
    let deadline = Instant::now() + Duration::from_secs(4);
    loop {
        assert!(Instant::now() < deadline, "did not observe a real parked writer");
        assert!(!client.join.as_ref().unwrap().is_finished(), "client exited before release/cancellation");
        let snap = sample(port, round, 1, phase).await;
        let before = client.bytes.load(Relaxed);
        if snap.waiters.tx_block + snap.waiters.adapter_tx > 0 || snap.budget_waiting || snap.record_waiters > 0 {
            // No snapshots or control requests in this window: they wake drivers.
            tokio::time::sleep(Duration::from_millis(300)).await;
            if before == client.bytes.load(Relaxed) && before < BYTES as u64 {
                assert!(!client.join.as_ref().unwrap().is_finished(), "client exited during held interval");
                emit(json!({"type":"held","round":round,"phase":phase,"bytes":before,"quiet_ms":300}));
                return before;
            }
        }
        tokio::time::sleep(Duration::from_millis(25)).await;
    }
}

#[tokio::main(flavor = "current_thread")]
async fn main() -> io::Result<()> {
    let opts = Opts::parse();
    let fd = open_tun(opts.mtu, opts.tun_queue_len)?;
    let global = GlobalBudget::new(opts.global_mib << 20);
    emit(json!({"type":"config","mtu":opts.mtu,"global_bytes":global.high(),"port_bytes":global.high(),
        "peer_bytes":global.high(),"flows_per_port":opts.flows,"rounds":opts.rounds,"trim":opts.trim,
        "tun_queue_len":std::fs::read_to_string(format!("/sys/class/net/{DEVICE}/tx_queue_len"))?.trim().parse::<u32>().map_err(io::Error::other)?,
        "ingress_capacity":opts.ingress_capacity,"bytes_per_flow":BYTES,"peer_id_on_both_ports":7,"scope":"two independent Tokio driver OS threads; shared GlobalBudget; kernel TCP over one TUN; borrowed ingress; no WG"}));
    for round in 0..opts.rounds {
        let errors = Arc::new(AtomicU64::new(0));
        let mut ports =
            [Port::start(fd.clone(), global.clone(), &opts, 0, errors.clone())?, Port::start(fd.clone(), global.clone(), &opts, 1, errors.clone())?];
        let kernel_drops_before = tun_tx_dropped()?;
        let mut reader = Reader::start(fd.clone(), &ports);
        let mut ready = Vec::new();
        for flow in 0..opts.flows {
            for port in 0..2 {
                ready.push(Client::prepare(port, (flow * 2 + port as u16) as u8)?);
            }
        }
        let clients = ready.into_iter().map(Client::start).collect::<io::Result<Vec<_>>>()?;
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut global_failures = [0; 2];
        while clients.iter().any(|client| !client.join.as_ref().unwrap().is_finished()) {
            assert!(Instant::now() < deadline, "natural traffic stalled");
            for (index, port) in ports.iter().enumerate() {
                let snap = sample(port, round, index, "natural").await;
                assert!(snap.global_reserved_bytes <= global.high() && snap.port_physical_bytes <= global.high());
                global_failures[index] = snap.failures_by_level[0];
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        for client in clients {
            client.finish()?;
        }
        assert!(global_failures.iter().sum::<u64>() > 0, "natural workload did not reach global pressure");
        emit(json!({"type":"natural_done","round":round,"verified_bytes":2 * opts.flows as usize * BYTES,"global_failures":global_failures,
            "tun_tx_dropped":tun_tx_dropped()? - kernel_drops_before}));

        let ready = Client::prepare(1, 199)?;
        let held = fill(&ports[0].memory).await;
        let held_bytes: usize = held.iter().map(|(buf, _)| buf.len()).sum();
        let client = Client::start(ready)?;
        let before = parked(&ports[1], &client, round, "release").await;
        let epoch = global.release_epoch();
        let release = Instant::now();
        drop(held);
        // Do not request snapshots, send packets, or notify either driver here.
        // Only the ordinary release wake and subsequent TCP traffic may progress.
        while client.bytes.load(Relaxed) == before && release.elapsed() < Duration::from_secs(2) {
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        let wake_us = release.elapsed().as_micros();
        assert!(client.bytes.load(Relaxed) > before, "cross-port release did not resume the writer");
        client.finish()?;
        emit(json!({"type":"release_done","round":round,"injected_host_bytes":held_bytes,"before_bytes":before,
            "verified_bytes":BYTES,"wake_observed_us":wake_us,"epoch_before":epoch,"epoch_after":global.release_epoch()}));

        let ready = Client::prepare(1, 211)?;
        let held = fill(&ports[0].memory).await;
        let client = Client::start(ready)?;
        let before = parked(&ports[1], &client, round, "cancel").await;
        assert!(ports[1].apps.errors.lock().unwrap().is_empty());
        ports[1].stop()?;
        assert!(ports[1].apps.cancelled.load(Relaxed) > 0, "pending application writer was not woken by driver shutdown");
        drop(client); // Controller closes the kernel socket; shutdown is not a remote-RST guarantee.
        assert_eq!(ports[1].memory.peer_physical_used(), 0);
        let remaining_a = ports[0].memory.peer_physical_used();
        assert!(remaining_a > 0, "port A buffers must survive cancellation of the same peer id on B");
        drop(held);
        ports[0].stop()?;
        let ingress = reader.stop(); // Join before sampling: no late counter race.
        let ingress_queue_drops: u64 = ingress.drops.iter().sum();
        let kernel_drops = tun_tx_dropped()? - kernel_drops_before;
        assert_eq!(errors.load(Relaxed), 0, "TUN writes failed");
        let counts = global.connection_counts();
        assert_eq!((global.reserved(), global.cached_bytes(), counts, global.time_wait_bytes_reserved()), (0, 0, (0, 0), 0));
        emit(json!({"type":"round_done","round":round,"cancelled_after_bytes":before,"cancelled_app_writers":ports[1].apps.cancelled.load(Relaxed),
            "ingress_queue_drops":ingress_queue_drops,"ingress_received":ingress.received,
            "ingress_drops_by_port":ingress.drops,"ingress_ack_only_drops":ingress.ack_only_drops,
            "ingress_dropped_bytes":ingress.dropped_bytes,"ingress_closed":ingress.closed,
            "ingress_high_water":ingress.high_water,"tun_tx_dropped":kernel_drops,"peer_b_after_cancel":0,"peer_a_survived_bytes":remaining_a,"global_reserved":global.reserved(),
            "global_cached":global.cached_bytes(),"connection_counts":counts,"time_wait_bytes":global.time_wait_bytes_reserved()}));
    }
    drop(fd);
    emit(json!({"type":"summary","ok":true,"rounds":opts.rounds,"ledger_zero":global.reserved()==0}));
    Ok(())
}
