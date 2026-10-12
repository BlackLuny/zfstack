//! Scenario catalog for L0. Builders stay small on purpose: the matrix is a
//! minimum set, not a full cartesian product (issue #24).

use std::time::Duration;

use serde_json::{json, Value};

use crate::link::{wan, LinkParams};
use crate::report::{matrix_value, scenario_value};
use crate::world::{execute, Config, Expect, FlowSpec, FullFault, Kind, Limits, Report, Topology};

#[derive(Clone, Debug, Default)]
pub struct Overrides {
    pub seed: Option<u64>,
    pub ports: Option<u16>,
    pub lights: Option<u16>,
    pub heavy_bytes: Option<u64>,
    pub heavy_conns: Option<u16>,
    pub until_ms: Option<u64>,
    pub full: bool,
}

pub fn scenario_names() -> &'static [&'static str] {
    &[
        "matrix",
        "tuple-isolation",
        "peer-reuse",
        "same-shard-peer-alias",
        "noisy-same-iface",
        "noisy-cross-iface",
        "noisy-cross-shard",
        "noisy-shared-uplink",
        "heavy-fanout",
        "port-density",
        "quota",
        "zero-window",
        "sink-full",
        "sink-full-iface",
        "idle-reclaim",
        "lossy",
        "iface-rebuild",
        "wall-bulk",
        "crowd",
    ]
}

pub fn run_named(name: &str, o: &Overrides) -> Result<Value, String> {
    if name == "matrix" {
        return Ok(run_matrix(o));
    }
    if let Some(kind) = noisy_kind(name) {
        return Ok(run_noisy(name, kind, o));
    }
    let mut report = execute(build(name, o)?);
    let extra = extra_checks(name, &report);
    report.errors.extend(extra);
    report.ok = report.errors.is_empty();
    let notes = notes_for(name, &report);
    Ok(scenario_value(name, &report, &notes))
}

fn run_matrix(o: &Overrides) -> Value {
    let mut names: Vec<String> = scenario_names().iter().copied().filter(|n| *n != "matrix").map(str::to_string).collect();
    if o.full {
        names.push("port-density".into());
        names.push("port-density".into());
    }
    let mut rows = Vec::new();
    let mut density_n = 0u16;
    for name in &names {
        let mut over = o.clone();
        if o.full && name == "port-density" {
            density_n += 1;
            // The first port-density keeps the caller's port count (default 16).
            // The two extra rows are the high-port coverage.
            if density_n == 2 {
                over.ports = Some(64);
            } else if density_n == 3 {
                over.ports = Some(128);
            }
        }
        match run_named(name, &over) {
            Ok(v) => rows.push(v),
            Err(e) => rows.push(json!({"scenario": name, "ok": false, "errors": [e]})),
        }
    }
    matrix_value(rows)
}

#[derive(Clone, Copy)]
enum Noisy {
    SameIface,
    CrossIface,
    CrossShard,
    SharedUplink,
}

fn noisy_kind(name: &str) -> Option<Noisy> {
    Some(match name {
        "noisy-same-iface" => Noisy::SameIface,
        "noisy-cross-iface" => Noisy::CrossIface,
        "noisy-cross-shard" => Noisy::CrossShard,
        "noisy-shared-uplink" => Noisy::SharedUplink,
        _ => return None,
    })
}

fn run_noisy(name: &str, kind: Noisy, o: &Overrides) -> Value {
    let base = execute(noisy_cfg(kind, o, false));
    let loaded = execute(noisy_cfg(kind, o, true));
    let mut errors = Vec::new();
    if !base.ok {
        errors.extend(base.errors.iter().map(|e| format!("baseline: {e}")));
    }
    if !loaded.ok {
        errors.extend(loaded.errors.iter().map(|e| format!("loaded: {e}")));
    }
    let b = scenario_value(name, &base, &[]);
    let l = scenario_value(name, &loaded, &[]);
    let delta = latency_delta(&b, &l);
    json!({
        "scenario": name,
        "ok": errors.is_empty(),
        "errors": errors,
        "notes": [noisy_note(kind)],
        "clock": "virtual",
        "clock_note": b["clock_note"],
        "seed": loaded.seed,
        "topology": loaded.topology.as_str(),
        "server_shards": loaded.server_shards,
        "ports": loaded.ports,
        "shared_bottleneck": loaded.shared_bottleneck,
        "steps": loaded.steps,
        "wall_sec": base.wall_sec + loaded.wall_sec,
        "process_cpu_sec": base.cpu_sec + loaded.cpu_sec,
        "profile": "StackConfig::default (server). Not StackConfig::client().",
        "comparability": crate::report::COMPARABILITY,
        "budget": l["budget"],
        "baseline": {
            "steps": base.steps,
            "users": b["users"],
            "budget": b["budget"],
        },
        "loaded": {
            "steps": loaded.steps,
            "users": l["users"],
            "budget": l["budget"],
        },
        "sim_latency_delta_us": delta,
        "fairness": "Degradation is reported, not judged. Equal bandwidth across ifaces is not a product guarantee.",
    })
}

