//! Reproducer for the PacketPool idle-retirement maintenance cost (#23).
//! cargo run --release --example issue23_trim_scaling -- <cached_buffers> <trials>
//! The measured region excludes pool construction, new allocations, and
//! the initial activity window. No network/throughput claim is implied.

use std::time::{Duration, Instant};
use zfstack::budget::{Budget, GlobalBudget};
use zfstack::pktpool::PacketPool;
use zfstack::PeerId;

fn sample(count: usize, hot: usize) -> u128 {
    let global = GlobalBudget::new(64 << 20);
    let mut budget = Budget::new(global.clone());
    budget.set_limits(global.high(), global.high(), u32::MAX);
    let pool = PacketPool::new(budget.memory_handle(PeerId(7)), 64, count);
    let blocks = (0..count).map(|_| pool.get().expect("pool allocation")).collect::<Vec<_>>();
    drop(blocks);
    let start = Instant::now();
    let idle = Duration::from_secs(5);
    assert_eq!(pool.trim_idle(start, idle, 0), 0);
    let reused = (0..hot).map(|_| pool.get().expect("cached packet")).collect::<Vec<_>>();
    drop(reused);
    let measured = Instant::now();
    let freed = pool.trim_idle(start + idle, idle, 0);
    let elapsed = measured.elapsed().as_nanos();
    assert_eq!(freed, count - hot);
    assert_eq!(pool.cached(), hot);
    assert_eq!(pool.in_use(), 0);
    assert_eq!(global.reserved(), (hot * 64) as u64);
    pool.trim_cached_to(0);
    assert_eq!(global.reserved(), 0);
    elapsed
}

fn main() {
    let mut args = std::env::args().skip(1);
    let count: usize = args.next().expect("cached_buffers").parse().unwrap();
    let trials: usize = args.next().unwrap_or_else(|| "11".into()).parse().unwrap();
    assert!(count > 32 && trials >= 3);
    let hot = 32;
    for _ in 0..3 {
        std::hint::black_box(sample(count, hot));
    }
    let mut values = (0..trials).map(|_| sample(count, hot)).collect::<Vec<_>>();
    values.sort_unstable();
    println!("count={count} hot={hot} trials={trials} min_ns={} median_ns={} max_ns={}", values[0], values[values.len() / 2], values[values.len() - 1]);
}
