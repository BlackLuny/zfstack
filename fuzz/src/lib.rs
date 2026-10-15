//! Version 1 bounded fuzz inputs. These public functions also run seed smoke
//! tests without coverage instrumentation; smoke is not coverage-guided fuzzing.
use std::time::Duration;
use zfstack::budget::{Budget, GlobalBudget};
use zfstack::scoreboard::{Rec, Scoreboard, F_LOST, F_SACKED};
use zfstack::seq::{Seq, SeqSpace};
use zfstack::sim::{AppConn, LinkParams, Sim};
use zfstack::{wire, Instant, PeerId, StackConfig};

pub fn wire_parser(data: &[u8]) {
    let data = &data[..data.len().min(4096)];
    if let Ok(ip) = wire::parse_ip(data) {
        let unchecked = wire::parse_tcp_with(data, &ip, false);
        if let Ok(header) = wire::parse_tcp(data, &ip) {
            let raw = unchecked.unwrap();
            assert_eq!(header.seq, raw.seq);
            assert_eq!(header.opts, raw.opts);
            assert!(header.data_off <= ip.l4_len);
            assert!(header.opts.sack_blocks().len() <= wire::MAX_SACK_BLOCKS);
        }
    }
    // An IP envelope with trusted checksum reaches malformed TCP options even
    // when random bytes would otherwise fail the IP/TCP checksum immediately.
    let total = (20 + data.len()) as u16;
    let mut packet = vec![0; 20];
    packet[0] = 0x45;
    packet[2..4].copy_from_slice(&total.to_be_bytes());
    packet[8] = 64;
    packet[9] = wire::PROTO_TCP;
    packet[12..16].copy_from_slice(&[10, 0, 0, 1]);
    packet[16..20].copy_from_slice(&[10, 0, 0, 2]);
    let checksum = wire::checksum_fold(wire::sum_bytes(0, &packet));
    packet[10..12].copy_from_slice(&checksum.to_be_bytes());
    packet.extend_from_slice(data);
    let ip = wire::parse_ip(&packet).unwrap();
    if let Ok(tcp) = wire::parse_tcp_with(&packet, &ip, false) {
        assert!(tcp.data_off >= 20 && tcp.data_off <= data.len());
        assert!(tcp.opts.sack_n <= 4);
        assert!(tcp.opts.wscale.is_none_or(|scale| scale <= 14));
    }
}

pub fn sequence_sack(data: &[u8]) {
    let mut prefix = [0u8; 16];
    let n = data.len().min(prefix.len());
    prefix[..n].copy_from_slice(&data[..n]);
    let isn = Seq(u32::from_le_bytes(prefix[..4].try_into().unwrap()));
    // Keep reference well within i64; SeqSpace's contract resolves ±2^31.
    let reference = u64::from_le_bytes(prefix[4..12].try_into().unwrap()) & ((1 << 61) - 1);
    let delta = i16::from_le_bytes(prefix[12..14].try_into().unwrap()) as i64;
    let space = SeqSpace { isn };
    let sequence = space.seq(reference).add(delta as u32);
    assert_eq!(space.off(sequence, reference), reference as i64 + delta);

    let global = GlobalBudget::new(2 << 20);
    let mut budget = Budget::new(global.clone());
    let mut board = Scoreboard::default();
    assert!(board.try_reserve(96, &mut budget, PeerId(1)));
    const END: u64 = 1 + 32 * 64;
    for index in 0..32 {
        let start = 1 + index * 64;
        board.push(Rec {
            start,
            end: start + 64,
            xmit: Instant::ZERO,
            delivered: 0,
            delivered_ts: Instant::ZERO,
            first_tx_ts: Instant::ZERO,
            tx_in_flight: 0,
            lost_at_send: 0,
            flags: 0,
            app_limited: false,
            seg: 32,
        });
    }
    let mut ack = 1;
    for command in data.get(16..).unwrap_or_default().chunks_exact(5).take(128) {
        let left = u16::from_le_bytes([command[1], command[2]]) as u64 % END;
        let right = u16::from_le_bytes([command[3], command[4]]) as u64 % END;
        match command[0] % 7 {
            0 => {
                board.sack(left.min(right), left.max(right), |_| {});
            }
            1 => {
                ack = ack.max(left);
                board.ack_to(ack, |_| {});
            }
            2 if !board.is_empty() => {
                board.mark_lost(left as usize % board.recs.len());
            }
            3 if !board.is_empty() => {
                let index = left as usize % board.recs.len();
                if board.recs[index].has(F_LOST) && !board.recs[index].has(F_SACKED) {
                    board.mark_retransmitted(index, Instant::from_millis(1));
                }
            }
            4 => {
                board.renege_all();
            }
            5 => {
                board.mark_all_lost();
            }
            _ => {
                board.next_lost();
            }
        }
        board.check();
        assert!(global.reserved() <= global.high());
    }
    board.ack_to(END, |_| {});
    board.check();
    assert!(board.is_empty());
    drop((board, budget));
    assert_eq!(global.reserved(), 0);
}

