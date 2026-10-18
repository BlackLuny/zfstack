//! A0 has no OS I/O. A1 deliberately uses real time and loopback sockets.
//! Every added test compiles only inside the existing cfg(test) module.

use super::*;
use std::future::Future;
use std::time::Duration;
use tokio::io::AsyncReadExt;

/// Real core handshake, then an unspawned production driver. Preparation may
/// pump; the unique-release interval below must never service the driver.
fn prepared_stream() -> (TestRelayDriver, TcpStream) {
    let mut sim = crate::sim::Sim::new(0xA000, StackConfig::default(), StackConfig::default(), Default::default(), Default::default());
    sim.a.shard.set_budget_limits(1 << 20, 1 << 20, 16);
    sim.a.shard.reserve_stream_state(STREAM_STATE_BYTES);
    sim.connect(44000, Default::default());
    sim.run_until(sim.now + Duration::from_millis(100));
    let id = sim.a.accepted[0];
    while sim.a.shard.poll_event().is_some() {}
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
        epoch: tokio::time::Instant::now() - Duration::from_nanos(sim.now.as_nanos()),
        closes: CloseCounts::default(),
        aborts: AbortCounts::default(),
        relays: HashMap::new(),
    };
    driver.on_event(Event::Accepted(id));
    (driver, accept_rx.try_recv().unwrap())
}

struct WakeGate {
    delivered: AtomicUsize,
    suppress: bool,
}
impl Wake for WakeGate {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }
    fn wake_by_ref(self: &Arc<Self>) {
        if !self.suppress {
            self.delivered.fetch_add(1, Ordering::Relaxed);
        }
    }
}

