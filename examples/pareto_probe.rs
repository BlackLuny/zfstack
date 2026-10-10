//! Rootless, fixed-work CPU probes for before/after comparisons.
//!
//! Build: cargo build --release --features test-peer --example pareto_probe
//! Examples (run one process at a time, alternating baseline and candidate):
//!   pareto_probe budget --iterations 5000000 --flows 8 --payload 128
//!   pareto_probe budget-handle --iterations 5000000 --flows 8 --payload 128
//!   pareto_probe rx-chunk --iterations 1000000 --flows 8 --payload 1400
//!   pareto_probe rx-tail --iterations 500000 --payload 1400
//!   pareto_probe heap-equal --iterations 10000000 --flows 4096
//!   pareto_probe heap-mixed --iterations 10000000 --flows 4096
//!   pareto_probe heap-changing --iterations 10000000 --flows 4096
//!   pareto_probe pump-down --iterations 3 --flows 8 --payload 8388608
//!   pareto_probe pump-up --iterations 3 --flows 64 --peers 64 --payload 1048576
//!   pareto_probe pump-bidi --flows 8 --peers 1 --payload 4194304 --owned-server
//!
//! The packet pump executes both real Shards, materializes IP packets, verifies
//! every received byte, and advances a SYNTHETIC clock. It uses no sockets, TUN,
//! WireGuard, NIC, or real network. Its wall/CPU rates include the entire pump
//! and both endpoints; they are not a standalone stack or network throughput.
//! Virtual elapsed time is only a protocol-workload descriptor, NEVER a rate
//! denominator. Peak charged memory is a sampling lower bound; VmHWM is the
//! process lifetime peak, including setup and verification.

use std::hint::black_box;
use std::time::Instant as WallInstant;

use zfstack::budget::{AllocationKind, Budget, GlobalBudget};
use zfstack::buf::{BlockPool, RxQueue, TxBuf};
use zfstack::PeerId;

// This intentionally compiles the exact heap source of each compared tree.
// No library visibility/API change is needed for the private-heap control.
pub use zfstack::time;
#[allow(dead_code)]
#[path = "../src/heap.rs"]
mod heap;

type ProbeResult<T> = Result<T, String>;

#[derive(Clone, Debug)]
struct Args {
    case: String,
    iterations: u64,
    flows: usize,
    peers: usize,
    payload: usize,
    borrowed_server: bool,
    pacing: bool,
}

impl Args {
    fn parse() -> ProbeResult<Self> {
        let mut argv = std::env::args().skip(1);
        let case = argv.next().ok_or_else(|| {
            "expected case: budget, budget-handle, rx, rx-chunk, rx-tail, tx-alloc, heap-equal, heap-mixed, heap-changing, heap-pop, pump-down, pump-up, pump-bidi".to_string()
        })?;
        let is_pump = case.starts_with("pump-");
        let is_heap = case.starts_with("heap-");
        let mut a = Self {
            case,
            iterations: if is_pump { 1 } else { 1_000_000 },
            flows: if is_heap { 64 } else { 1 },
            peers: 0,
            payload: if is_pump {
                16 << 20
            } else if is_heap {
                0
            } else {
                1400
            },
            borrowed_server: true,
            pacing: true,
        };
        while let Some(flag) = argv.next() {
            match flag.as_str() {
                "--iterations" => a.iterations = parse_number(argv.next(), &flag)?,
                "--flows" => a.flows = usize::try_from(parse_number(argv.next(), &flag)?).map_err(|_| "flows overflow")?,
                "--peers" => a.peers = usize::try_from(parse_number(argv.next(), &flag)?).map_err(|_| "peers overflow")?,
                "--payload" => a.payload = usize::try_from(parse_number(argv.next(), &flag)?).map_err(|_| "payload overflow")?,
                "--owned-server" => a.borrowed_server = false,
                "--no-pacing" => a.pacing = false,
                _ => return Err(format!("unknown option {flag}")),
            }
        }
        if a.iterations == 0 || a.flows == 0 || a.flows > 4096 {
            return Err("iterations must be positive; flows must be in 1..=4096".into());
        }
        if a.peers == 0 {
            a.peers = a.flows;
        }
        if a.peers > a.flows {
            return Err("peers must not exceed flows".into());
        }
        if !is_heap && a.payload == 0 {
            return Err("payload must be positive".into());
        }
        if a.case == "rx-tail" && a.payload < 4 {
            return Err("rx-tail needs payload >= 4".into());
        }
        if a.case.starts_with("rx") && a.payload > 64 << 20 {
            return Err("RX microprobe payload must be <= 64 MiB".into());
        }
        (a.payload as u64)
            .checked_mul(a.iterations)
            .and_then(|n| n.checked_mul(a.flows as u64))
            .and_then(|n| n.checked_mul(2))
            .ok_or("requested work overflows byte counters")?;
        Ok(a)
    }
}

