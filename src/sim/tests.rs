//! End-to-end scenarios on the deterministic simulator. Every step checks the
//! invariants of §14.4 (scoreboard accounting, window edges, queue uniqueness).

use super::*;
use crate::{CcAlgo, CloseReason, StackConfig};

fn secs(s: u64) -> Duration {
    Duration::from_secs(s)
}

fn sim(seed: u64, down: LinkParams, up: LinkParams) -> Sim {
    Sim::new(seed, StackConfig::default(), StackConfig::default(), down, up)
}

fn download(sim: &mut Sim, bytes: u64, limit: Duration) -> (ConnId, AppConn) {
    sim.a.accept_template = AppConn { to_send: bytes, fin_after_send: true, ..Default::default() };
    let id = sim.connect(40000, AppConn::default());
    let end = sim.now + limit;
    while sim.now < end {
        let t = sim.now + Duration::from_millis(100);
        sim.run_until(t);
        let c = &sim.b.conns[&id];
        if c.eof {
            break;
        }
    }
    (id, sim.b.conns[&id].clone())
}

fn assert_download_ok(c: &AppConn, bytes: u64) {
    assert!(!c.corrupt, "byte stream corrupted");
    assert_eq!(c.received, bytes, "received {} of {}", c.received, bytes);
    assert!(c.eof, "no EOF");
}

#[test]
fn clean_download() {
    let mut s = sim(1, LinkParams::default(), LinkParams::default());
    let start = s.now;
    let (_, c) = download(&mut s, 20 << 20, secs(20));
    assert_download_ok(&c, 20 << 20);
    let took = s.now - start;
    // 20 MiB at 100 Mbit/s ≈ 1.68 s + slow start; allow some slack.
    assert!(took < Duration::from_millis(2600), "took {took:?}");
    assert_eq!(s.ab.stats.random_drops, 0);
    let st = &s.a.shard.stats();
    assert_eq!(st.rx_dropped_parse, 0);
}

#[test]
fn download_with_random_loss() {
    for (seed, loss) in [(2, 0.001), (3, 0.01), (4, 0.03), (5, 0.05)] {
        let down = LinkParams { loss, ..Default::default() };
        let mut s = sim(seed, down, LinkParams { loss: loss / 2.0, ..Default::default() });
        let (_, c) = download(&mut s, 8 << 20, secs(120));
        assert_download_ok(&c, 8 << 20);
    }
}

#[test]
fn download_with_burst_loss_reorder_dup() {
    for seed in 10..16 {
        let down = LinkParams {
            loss: 0.01,
            burst_continue: 0.5,
            reorder: 0.02,
            reorder_delay: Duration::from_millis(3),
            duplicate: 0.01,
            ..Default::default()
        };
        let up = LinkParams { loss: 0.005, reorder: 0.01, duplicate: 0.01, ..Default::default() };
        let mut s = sim(seed, down, up);
        let (_, c) = download(&mut s, 4 << 20, secs(120));
        assert_download_ok(&c, 4 << 20);
    }
}

#[test]
fn shallow_queue_long_rtt() {
    // 200 Mbit/s, 80 ms RTT, queue = 0.25 BDP: loss comes only from the bottleneck.
    let bdp = 200_000_000 / 8 * 80 / 1000;
    let p = LinkParams { rate_bps: 200_000_000, delay: Duration::from_millis(40), queue_bytes: bdp / 4, ..Default::default() };
    let mut s = sim(21, p.clone(), p);
    let start = s.now;
    let (_, c) = download(&mut s, 64 << 20, secs(60));
    assert_download_ok(&c, 64 << 20);
    let took = (s.now - start).as_secs_f64();
    let mbps = (64u64 << 20) as f64 * 8.0 / took / 1e6;
    assert!(mbps > 100.0, "goodput {mbps:.1} Mbit/s");
}

#[test]
fn upload() {
    let mut s = sim(31, LinkParams { loss: 0.01, ..Default::default() }, LinkParams { loss: 0.01, ..Default::default() });
    s.a.accept_template = AppConn::default();
    let id = s.connect(40001, AppConn { to_send: 6 << 20, fin_after_send: true, ..Default::default() });
    s.run_until(s.now + secs(60));
    let srv = s.a.accepted[0];
    let c = &s.a.conns[&srv];
    assert!(!c.corrupt);
    assert_eq!(c.received, 6 << 20);
    assert!(c.eof);
    assert!(s.b.conns[&id].fin_sent);
}

