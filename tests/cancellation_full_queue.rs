//! Cancellation while the send queue is full.
#![cfg(feature = "test-peer")]
use std::time::Duration;
use zfstack::sim::{AppConn, LinkParams, Sim};
use zfstack::{StackConfig, WriteResult};

#[test]
fn cancellation_while_send_queue_full_reclaims_budget() {
    let link = LinkParams { rate_bps: 1_000_000_000, delay: Duration::from_millis(5), ..Default::default() };
    let mut s = Sim::new(77, StackConfig::default(), StackConfig::default(), link.clone(), link);

    // Set a moderate budget so we can fill it.
    let limit = 256 * 1024;
    s.a.shard.set_budget_limits(limit, limit, 10);

    // The client is configured to never read, so the window stays zero or fills up quickly.
    // 'a' is the server, 'b' is the client making the connection.
    s.a.accept_template = AppConn::default();
    s.connect(10000, AppConn { read_paused_until: s.now + Duration::from_secs(100), ..Default::default() });

    s.run_until(s.now + Duration::from_millis(50));

    assert_eq!(s.a.accepted.len(), 1);
    let id = s.a.accepted[0];

    // Fill the send queue until it is full.
    let data = vec![0x42; 16384];
    let mut sent = 0;
    for _ in 0..100 {
        match s.a.shard.write(id, &data) {
            WriteResult::Written(n) => sent += n,
            WriteResult::WouldBlock | WriteResult::QuotaBlocked | WriteResult::MemoryBlocked => break,
            WriteResult::Closed => panic!("unexpected closed"),
        }
        s.run_until(s.now + Duration::from_millis(1));
    }

    assert!(sent > 0, "must have written some data");

    // Check invariants and that some physical memory is actually reserved.
    s.a.shard.check_invariants();
    let global_a = s.a.shard.budget().global().clone();
    assert!(global_a.reserved() > 0, "budget must be reserved for the queued data");

    // Now abort the connection while the send queue holds data.
    s.a.shard.abort(id);

    // Process the abort event.
    s.run_until(s.now + Duration::from_millis(50));

    // Sim drops AppConn map entries manually when we abort directly via Shard
    s.a.conns.remove(&id);

    // Let everything settle
    s.run_until(s.now + Duration::from_secs(1));

    // Drop sim to release final shard references if needed, though they shouldn't hold much
    let global = s.a.shard.budget().global().clone();
    assert_eq!(s.a.shard.budget().used, 0, "cancellation did not reclaim port logical budget");
    drop(s);

    // Budget should be fully reclaimed.
    assert_eq!(global.reserved(), 0, "cancellation did not reclaim global budget");
    assert_eq!(global.cached_bytes(), 0, "cancellation left cached bytes");
}
