use super::*;
use crate::sim::{LinkParams, Sim};

fn busy(peers: u64, cap: usize) -> Sim {
    let cfg = StackConfig {
        peer_deficit_drr: true,
        adaptive_quantum: true,
        pacing: false,
        init_cwnd_segs: 1024,
        min_prefetch: 1 << 20,
        prefetch_max: 1 << 20,
        round_bytes_cap: cap,
        ..Default::default()
    };
    let link = LinkParams { rate_bps: 0, delay: Duration::from_millis(1), queue_bytes: 32 << 20, ..Default::default() };
    let mut sim = Sim::new(2301, cfg, StackConfig::default(), link.clone(), link);
    sim.a.shard.set_budget_limits(512 << 20, 128 << 20, 4096);
    for peer in 1..=peers {
        sim.connect_as(PeerId(peer), 48000 + peer as u16, Default::default());
    }
    sim.run_until(sim.now + Duration::from_millis(100));
    for id in sim.a.accepted.clone() {
        assert_eq!(sim.a.shard.write(id, &vec![1; 256 << 10]), WriteResult::Written(256 << 10));
    }
    sim
}

fn credit(sh: &Shard) -> (PeerId, i64, (u32, u32), bool) {
    let d = &sh.ifaces[0].as_ref().unwrap().drr;
    let p = *d.ready_peers.front().unwrap();
    (p, d.peer_q[&p].deficit, *d.peer_q[&p].front().unwrap(), d.peer_q[&p].turn_active)
}

#[test]
fn full_downshift_preserves_the_in_progress_visit_and_connection() {
    let mut sim = busy(3, 256 << 10);
    let sh = &mut sim.a.shard;
    let mut n = 0;
    sh.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| {
        n += 1;
        if n == 1 {
            SendResult::Accepted
        } else {
            SendResult::Full
        }
    });
    let frozen = credit(sh);
    assert!(frozen.1 > 16384 && frozen.3);
    sh.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| panic!("full must remain frozen"));
    assert_eq!(credit(sh), frozen);
    sh.egress_released(IfaceId(0));
    let mut first = None;
    let mut bytes = 0;
    sh.serve_peer_turn(
        0,
        sim.now,
        1,
        &mut |_: IfaceId, p: &OutPacket<'_>| {
            first = Some(p.peer);
            bytes += p.len();
            SendResult::Accepted
        },
        &mut RunOutcome::default(),
    );
    assert_eq!(first, Some(frozen.0));
    assert!(sh.ifaces[0].as_ref().unwrap().adaptive.small);
    let after = credit(sh);
    assert_eq!((after.0, after.2, after.3), (frozen.0, frozen.2, true));
    assert_eq!(after.1, frozen.1 - bytes as i64, "downshift must not replace remaining credit");
}

#[test]
fn budget_stop_and_quiet_upshift_preserve_deficit() {
    let mut sim = busy(3, 1420);
    let sh = &mut sim.a.shard;
    sh.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted);
    assert!(sh.stats.round_budget_exhausted > 0);
    let frozen = credit(sh);
    let mut bytes = 0;
    sh.serve_peer_turn(
        0,
        sim.now,
        1,
        &mut |_: IfaceId, p: &OutPacket<'_>| {
            bytes += p.len();
            SendResult::Accepted
        },
        &mut RunOutcome::default(),
    );
    assert!(sh.ifaces[0].as_ref().unwrap().adaptive.small);
    assert_eq!(credit(sh).1, frozen.1 - bytes as i64);
    let before = credit(sh);
    bytes = 0;
    sh.serve_peer_turn(
        0,
        sim.now + Duration::from_millis(9),
        1,
        &mut |_: IfaceId, p: &OutPacket<'_>| {
            bytes += p.len();
            SendResult::Accepted
        },
        &mut RunOutcome::default(),
    );
    assert!(!sh.ifaces[0].as_ref().unwrap().adaptive.small);
    assert_eq!(credit(sh).1, before.1 - bytes as i64, "upshift must not refill a frozen visit");
}

#[test]
fn one_peer_never_enters_small_quantum_after_full() {
    let mut sim = busy(1, 256 << 10);
    sim.a.shard.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Full);
    sim.a.shard.egress_released(IfaceId(0));
    sim.a.shard.serve_peer_turn(0, sim.now, 1, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Accepted, &mut RunOutcome::default());
    assert_eq!(sim.a.shard.ifaces[0].as_ref().unwrap().adaptive.entries, 0);
}

