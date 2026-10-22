//! Public API contract for downstream polling; no zfc changes.
use zfstack::{Shard, ShardStats, StackConfig};
#[test]
fn scheduler_counters_are_readable_without_private_modules() {
    let shard = Shard::new(StackConfig::default());
    let s: &ShardStats = shard.stats();
    let counters: [u64; 9] = [
        s.runs,
        s.serve_calls,
        s.serve_zero,
        s.sink_full,
        s.round_budget_exhausted,
        s.share_retry_visits,
        s.share_retry_blocked,
        s.share_wakes,
        s.share_wake_noop,
    ];
    assert_eq!(counters, [0; 9]);
}
