//! Explicit, fixed L0 workload for scheduler experiments (EventOnly).
use std::time::Duration;
use zfharness::world::{execute, Config, Expect, FlowSpec, Kind, Limits, Topology};

fn flow(peer: u64, class: &'static str, kind: Kind, start_ms: u64) -> FlowSpec {
    FlowSpec { port: 0, peer, class, tuple: None, start_after: Duration::from_millis(start_ms), expect: Expect::Complete, kind, read_pause: None }
}

fn main() {
    let args: Vec<_> = std::env::args().collect();
    let name = args.get(1).expect("case name");
    let mode = args.get(2).map(String::as_str).unwrap_or("off");

    let mut cfg = Config::skeleton(1, Topology::SharedShard);
    cfg.app_drive = zfharness::schedule::AppDriveMode::EventOnly;
    cfg.seed = 2201;
    cfg.until = Duration::from_secs(60);
    cfg.limits = Limits { global: 512 << 20, port: 512 << 20, peer: 128 << 20, peer_max_conns: 4096 };
    cfg.link.rate_bps = 0;
    cfg.link.delay = Duration::from_millis(1);
    cfg.link.queue_bytes = 32 << 20;
    let bulk = |n| Kind::Bulk { from_server: n, from_client: 0 };
    if let Some(n) = name.strip_prefix("mixed-") {
        let n: usize = n.parse().unwrap();
        for _ in 0..n {
            cfg.flows.push(flow(1, "bulk", bulk(1 << 20), 0));
        }
        for peer in 2..=4 {
            cfg.flows.push(flow(peer, "light", Kind::Rr { req: 64, resp: 512, rounds: 128, pause: Duration::from_millis(2) }, 10));
        }
        cfg.local_egress_burst = Some(64 << 10);
    } else if let Some(n) = name.strip_prefix("bulk-").and_then(|n| n.strip_suffix("-1-1")) {
        let n: usize = n.parse().unwrap();
        for (peer, count) in [(1, n), (2, 1), (3, 1)] {
            for _ in 0..count {
                cfg.flows.push(flow(peer, "bulk", bulk(1 << 20), 0));
            }
        }
        cfg.local_egress_burst = Some(64 << 10);
    } else if name.starts_with("peers-") {
        let parts: Vec<_> = name.split('-').collect();
        let n: u64 = parts[1].parse().unwrap();
        for peer in 1..=n {
            cfg.flows.push(flow(peer, "bulk", bulk(64 << 10), 0));
        }
        cfg.local_egress_burst = (parts[2] == "congested").then_some(64 << 10);
    } else {
        panic!("unknown case {name}");
    }
    let mut stack = zfstack::StackConfig::default();
    zfharness::sched_mode::apply(&mut stack, mode).unwrap();
    cfg.stack_override = Some(stack);
    let report = execute(cfg);
    let mut v = zfharness::report::scenario_value(name, &report, &[]);
    v["sched_mode"] = serde_json::json!(mode);
    println!("{}", serde_json::to_string(&v).unwrap());
    if !report.ok {
        std::process::exit(1);
    }
}