fn noisy_note(kind: Noisy) -> &'static str {
    match kind {
        Noisy::SameIface => "heavy and lights share one iface and therefore one link; peers differ",
        Noisy::CrossIface => "heavy is iface 0, lights are other ifaces of the same shard; each iface has its own link",
        Noisy::CrossShard => "one shard per port, one shared GlobalBudget; each port has its own link",
        Noisy::SharedUplink => "cross-iface on one shard, but every port shares one bottleneck queue",
    }
}

fn latency_delta(base: &Value, loaded: &Value) -> Value {
    let mut rows = Vec::new();
    let Some(users) = loaded["users"].as_array() else { return json!(rows) };
    for u in users {
        if u["class"] != "light" {
            continue;
        }
        let port = u["port"].clone();
        let peer = u["peer"].clone();
        let b = base["users"].as_array().and_then(|xs| xs.iter().find(|x| x["class"] == "light" && x["port"] == port && x["peer"] == peer));
        let bp = b.and_then(|x| x["sim_latency"]["p50_us"].as_u64());
        let lp = u["sim_latency"]["p50_us"].as_u64();
        rows.push(json!({
            "port": port,
            "peer": peer,
            "baseline_p50_us": bp,
            "loaded_p50_us": lp,
            "delta_p50_us": match (bp, lp) {
                (Some(a), Some(c)) => Some(c as i64 - a as i64),
                _ => None,
            },
            "loaded_n": u["sim_latency"]["n"],
        }));
    }
    json!(rows)
}

fn build(name: &str, o: &Overrides) -> Result<Config, String> {
    let mut cfg = match name {
        "tuple-isolation" => cfg_tuple(),
        "peer-reuse" => cfg_peer_reuse(),
        "same-shard-peer-alias" => cfg_peer_alias(),
        "heavy-fanout" => cfg_fanout(o),
        "port-density" => cfg_density(o.ports.unwrap_or(16)),
        "quota" => cfg_quota(),
        "zero-window" => cfg_zero_window(),
        "sink-full" => cfg_sink_full(),
        "sink-full-iface" => cfg_sink_full_iface(),
        "crowd" => cfg_crowd(o),
        "idle-reclaim" => cfg_idle(),
        "lossy" => cfg_lossy(),
        "iface-rebuild" => cfg_rebuild(),
        "wall-bulk" => cfg_wall(o.heavy_bytes.unwrap_or(64 * 1024)),
        other => return Err(format!("unknown scenario {other}; try: {}", scenario_names().join(", "))),
    };
    apply(o, &mut cfg);
    Ok(cfg)
}

fn apply(o: &Overrides, cfg: &mut Config) {
    if let Some(seed) = o.seed {
        cfg.seed = seed;
    }
    if let Some(ms) = o.until_ms {
        cfg.until = Duration::from_millis(ms);
    }
}

fn notes_for(name: &str, report: &Report) -> Vec<String> {
    match name {
        "same-shard-peer-alias" => {
            let peak = report.peer_peaks.iter().map(|p| p.2).max().unwrap_or(0);
            let measured = if peak > 8192 + 2048 {
                "measured peer.bytes peak covers both ifaces, so this PeerId shared one ceiling"
            } else {
                "measured peer.bytes peak stayed near a single flow's payload"
            };
            vec![format!(
                "Budget::peers is keyed only by PeerId inside a shard, not by iface. {measured} (peak={peak}). A duplicate PeerId on another shard is a different Budget; peer-reuse checks that."
            )]
        }
        "wall-bulk" => {
            vec!["Headline figures are wall_sec and process_cpu_sec for this fixed byte count. finished_at_us is virtual and is not a latency result.".into()]
        }
        "port-density" => vec!["Port count and connection count both change together here; do not read the result as a per-port fixed cost.".into()],
        _ => Vec::new(),
    }
}