/// The executor may repoll only if the registered task became ready. Returning
/// false under the dropped-wake mutation proves that no test-side rescue poll
/// manufactures progress. Completion here means accepted into the adapter TX
/// queue; end-to-end delivery is covered by A1, not claimed by this A0 oracle.
fn only_release(suppress_wake: bool, release_during_attempt: bool) -> bool {
    let (driver, mut stream) = prepared_stream();
    let global = stream.sh.memory.global().clone();
    let mut cached = stream.sh.memory.try_allocate_kind(65536, AllocationKind::TxBlock).unwrap();
    assert!(cached.park_cached());
    let ceiling = (1 << 20) - global.headroom(1 << 20, crate::budget::Tier::Block);
    let held = stream.sh.memory.try_allocate_kind(ceiling - driver.shard.budget().physical_used(), AllocationKind::TxBlock).unwrap();
    let reclaim = Arc::new(ReleaseOnReclaim(Mutex::new(Some(held))));
    let holder = global.new_waiter_id();
    if release_during_attempt {
        global.register_cache_holder(holder, &Waker::from(reclaim.clone()));
    }
    let gate = Arc::new(WakeGate { delivered: AtomicUsize::new(0), suppress: suppress_wake });
    let wake = Waker::from(gate.clone());
    let mut cx = Context::from_waker(&wake);
    assert_eq!(driver.shard.tx_queued(stream.sh.id), Some(0), "no in-flight ACK can rescue this write");
    assert!(Pin::new(&mut stream).poll_write(&mut cx, b"unique release").is_pending());
    assert!(stream.sh.memory_waiting.load(Ordering::Acquire));
    assert!(stream.sh.q.lock().unwrap().tx.is_empty(), "the failing attempt must not commit data");
    if !release_during_attempt {
        assert_eq!(gate.delivered.load(Ordering::Relaxed), 0);
        assert_eq!(driver.shard.budget().physical_waiters().adapter_tx, 1);
        drop(reclaim.0.lock().unwrap().take());
    }
    assert!(reclaim.0.lock().unwrap().is_none(), "the only release happened at the selected boundary");
    // Querying capacity does not allocate or notify. Prove the release meets
    // both descriptor and payload charges, rather than releasing one token.
    let charge = (8 * std::mem::size_of::<TxChunk>() * 2 + 128) as u64;
    assert!(driver.shard.budget().can_allocate(stream.sh.peer, charge, AllocationKind::AdapterTx));
    let completed = gate.delivered.load(Ordering::Relaxed) > 0 && matches!(Pin::new(&mut stream).poll_write(&mut cx, b"unique release"), Poll::Ready(Ok(14)));
    if completed {
        assert_eq!(stream.sh.q.lock().unwrap().tx.front(), b"unique release");
        assert!(!stream.sh.memory_waiting.load(Ordering::Acquire));
    }
    global.remove_cache_holder(holder);
    drop((stream, driver, cached, reclaim));
    assert_eq!(global.reserved(), 0);
    completed
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn a0_only_release_before_and_after_registration() {
    for during_attempt in [false, true] {
        assert!(only_release(false, during_attempt));
    }
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn a0_negative_control_dropped_wake_cannot_pass_by_rescue_poll() {
    for during_attempt in [false, true] {
        assert!(!only_release(true, during_attempt), "mutation unexpectedly passed the notification oracle");
    }
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn a0_memory_waiter_migration_and_cancel_remove_old_notification() {
    for cancel in [false, true] {
        let (mut driver, mut stream) = prepared_stream();
        let global = stream.sh.memory.global().clone();
        let ceiling = (1 << 20) - global.headroom(1 << 20, crate::budget::Tier::Block);
        let held = stream.sh.memory.try_allocate_kind(ceiling - driver.shard.budget().physical_used(), AllocationKind::TxBlock).unwrap();
        let old = Arc::new(CountWake(AtomicUsize::new(0)));
        let latest = Arc::new(CountWake(AtomicUsize::new(0)));
        for counter in [old.clone(), latest.clone()] {
            let wake = Waker::from(counter);
            assert!(Pin::new(&mut stream).poll_write(&mut Context::from_waker(&wake), b"x").is_pending());
        }
        assert_eq!(driver.shard.budget().physical_waiters().adapter_tx, 1);
        let mut remaining = Some(stream);
        if cancel {
            let id = remaining.as_ref().unwrap().sh.id;
            drop(remaining.take());
            driver.service(); // consumes the real drop mark, before release
            assert!(!driver.streams.contains_key(&id));
            assert_eq!(driver.shard.budget().physical_waiters().adapter_tx, 0);
        }
        let before_release = latest.0.load(Ordering::Relaxed);
        if !cancel {
            assert_eq!(before_release, 0, "still-blocked writer was spuriously notified");
        }
        drop(held);
        assert_eq!(old.0.load(Ordering::Relaxed), 0, "superseded task was notified");
        assert_eq!(latest.0.load(Ordering::Relaxed), before_release + usize::from(!cancel), "canceled waiter received a late budget wake");
        drop((remaining, driver));
        assert_eq!(global.reserved(), 0);
    }
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn a0_driver_deadline_reclaims_idle_cache_on_virtual_time_only() {
    let global = GlobalBudget::new(16 << 20);
    let (input_tx, input_rx) = mpsc::channel::<()>(1);
    let high = global.high();
    let (handle, acceptor, _, task, ()) = spawn_with_source_factory(
        StackConfig::default(),
        StreamConfig::default(),
        vec![IfaceConfig::default()],
        |shard| {
            shard
                .set_tx_cache_trim(Some(crate::TxCacheTrimPolicy {
                    check_interval: Duration::from_secs(1),
                    warm_after: Duration::from_secs(2),
                    cold_after: Duration::from_secs(4),
                    warm_large: 1,
                    warm_small: 0,
                }))
                .unwrap();
            shard.prime_tx_cache_for_test(4);
            ((|_, _, _: &OutPacket<'_>| SendResult::Accepted) as fn(IfaceId, PeerId, &OutPacket<'_>) -> SendResult, ())
        },
        global.clone(),
        ResourceLimits { port_bytes: high, peer_bytes: high, peer_max_connections: 4 },
        input_rx,
        |_, _, ()| {},
    );
    let start = tokio::time::Instant::now();
    let initial = global.cached_bytes();
    assert!(initial > 0);
    tokio::task::yield_now().await; // owner arms its first timer
    tokio::time::advance(Duration::from_millis(999)).await;
    tokio::task::yield_now().await;
    assert_eq!(global.cached_bytes(), initial, "maintenance fired early");
    // Step across deadlines separately; advance() is not a timer-order oracle.
    for _ in 0..6 {
        tokio::time::advance(Duration::from_secs(1)).await;
        tokio::task::yield_now().await;
    }
    assert!(tokio::time::Instant::now() >= start + Duration::from_secs(4));
    assert_eq!(global.cached_bytes(), 0, "no snapshot, ingress or manual service can rescue the maintenance timer");
    task.shutdown_and_join().await.unwrap();
    drop((handle, acceptor, input_tx));
    assert_eq!(global.reserved(), 0);
}

/// An independent wall-clock watchdog bounds the entire runtime, including
/// timer starvation. Normal and async-timeout paths shut down and join it.
fn on_runtime(workers: usize, body: impl Future<Output = ()> + Send + 'static) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    let owner = std::thread::spawn(move || {
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut builder = if workers == 0 {
                tokio::runtime::Builder::new_current_thread()
            } else {
                let mut b = tokio::runtime::Builder::new_multi_thread();
                b.worker_threads(workers);
                b
            };
            let runtime = builder.enable_all().build().unwrap();
            let completed = runtime.block_on(async { tokio::time::timeout(Duration::from_secs(15), body).await });
            runtime.shutdown_timeout(Duration::from_secs(2));
            completed.expect("A1 runtime scenario exceeded its deadline");
        }));
        tx.send(result).unwrap();
    });
    let result = rx.recv_timeout(Duration::from_secs(20)).expect("A1 independent wall-clock watchdog expired");
    owner.join().unwrap();
    if let Err(panic) = result {
        std::panic::resume_unwind(panic);
    }
}