#[test]
fn bidirectional() {
    let mut s = sim(32, LinkParams { loss: 0.005, ..Default::default() }, LinkParams { loss: 0.005, ..Default::default() });
    s.a.accept_template = AppConn { to_send: 3 << 20, fin_after_send: true, ..Default::default() };
    let id = s.connect(40002, AppConn { to_send: 3 << 20, fin_after_send: true, ..Default::default() });
    s.run_until(s.now + secs(60));
    let srv = s.a.accepted[0];
    assert_download_ok(&s.b.conns[&id], 3 << 20);
    let a = &s.a.conns[&srv];
    assert!(!a.corrupt && a.eof && a.received == 3 << 20);
    // Both sides closed; after TIME_WAIT everything is released.
    s.run_until(s.now + secs(70));
    assert_eq!(s.a.shard.conn_count(), 0, "server connections left");
    assert_eq!(s.b.shard.conn_count(), 0, "client connections left");
    assert_eq!(s.a.shard.budget().used, 0);
}

#[test]
fn zero_window_and_persist() {
    let mut cfg_b = StackConfig::default();
    cfg_b.max_rcv_buf = 128 * 1024;
    cfg_b.init_rcv_wnd = 32 * 1024;
    let mut s = Sim::new(41, StackConfig::default(), cfg_b, LinkParams::default(), LinkParams::default());
    s.a.accept_template = AppConn { to_send: 2 << 20, fin_after_send: true, ..Default::default() };
    let id = s.connect(40003, AppConn::default());
    // Pause the reader for 5 s right away: the window closes, the sender persists.
    s.b.conns.get_mut(&id).unwrap().read_paused_until = s.now + secs(5);
    s.run_until(s.now + secs(4));
    let srv = s.a.accepted[0];
    let info = s.a.shard.info(srv).unwrap();
    assert_eq!(info.snd_wnd, 0, "window should be closed");
    assert_eq!(info.pipe, 0);
    assert!(info.stats.zero_window_probes > 0, "persist probes expected");
    s.run_until(s.now + secs(30));
    assert_download_ok(&s.b.conns[&id], 2 << 20);
}

#[test]
fn egress_full_does_not_count_as_sent() {
    let mut s = sim(51, LinkParams::default(), LinkParams::default());
    s.a.accept_template = AppConn { to_send: 4 << 20, fin_after_send: true, ..Default::default() };
    let id = s.connect(40004, AppConn::default());
    s.run_until(s.now + Duration::from_millis(300));
    // Block the sink for 2 s.
    s.a.egress_budget = Some(0);
    let srv = s.a.accepted[0];
    let before = s.a.shard.info(srv).unwrap();
    let runs_before = s.a.shard.stats().runs;
    s.run_until(s.now + secs(2));
    let during = s.a.shard.info(srv).unwrap();
    assert_eq!(before.stats.bytes_sent, during.stats.bytes_sent, "nothing may count as sent while Full");
    // No busy loop: the shard is not re-run for a blocked sink.
    let runs = s.a.shard.stats().runs - runs_before;
    assert!(runs < 2000, "busy loop: {runs} runs");
    s.a.egress_budget = None;
    s.a.shard.egress_released(s.a.iface);
    s.run_until(s.now + secs(20));
    assert_download_ok(&s.b.conns[&id], 4 << 20);
}

#[test]
fn syn_cookies() {
    let mut cfg_a = StackConfig::default();
    cfg_a.syn_backlog = 0;
    let mut s = Sim::new(61, cfg_a, StackConfig::default(), LinkParams::default(), LinkParams::default());
    s.a.accept_template = AppConn { to_send: 1 << 20, fin_after_send: true, ..Default::default() };
    let id = s.connect(40005, AppConn::default());
    s.run_until(s.now + secs(10));
    assert!(s.a.shard.stats().syn_cookies_ok >= 1);
    assert_download_ok(&s.b.conns[&id], 1 << 20);
    let srv = s.a.accepted[0];
    let _ = srv;
}

#[test]
fn mtu_decrease_resegments() {
    let mut s = sim(71, LinkParams { loss: 0.01, ..Default::default() }, LinkParams::default());
    s.a.accept_template = AppConn { to_send: 8 << 20, fin_after_send: true, ..Default::default() };
    let id = s.connect(40006, AppConn::default());
    s.run_until(s.now + Duration::from_millis(400));
    s.a.shard.set_iface_mtu(s.a.iface, 1280);
    let srv = s.a.accepted[0];
    s.run_until(s.now + secs(40));
    assert_download_ok(&s.b.conns[&id], 8 << 20);
    let _ = srv;
}

#[test]
fn close_with_unread_data_sends_rst() {
    let mut s = sim(81, LinkParams::default(), LinkParams::default());
    s.a.accept_template = AppConn::default();
    // Client sends 100 KiB; server never reads, then closes -> RST.
    let id = s.connect(40007, AppConn { to_send: 100 * 1024, ..Default::default() });
    s.a.accept_template.read_paused_until = Instant::MAX;
    s.run_until(s.now + Duration::from_millis(10));
    // Accept happened with the old template; pause reading explicitly.
    for c in s.a.conns.values_mut() {
        c.read_paused_until = Instant::MAX;
    }
    s.run_until(s.now + Duration::from_millis(500));
    let srv = s.a.accepted[0];
    let now = s.now;
    s.a.shard.close(now, srv);
    s.a.conns.remove(&srv);
    s.run_until(s.now + Duration::from_millis(500));
    assert_eq!(s.b.conns[&id].closed, Some(CloseReason::Reset));
    assert_eq!(s.a.shard.conn_count(), 0);
}

