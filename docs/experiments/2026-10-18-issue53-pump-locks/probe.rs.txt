//! Temporary, unpublished overlay. Executes production Driver::pump directly.
//! The fixture adapts tests/qa.rs::prepared_stream, with a real Sim handshake.
use super::*;
use std::hint::black_box;
use std::sync::atomic::AtomicU64;
use std::time::{Duration, Instant as WallInstant};

type ProbeDriver = Driver<fn(IfaceId, PeerId, &OutPacket<'_>) -> SendResult, (), NoInput>;

#[cfg(feature = "issue53-pump-counts")]
static LOCKS: AtomicU64 = AtomicU64::new(0);
#[cfg(feature = "issue53-pump-counts")]
static WAIT_NS: AtomicU64 = AtomicU64::new(0);
#[cfg(feature = "issue53-pump-counts")]
static HOLD_NS: AtomicU64 = AtomicU64::new(0);
#[cfg(feature = "issue53-pump-counts")]
static MAX_HOLD_NS: AtomicU64 = AtomicU64::new(0);

#[cfg(feature = "issue53-pump-counts")]
pub(super) struct CountedGuard<'a> {
    inner: std::sync::MutexGuard<'a, Queues>,
    acquired: WallInstant,
}
#[cfg(feature = "issue53-pump-counts")]
impl std::ops::Deref for CountedGuard<'_> {
    type Target = Queues;
    fn deref(&self) -> &Queues { &self.inner }
}
#[cfg(feature = "issue53-pump-counts")]
impl std::ops::DerefMut for CountedGuard<'_> {
    fn deref_mut(&mut self) -> &mut Queues { &mut self.inner }
}
#[cfg(feature = "issue53-pump-counts")]
impl Drop for CountedGuard<'_> {
    fn drop(&mut self) {
        let held = self.acquired.elapsed().as_nanos() as u64;
        HOLD_NS.fetch_add(held, Ordering::Relaxed);
        MAX_HOLD_NS.fetch_max(held, Ordering::Relaxed);
    }
}
#[cfg(feature = "issue53-pump-counts")]
pub(super) fn lock_queue(sh: &Shared) -> CountedGuard<'_> {
    let before = WallInstant::now();
    let inner = sh.q.lock().unwrap();
    let acquired = WallInstant::now();
    LOCKS.fetch_add(1, Ordering::Relaxed);
    WAIT_NS.fetch_add(acquired.duration_since(before).as_nanos() as u64, Ordering::Relaxed);
    CountedGuard { inner, acquired }
}

fn reset_counts() {
    #[cfg(feature = "issue53-pump-counts")]
    for value in [&LOCKS, &WAIT_NS, &HOLD_NS, &MAX_HOLD_NS] {
        value.store(0, Ordering::Relaxed);
    }
}
fn count_json() -> String {
    #[cfg(feature = "issue53-pump-counts")]
    { format!("\"instrumented\":true,\"pump_queue_locks\":{},\"lock_wait_ns\":{},\"lock_hold_ns\":{},\"max_lock_hold_ns\":{}",
        LOCKS.load(Ordering::Relaxed), WAIT_NS.load(Ordering::Relaxed), HOLD_NS.load(Ordering::Relaxed), MAX_HOLD_NS.load(Ordering::Relaxed)) }
    #[cfg(not(feature = "issue53-pump-counts"))]
    { "\"instrumented\":false,\"pump_queue_locks\":null,\"lock_wait_ns\":null,\"lock_hold_ns\":null,\"max_lock_hold_ns\":null".into() }
}