fn byte(salt: u8, index: usize) -> u8 {
    pattern_byte(index as u64).wrapping_add(salt)
}

#[cfg(target_os = "linux")]
fn socket_identity(socket: &impl std::os::fd::AsRawFd) -> std::path::PathBuf {
    std::fs::read_link(format!("/proc/self/fd/{}", socket.as_raw_fd())).unwrap()
}

async fn loopback_lane(global: Arc<GlobalBudget>, barrier: Arc<tokio::sync::Barrier>, salt: u8) -> (Bytes, Vec<std::path::PathBuf>) {
    const TOTAL: usize = 32 * 1024 + 37;
    let mut sockets = Vec::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    #[cfg(target_os = "linux")]
    sockets.push(socket_identity(&listener));
    let upstream = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        #[cfg(target_os = "linux")]
        let identity = Some(socket_identity(&socket));
        #[cfg(not(target_os = "linux"))]
        let identity: Option<std::path::PathBuf> = None;
        let mut upload = Vec::new();
        socket.read_to_end(&mut upload).await.unwrap();
        assert_eq!(upload.len(), TOTAL);
        for (i, b) in upload.iter().enumerate() {
            assert_eq!(*b, byte(salt, i), "cross-stream upload at {i}");
        }
        socket.write_all(&upload).await.unwrap();
        socket.shutdown().await.unwrap();
        identity
    });
    let (to_peer, from_stack) = mpsc::unbounded_channel();
    let (input_tx, input_rx) = mpsc::channel::<()>(1);
    let (handle, mut acceptor, ifaces, task, memory) = spawn_with_source_factory(
        StackConfig::default(),
        StreamConfig::default(),
        vec![IfaceConfig::default()],
        move |shard| {
            let memory = shard.memory_handle(PeerId(9)); // same PeerId on separate shards
            let egress = move |_: IfaceId, _: PeerId, packet: &OutPacket<'_>| {
                to_peer.send(packet.to_vec()).unwrap();
                SendResult::Accepted
            };
            (egress, memory)
        },
        global,
        ResourceLimits { port_bytes: 4 << 20, peer_bytes: 2 << 20, peer_max_connections: 4 },
        input_rx,
        |_, _, ()| {},
    );
    let relay = tokio::spawn(async move {
        let stream = acceptor.accept().await.unwrap();
        let socket = tokio::net::TcpStream::connect(address).await.unwrap();
        #[cfg(target_os = "linux")]
        let identity = Some(socket_identity(&socket));
        #[cfg(not(target_os = "linux"))]
        let identity: Option<std::path::PathBuf> = None;
        (stream.splice(socket).await.unwrap(), identity)
    });
    let mut peer = TunPeer::new(1500, from_stack);
    let id = peer.c.connect(peer.now(), peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
    let payload: Vec<_> = (0..TOTAL).map(|i| byte(salt, i)).collect();
    let (mut sent, mut received, mut fin) = (0, Vec::new(), false);
    let mut buffer = [0; 4096];
    barrier.wait().await;
    loop {
        peer.step(&handle, ifaces[0]).await;
        while peer.c.poll_event().is_some() {}
        if sent < TOTAL {
            if let WriteResult::Written(n) = peer.c.write(id, &payload[sent..]) {
                sent += n;
            }
        }
        if sent == TOTAL && !fin {
            peer.c.shutdown_write(id);
            fin = true;
        }
        match peer.c.read(peer.now(), id, &mut buffer) {
            ReadResult::Data(n) => received.extend_from_slice(&buffer[..n]),
            ReadResult::Eof => break,
            ReadResult::WouldBlock => {}
            ReadResult::Closed(reason) => panic!("lane {salt} closed: {reason:?}"),
        }
    }
    assert_eq!(received, payload, "cross-shard response mix-up");
    while !relay.is_finished() {
        peer.step(&handle, ifaces[0]).await;
    }
    let (stats, identity) = relay.await.unwrap();
    sockets.extend(identity);
    sockets.extend(upstream.await.unwrap());
    assert_eq!(stats, SpliceStats { uploaded: TOTAL as u64, downloaded: TOTAL as u64 });
    // External packet slices legitimately retain their physical lease across
    // driver cancellation and across a task/thread boundary.
    let pool = crate::pktpool::PacketPool::new(memory.clone(), 4096, 0);
    let mut packet = pool.get().unwrap();
    for slot in &mut packet.spare_capacity_mut()[..16] {
        slot.write(salt);
    }
    // SAFETY: all 16 bytes above were initialized, within the packet capacity.
    unsafe {
        packet.set_len(16);
    }
    let owner = Bytes::from_owner(packet);
    let fragment = owner.slice(3..13);
    drop((owner, pool));
    task.shutdown_and_join().await.unwrap();
    assert!(handle.snapshot().await.is_none());
    assert!(input_tx.is_closed());
    assert_eq!(memory.peer_physical_used(), 4096, "only the external owner may remain");
    drop((memory, handle, input_tx, peer));
    (fragment, sockets)
}