fn extra_checks(name: &str, report: &Report) -> Vec<String> {
    let mut errors = Vec::new();
    match name {
        "zero-window" => {
            let light = report.flows.iter().find(|f| f.class == "light");
            let heavy = report.flows.iter().find(|f| f.class == "heavy");
            match (light.and_then(|f| f.finished_at_ns), heavy.and_then(|f| f.finished_at_ns)) {
                (Some(l), Some(h)) => {
                    if l >= 1_000_000_000 {
                        errors.push(format!("light user finished at {l} ns virtual, not during the 1s read pause"));
                    }
                    if h < 1_000_000_000 {
                        errors.push(format!("heavy download finished at {h} ns virtual, before the receiver resumed"));
                    }
                }
                _ => errors.push("zero-window flows did not record a finish time".into()),
            }
            match heavy {
                Some(h) if h.saw_zero_wnd && h.saw_wnd_reopen => {}
                Some(h) => errors.push(format!(
                    "sender was not window-blocked with unsent data and then offered a larger window (blocked={} reopen={} min_snd_wnd={:?} max_unsent={} at_resume={:?})",
                    h.saw_zero_wnd, h.saw_wnd_reopen, h.min_snd_wnd, h.max_tx_unsent, h.unsent_at_resume
                )),
                None => errors.push("zero-window heavy flow missing".into()),
            }
        }
        "sink-full" => check_backpressure(&mut errors, report, 200_000_000),
        "sink-full-iface" => {
            check_backpressure(&mut errors, report, 200_000_000);
            match report.flows.iter().find(|f| f.class == "open").and_then(|f| f.finished_at_ns) {
                Some(t) if (40_000_000..200_000_000).contains(&t) && !report.flows.iter().find(|f| f.class == "open").is_some_and(|f| f.queued_during_block) => {}
                Some(t) => errors.push(format!("unblocked iface did not finish during the other iface's Full window (finished={t})")),
                None => errors.push("unblocked iface flow did not finish".into()),
            }
        }
        "idle-reclaim" => {
            if report.conns_end != 0 {
                errors.push(format!("connections still in the table after idle: {}", report.conns_end));
            }
            match report.reserved_at_complete {
                Some(at) if report.reserved_end <= at => {}
                Some(at) => errors.push(format!("reserved grew during idle: complete {at} end {}", report.reserved_end)),
                None => errors.push("idle run never reached a settled point".into()),
            }
        }
        "iface-rebuild" => {
            let b = &report.rebuild;
            if !b.happened {
                errors.push("iface rebuild did not run".into());
            }
            if b.live_before == 0 {
                errors.push("rebuild ran before the server had a live connection on the old iface".into());
            }
            if b.still_on_old != 0 {
                errors.push(format!("{} connections still on the old iface after remove_iface", b.still_on_old));
            }
            if !b.server_slots_gone {
                errors.push("remove_iface left a server slot info() could still see".into());
            }
            if !b.client_closed {
                errors.push("client abort did not leave the conn Closed or gone".into());
            }
            if b.freed_delta < b.live_before as u64 {
                errors.push(format!("conns_freed rose by {} but {} conns were live", b.freed_delta, b.live_before));
            }
            if b.reserved_after > b.reserved_before {
                errors.push(format!("reserved grew across rebuild: {} -> {}", b.reserved_before, b.reserved_after));
            }
            if b.untouched_live_before == 0 || b.untouched_live_after != b.untouched_live_before {
                errors.push(format!("untouched iface did not retain live connections across rebuild: {} -> {}", b.untouched_live_before, b.untouched_live_after));
            }
            match report.flows.iter().find(|f| f.class == "other").and_then(|f| f.finished_at_ns) {
                Some(t) if t > 40_000_000 => {}
                _ => errors.push("untouched flow did not make progress through the rebuild".into()),
            }
        }
        "quota" => {
            if report.share_blocked == 0 && report.allocation_failures_by_level == [0; 3] {
                errors.push("quota run never reached a send share or physical budget limit".into());
            }
        }
        "peer-reuse" => {
            for shard in 0..2 {
                let peak = report.peer_peaks.iter().find(|p| p.0 == shard && p.1 == 9).map(|p| p.2).unwrap_or(0);
                if peak == 0 {
                    errors.push(format!("shard {shard} never charged PeerId 9"));
                }
                if peak > 4096 + 8192 {
                    errors.push(format!("shard {shard} PeerId 9 peak {peak} looks like it includes the other shard"));
                }
            }
        }
        _ => {}
    }
    errors
}