/// One driver, N established streams, each attributed to a distinct peer.
/// Optional client bytes are received by the real TCP core while its app is paused.
fn prepared(count: usize, rx_bytes: usize) -> (ProbeDriver, Vec<TcpStream>) {
    assert!((1..=64).contains(&count));
    let mut sim = crate::sim::Sim::new(0xA000, StackConfig::default(), StackConfig::default(), Default::default(), Default::default());
    sim.a.shard.set_budget_limits(64 << 20, 1 << 20, 16);
    sim.a.shard.reserve_stream_state(STREAM_STATE_BYTES);
    sim.a.accept_template.read_paused_until = crate::Instant::MAX;
    for index in 0..count {
        sim.connect_as(PeerId(index as u64 + 1), 44000 + index as u16,
            crate::sim::AppConn { to_send: rx_bytes as u64, ..Default::default() });
    }
    sim.run_until(sim.now + Duration::from_millis(100));
    assert_eq!(sim.a.accepted.len(), count);
    let ids = sim.a.accepted.clone();
    while sim.a.shard.poll_event().is_some() {}
    let global = sim.a.shard.budget().global().clone();
    let (_, cmd_rx) = mpsc::channel(1);
    let (accept_tx, mut accept_rx) = mpsc::channel(count);
    let ctl = Arc::new(Ctl { dirty: Mutex::new(HashSet::new()), pending: Mutex::new(PendingControl::default()), notify: Notify::new() });
    let mut driver = Driver {
        shard: sim.a.shard,
        egress: (|_, _, _| SendResult::Accepted) as fn(IfaceId, PeerId, &OutPacket<'_>) -> SendResult,
        input: None::<InputSource<(), NoInput>>, cmd_rx, accept_tx, ctl: ctl.clone(),
        stop: Arc::new(AtomicBool::new(false)), budget_waiter: global.new_waiter_id(),
        budget_waiting: false, budget_wake: Waker::from(Arc::new(BudgetWake { ctl })),
        streams: HashMap::new(), orphans: BTreeSet::new(), stream_cfg: StreamConfig::default(),
        epoch: tokio::time::Instant::now() - Duration::from_nanos(sim.now.as_nanos()),
        closes: CloseCounts::default(), aborts: AbortCounts::default(), relays: HashMap::new(),
    };
    let mut streams = Vec::with_capacity(count);
    for id in ids {
        driver.on_event(Event::Accepted(id));
        streams.push(accept_rx.try_recv().unwrap());
    }
    (driver, streams)
}

struct Noop;
impl Wake for Noop { fn wake(self: Arc<Self>) {} }

// Linux clock_gettime is used only at measurement boundaries. This example is
// a real release binary, so CARGO_PROFILE_RELEASE_PANIC=abort is effective.
#[cfg(target_os = "linux")]
fn thread_cpu_ns() -> u64 {
    #[repr(C)] struct Timespec { sec: std::os::raw::c_long, nsec: std::os::raw::c_long }
    unsafe extern "C" { fn clock_gettime(clock: std::os::raw::c_int, output: *mut Timespec) -> std::os::raw::c_int; }
    let mut value = Timespec { sec: 0, nsec: 0 };
    // SAFETY: output points to a live, correctly represented timespec. Linux
    // CLOCK_THREAD_CPUTIME_ID is 3; no pointer escapes this synchronous call.
    assert_eq!(unsafe { clock_gettime(3, &mut value) }, 0);
    value.sec as u64 * 1_000_000_000 + value.nsec as u64
}
#[cfg(not(target_os = "linux"))]
fn thread_cpu_ns() -> u64 { panic!("This frozen CPU probe requires Linux"); }

pub fn main() {
    let arguments: Vec<String> = std::env::args().collect();
    let case = arguments.get(1).map(String::as_str).unwrap_or("idle1");
    let iterations: u64 = arguments.get(2).map(|s| s.parse().unwrap()).unwrap_or(1_000_000);
    assert!(iterations > 0);
    let (count, rx_bytes) = match case {
        "idle1" => (1, 0), "idle64" => (64, 0), "consumed" => (1, 64), "tx" => (1, 0),
        _ => panic!("case must be idle1, idle64, consumed, or tx"),
    };
    if matches!(case, "consumed" | "tx") { assert_eq!(iterations, 1, "one-shot diagnostics have exactly one pump"); }
    let runtime = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
    let _entered = runtime.enter();
    let (mut driver, mut streams) = prepared(count, rx_bytes);
    let ids: Vec<ConnId> = streams.iter().map(|stream| stream.sh.id).collect();
    let global = driver.shard.budget().global().clone();
    let waker = Waker::from(Arc::new(Noop));
    let mut cx = Context::from_waker(&waker);
    let mut data = [0u8; 64];
    if case == "consumed" {
        let mut buf = ReadBuf::new(&mut data);
        assert!(matches!(Pin::new(&mut streams[0]).poll_read(&mut cx, &mut buf), Poll::Ready(Ok(()))));
        assert_eq!(buf.filled().len(), 64);
        for (index, byte) in buf.filled().iter().enumerate() { assert_eq!(*byte, crate::sim::pattern_byte(index as u64)); }
        assert_eq!(streams[0].sh.q.lock().unwrap().rx_consumed_pending, 64);
    } else if case == "tx" {
        data.fill(0x53);
        assert!(matches!(Pin::new(&mut streams[0]).poll_write(&mut cx, &data), Poll::Ready(Ok(64))));
        assert_eq!(streams[0].sh.q.lock().unwrap().tx.len(), 64);
    } else {
        for index in 0..10_000usize { driver.pump(ids[index % ids.len()]); }
    }
    let before_reserved = global.reserved();
    reset_counts();
    let cpu_start = thread_cpu_ns();
    let wall_start = WallInstant::now();
    for index in 0..iterations { driver.pump(black_box(ids[index as usize % ids.len()])); }
    let wall_ns = wall_start.elapsed().as_nanos();
    let cpu_ns = thread_cpu_ns() - cpu_start;
    let counts = count_json();
    let after_reserved = global.reserved();
    for stream in &streams {
        let q = stream.sh.q.lock().unwrap();
        assert!(q.error.is_none());
        assert!(q.tx.is_empty());
        assert!(q.rx.is_empty());
        assert_eq!(q.rx_consumed_pending, 0);
    }
    if case.starts_with("idle") { assert_eq!(before_reserved, after_reserved); }
    if case == "tx" { assert_eq!(driver.shard.tx_queued(ids[0]), Some(64)); }
    drop((streams, driver));
    let final_reserved = global.reserved();
    assert_eq!(final_reserved, 0);
    println!("{{\"case\":\"{case}\",\"seed\":40960,\"iterations\":{iterations},\"streams\":{count},\"warmup_pumps\":{},\"thread_cpu_ns\":{cpu_ns},\"wall_ns\":{wall_ns},\"reserved_before\":{before_reserved},\"reserved_after\":{after_reserved},\"reserved_final\":{final_reserved},{} }}",
        if case.starts_with("idle") { 10_000 } else { 0 }, counts);
}