async fn concurrent_lifecycle() {
    let global = GlobalBudget::new(32 << 20);
    let barrier = Arc::new(tokio::sync::Barrier::new(4));
    let mut tasks = Vec::new();
    for salt in [3, 67, 131, 197] {
        tasks.push(tokio::spawn(loopback_lane(global.clone(), barrier.clone(), salt)));
    }
    let mut owners = Vec::new();
    let mut sockets = Vec::new();
    for task in tasks {
        let (owner, lane_sockets) = task.await.unwrap();
        owners.push(owner);
        sockets.extend(lane_sockets);
    }
    assert_eq!(global.connection_counts(), (0, 0));
    assert_eq!(global.reserved(), 4 * 4096, "driver state must converge while external owners remain");
    // The final owner release occurs on a separate native thread.
    std::thread::spawn(move || drop(owners)).join().unwrap();
    assert_eq!(global.reserved(), 0);
    #[cfg(target_os = "linux")]
    {
        let live: Vec<_> = std::fs::read_dir("/proc/self/fd").unwrap().filter_map(|f| std::fs::read_link(f.ok()?.path()).ok()).collect();
        for socket in sockets {
            assert!(!live.contains(&socket), "owned socket descriptor leaked: {socket:?}");
        }
    }
}

#[test]
fn a1_loopback_current_thread_shutdown_and_last_owner() {
    on_runtime(0, concurrent_lifecycle());
}
#[test]
fn a1_loopback_two_workers_shutdown_and_last_owner() {
    on_runtime(2, concurrent_lifecycle());
}
#[test]
fn a1_loopback_four_workers_shutdown_and_last_owner() {
    on_runtime(4, concurrent_lifecycle());
}

async fn pending_cancel() {
    for cancel_writer in [false, true] {
        let global = GlobalBudget::new(16 << 20);
        let (tx, rx) = mpsc::unbounded_channel();
        let (input_tx, input_rx) = mpsc::channel::<()>(1);
        let (handle, mut acceptor, ifaces, task, memory) = spawn_with_source_factory(
            StackConfig::default(),
            StreamConfig::default(),
            vec![IfaceConfig::default()],
            move |shard| {
                let memory = shard.memory_handle(PeerId(9));
                let egress = move |_: IfaceId, _: PeerId, packet: &OutPacket<'_>| {
                    tx.send(packet.to_vec()).unwrap();
                    SendResult::Accepted
                };
                (egress, memory)
            },
            global.clone(),
            ResourceLimits { port_bytes: 1 << 20, peer_bytes: 1 << 20, peer_max_connections: 4 },
            input_rx,
            |_, _, ()| {},
        );
        let mut peer = TunPeer::new(1500, rx);
        peer.c.connect(peer.now(), peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        let stream = loop {
            peer.step(&handle, ifaces[0]).await;
            if let Ok(stream) = acceptor.rx.try_recv() {
                break stream;
            }
        };
        let shared = stream.sh.clone();
        let weak = Arc::downgrade(&shared);
        let used = handle.snapshot().await.unwrap().port_physical_bytes;
        let ceiling = (1 << 20) - global.headroom(1 << 20, crate::budget::Tier::Block);
        let held_bytes = ceiling - used;
        let held = memory.try_allocate_kind(held_bytes, AllocationKind::TxBlock).unwrap();
        let (mut read, mut write) = tokio::io::split(stream);
        let reader = tokio::spawn(async move { read.read_u8().await });
        let writer = tokio::spawn(async move { write.write_all(b"pending").await });
        loop {
            let read_pending = shared.q.lock().unwrap().read_waker.is_some();
            if read_pending && shared.memory_waiting.load(Ordering::Acquire) {
                break;
            }
            tokio::task::yield_now().await;
        }
        assert!(shared.q.lock().unwrap().tx.is_empty());
        if cancel_writer {
            writer.abort();
            assert!(writer.await.unwrap_err().is_cancelled());
        } else {
            // A live ordinary writer must observe the driver terminal error.
            task.shutdown();
            assert_eq!(writer.await.unwrap().unwrap_err().kind(), io::ErrorKind::BrokenPipe);
        }
        task.shutdown_and_join().await.unwrap();
        assert_eq!(reader.await.unwrap().unwrap_err().kind(), io::ErrorKind::ConnectionReset);
        drop((shared, memory, handle, acceptor, input_tx, peer));
        assert!(weak.upgrade().is_none(), "pending/canceled task retained the stream");
        assert_eq!(global.reserved(), held_bytes, "only the deliberately held external lease remains");
        drop(held); // late notification must find no live stream or task
        assert_eq!(global.reserved(), 0);
        assert_eq!(global.connection_counts(), (0, 0));
    }
}