#[test]
fn many_connections_and_full_release() {
    let mut s = sim(91, LinkParams { loss: 0.005, ..Default::default() }, LinkParams { loss: 0.005, ..Default::default() });
    s.a.accept_template = AppConn { to_send: 50_000, fin_after_send: true, ..Default::default() };
    let mut ids = Vec::new();
    for i in 0..200u16 {
        ids.push(s.connect(20000 + i, AppConn { to_send: 10_000, fin_after_send: true, ..Default::default() }));
    }
    s.run_until(s.now + secs(30));
    for id in &ids {
        let c = &s.b.conns[id];
        assert_download_ok(c, 50_000);
    }
    for id in &s.a.accepted {
        let c = &s.a.conns[id];
        assert!(!c.corrupt && c.received == 10_000 && c.eof);
    }
    s.run_until(s.now + secs(70));
    assert_eq!(s.a.shard.conn_count(), 0);
    assert_eq!(s.b.shard.conn_count(), 0);
    assert_eq!(s.a.shard.budget().used, 0, "budget must return to 0");
    let (t, p, _) = s.a.shard.container_sizes();
    assert_eq!((t, p), (0, 0));
}

#[test]
fn per_peer_fairness() {
    // G-fair (§8.2, §15): the bottleneck is the local egress (sink rate-limited).
    // Peer 1 opens 1 download, peer 2 opens 16; per-peer DRR should split ~50/50.
    let p = LinkParams { rate_bps: 0, delay: Duration::from_millis(5), ..Default::default() };
    let mut s = sim(101, p.clone(), p);
    s.a.egress_rate = Some(100_000_000);
    s.a.accept_template = AppConn { to_send: u64::MAX, ..Default::default() };
    let one = s.connect_as(PeerId(1), 30000, AppConn::default());
    let mut many = Vec::new();
    for i in 0..16 {
        many.push(s.connect_as(PeerId(2), 31000 + i, AppConn::default()));
    }
    s.check_invariants = false;
    s.run_until(s.now + secs(2));
    let r0: Vec<u64> = std::iter::once(one).chain(many.iter().copied()).map(|i| s.b.conns[&i].received).collect();
    s.run_until(s.now + secs(4));
    let r1: Vec<u64> = std::iter::once(one).chain(many.iter().copied()).map(|i| s.b.conns[&i].received).collect();
    let single = (r1[0] - r0[0]) as f64;
    let group: f64 = r1[1..].iter().zip(&r0[1..]).map(|(a, b)| (a - b) as f64).sum();
    let ratio = single / group;
    let total_mbps = (single + group) * 8.0 / 4.0 / 1e6;
    eprintln!("fairness: single={single} group={group} ratio={ratio:.2} total={total_mbps:.1} Mbit/s");
    assert!((0.7..=1.3).contains(&ratio), "G-fair ratio {ratio:.2}");
    assert!(total_mbps > 85.0, "egress underused: {total_mbps:.1}");
}

#[test]
fn execution_gap_pacing_keeps_rate() {
    // Shard only gets CPU every 5 ms: pacing credit must still allow full rate (§7.2).
    let p = LinkParams { rate_bps: 200_000_000, delay: Duration::from_millis(20), queue_bytes: 2_000_000, ..Default::default() };
    let mut s = sim(111, p.clone(), p);
    s.gap_a = Duration::from_millis(5);
    let start = s.now;
    let (_, c) = download(&mut s, 64 << 20, secs(60));
    assert_download_ok(&c, 64 << 20);
    let mbps = (64u64 << 20) as f64 * 8.0 / (s.now - start).as_secs_f64() / 1e6;
    eprintln!("gap 5ms goodput {mbps:.1} Mbit/s credit {:?}", s.a.shard.pacing_credit());
    assert!(mbps > 120.0, "goodput {mbps:.1}");
}

#[test]
fn deterministic_replay() {
    let run = || {
        let mut s = sim(123, LinkParams { loss: 0.02, reorder: 0.02, ..Default::default() }, LinkParams::default());
        let (_, c) = download(&mut s, 2 << 20, secs(60));
        (c.received, s.steps, s.ab.stats.random_drops, s.now)
    };
    assert_eq!(run(), run());
}

#[test]
fn bbr_and_brutal_complete() {
    for cc in [CcAlgo::Bbr, CcAlgo::Brutal(8_000_000)] {
        let mut cfg = StackConfig::default();
        cfg.cc = cc;
        let mut s = Sim::new(131, cfg, StackConfig::default(), LinkParams { loss: 0.01, ..Default::default() }, LinkParams::default());
        let (_, c) = download(&mut s, 8 << 20, secs(60));
        assert_download_ok(&c, 8 << 20);
    }
}