fn cfg_tuple() -> Config {
    let mut cfg = Config::skeleton(2, Topology::SharedShard);
    cfg.flows = vec![bulk_on(0, 7, "a", 4096, Some(40_000), None), bulk_on(1, 7, "b", 4096, Some(40_000), None)];
    cfg
}

fn cfg_peer_reuse() -> Config {
    let mut cfg = Config::skeleton(2, Topology::ShardPerPort);
    cfg.flows = vec![bulk(0, 9, "a", 4096), bulk(1, 9, "b", 4096)];
    cfg
}

fn cfg_peer_alias() -> Config {
    let mut cfg = Config::skeleton(2, Topology::SharedShard);
    cfg.flows = vec![bulk(0, 9, "a", 8192), bulk(1, 9, "b", 8192)];
    cfg
}

fn cfg_fanout(o: &Overrides) -> Config {
    let conns = o.heavy_conns.unwrap_or(4) as usize;
    let bytes = o.heavy_bytes.unwrap_or(16 * 1024);
    let lights = o.lights.unwrap_or(2);
    let mut cfg = Config::skeleton(o.ports.unwrap_or(1), Topology::SharedShard);
    cfg.until = Duration::from_secs(4);
    for _ in 0..conns {
        cfg.flows.push(bulk(0, 1, "heavy", bytes));
    }
    for i in 0..lights {
        cfg.flows.push(rr(0, 10 + i as u64, "light", 256, 256, 3));
    }
    cfg
}

fn cfg_density(ports: u16) -> Config {
    let mut cfg = Config::skeleton(ports, Topology::SharedShard);
    cfg.until = Duration::from_secs(3);
    cfg.step_cap = 4_000_000;
    for p in 0..ports {
        cfg.flows.push(bulk(p, 1, "density", 1024));
    }
    cfg
}

fn cfg_quota() -> Config {
    let mut cfg = Config::skeleton(1, Topology::SharedShard);
    cfg.limits = Limits::tight();
    cfg.until = Duration::from_secs(8);
    cfg.step_cap = 4_000_000;
    for i in 0..4u64 {
        // Four senders retain more than their share of the 2 MiB port budget.
        // Completion then requires ACK-driven release and a successful retry.
        cfg.flows.push(bulk(0, 1 + i, "quota", 512 * 1024));
    }
    cfg
}

fn cfg_zero_window() -> Config {
    let mut cfg = Config::skeleton(1, Topology::SharedShard);
    cfg.until = Duration::from_secs(4);
    // The receiver is the load generator. A small buffer is what makes the
    // paused app advertise a zero window instead of absorbing the download.
    cfg.client_max_rcv = Some(32 * 1024);
    cfg.flows.push(FlowSpec {
        port: 0,
        peer: 1,
        class: "heavy",
        tuple: None,
        start_after: Duration::ZERO,
        expect: Expect::Complete,
        kind: Kind::Bulk { from_server: 128 * 1024, from_client: 0 },
        read_pause: Some(Duration::from_secs(1)),
    });
    cfg.flows.push(bulk(0, 2, "light", 8 * 1024));
    cfg
}

fn cfg_sink_full() -> Config {
    // Slow enough that a 256 KiB download is established and still holding
    // payload when the sink starts returning Full at 40 ms. Blocking from t=0
    // only delayed the SYN.
    let mut cfg = Config::skeleton(1, Topology::SharedShard);
    cfg.link = wan(20_000_000, Duration::from_millis(12), 2.0, 0.0);
    cfg.full = Some(FullFault { port: None, from: Duration::from_millis(40), until: Duration::from_millis(200) });
    cfg.until = Duration::from_secs(3);
    cfg.flows.push(bulk(0, 1, "blocked", 256 * 1024));
    cfg
}