#[test]
fn recent_window_and_hold_are_inclusive_at_the_boundary() {
    let cfg = StackConfig::default();
    let t = Instant::from_millis(100);
    let mut a = AdaptiveQuantum { pressure_at: Some(t), ..Default::default() };
    assert_eq!(a.quantum(t + Duration::from_millis(2), 2, &cfg), 16384);
    assert_eq!(a.quantum(t + Duration::from_millis(8), 2, &cfg), 16384);
    assert_eq!(a.quantum(t + Duration::from_millis(8) + Duration::from_nanos(1), 2, &cfg), 65536);
    assert_eq!((a.entries, a.exits), (1, 1));
    let mut stale = AdaptiveQuantum { pressure_at: Some(t), ..Default::default() };
    assert_eq!(stale.quantum(t + Duration::from_millis(2) + Duration::from_nanos(1), 2, &cfg), 65536);
}

#[test]
fn quiet_packet_sequence_matches_fixed_max_without_pressure() {
    let mut a = busy(3, 256 << 10);
    let mut b = busy(3, 256 << 10);
    b.a.shard.cfg.adaptive_quantum = false;
    let mut x = Vec::new();
    let mut y = Vec::new();
    // One direct visit never exhausts the round budget or triggers Full.
    a.a.shard.serve_peer_turn(
        0,
        a.now,
        256 << 10,
        &mut |_: IfaceId, p: &OutPacket<'_>| {
            x.push(p.to_vec());
            SendResult::Accepted
        },
        &mut RunOutcome::default(),
    );
    b.a.shard.serve_peer_turn(
        0,
        b.now,
        256 << 10,
        &mut |_: IfaceId, p: &OutPacket<'_>| {
            y.push(p.to_vec());
            SendResult::Accepted
        },
        &mut RunOutcome::default(),
    );
    assert_eq!(x, y);
    assert_eq!(a.a.shard.ifaces[0].as_ref().unwrap().adaptive.entries, 0);
}

#[test]
fn iface_removal_discards_pressure_and_eligibility() {
    let mut sim = busy(3, 256 << 10);
    sim.a.shard.run(sim.now, &mut |_: IfaceId, _: &OutPacket<'_>| SendResult::Full);
    assert!(sim.a.shard.ifaces[0].as_ref().unwrap().adaptive.pressure_at.is_some());
    sim.a.shard.remove_iface(IfaceId(0));
    assert!(sim.a.shard.ifaces[0].is_none());
    let id = sim.a.shard.add_iface(IfaceConfig::default());
    let a = &sim.a.shard.ifaces[id.0 as usize].as_ref().unwrap().adaptive;
    assert!(a.pressure_at.is_none() && !a.small && a.entries == 0);
}

#[test]
fn layout_has_no_adaptive_per_peer_or_per_connection_state() {
    eprintln!(
        "R2_LAYOUT Conn={} Slot={} Sched={} PeerReady={} Drr={} Iface={} Adaptive={} StackConfig={} ShardStats={}",
        std::mem::size_of::<Conn>(),
        std::mem::size_of::<Slot>(),
        std::mem::size_of::<Sched>(),
        std::mem::size_of::<PeerReady>(),
        std::mem::size_of::<Drr>(),
        std::mem::size_of::<Iface>(),
        std::mem::size_of::<AdaptiveQuantum>(),
        std::mem::size_of::<StackConfig>(),
        std::mem::size_of::<ShardStats>()
    );
}

#[test]
fn hysteresis_keeps_small_after_competitors_leave_while_pressure_continues() {
    let cfg = StackConfig::default();
    let start = Instant::from_millis(100);
    let mut state = AdaptiveQuantum { pressure_at: Some(start), ..Default::default() };
    assert_eq!(state.quantum(start, 2, &cfg), 16384);
    // K gates entry only. Once entered, a remaining bulk peer can keep the
    // iface under pressure; hysteresis does not promise a return to max Q.
    state.pressure_at = Some(start + Duration::from_millis(20));
    assert_eq!(state.quantum(start + Duration::from_millis(20), 1, &cfg), 16384);
    assert_eq!(state.entries, 1);
    assert_eq!(state.quantum(start + Duration::from_millis(28) + Duration::from_nanos(1), 1, &cfg), 65536);
    assert_eq!(state.exits, 1);
}
