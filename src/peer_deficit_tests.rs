use super::*;
use crate::sim::{LinkParams, Sim};

fn peers(quantum: usize, cap: usize, heavy: usize) -> Sim {
    peers_with_cwnd(quantum, cap, heavy, 1024)
}

fn peers_with_cwnd(quantum: usize, cap: usize, heavy: usize, cwnd: u32) -> Sim {
    let cfg = StackConfig {
        pacing: false,
        peer_deficit_drr: true,
        peer_drr_quantum: quantum,
        round_bytes_cap: cap,
        init_cwnd_segs: cwnd,
        min_prefetch: 1 << 20,
        prefetch_max: 1 << 20,
        ..Default::default()
    };
    let link = LinkParams { rate_bps: 0, delay: Duration::from_millis(1), queue_bytes: 32 << 20, ..Default::default() };
    let mut sim = Sim::new(2205, cfg, StackConfig::default(), link.clone(), link);
    sim.a.shard.set_budget_limits(512 << 20, 128 << 20, 4096);
    for (peer, n) in [(1, heavy), (2, 1), (3, 1)] {
        for i in 0..n {
            sim.connect_as(PeerId(peer), 47000 + (peer as u16) * 100 + i as u16, Default::default());
        }
    }
    sim.run_until(sim.now + Duration::from_millis(100));
    for id in sim.a.accepted.clone() {
        assert_eq!(sim.a.shard.write(id, &vec![1; 256 << 10]), WriteResult::Written(256 << 10));
    }
    sim
}

#[test]
fn real_peer_drr_bounds_intervening_wire_bytes_with_packet_rounding() {
    for q in [65536, 16384, 2736] {
        let mut sim = peers(q, 256 << 10, 16);
        let mut packets = Vec::new();
        for _ in 0..64 {
            sim.a.shard.run(sim.now, &mut |_: IfaceId, p: &OutPacket<'_>| {
                packets.push((p.peer, p.len()));
                SendResult::Accepted
            });
            let sums = [1, 2, 3].map(|peer| packets.iter().filter(|(p, _)| p.0 == peer).map(|(_, n)| n).sum::<usize>());
            if sums.iter().all(|&n| n > 128 << 10) {
                break;
            }
        }
        let max_packet = packets.iter().map(|(_, n)| *n).max().unwrap();
        let bound = 2 * (q + max_packet - 1);
        let mut total = 0;
        let mut last = HashMap::new();
        let mut worst = 0;
        for (peer, n) in packets {
            if let Some(end) = last.insert(peer, total + n) {
                worst = worst.max(total - end);
                assert!(total - end <= bound, "q={q} peer={peer:?} gap={} bound={bound}", total - end);
            }
            total += n;
        }
        assert_eq!(last.len(), 3);
        eprintln!("peer Q={q}: worst intervening wire bytes={worst}, packetized bound={bound}, ideal bound={}", 2 * q);
    }
}

#[test]
fn one_peer_visit_serves_multiple_connections_and_resets_empty_deficit() {
    let mut sim = peers_with_cwnd(65536, 256 << 10, 3, 4);
    // Use the public shutdown-free data path; enough credit for several
    // cwnd-limited sibling connections is exercised by a smaller initial cwnd.
    let peer = PeerId(1);
    let sh = &mut sim.a.shard;
    let mut out = RunOutcome::default();
    let mut ports = HashSet::new();
    sh.serve_peer_turn(
        0,
        sim.now,
        256 << 10,
        &mut |_: IfaceId, p: &OutPacket<'_>| {
            if p.peer == peer {
                ports.insert(u16::from_be_bytes([p.header[22], p.header[23]]));
            }
            SendResult::Accepted
        },
        &mut out,
    );
    assert_eq!(ports.len(), 3);
    let mut drr = Drr::default();
    drr.push(peer, (0, 0));
    drr.peer_q.get_mut(&peer).unwrap().deficit = 55;
    drr.ready_peers.pop_front();
    drr.peer_q.get_mut(&peer).unwrap().pop_front();
    drr.recycle(peer);
    drr.push(peer, (1, 0));
    assert_eq!(drr.peer_q[&peer].deficit, 0);
}

#[test]
fn full_freezes_peer_connection_and_remaining_credit_exactly() {
    let mut sim = peers(16384, 256 << 10, 3);
    let sh = &mut sim.a.shard;
    let mut accepted = 0;
    sh.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| {
        if accepted == 1 {
            SendResult::Full
        } else {
            accepted += 1;
            SendResult::Accepted
        }
    });
    let snapshot = |sh: &Shard| {
        let d = &sh.ifaces[0].as_ref().unwrap().drr;
        let p = *d.ready_peers.front().unwrap();
        (p, d.peer_q[&p].deficit, *d.peer_q[&p].front().unwrap(), d.peer_q[&p].turn_active)
    };
    let frozen = snapshot(sh);
    assert!(frozen.1 > 0 && frozen.3);
    sh.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| panic!("Full iface must not retry"));
    assert_eq!(snapshot(sh), frozen);
    sh.egress_released(IfaceId(0));
    let mut first = None;
    sh.run(sim.now, &mut |_: IfaceId, p: &OutPacket<'_>| {
        if first.is_none() {
            first = Some(p.peer);
            SendResult::Accepted
        } else {
            SendResult::Full
        }
    });
    assert_eq!(first, Some(frozen.0));
    assert!(snapshot(sh).1 < frozen.1, "resume must not earn a second quantum");
}

#[test]
fn round_budget_resumes_unfinished_peer_without_new_credit() {
    let mut sim = peers(16384, 1420, 1);
    let sh = &mut sim.a.shard;
    sh.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted);
    let p = *sh.ifaces[0].as_ref().unwrap().drr.ready_peers.front().unwrap();
    let before = sh.ifaces[0].as_ref().unwrap().drr.peer_q[&p].deficit;
    sh.run(sim.now, &mut |_: IfaceId, pkt: &OutPacket<'_>| {
        assert_eq!(pkt.peer, p);
        SendResult::Accepted
    });
    assert!(sh.ifaces[0].as_ref().unwrap().drr.peer_q[&p].deficit < before);
}

#[test]
fn structure_sizes_are_recorded_for_memory_audit() {
    eprintln!(
        "layout: VecDeque={} PeerReady={} Drr={} Iface={} Sched={} Slot={} Conn={} StackConfig={}",
        std::mem::size_of::<VecDeque<(u32, u32)>>(),
        std::mem::size_of::<PeerReady>(),
        std::mem::size_of::<Drr>(),
        std::mem::size_of::<Iface>(),
        std::mem::size_of::<Sched>(),
        std::mem::size_of::<Slot>(),
        std::mem::size_of::<Conn>(),
        std::mem::size_of::<StackConfig>()
    );
    assert!(std::mem::size_of::<PeerReady>() <= std::mem::size_of::<VecDeque<(u32, u32)>>() + 32);
}