fn cfg_sink_full_iface() -> Config {
    let mut cfg = Config::skeleton(2, Topology::SharedShard);
    cfg.link = wan(20_000_000, Duration::from_millis(12), 2.0, 0.0);
    cfg.full = Some(FullFault { port: Some(0), from: Duration::from_millis(40), until: Duration::from_millis(200) });
    cfg.until = Duration::from_secs(3);
    cfg.flows.push(bulk(0, 1, "blocked", 256 * 1024));
    let mut open = bulk(1, 1, "open", 4096);
    // Start inside the fault: an already-finished flow cannot prove isolation.
    open.start_after = Duration::from_millis(50);
    cfg.flows.push(open);
    cfg
}

fn cfg_crowd(o: &Overrides) -> Config {
    // One heavy port beside many light ports. Not a substitute for a long
    // 128-port soak; `--ports 128` opts into that. The 1 KiB density row only
    // checks that many ifaces can handshake.
    let ports = o.ports.unwrap_or(16).max(2);
    let mut cfg = Config::skeleton(ports, Topology::SharedShard);
    cfg.until = Duration::from_secs(3);
    cfg.step_cap = 4_000_000;
    cfg.flows.push(bulk(0, 1, "heavy", o.heavy_bytes.unwrap_or(32 * 1024)));
    for p in 1..ports {
        cfg.flows.push(rr(p, 1, "light", 256, 256, 2));
    }
    cfg
}

fn check_backpressure(errors: &mut Vec<String>, report: &Report, until_ns: u64) {
    let before = report.sink_full_before.unwrap_or(0);
    if report.sink_full_total <= before {
        errors.push(format!("sink Full was not observed (before={before} total={})", report.sink_full_total));
    }
    let Some(flow) = report.flows.iter().find(|f| f.class == "blocked") else {
        errors.push("backpressure flow missing".into());
        return;
    };
    if flow.sent_at_block.unwrap_or(0) == 0 {
        errors.push("sink fault started before any payload had been accepted".into());
    }
    if !flow.queued_during_block {
        errors.push("no unsent stack payload while the sink was Full".into());
    }
    if !flow.progress_after_block {
        errors.push("receiver made no progress after the sink was released".into());
    }
    match flow.finished_at_ns {
        Some(t) if t >= until_ns => {}
        Some(t) => errors.push(format!("transfer finished at {t} ns, inside the Full window")),
        None => errors.push("backpressure flow did not finish".into()),
    }
}

fn cfg_idle() -> Config {
    let mut cfg = Config::skeleton(1, Topology::SharedShard);
    cfg.time_wait = Some(Duration::from_secs(1));
    cfg.until = Duration::from_secs(4);
    cfg.flows.push(bulk(0, 1, "a", 4096));
    cfg.flows.push(bulk(0, 2, "b", 4096));
    cfg
}

fn cfg_lossy() -> Config {
    let mut cfg = Config::skeleton(1, Topology::SharedShard);
    cfg.until = Duration::from_secs(20);
    cfg.step_cap = 4_000_000;
    let base = wan(200_000_000, Duration::from_millis(12), 0.25, 0.01);
    cfg.link = LinkParams { reorder: 0.01, reorder_delay: Duration::from_millis(2), ..base };
    cfg.flows.push(bulk(0, 1, "lossy", 16 * 1024));
    cfg
}

fn cfg_rebuild() -> Config {
    // 40 ms is after the handshake on a 20 Mbit/s, 12 ms link, and before a
    // 256 KiB download can finish. Rebuilding at 5 ms only aborted a SYN.
    let mut cfg = Config::skeleton(2, Topology::SharedShard);
    cfg.link = wan(20_000_000, Duration::from_millis(12), 2.0, 0.0);
    cfg.until = Duration::from_secs(3);
    cfg.rebuild = Some((0, Duration::from_millis(40)));
    cfg.flows.push(FlowSpec {
        port: 0,
        peer: 1,
        class: "old",
        tuple: Some(41_000),
        start_after: Duration::ZERO,
        expect: Expect::Reset,
        kind: Kind::Bulk { from_server: 256 * 1024, from_client: 0 },
        read_pause: None,
    });
    cfg.flows.push(FlowSpec {
        port: 0,
        peer: 1,
        class: "new",
        tuple: Some(41_001),
        start_after: Duration::from_millis(40),
        expect: Expect::Complete,
        kind: Kind::Bulk { from_server: 4096, from_client: 0 },
        read_pause: None,
    });
    // An untouched port must keep its own flow.
    cfg.flows.push(bulk_on(1, 1, "other", 256 * 1024, Some(41_002), None));
    cfg
}

