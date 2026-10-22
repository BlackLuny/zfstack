//! Frozen scheduler round-two workloads. Both A arms use this identical driver.
use std::time::Duration;
use zfharness::event::{Action, Condition, ScriptEvent, Side};
use zfharness::scenario::{run_named, Overrides};
use zfharness::schedule::AppDriveMode;
use zfharness::world::{execute, Config, Expect, FlowSpec, Kind, Limits, Topology};
use zfstack::StackConfig;

fn stack(mode: &str) -> Option<StackConfig> {
    assert_eq!(mode, "off", "isolated A build has no DRR or sparse policy");
    None
}

fn flow(peer: u64, class: &'static str, kind: Kind, start_ms: u64) -> FlowSpec {
    FlowSpec { port: 0, peer, class, tuple: None, start_after: Duration::from_millis(start_ms), expect: Expect::Complete, kind, read_pause: None }
}

fn arrival(flow: usize, at: Duration, bytes: u64, sequence: usize) -> ScriptEvent {
    ScriptEvent {
        id: format!("w{sequence}"),
        at,
        deadline: at,
        priority: 0,
        sequence,
        condition: Condition::Always,
        action: Action::AppWrite { flow: flow as u64, side: Side::Server, bytes },
    }
}

fn main() {
    let args: Vec<_> = std::env::args().collect();
    let name = args.get(1).expect("case").as_str();
    let mode = args.get(2).map(String::as_str).unwrap_or("off");
    let period_us: u64 = args.get(3).map(|s| s.parse().unwrap()).unwrap_or(1000);
    let stack_override = stack(mode);
    let mut value = if let Some(matrix) = name.strip_prefix("matrix/") {
        let (scenario, ports) = match matrix {
            "port-density-64" => ("port-density", Some(64)),
            "port-density-128" => ("port-density", Some(128)),
            n => (n, None),
        };
        run_named(scenario, &Overrides { seed: Some(2201), app_drive: AppDriveMode::EventOnly, ports, stack_override, ..Default::default() }).unwrap()
    } else {
        let mut cfg = Config::skeleton(1, Topology::SharedShard);
        cfg.app_drive = AppDriveMode::EventOnly;
        cfg.seed = 2201;
        cfg.until = Duration::from_secs(60);
        cfg.limits = Limits { global: 512 << 20, port: 512 << 20, peer: 128 << 20, peer_max_conns: 4096 };
        cfg.link.rate_bps = 0;
        cfg.link.delay = Duration::from_millis(1);
        cfg.link.queue_bytes = 32 << 20;
        cfg.stack_override = stack_override;
        cfg.local_egress_period = Duration::from_micros(period_us);
        let burst = if period_us == 100 { 6553 } else { 64 << 10 };
        let bulk = |n| Kind::Bulk { from_server: n, from_client: 0 };
        if let Some(n) = name.strip_prefix("mixed-") {
            for _ in 0..n.parse::<usize>().unwrap() {
                cfg.flows.push(flow(1, "bulk", bulk(1 << 20), 0));
            }
            for peer in 2..=4 {
                cfg.flows.push(flow(peer, "light", Kind::Rr { req: 64, resp: 512, rounds: 128, pause: Duration::from_millis(2) }, 10));
            }
            cfg.local_egress_burst = Some(burst);
        } else if let Some(n) = name.strip_prefix("bulk-").and_then(|s| s.strip_suffix("-1-1")) {
            for (peer, count) in [(1, n.parse::<usize>().unwrap()), (2, 1), (3, 1)] {
                for _ in 0..count {
                    cfg.flows.push(flow(peer, "bulk", bulk(1 << 20), 0));
                }
            }
            cfg.local_egress_burst = Some(burst);
        } else if name.starts_with("peers-") {
            let parts: Vec<_> = name.split('-').collect();
            for peer in 1..=parts[1].parse::<u64>().unwrap() {
                cfg.flows.push(flow(peer, "bulk", bulk(if parts[2] == "uncongested" { 128 } else { 64 << 10 }), 0));
            }
            cfg.local_egress_burst = (parts[2] == "congested").then_some(burst);
        } else if name.starts_with("gate-") {
            cfg.stack_override = Some(StackConfig { pacing: false, min_prefetch: 256 << 10, prefetch_max: 256 << 10, ..Default::default() });
            cfg.link.delay = Duration::from_millis(50);
            if let Some(peers) = name.strip_prefix("gate-peers-") {
                for peer in 1..=peers.parse::<u64>().unwrap() {
                    for _ in 0..4 {
                        cfg.flows.push(flow(peer, "cwnd", bulk(16384), 0));
                    }
                }
                for f in 0..cfg.flows.len() {
                    for i in 0..4 {
                        cfg.events.push(arrival(f, Duration::from_micros(150000 + i * 100), 4096, cfg.events.len()));
                    }
                }
            } else {
                let zero = name == "gate-rwnd-zero";
                cfg.flows.push(flow(1, if zero { "rwnd" } else { "cwnd" }, bulk(if zero { 196608 } else { 65536 }), 0));
                if zero {
                    cfg.client_max_rcv = Some(4096);
                    cfg.flows[0].read_pause = Some(Duration::from_millis(500));
                    cfg.events.push(arrival(0, Duration::from_millis(150), 131072, 0));
                }
                for i in 0..128 {
                    cfg.events.push(arrival(0, Duration::from_micros(if zero { 300000 } else { 150000 } + i * 100), 512, cfg.events.len()));
                }
            }
        } else {
            panic!("unknown case {name}");
        }
        zfharness::report::scenario_value(name, &execute(cfg), &[])
    };
    zfharness::link::flush_capture();
    value["r2_case"] = serde_json::json!(name);
    value["sched_mode"] = serde_json::json!(mode);
    value["sink_refill_us"] = serde_json::json!(period_us);
    println!("{}", value);
    if value["ok"] != true {
        std::process::exit(1);
    }
}
