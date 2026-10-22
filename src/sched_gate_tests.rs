//! Gate and event-reconciliation regressions; no production test switches.
use super::*;
use crate::sim::{LinkParams, Sim};
use crate::wire::TcpOptions;

fn blocked() -> (Sim, ConnId) {
    let cfg = StackConfig { pacing: false, min_prefetch: 256 << 10, prefetch_max: 256 << 10, ..Default::default() };
    let link = LinkParams { rate_bps: 0, delay: Duration::from_millis(1), ..Default::default() };
    let mut sim = Sim::new(2203, cfg, StackConfig::default(), link.clone(), link);
    sim.connect(46001, Default::default());
    sim.run_until(sim.now + Duration::from_millis(100));
    let id = sim.a.accepted[0];
    assert_eq!(sim.a.shard.write(id, &vec![1; 128 << 10]), WriteResult::Written(128 << 10));
    sim.a.shard.run(sim.now, &mut |_: IfaceId, _: &crate::OutPacket<'_>| crate::SendResult::Accepted);
    assert!(!sim.a.shard.test_conn_event(id, |c, _| c.wants_tx()));
    assert!(!sim.a.shard.test_conn_ready(id));
    (sim, id)
}

fn ack(c: &mut Conn, ctx: &mut Ctx, now: Instant, off: u64, window: u16, opts: TcpOptions) {
    let h = TcpHeader {
        src_port: c.remote.port(),
        dst_port: c.local.port(),
        seq: c.rx_sp.seq(c.rcv_nxt),
        ack: c.tx_sp.seq(off),
        flags: ACK,
        window,
        opts,
        data_off: 20,
    };
    c.process_ack(now, &h, c.rcv_nxt, off, ctx);
}

#[test]
fn ack_progress_and_cc_window_growth_reschedule_after_event() {
    let (mut sim, id) = blocked();
    sim.a.shard.test_conn_event(id, |c, ctx| {
        ack(c, ctx, sim.now, c.snd_max, 65535, TcpOptions::default());
        assert!(c.wants_tx());
        assert!(c.plan(sim.now, Pressure::Free).is_some());
    });
    assert!(sim.a.shard.test_conn_ready(id));
}

#[test]
fn pure_window_update_reschedules_without_ack_progress() {
    let (mut sim, id) = blocked();
    sim.a.shard.test_conn_event(id, |c, ctx| {
        ack(c, ctx, sim.now, c.snd_max, 0, TcpOptions::default());
        assert!(!c.wants_tx());
    });
    assert!(!sim.a.shard.test_conn_ready(id));
    sim.a.shard.test_conn_event(id, |c, ctx| {
        ack(c, ctx, sim.now, c.snd_una, 65535, TcpOptions::default());
        assert!(c.wants_tx());
    });
    assert!(sim.a.shard.test_conn_ready(id));
}

#[test]
fn sack_and_loss_marking_reschedule_and_preserve_empty_pipe_exception() {
    let (mut sim, id) = blocked();
    sim.a.shard.test_conn_event(id, |c, ctx| {
        let mut opts = TcpOptions { sack_n: 1, ..Default::default() };
        opts.sack[0] = (c.tx_sp.seq(c.snd_una + 2 * c.mss as u64), c.tx_sp.seq(c.snd_max));
        ack(c, ctx, sim.now + Duration::from_millis(5), c.snd_una, 65535, opts);
        c.sb.mark_lost(0);
        assert!(c.wants_tx());
        assert!(c.plan(sim.now, Pressure::Free).is_some());
    });
    assert!(sim.a.shard.test_conn_ready(id));
}

#[test]
fn rto_and_cwnd_reset_reenter_service_through_timer_event() {
    let (mut sim, id) = blocked();
    let at = sim.now + Duration::from_millis(1);
    sim.a.shard.test_conn_event(id, |c, _| c.timer = Some((TimerKind::Rto, at)));
    let before = sim.a.shard.stats().tx_packets;
    sim.a.shard.run(at, &mut |_: IfaceId, _: &crate::OutPacket<'_>| crate::SendResult::Accepted);
    assert!(sim.a.shard.stats().tx_packets > before);
    assert!(sim.a.shard.info(id).unwrap().stats.bytes_retrans > 0);
}

#[test]
fn prr_quota_opening_reenters_ready_after_event() {
    let (mut sim, id) = blocked();
    sim.a
        .shard
        .test_conn_event(id, |c, _| c.recovery = Some(Recovery { point: c.snd_max, prr_delivered: 0, prr_out: 0, recover_fs: c.pipe().max(1), quota: 0 }));
    assert!(!sim.a.shard.test_conn_ready(id));
    sim.a.shard.test_conn_event(id, |c, _| {
        c.recovery.as_mut().unwrap().quota = c.mss as u64;
        assert!(c.wants_tx());
    });
    assert!(sim.a.shard.test_conn_ready(id));
}

#[test]
fn controls_tlp_fin_and_retransmissions_bypass_new_data_gate() {
    for kind in 0..5 {
        let (mut sim, id) = blocked();
        sim.a.shard.test_conn_event(id, |c, _| {
            c.snd_wnd = 0;
            match kind {
                0 => c.ack_need = AckNeed::Now,
                1 => c.probe_pending = true,
                2 => c.tlp.pending = true,
                3 => c.rst_pending = true,
                _ => {
                    c.sb.mark_lost(0);
                    c.recovery = None;
                }
            }
            assert!(c.wants_tx());
            assert!(c.plan(sim.now, Pressure::Free).is_some());
        });
        assert!(sim.a.shard.test_conn_ready(id));
    }
    exercise_fin();
}

fn exercise_fin() {
    let (mut sim, id) = blocked();
    sim.a.shard.test_conn_event(id, |c, ctx| {
        ack(c, ctx, sim.now, c.snd_max, 65535, TcpOptions::default());
        // A FIN still behind unsent data must not bypass the data gate.
        c.shutdown_write();
        c.snd_wnd = 0;
        assert!(!c.wants_tx());
        // A standalone FIN retains its prior behavior even at zero rwnd.
        c.fin_off = None;
        c.state = State::Established;
        c.snd_wnd = 1 << 20;
        c.cc = crate::cc::new_cc(crate::CcAlgo::Cubic, c.mss, 1024);
        while c.unsent() > 0 {
            assert!(c.try_reserve_send_record(ctx.budget));
            let p = c.plan(sim.now, Pressure::Free).unwrap();
            c.commit(&p, sim.now, ctx.cfg);
        }
        ack(c, ctx, sim.now, c.snd_max, 65535, TcpOptions::default());
        c.shutdown_write();
        c.snd_wnd = 0;
        assert!(c.wants_tx());
        assert!(c.plan(sim.now, Pressure::Free).is_some());
    });
}

#[test]
fn randomized_events_never_leave_a_sendable_plan_outside_ready() {
    let (mut sim, id) = blocked();
    let mut rng = crate::sim::Rng::new(2204);
    for i in 0..4096 {
        let choice = rng.next_u64() % 9;
        let now = sim.now + Duration::from_micros(i);
        let (wants, can_plan) = sim.a.shard.test_conn_event(id, |c, ctx| {
            match choice {
                0 => c.snd_wnd = 0,
                1 => c.snd_wnd = 65535,
                2 => {
                    if c.snd_una < c.snd_max {
                        ack(c, ctx, now, c.snd_max, 65535, TcpOptions::default());
                    }
                }
                3 => c.cc = crate::cc::new_cc(crate::CcAlgo::Cubic, c.mss, 128),
                4 => c.cc = crate::cc::new_cc(crate::CcAlgo::Cubic, c.mss, 1),
                5 => c.tlp.pending = true,
                6 => c.ack_need = AckNeed::Now,
                7 => {
                    if !c.sb.recs.is_empty() {
                        c.sb.mark_lost(0);
                    }
                }
                _ => {
                    let _ = c.write(&[1; 256], ctx);
                }
            }
            let wants = c.wants_tx();
            let plan = c.plan(now, Pressure::Free);
            let can = plan.is_some();
            // Commit only a real plan; generated events are reconciled by the
            // same after_conn_change -> schedule path as application changes.
            if let Some(p) = plan {
                c.commit(&p, now, ctx.cfg);
            }
            assert!(wants || !can, "false-negative gate at event {i}");
            (c.wants_tx(), can)
        });
        assert!(!wants || sim.a.shard.test_conn_ready(id), "lost wake at event {i}; planned={can_plan}");
        sim.a.shard.run(now, &mut |_: IfaceId, _: &crate::OutPacket<'_>| crate::SendResult::Accepted);
        sim.a.shard.check_invariants();
    }
}