fn cfg_wall(bytes: u64) -> Config {
    let mut cfg = Config::skeleton(1, Topology::SharedShard);
    cfg.until = Duration::from_secs(5);
    cfg.flows.push(bulk(0, 1, "wall", bytes));
    cfg
}

fn noisy_cfg(kind: Noisy, o: &Overrides, heavy: bool) -> Config {
    let lights = o.lights.unwrap_or(4);
    let bytes = o.heavy_bytes.unwrap_or(32 * 1024);
    let (ports, topology, shared) = match kind {
        Noisy::SameIface => (o.ports.unwrap_or(1).max(1), Topology::SharedShard, false),
        Noisy::CrossIface => (o.ports.unwrap_or(4).max(2), Topology::SharedShard, false),
        Noisy::CrossShard => (o.ports.unwrap_or(4).max(2), Topology::ShardPerPort, false),
        Noisy::SharedUplink => (o.ports.unwrap_or(4).max(2), Topology::SharedShard, true),
    };
    let mut cfg = Config::skeleton(ports, topology);
    cfg.shared_bottleneck = shared;
    cfg.until = Duration::from_secs(3);
    if heavy {
        let conns = o.heavy_conns.unwrap_or(1).max(1);
        for _ in 0..conns {
            cfg.flows.push(bulk(0, 1, "heavy", bytes));
        }
    }
    for i in 0..lights {
        let port = match kind {
            Noisy::SameIface => 0,
            _ => 1 + (i % (ports - 1)),
        };
        cfg.flows.push(rr(port, 2 + i as u64, "light", 256, 256, 4));
    }
    apply(o, &mut cfg);
    cfg
}

fn bulk(port: u16, peer: u64, class: &'static str, bytes: u64) -> FlowSpec {
    bulk_on(port, peer, class, bytes, None, None)
}

fn bulk_on(port: u16, peer: u64, class: &'static str, bytes: u64, tuple: Option<u16>, read_pause: Option<Duration>) -> FlowSpec {
    FlowSpec {
        port,
        peer,
        class,
        tuple,
        start_after: Duration::ZERO,
        expect: Expect::Complete,
        kind: Kind::Bulk { from_server: bytes, from_client: 0 },
        read_pause,
    }
}

