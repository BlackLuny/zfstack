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
    if s.b.conns[&id].received != 4 << 20 {
        eprintln!("after release: {:?}", s.a.shard.info(srv));
    }
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
        if c.received != 50_000 {
            eprintln!("stuck client {:?}: {:?} info={:?}", id, c, s.b.shard.info(*id));
            let srv = s.a.accepted.iter().find(|x| s.a.shard.info(**x).is_some_and(|i| i.remote == s.b.shard.info(*id).map(|j| j.local).unwrap_or(i.remote)));
            eprintln!("server: {:?}", srv.and_then(|x| s.a.shard.info(*x)));
        }
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

fn goodput_run(cc: CcAlgo, rate: u64, rtt_ms: u64, queue_bdp: f64, loss: f64, bytes: u64, gap: Duration) -> (f64, crate::ConnInfo) {
    let bdp = (rate / 8) as f64 * rtt_ms as f64 / 1000.0;
    let p = LinkParams {
        rate_bps: rate,
        delay: Duration::from_millis(rtt_ms / 2),
        queue_bytes: (bdp * queue_bdp) as usize,
        loss,
        ..Default::default()
    };
    let mut cfg = StackConfig::default();
    cfg.cc = cc;
    let mut s = Sim::new(777, cfg, StackConfig::default(), p.clone(), LinkParams { loss: 0.0, ..p });
    s.gap_a = gap;
    s.check_invariants = false;
    let start = s.now;
    let (_, c) = download(&mut s, bytes, secs(120));
    assert_download_ok(&c, bytes);
    let srv = s.a.accepted[0];
    let info = s.a.shard.info(srv).unwrap_or_else(|| panic!("no info"));
    let mbps = bytes as f64 * 8.0 / (s.now - start).as_secs_f64() / 1e6;
    (mbps, info)
}

#[test]
fn bbr_shallow_queue_long_rtt() {
    let (mbps, info) = goodput_run(CcAlgo::Bbr, 200_000_000, 80, 0.25, 0.0, 240 << 20, Duration::ZERO);
    let rtx = info.stats.bytes_retrans as f64 / info.stats.bytes_sent as f64;
    eprintln!("bbr 80ms/0.25BDP: {mbps:.1} Mbit/s, retrans {:.2}%", rtx * 100.0);
    assert!(mbps > 150.0, "goodput {mbps:.1}");
    assert!(rtx < 0.05, "retransmission ratio {rtx}");
}

#[test]
fn bbr_random_loss_keeps_rate() {
    // BBR must not collapse under 1% random loss (unlike loss-based CC).
    let (mbps, _) = goodput_run(CcAlgo::Bbr, 100_000_000, 40, 2.0, 0.01, 48 << 20, Duration::ZERO);
    eprintln!("bbr 1% loss: {mbps:.1} Mbit/s");
    assert!(mbps > 50.0, "goodput {mbps:.1}");
}

#[test]
#[ignore]
fn bbr_trace() {
    let rate = 200_000_000u64;
    let bdp = (rate / 8) as f64 * 0.08;
    let p = LinkParams { rate_bps: rate, delay: Duration::from_millis(40), queue_bytes: (bdp * 0.25) as usize, ..Default::default() };
    let mut cfg = StackConfig::default();
    cfg.cc = CcAlgo::Bbr;
    let mut s = Sim::new(777, cfg, StackConfig::default(), p.clone(), p);
    s.check_invariants = false;
    s.a.accept_template = AppConn { to_send: u64::MAX, ..Default::default() };
    let id = s.connect(40000, AppConn::default());
    let mut last = 0;
    for i in 0..100 {
        s.run_until(s.now + Duration::from_millis(100));
        let got = s.b.conns[&id].received;
        let Some(&srv) = s.a.accepted.first() else { continue };
        let info = s.a.shard.info(srv).unwrap();
        eprintln!("{:>4}ms {:>6.1}Mbps drops={} rtx={} rto={} tlp={} fr={} {}", i * 100, (got - last) as f64 * 8.0 / 0.1 / 1e6, s.ab.stats.queue_drops, info.stats.bytes_retrans, info.stats.rto_count, info.stats.tlp_count, info.stats.fast_recoveries, info.cc_debug);
        last = got;
    }
}

#[test]
#[ignore]
fn bbr_probe_steady_state() {
    for (q, bytes) in [(0.25, 240u64 << 20), (2.0, 240 << 20)] {
        let (mbps, info) = goodput_run(CcAlgo::Bbr, 200_000_000, 80, q, 0.0, bytes, Duration::ZERO);
        eprintln!("q={q}: {mbps:.1} Mbit/s cwnd={} pacing={:?} srtt={:?} minrtt={:?} rtx={}", info.cwnd, info.pacing_rate, info.srtt, info.min_rtt, info.stats.bytes_retrans);
    }
    for (q, bytes) in [(0.25, 240u64 << 20), (2.0, 240 << 20)] {
        let (mbps, info) = goodput_run(CcAlgo::Cubic, 200_000_000, 80, q, 0.0, bytes, Duration::ZERO);
        eprintln!("cubic q={q}: {mbps:.1} Mbit/s cwnd={} rtx={}", info.cwnd, info.stats.bytes_retrans);
    }
}

