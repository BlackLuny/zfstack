//! P0 direct-pool retention probe. No TCP/TUN or automatic maintenance.
use serde_json::{json, Value};
use std::{
    fs, thread,
    time::{Duration, Instant},
};
use zfstack::{
    budget::{Budget, GlobalBudget},
    buf::{BlockPool, TxBuf, TX_BLOCK},
    pktpool::PacketPool,
    PeerId,
};
fn usage() -> Value {
    let mut r = std::mem::MaybeUninit::<libc::rusage>::zeroed();
    let ok = unsafe { libc::getrusage(libc::RUSAGE_SELF, r.as_mut_ptr()) } == 0;
    if !ok {
        return Value::Null;
    }
    let r = unsafe { r.assume_init() };
    let status = fs::read_to_string("/proc/self/status").unwrap_or_default();
    let kb =
        |key: &str| status.lines().find(|l| l.starts_with(key)).and_then(|l| l.split_whitespace().nth(1)).and_then(|s| s.parse::<u64>().ok()).map(|n| n * 1024);
    json!({"cpu_user_s":r.ru_utime.tv_sec as f64+r.ru_utime.tv_usec as f64/1e6,"cpu_system_s":r.ru_stime.tv_sec as f64+r.ru_stime.tv_usec as f64/1e6,"rss_bytes":kb("VmRSS:"),"vmhwm_bytes":kb("VmHWM:"),"minor_faults":r.ru_minflt,"major_faults":r.ru_majflt,"voluntary_switches":r.ru_nvcsw,"involuntary_switches":r.ru_nivcsw})
}
fn emit(v: Value) {
    println!("{}", v);
}
fn main() {
    assert!(cfg!(target_os = "linux"));
    let args: Vec<String> = std::env::args().collect();
    let arg = |k: &str, d: &str| args.windows(2).find(|a| a[0] == k).map(|a| a[1].clone()).unwrap_or_else(|| d.into());
    let idle: Vec<u64> = arg("--idle-secs", "5,30,60,180").split(',').map(|s| s.parse().unwrap()).collect();
    let small: usize = arg("--small", "8").parse().unwrap();
    let large: usize = arg("--large", "8").parse().unwrap();
    let packet_size: usize = arg("--packet-size", "65545").parse().unwrap();
    let packet_count: usize = arg("--packet-count", "128").parse().unwrap();
    assert!(small + large <= 256 && packet_count <= 4096 && packet_size > 0 && packet_size <= 131072 && idle.len() <= 32 && idle.iter().all(|&s| s <= 3600));
    let sha = arg("--source-sha", "unspecified");
    let g = GlobalBudget::new(64 << 20);
    let mut txb = Budget::new(g.clone());
    txb.set_limits(64 << 20, 64 << 20, u32::MAX);
    let mut pb = Budget::new(g.clone());
    pb.set_limits(64 << 20, 64 << 20, u32::MAX);
    let mut pool = BlockPool::new(16);
    let packets = PacketPool::new(pb.memory_handle(PeerId(2)), packet_size, 128);
    let mut bufs: Vec<TxBuf> = (0..small + large).map(|_| TxBuf::default()).collect();
    let tiny = vec![0x6du8; 64];
    let big = vec![0xa5u8; TX_BLOCK];
    let start = Instant::now();
    emit(
        json!({"type":"metadata","schema":1,"source_sha":sha,"scope":"direct pools; no Shard, TCP, TUN, automatic trim or allocator hooks","small":small,"large":large,"packet_size":packet_size,"packet_count":packet_count,"idle_seconds":idle,"cpu_scope":"whole probe incl payload verification","private_pool_class_counts":null,"descriptor_backing_bytes":null,"peer_reserved_bytes":null,"cache_hits":null,"cache_misses":null,"alloc_free_counts":null,"timer_wakeups":null,"network_p99":null,"network_throughput":null,"instructions":null,"kernel":fs::read_to_string("/proc/version").ok()}),
    );
    macro_rules! snap {($stage:expr,$cycle:expr)=>{emit(json!({"type":"snapshot","stage":$stage,"cycle":$cycle,"wall_s":start.elapsed().as_secs_f64(),"global_reserved":g.reserved(),"global_tx_parked_cached":g.cached_bytes(),"tx_port_reserved":txb.physical_used(),"packet_port_reserved":pb.physical_used(),"tx_cached":pool.cached(),"packet_cached":packets.cached(),"packet_in_use":packets.in_use(),"tx_active_bytes":bufs.iter().map(TxBuf::allocated).sum::<usize>(),"resources":usage()}));};}
    snap!("cold", 0);
    for cycle in 0..=idle.len() {
        let t = Instant::now();
        let before = usage();
        let mut checksum = 0u64;
        let mut verified = 0usize;
        for (i, b) in bufs.iter_mut().enumerate() {
            let src = if i < small { &tiny } else { &big };
            assert!(b.push(&mut pool, &mut txb, PeerId(1), src));
            let [a, c] = b.slices(0, b.len());
            assert_eq!(a, &src[..a.len()]);
            assert_eq!(c, &src[a.len()..]);
            checksum = a.iter().chain(c).fold(checksum, |s, &v| s.wrapping_add(v as u64));
            verified += src.len();
        }
        let mut held = Vec::with_capacity(packet_count);
        for i in 0..packet_count {
            let mut b = packets.get().expect("packet quota");
            assert!(b.capacity() >= packet_size);
            let value = (i % 251) as u8;
            for x in b.spare_capacity_mut().iter_mut().take(packet_size) {
                x.write(value);
            }
            unsafe { b.set_len(packet_size) };
            assert!(b.iter().all(|&v| v == value));
            checksum = b.iter().fold(checksum, |s, &v| s.wrapping_add(v as u64));
            verified += b.len();
            held.push(b);
        }
        let sampled_peak = g.reserved();
        snap!("burst_allocated", cycle);
        for b in &mut bufs {
            let n = b.len();
            b.consume(&mut pool, n);
            assert!(b.is_empty());
        }
        drop(held);
        assert_eq!(packets.in_use(), 0);
        emit(
            json!({"type":"burst","cycle":cycle,"wall_s":t.elapsed().as_secs_f64(),"verified_bytes":verified,"checksum":checksum,"sampled_peak_reserved_lower_bound":sampled_peak,"usage_before":before,"usage_after":usage()}),
        );
        snap!("after_burst", cycle);
        if let Some(&seconds) = idle.get(cycle) {
            let before = usage();
            let t = Instant::now();
            thread::sleep(Duration::from_secs(seconds));
            emit(
                json!({"type":"idle","cycle":cycle,"requested_s":seconds,"wall_s":t.elapsed().as_secs_f64(),"usage_before":before,"usage_after":usage(),"maintenance_calls":0}),
            );
            snap!("after_idle", cycle);
        }
    }
    let before = g.reserved();
    bufs.clear();
    emit(json!({"type":"cleanup","action":"drop_empty_tx_owners","released_ledger":before-g.reserved()}));
    let before = g.reserved();
    let (blocks, bytes) = pool.reclaim();
    assert_eq!(before - g.reserved(), bytes);
    emit(json!({"type":"cleanup","action":"explicit_tx_reclaim","blocks":blocks,"released_ledger":bytes,"resources":usage()}));
    drop(pool);
    drop(packets);
    assert_eq!(g.reserved(), 0);
    assert_eq!(txb.physical_used(), 0);
    assert_eq!(pb.physical_used(), 0);
    emit(
        json!({"type":"summary","ok":true,"final_reserved":g.reserved(),"wall_s":start.elapsed().as_secs_f64(),"resources":usage(),"automatic_reclaim_tested":false,"tcp_tun_tested":false,"note":"ledger release is not RSS release"}),
    );
}
