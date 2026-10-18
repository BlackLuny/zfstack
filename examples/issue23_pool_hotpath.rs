//! Isolated PacketPool borrow/return CPU cost (not an end-to-end proxy bench).
//! cargo run --release --example issue23_pool_hotpath -- <cached> [iterations=200000] [trials=9]
//! All runs use identical source on baseline and candidate to test whether
//! using VecDeque for O(1) retirement changes the common LIFO get/drop cost.

use std::time::Instant;
use zfstack::budget::{Budget, GlobalBudget};
use zfstack::pktpool::PacketPool;
use zfstack::PeerId;

fn sample(cached: usize, iterations: usize) -> u128 {
    let global = GlobalBudget::new(64 << 20);
    let mut budget = Budget::new(global.clone());
    budget.set_limits(global.high(), global.high(), u32::MAX);
    let pool = PacketPool::new(budget.memory_handle(PeerId(7)), 4096, cached);
    let warm = (0..cached).map(|_| pool.get().expect("warm-cache alloc")).collect::<Vec<_>>();
    drop(warm);
    let start = Instant::now();
    for _ in 0..iterations {
        let packet = pool.get().expect("borrow from cached pool");
        std::hint::black_box(packet.capacity());
        drop(packet);
    }
    let ns = start.elapsed().as_nanos();
    assert_eq!(pool.in_use(), 0);
    assert_eq!(pool.cached(), cached);
    assert_eq!(global.reserved(), (cached * 4096) as u64);
    pool.trim_cached_to(0);
    assert_eq!(global.reserved(), 0);
    ns
}

fn main() {
    let mut args = std::env::args().skip(1);
    let cached: usize = args.next().expect("cached").parse().unwrap();
    let iterations: usize = args.next().unwrap_or_else(|| "200000".into()).parse().unwrap();
    let trials: usize = args.next().unwrap_or_else(|| "9".into()).parse().unwrap();
    assert!(cached > 0 && cached <= 8192 && iterations > 1000 && trials >= 3);
    for _ in 0..3 {
        std::hint::black_box(sample(cached, iterations));
    }
    let mut values = (0..trials).map(|_| sample(cached, iterations)).collect::<Vec<_>>();
    values.sort_unstable();
    println!(
        "cached={cached} iterations={iterations} trials={trials} ns_per_op={:.3} min_ns={} median_ns={} max_ns={}",
        values[values.len() / 2] as f64 / iterations as f64,
        values[0],
        values[values.len() / 2],
        values[values.len() - 1]
    );
}