#[test]
fn fuzz_mutated_and_random_packets() {
    fuzz_run(4242, 99, true);
}

#[test]
fn fuzz_without_rst() {
    for seed in 0..6 {
        fuzz_run(5000 + seed, 700 + seed, false);
    }
}

fn fuzz_run(sim_seed: u64, fuzz_seed: u64, allow_rst: bool) {
    // Capture real segments of a transfer, then replay mutated copies and random
    // garbage into a live server while the transfer continues. No panics, invariants
    // hold, and the transfer still completes.
    let mut s = sim(sim_seed, LinkParams::default(), LinkParams::default());
    s.a.accept_template = AppConn { to_send: 4 << 20, fin_after_send: true, ..Default::default() };
    let id = s.connect(45000, AppConn::default());
    let mut rng = Rng::new(fuzz_seed);
    let mut corpus: Vec<Vec<u8>> = Vec::new();
    for round in 0..200 {
        s.run_until(s.now + Duration::from_millis(5));
        // Snapshot some in-flight client->server packets.
        for (_, (_, p)) in s.ba.inflight.iter().take(4) {
            if corpus.len() < 256 {
                corpus.push(p.to_vec());
            }
        }
        for _ in 0..20 {
            let mut pkt = if !corpus.is_empty() && rng.chance(0.8) {
                corpus[rng.below(corpus.len() as u64) as usize].clone()
            } else {
                let n = rng.below(120) as usize;
                (0..n).map(|_| rng.next_u64() as u8).collect()
            };
            // Mutate: flip bytes, truncate, or extend.
            match rng.below(4) {
                0 if !pkt.is_empty() => {
                    for _ in 0..1 + rng.below(4) {
                        let i = rng.below(pkt.len() as u64) as usize;
                        pkt[i] ^= 1 << rng.below(8);
                    }
                }
                1 => pkt.truncate(rng.below(pkt.len() as u64 + 1) as usize),
                2 => pkt.extend((0..rng.below(64)).map(|_| rng.next_u64() as u8)),
                _ => {}
            }
            if !allow_rst && pkt.len() >= 34 && pkt[0] >> 4 == 4 {
                let ihl = ((pkt[0] & 0xf) as usize) * 4;
                if pkt.len() > ihl + 13 {
                    pkt[ihl + 13] &= !crate::wire::RST;
                }
            }
            if rng.chance(0.5) || !allow_rst {
                fix_checksums(&mut pkt);
            }
            let now = s.now;
            s.a.shard.ingress(now, s.a.iface, PeerId(1), Bytes::from(pkt));
        }
        s.a.shard.check_invariants();
        let _ = round;
    }
    s.run_until(s.now + secs(30));
    let srv = s.a.accepted[0];
    if !s.b.conns[&id].eof {
        // A forged-but-plausible ACK or timestamp (valid checksum, in window) can
        // desynchronize the ends. With timestamps this is detected within a few RTOs
        // and reset (CloseReason::Desync); without them only the user timeout ends
        // it. Either way the connection must be torn down and release everything,
        // never hang forever. (Only the holder of the connection's packets can do
        // this; WG authenticates peers.)
        s.run_until(s.now + secs(150));
    }
    let c = &s.b.conns[&id];
    eprintln!(
        "fuzz end (seed {sim_seed}/{fuzz_seed}): client received={} eof={} closed={:?} corrupt={}; server app={:?}",
        c.received, c.eof, c.closed, c.corrupt, s.a.conns.get(&srv).map(|x| (x.closed, x.eof))
    );
    assert!(!c.corrupt, "stream corrupted");
    if c.eof {
        assert_download_ok(c, 4 << 20);
    } else {
        assert!(c.closed.is_some(), "connection hung without being torn down");
        s.run_until(s.now + secs(70));
        assert_eq!(s.a.shard.conn_count(), 0, "server connection not released");
        assert_eq!(s.a.shard.budget().used, 0, "budget not released");
    }
    let _ = allow_rst;
}

