//! Reproducer for the PacketPool idle-retirement maintenance cost (#23).
//! cargo run --release --example issue23_trim_scaling -- <buffers> <trials> [hot=32] [floor=0]
//! Construction, activity tracking, payload release, and validation are outside
//! the timed window. Validate exact survivor identity + LIFO order after trim.

use std::time::{Duration, Instant};
use zfstack::budget::{Budget, GlobalBudget};
use zfstack::pktpool::PacketPool;
use zfstack::PeerId;

fn sample(count: usize, hot: usize, floor: usize) -> u128 {
    let global = GlobalBudget::new(64 << 20);
    let mut budget = Budget::new(global.clone());
    budget.set_limits(global.high(), global.high(), u32::MAX);
    let pool = PacketPool::new(budget.memory_handle(PeerId(7)), 64, count);
    let mut blocks = (0..count).map(|_| pool.get().expect("pool allocation")).collect::<Vec<_>>();
    let original: Vec<_> = blocks.iter_mut().map(|p| p.spare_capacity_mut().as_mut_ptr() as usize).collect();
    drop(blocks);

    let start = Instant::now();
    let idle = Duration::from_secs(5);
    assert_eq!(pool.trim_idle(start, idle, floor), 0);
    let mut reused = (0..hot).map(|_| pool.get().expect("cached packet")).collect::<Vec<_>>();
    let touched: Vec<_> = reused.iter_mut().map(|p| p.spare_capacity_mut().as_mut_ptr() as usize).collect();
    drop(reused);

    // Returned hot buffers are appended to the LIFO tail in drop order.
    let mut expected_order = original[..count - hot].to_vec();
    expected_order.extend(touched);
    let keep = floor.max(hot);
    let expected: Vec<_> = expected_order.into_iter().rev().take(keep).collect();

    let measured = Instant::now();
    let freed = pool.trim_idle(start + idle, idle, floor);
    let elapsed = measured.elapsed().as_nanos();

    assert_eq!(freed, count - keep);
    assert_eq!(pool.cached(), keep);
    assert_eq!(pool.in_use(), 0);
    assert_eq!(global.reserved(), (keep * 64) as u64);
    let mut survivors = (0..keep).map(|_| pool.get().expect("retained packet")).collect::<Vec<_>>();
    let actual: Vec<_> = survivors.iter_mut().map(|p| p.spare_capacity_mut().as_mut_ptr() as usize).collect();
    assert_eq!(actual, expected, "trim must retain the exact newest-to-oldest LIFO tail");
    drop(survivors);
    pool.trim_cached_to(0);
    assert_eq!(global.reserved(), 0);
    elapsed
}

fn main() {
    let mut args = std::env::args().skip(1);
    let count: usize = args.next().expect("cached_buffers").parse().unwrap();
    let trials: usize = args.next().unwrap_or_else(|| "11".into()).parse().unwrap();
    let hot: usize = args.next().unwrap_or_else(|| "32".into()).parse().unwrap();
    let floor: usize = args.next().unwrap_or_else(|| "0".into()).parse().unwrap();
    assert!(count > 32 && hot <= count && floor <= count && trials >= 3);
    for _ in 0..3 {
        std::hint::black_box(sample(count, hot, floor));
    }
    let mut values = (0..trials).map(|_| sample(count, hot, floor)).collect::<Vec<_>>();
    values.sort_unstable();
    println!(
        "count={count} hot={hot} floor={floor} trials={trials} min_ns={} median_ns={} max_ns={}",
        values[0],
        values[values.len() / 2],
        values[values.len() - 1]
    );
}