fn rr(port: u16, peer: u64, class: &'static str, req: u64, resp: u64, rounds: u32) -> FlowSpec {
    FlowSpec {
        port,
        peer,
        class,
        tuple: None,
        start_after: Duration::ZERO,
        expect: Expect::Complete,
        kind: Kind::Rr { req, resp, rounds, pause: Duration::ZERO },
        read_pause: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn server_profile_is_not_the_client_tun_profile() {
        let cfg = crate::world::server_stack(zfstack::CcAlgo::Cubic, None);
        let client = zfstack::StackConfig::client();
        assert!(cfg.pacing);
        assert!(!client.pacing);
        assert_ne!(cfg.max_rcv_buf, client.max_rcv_buf);
    }

    #[test]
    fn tuple_isolation_replays_and_does_not_merge() {
        let mut cfg = cfg_tuple();
        cfg.seed = 7;
        let a = execute(cfg.clone());
        let b = execute(cfg);
        assert!(a.ok, "{:?}", a.errors);
        assert_eq!(a.steps, b.steps);
        assert_eq!(a.reserved_peak, b.reserved_peak);
        assert_eq!(a.flows.len(), 2);
        for f in &a.flows {
            assert_eq!(f.client_recv, 4096, "flow {} recv {}", f.id, f.client_recv);
            assert!(!f.corrupt);
        }
    }

    #[test]
    fn cross_shard_duplicate_peer_ids_stay_apart() {
        let r = execute(cfg_peer_reuse());
        assert!(r.ok, "{:?}", r.errors);
        assert!(extra_checks("peer-reuse", &r).is_empty(), "{:?}", extra_checks("peer-reuse", &r));
    }

    #[test]
    fn same_shard_duplicate_peer_still_isolates_bytes() {
        let r = execute(cfg_peer_alias());
        assert!(r.ok, "{:?}", r.errors);
        assert_eq!(r.flows[0].client_recv, 8192);
        assert_eq!(r.flows[1].client_recv, 8192);
        let note = notes_for("same-shard-peer-alias", &r);
        assert!(note[0].contains("PeerId"), "{note:?}");
    }

    #[test]
    fn zero_window_neighbor_still_finishes() {
        let r = execute(cfg_zero_window());
        assert!(r.ok, "{:?}", r.errors);
        assert!(extra_checks("zero-window", &r).is_empty(), "{:?} flows={:?}", extra_checks("zero-window", &r), finish_summary(&r));
    }

    #[test]
    fn sink_full_releases_and_completes() {
        let r = execute(cfg_sink_full());
        assert!(r.ok, "{:?}", r.errors);
        assert!(extra_checks("sink-full", &r).is_empty(), "{:?}", extra_checks("sink-full", &r));
    }

    #[test]
    fn sink_full_iface_rejects_a_fault_that_blocks_every_iface() {
        let mut cfg = cfg_sink_full_iface();
        cfg.full.as_mut().unwrap().port = None;
        let r = execute(cfg);
        assert!(r.ok, "{:?}", r.errors);
        assert!(!extra_checks("sink-full-iface", &r).is_empty());
    }

    #[test]
    fn quota_requires_observed_pressure_and_then_completes() {
        let mut r = execute(cfg_quota());
        assert!(r.ok, "{:?}", r.errors);
        assert!(extra_checks("quota", &r).is_empty(), "{:?}", extra_checks("quota", &r));
        r.share_blocked = 0;
        r.allocation_failures_by_level = [0; 3];
        assert!(!extra_checks("quota", &r).is_empty());
    }

    #[test]
    fn idle_after_transfer_does_not_grow_reserved() {
        let r = execute(cfg_idle());
        assert!(r.ok, "{:?}", r.errors);
        assert!(
            extra_checks("idle-reclaim", &r).is_empty(),
            "{:?} conns {} reserved {:?} -> {}",
            extra_checks("idle-reclaim", &r),
            r.conns_end,
            r.reserved_at_complete,
            r.reserved_end
        );
    }

    #[test]
    fn sixteen_ports_on_one_shard() {
        let r = execute(cfg_density(16));
        assert!(r.ok, "{:?}", r.errors);
        assert_eq!(r.flows.len(), 16);
        assert_eq!(r.server_shards, 1);
        assert!(r.reserved_peak <= r.global_high);
    }

    #[test]
    fn noisy_same_iface_lights_complete() {
        let o = Overrides { lights: Some(2), heavy_bytes: Some(16 * 1024), heavy_conns: Some(1), ..Overrides::default() };
        let v = run_noisy("noisy-same-iface", Noisy::SameIface, &o);
        assert!(v["ok"].as_bool().unwrap(), "{}", serde_json::to_string_pretty(&v).unwrap());
    }

    #[test]
    fn port_density_128_and_per_port_shards() {
        let r = execute(cfg_density(128));
        assert!(r.ok, "{:?}", r.errors);
        assert_eq!(r.flows.len(), 128);
        assert_eq!(r.server_shards, 1);
        let mut per_port = cfg_density(16);
        per_port.topology = Topology::ShardPerPort;
        let r = execute(per_port);
        assert!(r.ok, "{:?}", r.errors);
        assert_eq!(r.server_shards, 16);
    }

    #[test]
    fn stress_scenarios_complete() {
        let o = Overrides { lights: Some(2), heavy_bytes: Some(8 * 1024), heavy_conns: Some(2), ..Overrides::default() };
        for name in [
            "lossy",
            "iface-rebuild",
            "quota",
            "heavy-fanout",
            "wall-bulk",
            "noisy-cross-iface",
            "noisy-cross-shard",
            "noisy-shared-uplink",
            "sink-full-iface",
            "crowd",
        ] {
            let v = run_named(name, &o).unwrap_or_else(|e| panic!("{name}: {e}"));
            assert!(v["ok"].as_bool().unwrap(), "{name}: {}", v["errors"]);
        }
    }

    fn finish_summary(r: &Report) -> Vec<(u64, &'static str, Option<u64>)> {
        r.flows.iter().map(|f| (f.id, f.class, f.finished_at_ns)).collect()
    }
}