/// Recompute IPv4 header and TCP checksums so mutated packets reach the state machine.
fn fix_checksums(pkt: &mut [u8]) {
    use crate::wire::{checksum_fold, pseudo_sum, sum_bytes};
    if pkt.len() < 40 || pkt[0] >> 4 != 4 {
        return;
    }
    let ihl = ((pkt[0] & 0xf) as usize) * 4;
    let total = u16::from_be_bytes([pkt[2], pkt[3]]) as usize;
    if ihl < 20 || total > pkt.len() || total < ihl + 20 {
        return;
    }
    pkt[10] = 0;
    pkt[11] = 0;
    let c = checksum_fold(sum_bytes(0, &pkt[..ihl]));
    pkt[10..12].copy_from_slice(&c.to_be_bytes());
    let src = std::net::Ipv4Addr::new(pkt[12], pkt[13], pkt[14], pkt[15]);
    let dst = std::net::Ipv4Addr::new(pkt[16], pkt[17], pkt[18], pkt[19]);
    let seg = &mut pkt[ihl..total];
    seg[16] = 0;
    seg[17] = 0;
    let c = checksum_fold(sum_bytes(pseudo_sum(src.into(), dst.into(), seg.len() as u32), seg));
    seg[16..18].copy_from_slice(&c.to_be_bytes());
}

#[test]
fn synack_timeouts_release_half_open_slots() {
    // SYN-ACKs are all lost: half-open connections time out and must stop counting
    // towards the SYN backlog (otherwise the shard stays on SYN cookies forever).
    let mut cfg_a = StackConfig::default();
    cfg_a.syn_backlog = 8;
    let mut s = Sim::new(201, cfg_a, StackConfig::default(), LinkParams { loss: 1.0, ..Default::default() }, LinkParams::default());
    for i in 0..5 {
        s.connect(46000 + i, AppConn::default());
    }
    s.run_until(s.now + Duration::from_millis(100));
    assert_eq!(s.a.shard.half_open_and_time_wait().0, 5);
    // Client SYN retransmissions keep re-triggering SYN-ACKs; allow them to finish.
    s.run_until(s.now + secs(60));
    assert_eq!(s.a.shard.half_open_and_time_wait().0, 0, "half-open counter leaked");
    assert_eq!(s.a.shard.conn_count(), 0);
}

#[test]
fn unread_data_survives_orderly_close() {
    // The server replies and closes without reading the request; the close completes
    // (TIME_WAIT, then its expiry) and the unread request bytes must still be readable.
    let mut s = sim(202, LinkParams::default(), LinkParams::default());
    s.a.accept_template = AppConn { to_send: 10, fin_after_send: true, read_paused_until: Instant::MAX, ..Default::default() };
    let id = s.connect(46100, AppConn { to_send: 40_000, fin_after_send: true, ..Default::default() });
    s.run_until(s.now + secs(3));
    let srv = s.a.accepted[0];
    let info = s.a.shard.info(srv).unwrap();
    assert!(matches!(info.state, crate::State::TimeWait | crate::State::Closed), "state {:?}", info.state);
    assert_eq!(info.rx_queued, 40_000);
    s.run_until(s.now + secs(70));
    let info = s.a.shard.info(srv).unwrap();
    assert_eq!(info.state, crate::State::Closed);
    assert_eq!(info.rx_queued, 40_000, "unread data dropped at close");
    s.a.conns.get_mut(&srv).unwrap().read_paused_until = Instant::ZERO;
    s.run_until(s.now + Duration::from_millis(10));
    let c = &s.a.conns[&srv];
    assert!(!c.corrupt);
    assert_eq!(c.received, 40_000);
    assert!(c.eof);
    assert!(s.b.conns[&id].eof);
    assert_eq!(s.a.shard.conn_count(), 0, "released after the app read and closed");
}

#[test]
fn small_cwnd_cubic_is_not_paced() {
    // A window-based CC below `pacing_min_cwnd_segs` is ACK-clocked, not paced (each paced
    // segment would cost a wakeup); a rate-based CC is paced regardless of its window.
    // 1 Mbit/s × 80 ms keeps the window far below 32 segments even with slow-start overshoot.
    let (_, info) = goodput_run(CcAlgo::Cubic, 1_000_000, 80, 1.0, 0.0, 256 << 10, Duration::ZERO);
    assert_eq!(info.stats.limited_pacing_ns, 0, "small-window CUBIC was paced");
    let (_, info) = goodput_run(CcAlgo::Bbr, 1_000_000, 80, 1.0, 0.0, 256 << 10, Duration::ZERO);
    assert!(info.stats.limited_pacing_ns > 0, "BBR must stay paced");
}

/// TCP header offset of an IPv4 packet.
fn tcp_off(pkt: &[u8]) -> usize {
    ((pkt[0] & 0xf) as usize) * 4
}

/// Offset of the TSval field of the timestamp option, if present.
fn ts_opt_off(pkt: &[u8]) -> Option<usize> {
    let t = tcp_off(pkt);
    let end = t + ((pkt[t + 12] >> 4) as usize) * 4;
    let mut i = t + 20;
    while i < end {
        match pkt[i] {
            0 => return None,
            1 => i += 1,
            8 => return Some(i + 2),
            _ => i += pkt[i + 1].max(2) as usize,
        }
    }
    None
}