#[test]
fn a1_pending_read_write_cancel_two_workers() {
    on_runtime(2, pending_cancel());
}
#[test]
fn a1_pending_read_write_cancel_four_workers() {
    on_runtime(4, pending_cancel());
}

async fn issue48_empty_socket() -> (TestRelayDriver, TcpStream, tokio::net::TcpStream) {
    let (driver, stream, up, _peer) = parked_relay(&[]).await;
    let mut drained = [0u8; 32];
    assert_eq!(driver.relays[&stream.sh.id].sock.try_read(&mut drained).unwrap(), b"socket tail".len());
    assert_eq!(driver.relays[&stream.sh.id].sock.try_read(&mut drained).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    driver.ctl.dirty.lock().unwrap().clear();
    (driver, stream, up)
}

async fn issue48_wait_for_socket_wake(driver: &TestRelayDriver, id: ConnId) {
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if driver.ctl.dirty.lock().unwrap().contains(&id) {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("socket readiness did not wake the parked relay");
}

#[tokio::test(flavor = "current_thread")]
async fn issue48_idle_socket_skips_tx_preparation_and_wakes_on_data() {
    let (mut driver, stream, mut up) = issue48_empty_socket().await;
    let id = stream.sh.id;
    let global = stream.sh.memory.global().clone();
    let before = global.reserved();
    for _ in 0..128 {
        driver.pump(id);
    }
    assert_eq!(driver.shard.tx_queued(id), Some(0));
    assert_eq!(global.reserved(), before, "idle socket prepared TX backing");
    assert_eq!(stream.sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_NONE);
    assert_eq!(driver.shard.budget().physical_waiters().tx_block, 0);
    driver.ctl.dirty.lock().unwrap().clear();
    up.write_all(b"later response").await.unwrap();
    issue48_wait_for_socket_wake(&driver, id).await;
    driver.pump(id);
    assert_eq!(driver.shard.tx_queued(id), Some(b"later response".len()));
    assert_eq!(driver.relays[&id].stats.downloaded, b"later response".len() as u64);
    drop((stream, driver, up));
    assert_eq!(global.reserved(), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn issue48_idle_socket_clears_obsolete_quota_wait() {
    let (mut driver, stream, mut up, _peer) = parked_relay(&[]).await;
    let id = stream.sh.id;
    let global = stream.sh.memory.global().clone();
    assert!(driver.shard.budget_mut_for_test().try_reserve(stream.sh.peer, 1 << 20, false));
    driver.pump(id);
    assert_eq!(stream.sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_QUOTA);
    // Model readiness becoming stale while a budget wait is registered.
    let mut drained = [0u8; 32];
    assert_eq!(driver.relays[&id].sock.try_read(&mut drained).unwrap(), b"socket tail".len());
    assert_eq!(driver.relays[&id].sock.try_read(&mut drained).unwrap_err().kind(), io::ErrorKind::WouldBlock);
    driver.pump(id);
    assert_eq!(stream.sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_NONE);
    driver.ctl.dirty.lock().unwrap().clear();
    driver.shard.budget_mut_for_test().release(stream.sh.peer, 1 << 20);
    assert!(driver.ctl.dirty.lock().unwrap().is_empty(), "obsolete quota release woke idle socket");
    up.write_all(b"after quota").await.unwrap();
    issue48_wait_for_socket_wake(&driver, id).await;
    driver.pump(id);
    assert_eq!(driver.shard.tx_queued(id), Some(b"after quota".len()));
    drop((stream, driver, up));
    assert_eq!(global.reserved(), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn issue48_closed_core_does_not_wait_for_silent_socket() {
    let (mut driver, stream, up) = issue48_empty_socket().await;
    let id = stream.sh.id;
    let global = stream.sh.memory.global().clone();
    driver.shard.shutdown_write(id);
    driver.pump(id);
    assert!(driver.relays[&id].sock_eof, "closed core must be observed even with no socket readiness");
    assert_eq!(stream.sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_NONE);
    drop((stream, driver, up));
    assert_eq!(global.reserved(), 0);
}

#[tokio::test(flavor = "current_thread")]
async fn issue48_stale_readiness_parks_then_observes_eof() {
    let (mut driver, stream, mut up, _peer) = parked_relay(&[]).await;
    let id = stream.sh.id;
    let global = stream.sh.memory.global().clone();
    let mut drained = [0u8; 32];
    assert_eq!(driver.relays[&id].sock.try_read(&mut drained).unwrap(), b"socket tail".len());
    // Do not clear Tokio's cached readiness: the next actual read returns WouldBlock.
    driver.pump(id);
    assert_eq!(driver.shard.tx_queued(id), Some(0));
    assert!(!driver.relays[&id].sock_eof);
    assert_eq!(stream.sh.driver_tx_wait.load(Ordering::Acquire), TX_WAIT_NONE);
    driver.ctl.dirty.lock().unwrap().clear();
    up.shutdown().await.unwrap();
    issue48_wait_for_socket_wake(&driver, id).await;
    driver.pump(id);
    assert!(driver.relays[&id].sock_eof);
    drop((stream, driver, up));
    assert_eq!(global.reserved(), 0);
}

/// Leave two adapter-sized batches in the real TCP receive buffer, so a
/// single owner pump can consume the first and refill from the second.
fn issue53_prepared_rx() -> (TestRelayDriver, TcpStream) {
    let mut sim = crate::sim::Sim::new(0xA053, StackConfig::default(), StackConfig::default(), Default::default(), Default::default());
    sim.a.shard.set_budget_limits(1 << 20, 1 << 20, 16);
    sim.a.shard.reserve_stream_state(STREAM_STATE_BYTES);
    sim.a.accept_template.read_paused_until = sim.now + Duration::from_secs(1);
    sim.connect(44053, crate::sim::AppConn { to_send: 8192, ..Default::default() });
    sim.run_until(sim.now + Duration::from_millis(100));
    let id = sim.a.accepted[0];
    while sim.a.shard.poll_event().is_some() {}
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
        stream_cfg: StreamConfig { rx_cap: 4096, tx_cap: 1024, tx_low_watermark: 1 },
        epoch: tokio::time::Instant::now() - Duration::from_nanos(sim.now.as_nanos()),
        closes: CloseCounts::default(),
        aborts: AbortCounts::default(),
        relays: HashMap::new(),
    };
    driver.on_event(Event::Accepted(id));
    let stream = accept_rx.try_recv().unwrap();
    assert_eq!(stream.sh.q.lock().unwrap().rx_len, 4096);
    (driver, stream)
}

/// Synchronous callbacks probe the same queue an application poll would
/// acquire. Record contention instead of blocking or panicking under the
/// owner's guard, so a lock-boundary regression fails without a deadlock.
struct Issue53QueueWake {
    shared: std::sync::Weak<Shared>,
    calls: AtomicUsize,
    locked: AtomicUsize,
    observed: Mutex<Option<(usize, usize, usize)>>,
}

impl Issue53QueueWake {
    fn new(shared: &Arc<Shared>) -> Arc<Self> {
        Arc::new(Self { shared: Arc::downgrade(shared), calls: AtomicUsize::new(0), locked: AtomicUsize::new(0), observed: Mutex::new(None) })
    }

    fn assert_once_unlocked(&self, expected: (usize, usize, usize)) {
        assert_eq!(self.calls.load(Ordering::Relaxed), 1, "expected exactly one synchronous notification");
        assert_eq!(self.locked.load(Ordering::Relaxed), 0, "callback ran while the stream queue was locked");
        assert_eq!(*self.observed.lock().unwrap(), Some(expected), "callback observed the wrong pump phase");
    }
}

impl Wake for Issue53QueueWake {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }

    fn wake_by_ref(self: &Arc<Self>) {
        self.calls.fetch_add(1, Ordering::Relaxed);
        let Some(shared) = self.shared.upgrade() else { return };
        match shared.q.try_lock() {
            Ok(q) => *self.observed.lock().unwrap() = Some((q.rx_consumed_pending, q.rx_len, q.tx.len())),
            Err(_) => {
                self.locked.fetch_add(1, Ordering::Relaxed);
            }
        };
    }
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn a0_issue53_pump_consumes_refills_drains_and_wakes_outside_queue() {
    let (mut driver, mut stream) = issue53_prepared_rx();
    let id = stream.sh.id;
    let global = stream.sh.memory.global().clone();
    let mut first = [0u8; 4096];
    stream.read_exact(&mut first).await.unwrap();
    assert!(first.iter().enumerate().all(|(i, &b)| b == pattern_byte(i as u64)));
    stream.write_all(&[91u8; 1024]).await.unwrap();

    let read = Issue53QueueWake::new(&stream.sh);
    let write = Issue53QueueWake::new(&stream.sh);
    let flush = Issue53QueueWake::new(&stream.sh);
    let release = Issue53QueueWake::new(&stream.sh);
    let (read_wake, write_wake, flush_wake) = (Waker::from(read.clone()), Waker::from(write.clone()), Waker::from(flush.clone()));
    let mut next = [0u8; 1];
    assert!(Pin::new(&mut stream).poll_read(&mut Context::from_waker(&read_wake), &mut ReadBuf::new(&mut next)).is_pending());
    assert!(Pin::new(&mut stream).poll_write(&mut Context::from_waker(&write_wake), b"x").is_pending());
    assert!(Pin::new(&mut stream).poll_flush(&mut Context::from_waker(&flush_wake)).is_pending());
    assert_eq!(driver.shard.tx_queued(id), Some(0));
    let waiter = global.new_waiter_id();
    global.register_waiter(waiter, global.release_epoch(), &Waker::from(release.clone()));
    assert_eq!(release.calls.load(Ordering::Relaxed), 0, "no release may precede the tested pump");

    driver.pump(id);

    // The first release is the core's consume_adapter accounting, before
    // either queue is transferred. Later task wakes see the completed pump.
    release.assert_once_unlocked((0, 0, 1024));
    for wake in [&read, &write, &flush] {
        wake.assert_once_unlocked((0, 4096, 0));
    }
    assert_eq!(driver.shard.tx_queued(id), Some(1024));
    assert!(matches!(Pin::new(&mut stream).poll_flush(&mut Context::from_waker(&flush_wake)), Poll::Ready(Ok(()))));
    assert!(matches!(Pin::new(&mut stream).poll_write(&mut Context::from_waker(&write_wake), b"x"), Poll::Ready(Ok(1))));
    let mut second = [0u8; 4096];
    stream.read_exact(&mut second).await.unwrap();
    assert!(second.iter().enumerate().all(|(i, &b)| b == pattern_byte((i + first.len()) as u64)));
    driver.pump(id);
    assert_eq!(stream.sh.q.lock().unwrap().rx_consumed_pending, 0);
    assert_eq!(driver.shard.tx_queued(id), Some(1025));
    global.remove_waiter(waiter);
    drop((stream, driver));
    assert_eq!(global.reserved(), 0);
}

#[tokio::test(flavor = "current_thread", start_paused = true)]
async fn a0_issue53_dropped_unread_stream_releases_budget_outside_queue() {
    let (mut driver, stream) = issue53_prepared_rx();
    let shared = stream.sh.clone();
    let id = shared.id;
    let global = shared.memory.global().clone();
    let release = Issue53QueueWake::new(&shared);
    let waiter = global.new_waiter_id();
    global.register_waiter(waiter, global.release_epoch(), &Waker::from(release.clone()));
    drop(stream);
    assert_eq!(release.calls.load(Ordering::Relaxed), 0);
    driver.pump(id);
    release.assert_once_unlocked((0, 4096, 0));
    assert!(!driver.streams.contains_key(&id), "dropped stream was not removed");
    assert_eq!(driver.aborts.unread_drop, 1);
    global.remove_waiter(waiter);
    drop((shared, driver));
    assert_eq!(global.reserved(), 0);
}

async fn issue53_ordinary_lane(global: Arc<GlobalBudget>, barrier: Arc<tokio::sync::Barrier>, salt: u8) {
    const UPLOAD: usize = 64 * 1024 + 37;
    const PREFIX: usize = 16 * 1024 + 7;
    const DOWNLOAD: usize = 48 * 1024 + 29;
    let (to_peer, from_stack) = mpsc::unbounded_channel();
    let (input_tx, input_rx) = mpsc::channel::<()>(1);
    let (handle, mut acceptor, ifaces, task, memory) = spawn_with_source_factory(
        StackConfig::default(),
        StreamConfig { rx_cap: 4096, tx_cap: 4096, tx_low_watermark: 512 },
        vec![IfaceConfig::default()],
        move |shard| {
            let memory = shard.memory_handle(PeerId(9));
            let egress = move |_: IfaceId, _: PeerId, packet: &OutPacket<'_>| {
                to_peer.send(packet.to_vec()).unwrap();
                SendResult::Accepted
            };
            (egress, memory)
        },
        global,
        ResourceLimits { port_bytes: 2 << 20, peer_bytes: 2 << 20, peer_max_connections: 4 },
        input_rx,
        |_, _, ()| {},
    );
    let application = tokio::spawn(async move {
        let stream = acceptor.accept().await.unwrap();
        let weak = Arc::downgrade(&stream.sh);
        let (mut read, mut write) = tokio::io::split(stream);
        // Both ordinary API halves run independently from the owner and each
        // other. Small queue caps force repeated consumption and refill.
        let reader = tokio::spawn(async move {
            let mut payload = Vec::new();
            read.read_to_end(&mut payload).await.unwrap();
            assert_eq!(payload.len(), UPLOAD);
            for (i, b) in payload.iter().enumerate() {
                assert_eq!(*b, byte(salt, i), "ordinary upload lane {salt} at {i}");
            }
        });
        let writer = tokio::spawn(async move {
            let response: Vec<_> = (0..DOWNLOAD).map(|i| byte(salt ^ 0x80, i)).collect();
            for chunk in response.chunks(3073) {
                write.write_all(chunk).await.unwrap();
            }
            write.flush().await.unwrap();
            write.shutdown().await.unwrap();
        });
        let (read_result, write_result) = tokio::join!(reader, writer);
        read_result.unwrap();
        write_result.unwrap();
        weak
    });

    let mut peer = TunPeer::new(1500, from_stack);
    let id = peer.c.connect(peer.now(), peer.ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
    let payload: Vec<_> = (0..UPLOAD).map(|i| byte(salt, i)).collect();
    let (mut sent, mut received, mut fin, mut download_eof) = (0, Vec::new(), false, false);
    let mut buffer = [0u8; 2053];
    barrier.wait().await;
    loop {
        peer.step(&handle, ifaces[0]).await;
        while peer.c.poll_event().is_some() {}
        // The rest of the upload is withheld until the other direction has
        // reached EOF. Its successful delivery proves shutdown is a half-close.
        let limit = if download_eof { UPLOAD } else { PREFIX };
        if sent < limit {
            if let WriteResult::Written(n) = peer.c.write(id, &payload[sent..limit]) {
                sent += n;
            }
        }
        if sent == UPLOAD && !fin {
            peer.c.shutdown_write(id);
            fin = true;
        }
        if !download_eof {
            match peer.c.read(peer.now(), id, &mut buffer) {
                ReadResult::Data(n) => received.extend_from_slice(&buffer[..n]),
                ReadResult::Eof => {
                    assert!(!fin && sent < UPLOAD, "upload must remain open across the remote FIN");
                    download_eof = true;
                }
                ReadResult::WouldBlock => {}
                ReadResult::Closed(reason) => panic!("ordinary lane {salt} closed: {reason:?}"),
            }
        }
        if fin && download_eof && application.is_finished() {
            break;
        }
    }
    let expected: Vec<_> = (0..DOWNLOAD).map(|i| byte(salt ^ 0x80, i)).collect();
    assert_eq!(received, expected, "ordinary response lane {salt}");
    let weak = application.await.unwrap();
    task.shutdown_and_join().await.unwrap();
    assert!(weak.upgrade().is_none(), "completed ordinary stream retained its queues");
    assert!(handle.snapshot().await.is_none());
    assert!(input_tx.is_closed());
    assert_eq!(memory.peer_physical_used(), 0);
    drop((memory, handle, input_tx, peer));
}

async fn issue53_ordinary_concurrent_half_close() {
    let global = GlobalBudget::new(32 << 20);
    let barrier = Arc::new(tokio::sync::Barrier::new(4));
    let mut lanes = Vec::new();
    for salt in [5, 61, 137, 211] {
        lanes.push(tokio::spawn(issue53_ordinary_lane(global.clone(), barrier.clone(), salt)));
    }
    for lane in lanes {
        lane.await.unwrap();
    }
    assert_eq!(global.reserved(), 0);
    assert_eq!(global.connection_counts(), (0, 0));
}

#[test]
fn a1_issue53_ordinary_two_workers_bidirectional_half_close() {
    on_runtime(2, issue53_ordinary_concurrent_half_close());
}

#[test]
fn a1_issue53_ordinary_four_workers_bidirectional_half_close() {
    on_runtime(4, issue53_ordinary_concurrent_half_close());
}