fn parse_number(value: Option<String>, flag: &str) -> ProbeResult<u64> {
    value.ok_or_else(|| format!("missing value for {flag}"))?.parse().map_err(|_| format!("invalid positive integer for {flag}"))
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
mod cpu_clock {
    use std::os::raw::{c_int, c_long};

    #[repr(C)]
    struct Timespec {
        tv_sec: c_long,
        tv_nsec: c_long,
    }
    extern "C" {
        fn clock_gettime(clock_id: c_int, result: *mut Timespec) -> c_int;
        fn clock_getres(clock_id: c_int, result: *mut Timespec) -> c_int;
    }
    #[cfg(target_os = "linux")]
    const CLOCK_PROCESS_CPUTIME_ID: c_int = 2;
    #[cfg(target_os = "macos")]
    const CLOCK_PROCESS_CPUTIME_ID: c_int = 12;

    pub fn now_ns() -> u64 {
        let mut t = Timespec { tv_sec: 0, tv_nsec: 0 };
        // SAFETY: Linux clock ID; t is a valid writable timespec for the call.
        assert_eq!(unsafe { clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &mut t) }, 0, "CLOCK_PROCESS_CPUTIME_ID unavailable");
        t.tv_sec as u64 * 1_000_000_000 + t.tv_nsec as u64
    }
    pub fn resolution_ns() -> u64 {
        let mut t = Timespec { tv_sec: 0, tv_nsec: 0 };
        // SAFETY: same arguments as clock_gettime above.
        assert_eq!(unsafe { clock_getres(CLOCK_PROCESS_CPUTIME_ID, &mut t) }, 0, "clock_getres unavailable");
        t.tv_sec as u64 * 1_000_000_000 + t.tv_nsec as u64
    }
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
mod cpu_clock {
    pub fn now_ns() -> u64 {
        panic!("pareto_probe CPU clock currently requires Linux")
    }
    pub fn resolution_ns() -> u64 {
        panic!("pareto_probe CPU clock currently requires Linux")
    }
}

struct Timer {
    wall: WallInstant,
    cpu: u64,
}

impl Timer {
    fn start() -> Self {
        Self { wall: WallInstant::now(), cpu: cpu_clock::now_ns() }
    }
    fn stop(self) -> (u64, u64) {
        let cpu = cpu_clock::now_ns() - self.cpu;
        (self.wall.elapsed().as_nanos() as u64, cpu)
    }
}

#[derive(Default)]
struct ResultRow {
    operations: u64,
    effective_bytes: u64,
    allocation_requested_bytes: u64,
    checksum: u64,
    expected_checksum: u64,
    work_wall_ns: u64,
    work_cpu_ns: u64,
    cleanup_wall_ns: u64,
    cleanup_cpu_ns: u64,
    virtual_work_ns: u64,
    sampled_peak_reserved_bytes: u64,
    retained_reserved_bytes_before_drop: u64,
    final_reserved_bytes: u64,
    peak_packet_queue_bytes: u64,
    wire_packets: u64,
    wire_bytes: u64,
    driver_rounds: u64,
    connections_checked: u64,
    heap_capacity_entries: usize,
}

impl ResultRow {
    #[cfg(feature = "test-peer")]
    fn add(&mut self, x: Self) {
        self.operations += x.operations;
        self.effective_bytes += x.effective_bytes;
        self.allocation_requested_bytes += x.allocation_requested_bytes;
        self.checksum = self.checksum.wrapping_add(x.checksum);
        self.expected_checksum = self.expected_checksum.wrapping_add(x.expected_checksum);
        self.work_wall_ns += x.work_wall_ns;
        self.work_cpu_ns += x.work_cpu_ns;
        self.cleanup_wall_ns += x.cleanup_wall_ns;
        self.cleanup_cpu_ns += x.cleanup_cpu_ns;
        self.virtual_work_ns += x.virtual_work_ns;
        self.sampled_peak_reserved_bytes = self.sampled_peak_reserved_bytes.max(x.sampled_peak_reserved_bytes);
        self.retained_reserved_bytes_before_drop = self.retained_reserved_bytes_before_drop.max(x.retained_reserved_bytes_before_drop);
        self.final_reserved_bytes += x.final_reserved_bytes;
        self.peak_packet_queue_bytes = self.peak_packet_queue_bytes.max(x.peak_packet_queue_bytes);
        self.wire_packets += x.wire_packets;
        self.wire_bytes += x.wire_bytes;
        self.driver_rounds += x.driver_rounds;
        self.connections_checked += x.connections_checked;
        self.heap_capacity_entries = self.heap_capacity_entries.max(x.heap_capacity_entries);
    }
}

fn pattern(len: usize) -> Vec<u8> {
    (0..len).map(|i| (i % 251) as u8).collect()
}

fn pattern_sum(n: u64) -> u64 {
    let rem = n % 251;
    (n / 251).wrapping_mul(31_375).wrapping_add(rem * rem.saturating_sub(1) / 2)
}

fn verify(data: &[u8], expected: &[u8], checksum: &mut u64) -> ProbeResult<()> {
    if data != expected {
        let at = data.iter().zip(expected).position(|(a, b)| a != b).unwrap_or(data.len().min(expected.len()));
        return Err(format!("byte mismatch at chunk offset {at}, got {} bytes, expected {}", data.len(), expected.len()));
    }
    // Exact comparison above is the correctness check; the sum is an auditable
    // output digest, not a substitute for comparing every byte in order.
    *checksum = checksum.wrapping_add(data.iter().map(|b| *b as u64).sum::<u64>());
    Ok(())
}

fn budget_probe(a: &Args) -> ProbeResult<ResultRow> {
    let global = GlobalBudget::new(1 << 30);
    let mut budget = Budget::new(global.clone());
    let handles: Vec<_> = (0..a.peers).map(|i| budget.memory_handle(PeerId(i as u64 + 1))).collect();
    for h in &handles {
        drop(h.try_allocate_kind(a.payload as u64, AllocationKind::RxChunk).ok_or("budget warmup allocation failed")?);
    }
    let persistent = a.case == "budget-handle";
    let timer = Timer::start();
    for i in 0..a.iterations {
        let peer = (i % a.peers as u64) as usize;
        let lease = if persistent {
            black_box(&handles[peer]).try_allocate_kind(black_box(a.payload as u64), AllocationKind::RxChunk)
        } else {
            black_box(&mut budget).try_allocate_kind(PeerId(peer as u64 + 1), black_box(a.payload as u64), AllocationKind::RxChunk)
        }
        .ok_or("budget allocation failed during measured work")?;
        drop(black_box(lease));
    }
    let (work_wall_ns, work_cpu_ns) = timer.stop();
    if budget.physical_used() != 0 || global.reserved() != 0 {
        return Err("budget lease survived drop".into());
    }
    drop(handles);
    drop(budget);
    Ok(ResultRow {
        operations: a.iterations,
        allocation_requested_bytes: a.payload as u64 * a.iterations,
        work_wall_ns,
        work_cpu_ns,
        sampled_peak_reserved_bytes: a.payload as u64,
        final_reserved_bytes: global.reserved(),
        ..Default::default()
    })
}

fn rx_probe(a: &Args) -> ProbeResult<ResultRow> {
    let global = GlobalBudget::new(1 << 30);
    let mut budget = Budget::new(global.clone());
    let mut queues: Vec<RxQueue> = (0..a.flows).map(|_| RxQueue::default()).collect();
    let tail_case = a.case == "rx-tail";
    let chunk_case = a.case == "rx-chunk";
    let per_iteration = a.payload * if tail_case { 2 } else { 1 };
    let src = pattern(per_iteration);
    let mut out = vec![0u8; per_iteration];
    // Persistent queues, peer counters, and descriptor indexes are warmed once.
    // Reads preserve the small charged descriptor capacity between iterations.
    for (i, q) in queues.iter_mut().enumerate() {
        if !q.push_charged(&src[..a.payload], &mut budget, PeerId((i % a.peers + 1) as u64)) {
            return Err("RX warmup failed".into());
        }
        while q.read_chunk(a.payload).is_some() {}
        if !q.is_empty() {
            return Err("RX warmup did not drain".into());
        }
    }
    let mut r = ResultRow::default();
    let timer = Timer::start();
    for i in 0..a.iterations {
        let qi = (i % a.flows as u64) as usize;
        let q = black_box(&mut queues[qi]);
        let peer = PeerId((qi % a.peers + 1) as u64);
        let mut got = 0;
        if tail_case {
            // Two appends first exercise spare room in an existing charged
            // tail; then a partial read and another append exercise a queued
            // prefix coexisting with a new tail. The queue itself persists.
            let split = a.payload / 3;
            if !q.push_charged(&src[..split], &mut budget, peer) || !q.push_charged(&src[split..a.payload], &mut budget, peer) {
                return Err("RX split append failed".into());
            }
            r.sampled_peak_reserved_bytes = r.sampled_peak_reserved_bytes.max(global.reserved());
            let n = q.read(&mut out[..split]);
            if n != split {
                return Err("RX partial read was short".into());
            }
            verify(&out[..n], &src[..n], &mut r.checksum)?;
            got += n;
            if !q.push_charged(&src[a.payload..], &mut budget, peer) {
                return Err("RX append after partial read failed".into());
            }
        } else if !q.push_charged(black_box(&src), &mut budget, peer) {
            return Err("RX append failed".into());
        }
        r.sampled_peak_reserved_bytes = r.sampled_peak_reserved_bytes.max(global.reserved());
        while got < per_iteration {
            let n = if chunk_case {
                let b = q.read_chunk(per_iteration - got).ok_or("RX chunk unexpectedly empty")?;
                let n = b.len();
                verify(&b, &src[got..got + n], &mut r.checksum)?;
                n
            } else {
                let n = q.read(&mut out[..per_iteration - got]);
                if n == 0 {
                    return Err("RX read unexpectedly empty".into());
                }
                verify(&out[..n], &src[got..got + n], &mut r.checksum)?;
                n
            };
            got += n;
        }
        if !q.is_empty() {
            return Err("RX iteration left unread bytes".into());
        }
    }
    (r.work_wall_ns, r.work_cpu_ns) = timer.stop();
    r.operations = a.iterations;
    r.effective_bytes = per_iteration as u64 * a.iterations;
    r.expected_checksum = pattern_sum(per_iteration as u64).wrapping_mul(a.iterations);
    if r.checksum != r.expected_checksum {
        return Err("RX checksum total mismatch".into());
    }
    r.retained_reserved_bytes_before_drop = global.reserved();
    drop(queues);
    if budget.physical_used() != 0 {
        return Err("RX backing lease survived queue destruction".into());
    }
    drop(budget);
    r.final_reserved_bytes = global.reserved();
    Ok(r)
}

fn tx_alloc_probe(a: &Args) -> ProbeResult<ResultRow> {
    let global = GlobalBudget::new(1 << 30);
    let mut budget = Budget::new(global.clone());
    // No idle cache: every iteration allocates a fresh block (the memset path).
    let mut pool = BlockPool::new(0);
    let mut tx = TxBuf::default();
    let src = pattern(a.payload);
    if !tx.push(&mut pool, &mut budget, PeerId(1), &src) {
        return Err("TX warmup allocation failed".into());
    }
    let [s0, s1] = tx.slices(0, src.len());
    let mut warmup_sum = 0u64;
    verify(s0, &src[..s0.len()], &mut warmup_sum)?;
    if !s1.is_empty() {
        verify(s1, &src[s0.len()..], &mut warmup_sum)?;
    }
    tx.consume(&mut pool, src.len());
    let mut checksum = 0u64;
    let timer = Timer::start();
    for _ in 0..a.iterations {
        if !black_box(&mut tx).push(black_box(&mut pool), black_box(&mut budget), PeerId(1), black_box(&src)) {
            return Err("TX allocation failed during measured work".into());
        }
        let [s0, s1] = tx.slices(0, src.len());
        verify(s0, &src[..s0.len()], &mut checksum)?;
        if !s1.is_empty() {
            verify(s1, &src[s0.len()..], &mut checksum)?;
        }
        tx.consume(&mut pool, src.len());
    }
    let (work_wall_ns, work_cpu_ns) = timer.stop();
    drop(tx);
    drop(pool);
    if budget.physical_used() != 0 || global.reserved() != 0 {
        return Err("TX block lease survived drop".into());
    }
    Ok(ResultRow {
        operations: a.iterations,
        effective_bytes: a.payload as u64 * a.iterations,
        allocation_requested_bytes: a.payload as u64 * a.iterations,
        checksum,
        expected_checksum: pattern_sum(a.payload as u64).wrapping_mul(a.iterations),
        work_wall_ns,
        work_cpu_ns,
        sampled_peak_reserved_bytes: a.payload as u64 + 64,
        final_reserved_bytes: global.reserved(),
        ..Default::default()
    })
}

fn heap_probe(a: &Args) -> ProbeResult<ResultRow> {
    let mut h = heap::IndexedHeap::default();
    let mut keys: Vec<u64> = (0..a.flows).map(|i| 1_000_000 + i as u64 * 17).collect();
    for (i, &key) in keys.iter().enumerate() {
        h.set(i as u32, time::Instant::from_nanos(key));
    }
    let mixed = a.case == "heap-mixed";
    let (work_wall_ns, work_cpu_ns) = if a.case == "heap-pop" {
        let timer = Timer::start();
        for _ in 0..a.iterations {
            let mut n = 0;
            while black_box(&mut h).pop_due(black_box(time::Instant::MAX)).is_some() {
                n += 1;
            }
            if n != a.flows {
                return Err("heap-pop drained the wrong number of entries".into());
            }
            for (i, &key) in keys.iter().enumerate() {
                black_box(&mut h).set(black_box(i as u32), black_box(time::Instant::from_nanos(key)));
            }
        }
        timer.stop()
    } else if a.case == "heap-changing" {
        let timer = Timer::start();
        for i in 0..a.iterations {
            let idx = (i % a.flows as u64) as usize;
            // Toggling bit 8 makes each index alternate between +256 and
            // -256. Every update changes the key, with no overflow or
            // saturation; a complete pair of passes is exactly half each.
            let key = keys[idx] ^ 256;
            keys[idx] = key;
            black_box(&mut h).set(black_box(idx as u32), black_box(time::Instant::from_nanos(key)));
        }
        timer.stop()
    } else {
        let timer = Timer::start();
        for i in 0..a.iterations {
            let idx = (i % a.flows as u64) as usize;
            let old = keys[idx];
            // 60% equal, 20% decrease, 20% increase. Every index changes operation
            // class between passes, even when flows is a multiple of ten.
            let class = ((i / a.flows as u64) + idx as u64) % 10;
            let key = if mixed && class >= 8 {
                old.saturating_add(193)
            } else if mixed && class >= 6 {
                old.saturating_sub(127)
            } else {
                old
            };
            keys[idx] = key;
            black_box(&mut h).set(black_box(idx as u32), black_box(time::Instant::from_nanos(key)));
        }
        timer.stop()
    };
    if h.len() != a.flows {
        return Err("heap lost/duplicated entries".into());
    }
    let mut r = ResultRow { operations: a.iterations, work_wall_ns, work_cpu_ns, heap_capacity_entries: h.capacity(), ..Default::default() };
    let mut seen = vec![false; a.flows];
    let mut previous = 0;
    while let Some((key, idx)) = h.peek() {
        let popped = h.pop_due(time::Instant::MAX).ok_or("heap pop failed")?;
        if popped != idx || seen[idx as usize] || keys[idx as usize] != key.as_nanos() || key.as_nanos() < previous {
            return Err("heap order/key/uniqueness mismatch".into());
        }
        seen[idx as usize] = true;
        previous = key.as_nanos();
        r.checksum = r.checksum.wrapping_add(key.as_nanos() ^ idx as u64);
    }
    if seen.iter().any(|x| !x) || h.len() != 0 {
        return Err("heap did not fully drain".into());
    }
    r.expected_checksum = keys.iter().enumerate().fold(0u64, |acc, (i, key)| acc.wrapping_add(*key ^ i as u64));
    drop(h);
    Ok(r)
}

#[cfg(feature = "test-peer")]
mod packet_pump {
    use super::*;
    use bytes::Bytes;
    use std::collections::VecDeque;
    use std::net::{Ipv4Addr, SocketAddr};
    use std::sync::Arc;
    use std::time::Duration;
    use zfstack::{CloseReason, ConnId, Event, IfaceId, OutPacket, ReadResult, SendResult, Shard, StackConfig, WriteResult};

    const CHUNK: usize = 64 << 10;
    const STEP: Duration = Duration::from_micros(50);

    struct Flow {
        id: ConnId,
        send_goal: u64,
        receive_goal: u64,
        sent: u64,
        received: u64,
        checksum: u64,
        ready: bool,
        fin_sent: bool,
        eof: bool,
        released: bool,
    }

    impl Flow {
        fn new(id: ConnId, send_goal: u64, receive_goal: u64, ready: bool) -> Self {
            Self { id, send_goal, receive_goal, sent: 0, received: 0, checksum: 0, ready, fin_sent: false, eof: false, released: false }
        }
        fn complete(&self) -> bool {
            self.ready && self.sent == self.send_goal && self.received == self.receive_goal && self.fin_sent && self.eof && self.released
        }
    }

    struct Side {
        shard: Shard,
        iface: IfaceId,
        flows: Vec<Flow>,
        send_goal: u64,
        receive_goal: u64,
        pattern: Vec<u8>,
        read_buf: Vec<u8>,
        borrowed: bool,
    }

    impl Side {
        fn new(cfg: StackConfig, global: Arc<GlobalBudget>, flows: usize, send_goal: u64, receive_goal: u64, borrowed: bool) -> Self {
            let mut shard = Shard::with_budget(cfg, global);
            let iface = shard.add_iface(Default::default());
            Self { shard, iface, flows: Vec::with_capacity(flows), send_goal, receive_goal, pattern: pattern(CHUNK + 251), read_buf: vec![0; CHUNK], borrowed }
        }

        fn app(&mut self, now: time::Instant) -> ProbeResult<()> {
            while let Some(ev) = self.shard.poll_event() {
                match ev {
                    Event::Accepted(id) => self.flows.push(Flow::new(id, self.send_goal, self.receive_goal, true)),
                    Event::Connected(id) => self.flows.iter_mut().find(|f| f.id == id).ok_or("unknown connected flow")?.ready = true,
                    Event::Closed(_, reason) if reason != CloseReason::Normal => return Err(format!("abnormal close: {reason:?}")),
                    _ => {}
                }
            }
            // Stable vector order avoids introducing a randomized app HashMap
            // walk into the A/B workload. This scan is included in measured CPU.
            for f in &mut self.flows {
                if !f.ready || f.released {
                    continue;
                }
                while !f.eof {
                    match self.shard.read(now, f.id, &mut self.read_buf) {
                        ReadResult::Data(n) => {
                            if n == 0 || f.received + n as u64 > f.receive_goal {
                                return Err("unexpected application byte count".into());
                            }
                            let offset = (f.received % 251) as usize;
                            verify(&self.read_buf[..n], &self.pattern[offset..offset + n], &mut f.checksum)?;
                            f.received += n as u64;
                        }
                        ReadResult::Eof => {
                            if f.received != f.receive_goal {
                                return Err(format!("premature EOF: {} of {} bytes", f.received, f.receive_goal));
                            }
                            f.eof = true;
                        }
                        ReadResult::WouldBlock => break,
                        ReadResult::Closed(reason) => return Err(format!("read closed before expected EOF: {reason:?}")),
                    }
                }
                while f.sent < f.send_goal {
                    let n = (f.send_goal - f.sent).min(CHUNK as u64) as usize;
                    let offset = (f.sent % 251) as usize;
                    match self.shard.write(f.id, &self.pattern[offset..offset + n]) {
                        WriteResult::Written(0) => return Err("zero-length successful write".into()),
                        WriteResult::Written(n) => f.sent += n as u64,
                        WriteResult::Closed => return Err("write closed before all bytes were accepted".into()),
                        _ => break,
                    }
                }
                if f.sent == f.send_goal && !f.fin_sent {
                    self.shard.shutdown_write(f.id);
                    f.fin_sent = true;
                }
                if f.eof && f.fin_sent {
                    self.shard.close(now, f.id);
                    f.released = true;
                }
            }
            Ok(())
        }

        fn complete(&self, expected_flows: usize) -> bool {
            self.flows.len() == expected_flows && self.flows.iter().all(Flow::complete)
        }
    }

    #[derive(Default)]
    struct Packets {
        q: VecDeque<(PeerId, Vec<u8>)>,
        queued_bytes: u64,
        peak_bytes: u64,
        packets: u64,
        bytes: u64,
    }

    impl Packets {
        fn send(&mut self, _iface: IfaceId, packet: &OutPacket<'_>) -> SendResult {
            assert!(!packet.csum_partial && packet.gso_size == 0, "pump requires ordinary fully checksummed IP packets");
            self.queued_bytes += packet.len() as u64;
            self.peak_bytes = self.peak_bytes.max(self.queued_bytes);
            self.packets += 1;
            self.bytes += packet.len() as u64;
            self.q.push_back((packet.peer, packet.to_vec()));
            SendResult::Accepted
        }

        fn deliver(&mut self, side: &mut Side, now: time::Instant) {
            while let Some((peer, packet)) = self.q.pop_front() {
                self.queued_bytes -= packet.len() as u64;
                // Both entry points VERIFY IP/TCP checksums. The server's
                // borrowed default models the relevant WG ingress API only;
                // there is no encryption in this probe.
                if side.borrowed {
                    side.shard.ingress_borrowed(now, side.iface, peer, &packet);
                } else {
                    side.shard.ingress(now, side.iface, peer, Bytes::from(packet));
                }
            }
        }
    }

    fn round(a: &mut Side, b: &mut Side, ab: &mut Packets, ba: &mut Packets, now: time::Instant, result: &mut ResultRow) -> ProbeResult<bool> {
        ba.deliver(a, now);
        ab.deliver(b, now);
        result.sampled_peak_reserved_bytes =
            result.sampled_peak_reserved_bytes.max(a.shard.budget().global().reserved() + b.shard.budget().global().reserved());
        a.app(now)?;
        b.app(now)?;
        result.sampled_peak_reserved_bytes =
            result.sampled_peak_reserved_bytes.max(a.shard.budget().global().reserved() + b.shard.budget().global().reserved());
        let ar = a.shard.run(now, &mut |iface, packet: &OutPacket<'_>| ab.send(iface, packet));
        let br = b.shard.run(now, &mut |iface, packet: &OutPacket<'_>| ba.send(iface, packet));
        result.sampled_peak_reserved_bytes =
            result.sampled_peak_reserved_bytes.max(a.shard.budget().global().reserved() + b.shard.budget().global().reserved());
        result.driver_rounds += 1;
        Ok(ar.more || br.more || !ab.q.is_empty() || !ba.q.is_empty())
    }

    fn next(a: &Side, b: &Side, now: time::Instant, ready: bool) -> ProbeResult<time::Instant> {
        if ready {
            return Ok(now + STEP);
        }
        let deadline = [a.shard.next_deadline(), b.shard.next_deadline()].into_iter().flatten().min().ok_or("packet pump stalled with no next deadline")?;
        Ok(deadline.max(now + Duration::from_nanos(1)))
    }

    pub(super) fn run(args: &Args) -> ProbeResult<ResultRow> {
        let mut total = ResultRow::default();
        for _ in 0..args.iterations {
            total.add(one(args)?);
        }
        Ok(total)
    }

    fn one(args: &Args) -> ProbeResult<ResultRow> {
        let payload = args.payload as u64;
        let server_send = if args.case == "pump-up" { 0 } else { payload };
        let client_send = if args.case == "pump-down" { 0 } else { payload };
        let ga = GlobalBudget::new(1 << 30);
        let gb = GlobalBudget::new(1 << 30);
        let cfg = StackConfig { pacing: args.pacing, ..Default::default() };
        let time_wait = cfg.time_wait;
        let mut a = Side::new(cfg.clone(), ga.clone(), args.flows, server_send, client_send, args.borrowed_server);
        let mut b = Side::new(cfg, gb.clone(), args.flows, client_send, server_send, false);
        let mut ab = Packets::default();
        let mut ba = Packets::default();
        let start = time::Instant::from_millis(1000);
        let mut now = start;
        let mut result = ResultRow::default();
        let timer = Timer::start();
        for i in 0..args.flows {
            let local = SocketAddr::from((Ipv4Addr::new(10, 0, 0, 2), 40_000 + i as u16));
            let remote = SocketAddr::from((Ipv4Addr::new(10, 0, 0, 1), 80));
            let id = b.shard.connect(now, b.iface, PeerId((i % args.peers + 1) as u64), local, remote);
            b.flows.push(Flow::new(id, client_send, server_send, false));
        }
        loop {
            let ready = round(&mut a, &mut b, &mut ab, &mut ba, now, &mut result)?;
            if a.complete(args.flows) && b.complete(args.flows) && ab.q.is_empty() && ba.q.is_empty() {
                break;
            }
            if now - start > Duration::from_secs(600) || result.driver_rounds > 50_000_000 {
                return Err("packet pump exceeded fixed virtual/work safety limit".into());
            }
            now = next(&a, &b, now, ready)?;
        }
        (result.work_wall_ns, result.work_cpu_ns) = timer.stop();
        result.virtual_work_ns = (now - start).as_nanos() as u64;
        result.operations = 1;
        result.connections_checked = args.flows as u64;
        for f in a.flows.iter().chain(&b.flows) {
            if !f.complete() || f.checksum != pattern_sum(f.receive_goal) {
                return Err("final flow size/EOF/checksum mismatch".into());
            }
            result.effective_bytes += f.received;
            result.checksum = result.checksum.wrapping_add(f.checksum);
            result.expected_checksum = result.expected_checksum.wrapping_add(pattern_sum(f.receive_goal));
        }
        if result.effective_bytes != (server_send + client_send) * args.flows as u64 {
            return Err("aggregate application byte count mismatch".into());
        }
        let cleanup = Timer::start();
        a.shard.check_invariants();
        b.shard.check_invariants();
        // All application FINs and their ACKs have been exchanged. Advance the
        // synthetic clock to expire TIME_WAIT naturally, without abort/remove.
        now += time_wait + Duration::from_secs(1);
        for _ in 0..128 {
            let ready = round(&mut a, &mut b, &mut ab, &mut ba, now, &mut result)?;
            if a.shard.conn_count() == 0 && b.shard.conn_count() == 0 && ab.q.is_empty() && ba.q.is_empty() {
                break;
            }
            now = next(&a, &b, now, ready)?;
        }
        for side in [&a, &b] {
            if side.shard.conn_count() != 0 || side.shard.container_sizes() != (0, 0, 0) || side.shard.budget().used != 0 {
                return Err("connection/logical state survived natural close cleanup".into());
            }
            let g = side.shard.budget().global();
            if g.connection_counts() != (0, 0) || g.time_wait_bytes_reserved() != 0 {
                return Err("connection or TIME_WAIT permit leaked".into());
            }
            if side.shard.stats().rx_dropped_parse != 0 || side.shard.stats().rx_dropped_no_iface != 0 || side.shard.stats().rx_dropped_peer_mismatch != 0 {
                return Err("packet parse/checksum/interface/peer rejection".into());
            }
        }
        if a.shard.stats().rx_packets != ba.packets || b.shard.stats().rx_packets != ab.packets {
            return Err("wire packet delivery accounting mismatch".into());
        }
        result.wire_packets = ab.packets + ba.packets;
        result.wire_bytes = ab.bytes + ba.bytes;
        // Sum of each directional maximum is an upper bound for simultaneous
        // packet-queue occupancy, not charged stack memory.
        result.peak_packet_queue_bytes = ab.peak_bytes + ba.peak_bytes;
        result.retained_reserved_bytes_before_drop = ga.reserved() + gb.reserved();
        drop(a);
        drop(b);
        drop(ab);
        drop(ba);
        result.final_reserved_bytes = ga.reserved() + gb.reserved();
        if result.final_reserved_bytes != 0 {
            return Err("physical backing lease survived Shard destruction".into());
        }
        (result.cleanup_wall_ns, result.cleanup_cpu_ns) = cleanup.stop();
        Ok(result)
    }
}

fn quote(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '"' => out.push_str("\\\""),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c < ' ' => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

fn rss_bytes(name: &str) -> Option<u64> {
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    status.lines().find_map(|l| {
        let (key, value) = l.split_once(':')?;
        (key == name).then(|| value.split_whitespace().next()?.parse::<u64>().ok().map(|v| v * 1024)).flatten()
    })
}

fn float_or_null(n: Option<f64>) -> String {
    n.filter(|x| x.is_finite()).map_or_else(|| "null".into(), |x| format!("{x:.9}"))
}

fn run() -> ProbeResult<()> {
    let args = Args::parse()?;
    let whole = Timer::start();
    let before_rss = rss_bytes("VmRSS");
    let result = match args.case.as_str() {
        "budget" | "budget-handle" => budget_probe(&args)?,
        "rx" | "rx-chunk" | "rx-tail" => rx_probe(&args)?,
        "tx-alloc" => tx_alloc_probe(&args)?,
        "heap-equal" | "heap-mixed" | "heap-changing" | "heap-pop" => heap_probe(&args)?,
        "pump-down" | "pump-up" | "pump-bidi" => {
            #[cfg(feature = "test-peer")]
            {
                packet_pump::run(&args)?
            }
            #[cfg(not(feature = "test-peer"))]
            {
                return Err("packet pump requires --features test-peer".into());
            }
        }
        _ => return Err(format!("unknown case {}", args.case)),
    };
    let (total_wall_ns, total_cpu_ns) = whole.stop();
    if result.final_reserved_bytes != 0 || result.checksum != result.expected_checksum {
        return Err("final release or checksum assertion failed".into());
    }
    let bytes = result.effective_bytes as f64;
    let wall_sec = result.work_wall_ns as f64 / 1e9;
    let cpu_sec = result.work_cpu_ns as f64 / 1e9;
    let actual_bytes = result.effective_bytes > 0;
    let is_pump = args.case.starts_with("pump-");
    let scope = if is_pump {
        "both_shards_packet_materialization_app_scan_byte_verification_and_handshake_excludes_setup_and_cleanup"
    } else if args.case.starts_with("heap-") {
        "heap_updates_loop_only_final_order_key_and_uniqueness_validation_outside_work_timing"
    } else {
        "microprobe_work_loop_includes_validation_excludes_setup_and_cleanup"
    };
    let fields = vec![
        ("tool", quote("zfstack-pareto-probe")),
        ("schema_version", "1".into()),
        ("ok", "true".into()),
        ("case", quote(&args.case)),
        ("iterations", args.iterations.to_string()),
        ("flows", args.flows.to_string()),
        ("peers", args.peers.to_string()),
        ("payload_bytes", args.payload.to_string()),
        ("server_ingress", if is_pump { quote(if args.borrowed_server { "borrowed_verify" } else { "owned_verify" }) } else { "null".into() }),
        ("pacing", if is_pump { args.pacing.to_string() } else { "null".into() }),
        ("timing_scope", quote(scope)),
        ("cpu_clock", quote("CLOCK_PROCESS_CPUTIME_ID_user_plus_system")),
        ("cpu_clock_resolution_ns", cpu_clock::resolution_ns().to_string()),
        ("work_wall_ns", result.work_wall_ns.to_string()),
        ("work_cpu_ns", result.work_cpu_ns.to_string()),
        ("work_wall_sec", float_or_null(Some(wall_sec))),
        ("work_cpu_sec", float_or_null(Some(cpu_sec))),
        ("cleanup_wall_ns", result.cleanup_wall_ns.to_string()),
        ("cleanup_cpu_ns", result.cleanup_cpu_ns.to_string()),
        ("process_total_wall_ns", total_wall_ns.to_string()),
        ("process_total_cpu_ns", total_cpu_ns.to_string()),
        ("operations", result.operations.to_string()),
        ("work_cpu_ns_per_operation", float_or_null((result.operations > 0).then_some(result.work_cpu_ns as f64 / result.operations as f64))),
        ("effective_bytes", result.effective_bytes.to_string()),
        ("work_wall_Gbit_per_sec", float_or_null(actual_bytes.then_some(bytes * 8.0 / wall_sec / 1e9))),
        ("work_process_cpu_sec_per_GB", float_or_null(actual_bytes.then_some(cpu_sec * 1e9 / bytes))),
        ("allocation_requested_bytes", result.allocation_requested_bytes.to_string()),
        ("byte_validation", quote(if actual_bytes { "every_received_byte_compared_in_order" } else { "not_applicable_metadata_case" })),
        ("checksum_u64", result.checksum.to_string()),
        ("expected_checksum_u64", result.expected_checksum.to_string()),
        ("virtual_work_ns", if is_pump { result.virtual_work_ns.to_string() } else { "null".into() }),
        ("virtual_clock_used_for_rate", "false".into()),
        ("real_network_io", "false".into()),
        ("wire_packets", result.wire_packets.to_string()),
        ("wire_bytes", result.wire_bytes.to_string()),
        ("driver_rounds_including_cleanup", result.driver_rounds.to_string()),
        ("connections_checked", result.connections_checked.to_string()),
        ("sampled_peak_reserved_bytes", result.sampled_peak_reserved_bytes.to_string()),
        ("peak_reserved_is_sampled_lower_bound", "true".into()),
        ("retained_reserved_bytes_before_drop", result.retained_reserved_bytes_before_drop.to_string()),
        ("final_reserved_bytes", result.final_reserved_bytes.to_string()),
        ("all_budget_leases_released", "true".into()),
        ("directional_packet_queue_peak_sum_bytes", result.peak_packet_queue_bytes.to_string()),
        ("heap_capacity_entries", result.heap_capacity_entries.to_string()),
        ("rss_before_bytes", before_rss.map_or_else(|| "null".into(), |n| n.to_string())),
        ("rss_after_bytes", rss_bytes("VmRSS").map_or_else(|| "null".into(), |n| n.to_string())),
        ("process_lifetime_peak_rss_bytes", rss_bytes("VmHWM").map_or_else(|| "null".into(), |n| n.to_string())),
    ];
    println!("{{{}}}", fields.into_iter().map(|(k, v)| format!("{}:{v}", quote(k))).collect::<Vec<_>>().join(","));
    Ok(())
}

fn main() {
    let result = std::panic::catch_unwind(run);
    let error = match result {
        Ok(Ok(())) => return,
        Ok(Err(e)) => e,
        Err(p) => p.downcast_ref::<String>().cloned().or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string())).unwrap_or_else(|| "probe panicked".into()),
    };
    println!("{{\"tool\":\"zfstack-pareto-probe\",\"ok\":false,\"error\":{}}}", quote(&error));
    std::process::exit(1);
}