/// Start a download, then inject one forged client->server segment built by `forge`
/// from the newest real one. Returns (server close reason, time from injection to close).
fn forged_segment_run(forge: impl FnOnce(&mut Sim, &mut Vec<u8>)) -> (Option<CloseReason>, Duration) {
    let mut s = sim(900, LinkParams::default(), LinkParams::default());
    s.a.accept_template = AppConn { to_send: 64 << 20, fin_after_send: true, ..Default::default() };
    let id = s.connect(46000, AppConn::default());
    s.run_until(s.now + Duration::from_millis(300));
    let mut pkt = s.ba.inflight.values().last().expect("no client segment in flight").1.to_vec();
    assert!(ts_opt_off(&pkt).is_some(), "timestamps not negotiated");
    forge(&mut s, &mut pkt);
    fix_checksums(&mut pkt);
    let t0 = s.now;
    s.a.shard.ingress(t0, s.a.iface, PeerId(1), Bytes::from(pkt));
    let srv = s.a.accepted[0];
    while s.now < t0 + secs(30) && s.a.conns.get(&srv).and_then(|c| c.closed).is_none() {
        s.run_until(s.now + Duration::from_millis(10));
    }
    let reason = s.a.conns.get(&srv).and_then(|c| c.closed);
    let took = s.now - t0;
    assert!(!s.b.conns[&id].corrupt);
    s.run_until(s.now + secs(70));
    assert_eq!(s.a.shard.conn_count(), 0, "server connection not released");
    assert_eq!(s.a.shard.budget().used, 0, "budget not released");
    (reason, took)
}

#[test]
fn forged_ack_desync_is_reset_quickly() {
    // Ack everything in flight to the client, then lose it: the sender has released
    // data the receiver never got. Previously only the 120 s user timeout ended this.
    let (reason, took) = forged_segment_run(|s, pkt| {
        let mut end = 0u32;
        let mut base = None;
        for (_, p) in s.ab.inflight.values() {
            let t = tcp_off(p);
            let seq = u32::from_be_bytes(p[t + 4..t + 8].try_into().unwrap());
            let len = (u16::from_be_bytes([p[2], p[3]]) as usize - t - ((p[t + 12] >> 4) as usize) * 4) as u32;
            let e = seq.wrapping_add(len);
            let b = *base.get_or_insert(seq);
            if e.wrapping_sub(b) > end.wrapping_sub(b) {
                end = e;
            }
        }
        assert!(base.is_some(), "no server data in flight");
        s.ab.inflight.clear();
        let t = tcp_off(pkt);
        pkt[t + 8..t + 12].copy_from_slice(&end.to_be_bytes());
    });
    assert_eq!(reason, Some(CloseReason::Desync));
    assert!(took < secs(10), "took {took:?}");
}

#[test]
fn forged_timestamp_desync_is_reset_quickly() {
    // A TSval far ahead moves TS.Recent, after which PAWS drops every real segment.
    let (reason, took) = forged_segment_run(|_, pkt| {
        let o = ts_opt_off(pkt).unwrap();
        let v = u32::from_be_bytes(pkt[o..o + 4].try_into().unwrap()).wrapping_add(1 << 30);
        pkt[o..o + 4].copy_from_slice(&v.to_be_bytes());
    });
    assert_eq!(reason, Some(CloseReason::Desync));
    assert!(took < secs(10), "took {took:?}");
}

#[test]
fn reordered_and_duplicated_acks_are_not_desync() {
    // Stale ACKs (heavy reordering, long delays, duplicates) must never look like desync.
    let mut rtos = 0;
    // Burst loss forces RTOs (the stall epochs evidence is counted against).
    for seed in 20..28 {
        let burst = if seed >= 24 { 0.7 } else { 0.0 };
        let down = LinkParams {
            loss: 0.02,
            burst_continue: burst,
            reorder: 0.05,
            reorder_delay: Duration::from_millis(30),
            duplicate: 0.05,
            ..Default::default()
        };
        let up = LinkParams {
            loss: 0.01,
            reorder: 0.3,
            reorder_delay: Duration::from_millis(1500),
            duplicate: 0.2,
            ..Default::default()
        };
        let mut s = sim(seed, down, up);
        let (_, c) = download(&mut s, 4 << 20, secs(120));
        assert_download_ok(&c, 4 << 20);
        // Let delayed duplicates arrive while the connection winds down.
        s.run_until(s.now + secs(5));
        let srv = s.a.accepted[0];
        assert_ne!(s.a.conns.get(&srv).and_then(|c| c.closed), Some(CloseReason::Desync));
        rtos += s.a.shard.info(srv).map_or(0, |i| i.stats.rto_count);
    }
    assert!(rtos > 50, "scenario too gentle: {rtos} RTOs");
}
