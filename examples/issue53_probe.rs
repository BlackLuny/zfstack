// Issue #53 probe: Budget::try_reserve repeated-table-lookup cost.
// Run under `perf stat -e instructions` on baseline vs candidate.
// Each iteration hits the Occupied success path (a persistent 1-byte
// reservation keeps every peer entry alive), so the baseline hashes PeerId
// twice (get + entry) and the candidate once.
use std::hint::black_box;
use std::time::Instant;
use zfstack::budget::{Budget, GlobalBudget};
use zfstack::PeerId;

fn main() {
    let iters: u64 = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(20_000_000);
    let peers: u64 = std::env::args().nth(2).and_then(|s| s.parse().ok()).unwrap_or(64);
    let global = GlobalBudget::new(1 << 30);
    let mut budget = Budget::new(global);
    // Keep every peer entry resident for the whole run.
    for i in 0..peers {
        assert!(budget.try_reserve(PeerId(i + 1), 1, false), "persistent reserve");
    }
    // Warm the table and the branch predictors.
    for i in 0..1_000_000u64 {
        let p = PeerId(i % peers + 1);
        assert!(budget.try_reserve(p, 64, false));
        budget.cancel_reserve(p, 64);
    }
    let t = Instant::now();
    for i in 0..iters {
        let p = PeerId(i % peers + 1);
        let ok = budget.try_reserve(black_box(p), black_box(64), false);
        assert!(ok);
        budget.cancel_reserve(p, 64);
    }
    let el = t.elapsed();
    println!("iters={iters} peers={peers} wall={} ns_per_iter={:.3}", el.as_nanos(), el.as_nanos() as f64 / iters as f64);
}
