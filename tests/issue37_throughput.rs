//! Same test can run unchanged on the pre-fix revision for comparison.
#![cfg(feature = "test-peer")]
use std::time::Duration;
use zfstack::sim::{AppConn, LinkParams, Sim};
use zfstack::{PeerId, StackConfig, WriteResult};

fn pattern_at(pattern: &[u8], offset: u64, len: usize) -> &[u8] {
    let start = (offset % 251) as usize;
    &pattern[start..start + len]
}

#[test]
fn issue37_floor_pipelines_at_50ms_rtt() {
    floor_pipelines(64, 1_000_000_000);
}

#[test]
#[ignore = "full 256-stream throughput comparison; the scaled regression runs in CI"]
fn issue37_full_256_streams_at_50ms_rtt() {
    floor_pipelines(256, 4_000_000_000);
}

fn floor_pipelines(senders: u16, rate_bps: u64) {
    let mut rates = Vec::new();
    for write_size in [1024, 64 << 10] {
        let link = LinkParams { rate_bps, delay: Duration::from_millis(25), queue_bytes: usize::from(senders) * (128 << 10), ..Default::default() };
        let mut s = Sim::new(3702, StackConfig::default(), StackConfig::default(), link.clone(), link);
        // Per-packet checks scan all flows; sample invariants every virtual ms
        // so this throughput regression remains practical at high fan-out.
        s.check_invariants = false;
        let port = u64::from(senders) * (128 << 10);
        s.a.shard.set_budget_limits(port, port, 512);
        for n in 0..senders {
            s.connect(43000 + n, AppConn::default());
        }
        s.run_until(s.now + Duration::from_millis(200));
        assert_eq!(s.a.accepted.len(), senders as usize);
        let ids = s.a.accepted.clone();
        let pattern: Vec<u8> = (0..(65536 + 251)).map(|i| (i % 251) as u8).collect();
        let mut sent = vec![0; senders as usize];
        let mut start_bytes = 0;
        let mut peak = 0;
        let clients: Vec<_> = s.b.conns.keys().copied().collect();
        let mut progress = vec![(0, 0); clients.len()];
        let mut max_gap_ms = 0;
        // Register every real sender before allowing any to fill its share.
        // Otherwise the first application can occupy the whole empty port.
        for (i, &id) in ids.iter().enumerate() {
            let WriteResult::Written(n) = s.a.shard.write(id, pattern_at(&pattern, 0, write_size)) else { panic!("initial write") };
            sent[i] = n as u64;
        }
        for ms in 0..3000 {
            for (i, &id) in ids.iter().enumerate() {
                while let WriteResult::Written(n) = s.a.shard.write(id, pattern_at(&pattern, sent[i], write_size)) {
                    assert!(n > 0);
                    sent[i] += n as u64;
                }
            }
            s.run_until(s.now + Duration::from_millis(1));
            s.a.shard.check_invariants();
            s.b.shard.check_invariants();
            peak = peak.max(s.a.shard.budget().physical_used());
            for (i, id) in clients.iter().enumerate() {
                let received = s.b.conns[id].received;
                if received != progress[i].0 {
                    progress[i] = (received, ms);
                } else if received > 0 {
                    let gap = ms - progress[i].1;
                    max_gap_ms = max_gap_ms.max(gap);
                    assert!(gap < 1000, "flow {i} stalled at {received} bytes for {gap} ms");
                }
            }
            if ms == 1999 {
                assert_eq!(s.a.shard.budget().send_share(PeerId(1)), zfstack::budget::MIN_SEND_SHARE);
                start_bytes = s.b.conns.values().map(|c| c.received).sum::<u64>();
            }
        }
        let bytes = s.b.conns.values().map(|c| c.received).sum::<u64>() - start_bytes;
        let window_ceiling = (u64::from(senders) * zfstack::budget::MIN_SEND_SHARE * 20).min(rate_bps / 8);
        eprintln!(
            "issue37 RTT: write={write_size} bytes_per_sec={bytes} window_ceiling={window_ceiling} peak={peak}/{port} max_gap_ms={max_gap_ms} cached={} senders={}",
            s.a.shard.budget().global().cached_bytes(),
            s.a.shard.budget().senders()
        );
        rates.push((write_size, bytes, window_ceiling));
        assert!(peak <= port);
        assert!(s.b.conns.values().all(|c| !c.corrupt && c.received > 0));
        assert_eq!((s.ab.stats.queue_drops, s.ba.stats.queue_drops), (0, 0));
        let global = s.a.shard.budget().global().clone();
        drop(s);
        assert_eq!(global.reserved(), 0);
    }
    for (write_size, bytes, _) in rates {
        // The #39 pressure gate delivered 320 / 382 Mbit/s at 64 flows.
        // Reusing the ACKed prefix delivers 639 Mbit/s without a second block.
        // Keep slack, but reject a return to either pressure-gated result.
        let min_bps = 560_000_000;
        let minimum = min_bps / 8 * u64::from(senders) / 64;
        assert!(bytes > minimum, "write={write_size}: {bytes} B/s, expected >{minimum} B/s");
    }
}
