//! Quota-debt and real zero-window cancellation coverage for RX capacity leases.
use zfstack::budget::{Budget, GlobalBudget, Level, Tier};
use zfstack::buf::RxQueue;
use zfstack::PeerId;

#[test]
fn rx_descriptor_bulk_admission_retries_only_after_each_level_debt_is_released() {
    for level in [Level::Global, Level::Port, Level::Peer] {
        let high = 1 << 20;
        let global = GlobalBudget::new(high);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(high, high, 8);
        let peer = PeerId(1);
        let debt = budget.admit_debt(peer, level, 4096);
        let limit = high - global.headroom(high, Tier::Bulk) - 4096;
        // A fresh 64-byte compact owner needs 128 B, its first descriptor
        // allocation 256 B. Leave one byte less than their combined charge.
        let blocker = budget.try_allocate(peer, limit - 383).unwrap();
        let before = global.reserved();
        let mut rx = RxQueue::default();
        assert!(!rx.push_charged(&[7; 64], &mut budget, peer));
        assert!(rx.is_empty());
        assert_eq!(global.reserved(), before);
        let index = match level {
            Level::Global => 0,
            Level::Port => 1,
            Level::Peer => 2,
        };
        assert_eq!(budget.stats().failures_by_level()[index], 1);
        assert_eq!(debt.released(), 0);
        drop(debt);
        assert_eq!(budget.admission_debt(), [0; 3]);
        assert!(rx.push_charged(&[7; 64], &mut budget, peer));
        rx.clear();
        drop(blocker);
        assert_eq!(global.reserved(), 0);
        assert_eq!(global.cached_bytes(), 0);
    }
}

#[cfg(all(feature = "tokio", feature = "test-peer"))]
mod zero_window {
    use bytes::Bytes;
    use tokio::sync::mpsc;
    use zfstack::budget::GlobalBudget;
    use zfstack::tokio_adapter::*;
    use zfstack::*;
    #[tokio::test(flavor = "current_thread")]
    async fn rx_descriptor_cancel_after_observed_zero_window_releases_budget() {
        use tokio::io::AsyncWriteExt;
        // The server app writes more than fits and drops the stream; the client never
        // reads, so its window stays at zero and the adapter's tx queue never drains.
        // The orphan timeout must still end the connection (RST to the client).
        let (to_client_tx, mut to_client_rx) = mpsc::unbounded_channel::<Vec<u8>>();
        let egress = move |_i: IfaceId, _p: PeerId, pkt: &OutPacket<'_>| {
            let _ = to_client_tx.send(pkt.to_vec());
            SendResult::Accepted
        };
        let mut cfg = StackConfig::default();
        cfg.orphan_timeout = std::time::Duration::from_millis(500);
        let server_global = GlobalBudget::new(1 << 30);
        let (h, mut acc, ids, task) = spawn_with_budget(cfg, StreamConfig::default(), vec![IfaceConfig::default()], egress, server_global.clone());
        tokio::spawn(async move {
            let mut s = acc.accept().await.unwrap();
            let data = vec![7u8; 32 << 20];
            // More than the shard's send buffer takes, so the adapter's queue backs up.
            let r = tokio::time::timeout(std::time::Duration::from_millis(300), s.write_all(&data)).await;
            assert!(r.is_err(), "write should still be blocked by the zero window");
            drop(s);
        });

        let epoch = tokio::time::Instant::now();
        let now = || zfstack::Instant::from_nanos(epoch.elapsed().as_nanos() as u64 + 1);
        let mut c = Shard::with_budget(StackConfig::default(), zfstack::budget::GlobalBudget::new(1 << 30));
        let ci = c.add_iface(IfaceConfig::default());
        let id = c.connect(now(), ci, PeerId(9), "10.0.0.2:5555".parse().unwrap(), "10.0.0.1:80".parse().unwrap());
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(20);
        let mut max_rx = 0;
        let mut zero_window_advertisements = 0;
        let reason = loop {
            assert!(tokio::time::Instant::now() < deadline, "orphan never torn down");
            max_rx = max_rx.max(c.info(id).map_or(0, |i| i.rx_queued));
            let mut out: Vec<(PeerId, Bytes)> = Vec::new();
            let mut sink = |_: IfaceId, p: &OutPacket<'_>| {
                let packet = p.to_vec();
                let ip = zfstack::wire::parse_ip(&packet).unwrap();
                let tcp = zfstack::wire::parse_tcp(&packet, &ip).unwrap();
                if tcp.has(zfstack::wire::ACK) && tcp.window == 0 {
                    zero_window_advertisements += 1;
                }
                out.push((PeerId(9), Bytes::from(packet)));
                SendResult::Accepted
            };
            c.run(now(), &mut sink);
            if !out.is_empty() {
                assert!(h.ingress(ids[0], out).await);
            }
            let mut closed = None;
            while let Some(ev) = c.poll_event() {
                if let Event::Closed(i, r) = ev {
                    if i == id {
                        closed = Some(r);
                    }
                }
            }
            if let Some(r) = closed {
                break r;
            }
            let wait = c.next_deadline().map_or(std::time::Duration::from_millis(20), |d| {
                std::time::Duration::from_nanos(d.as_nanos().saturating_sub(now().as_nanos())).min(std::time::Duration::from_millis(20))
            });
            if let Ok(Some(p)) = tokio::time::timeout(wait, to_client_rx.recv()).await {
                c.ingress(now(), ci, PeerId(9), Bytes::from(p));
                while let Ok(p) = to_client_rx.try_recv() {
                    c.ingress(now(), ci, PeerId(9), Bytes::from(p));
                }
            }
        };
        assert_eq!(reason, CloseReason::Reset);
        assert!(zero_window_advertisements > 0, "cancellation scenario must observe a real zero-window ACK");
        assert!(max_rx >= 64 * 1024, "client window never filled ({max_rx})");
        task.shutdown_and_join().await.unwrap();
        assert_eq!(server_global.connection_counts(), (0, 0));
        assert_eq!(server_global.cached_bytes(), 0);
        assert_eq!(server_global.reserved(), 0, "adapter/core backing survived cancellation");
        println!("RX_CAPACITY_CANCEL zero_window_advertisements={zero_window_advertisements} reason={reason:?} final_reserved=0 final_cached=0");
    }
}