/// byte 0 selects legal / finite fault / malformed; byte 1 sets a bounded
/// payload; each remaining pair is one operation plus an argument (max 32).
/// This exercises the existing poll-based Sim; it does NOT prove event-only
/// application wakeups. The harness L0-E and adapter A0 own that separate claim.
pub fn event_script(data: &[u8]) {
    let class = data.first().copied().unwrap_or(0) % 3;
    let total = (data.get(1).copied().unwrap_or(0) as u64 + 1) * 64;
    let link = LinkParams { delay: Duration::from_millis(1), ..LinkParams::default() };
    let mut sim = Sim::new(0xF022_0001, StackConfig::default(), StackConfig::default(), link.clone(), link);
    sim.borrowed_server_ingress = true;
    sim.a.accept_template = AppConn { to_send: total, fin_after_send: true, ..AppConn::default() };
    let id = sim.connect(45000, AppConn { to_send: total, fin_after_send: true, ..AppConn::default() });
    let budgets = [sim.a.shard.budget().global().clone(), sim.b.shard.budget().global().clone()];
    for event in data.get(2..).unwrap_or_default().chunks_exact(2).take(32) {
        let duration = Duration::from_millis((event[1] % 5 + 1) as u64);
        match event[0] % 6 {
            0 => {
                sim.b.conns.get_mut(&id).unwrap().read_paused_until = sim.now + duration;
            }
            1 => {
                for app in sim.a.conns.values_mut() {
                    app.read_paused_until = sim.now + duration;
                }
            }
            2 if class == 1 => {
                sim.ab.p.loss = if event[1] & 1 == 0 { 1.0 } else { 0.25 };
            }
            3 if class == 1 => {
                sim.a.egress_budget = Some(0);
            }
            4 => {
                sim.b.conns.get_mut(&id).unwrap().read_paused_until = sim.now + duration;
            }
            _ if class == 2 => {
                let packet = bytes::Bytes::copy_from_slice(&data[..data.len().min(4096)]);
                sim.a.shard.ingress(sim.now, sim.a.iface, PeerId(1), packet);
            }
            _ => {}
        }
        sim.run_until(sim.now + duration);
        for budget in &budgets {
            assert!(budget.reserved() <= budget.high());
        }
    }
    // The fault contract guarantees recovery only after a finite fault ends.
    sim.ab.p.loss = 0.0;
    sim.ba.p.loss = 0.0;
    sim.a.egress_budget = None;
    sim.a.shard.egress_released(sim.a.iface);
    for app in sim.a.conns.values_mut().chain(sim.b.conns.values_mut()) {
        app.read_paused_until = Instant::ZERO;
    }
    sim.run_until(sim.now + Duration::from_secs(10));
    sim.a.shard.check_invariants();
    sim.b.shard.check_invariants();
    if class != 2 {
        assert_eq!(sim.a.accepted.len(), 1);
        for app in sim.a.conns.values().chain(sim.b.conns.values()) {
            assert!(!app.corrupt);
            assert_eq!(app.sent, total);
            assert_eq!(app.received, total);
            assert!(app.eof, "finite-fault trace failed to half-close");
        }
    }
    drop(sim);
    for budget in &budgets {
        assert_eq!(budget.reserved(), 0, "all owners dropped after fuzz script");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn checked_in_seed_corpus() {
        for (name, run) in [("wire_parser", wire_parser as fn(&[u8])), ("sequence_sack", sequence_sack), ("event_script", event_script)] {
            let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("corpus").join(name);
            let mut files: Vec<_> = std::fs::read_dir(path).unwrap().map(|f| f.unwrap().path()).collect();
            files.sort();
            assert!(!files.is_empty(), "required seed corpus missing for {name}");
            for file in files {
                eprintln!("seed_file={}", file.display());
                run(&std::fs::read(file).unwrap());
            }
        }
    }
    #[test]
    fn bounded_input_smoke_all_oracles() {
        for seed in 0..64u64 {
            let mut rng = zfstack::sim::Rng::new(seed);
            let data: Vec<_> = (0..128).map(|_| rng.next_u64() as u8).collect();
            eprintln!("deterministic_smoke_seed={seed} input={data:02x?}");
            wire_parser(&data);
            sequence_sack(&data);
            event_script(&data);
        }
    }
}
