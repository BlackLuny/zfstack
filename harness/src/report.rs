//! JSON / markdown for an L0 run.
//!
//! Latencies are virtual-clock microseconds. They are a deterministic signal,
//! not a wall-clock P99, and P99.9 is omitted unless the sample count could
//! support it.

use serde_json::{json, Value};

use crate::world::{Expect, FlowReport, Report};

pub const COMPARABILITY: &str = "L0 virtual time is not wall-clock latency, PPS, RSS, or a WireGuard path. L1 (Linux TUN + kernel TCP, no WG) and L2 (ZFC+WG) are different layers and are not numerically comparable to these numbers. Cross-iface equal bandwidth is not a zfstack guarantee (docs/design/0001: no user-level QoS).";

pub fn scenario_value(name: &str, report: &Report, notes: &[String]) -> Value {
    let mut users = Vec::new();
    let mut seen = Vec::new();
    for f in &report.flows {
        let key = (f.port, f.peer, f.class);
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        let group: Vec<&FlowReport> = report.flows.iter().filter(|g| g.port == f.port && g.peer == f.peer && g.class == f.class).collect();
        let mut lats = Vec::new();
        let mut waits = Vec::new();
        let mut recv = 0u64;
        for g in &group {
            lats.extend(g.latencies_ns.iter().copied());
            waits.extend(g.write_wait_ns.iter().copied());
            recv += g.client_recv;
        }
        lats.sort_unstable();
        waits.sort_unstable();
        users.push(json!({
            "port": f.port,
            "peer": f.peer,
            "shard_scope": "peer id is only unique together with shard and iface",
            "class": f.class,
            "flows": group.len(),
            "client_recv_bytes": recv,
            "finished_at_us": group.iter().filter_map(|g| g.finished_at_ns).max().map(|n| n / 1000),
            "sim_latency": latency_json(&lats),
            "write_wait": latency_json(&waits),
        }));
    }
    json!({
        "scenario": name,
        "ok": report.ok,
        "errors": report.errors,
        "notes": notes,
        "clock": report.clock,
        "clock_note": "sim_latency and finished_at_us use the virtual clock. wall_sec and process_cpu_sec are the harness process simulating that clock on one thread, not datapath PPS and not host RSS.",
        "seed": report.seed,
        "topology": report.topology.as_str(),
        "server_shards": report.server_shards,
        "ports": report.ports,
        "shared_bottleneck": report.shared_bottleneck,
        "steps": report.steps,
        "wall_sec": report.wall_sec,
        "process_cpu_sec": report.cpu_sec,
        "driver": "L0 app pump visits in-progress flows every step. That cost is the harness, not the production event-driven adapter.",
        "profile": "StackConfig::default (server). Not StackConfig::client().",
        "comparability": COMPARABILITY,
        "budget": {
            "global_high": report.global_high,
            "reserved_peak": report.reserved_peak,
            "reserved_end": report.reserved_end,
            "reserved_at_complete": report.reserved_at_complete,
            "within_high": report.reserved_peak <= report.global_high,
            "physical_peak_by_shard": report.physical_peaks,
            "peer_bytes_peak": report.peer_peaks.iter().map(|(shard, peer, bytes)| json!({
                "shard": shard, "peer": peer, "bytes": bytes
            })).collect::<Vec<_>>(),
            "share_blocked": report.share_blocked,
            "allocation_failures_by_level": {
                "global": report.allocation_failures_by_level[0],
                "port": report.allocation_failures_by_level[1],
                "peer": report.allocation_failures_by_level[2],
            },
            "conns_end": report.conns_end,
            "progress_reserve": report.progress_reserve,
        },
        "link": {
            "sent": report.link_sent,
            "delivered": report.link_delivered,
            "queue_drops": report.link_queue_drops,
            "random_drops": report.link_random_drops,
        },
        "sink_full": {
            "before": report.sink_full_before,
            "total": report.sink_full_total,
        },
        "rebuild": {
            "happened": report.rebuild.happened,
            "live_before": report.rebuild.live_before,
            "still_on_old": report.rebuild.still_on_old,
            "freed_delta": report.rebuild.freed_delta,
            "reserved_before": report.rebuild.reserved_before,
            "reserved_after": report.rebuild.reserved_after,
            "server_slots_gone": report.rebuild.server_slots_gone,
            "client_closed": report.rebuild.client_closed,
            "untouched_live_before": report.rebuild.untouched_live_before,
            "untouched_live_after": report.rebuild.untouched_live_after,
        },
        "users": users,
        "flows": report.flows.iter().map(flow_json).collect::<Vec<_>>(),
    })
}

fn flow_json(f: &FlowReport) -> Value {
    json!({
        "id": f.id,
        "port": f.port,
        "peer": f.peer,
        "class": f.class,
        "expect": match f.expect { Expect::Complete => "complete", Expect::Reset => "reset" },
        "client_sent": f.client_sent,
        "client_recv": f.client_recv,
        "server_sent": f.server_sent,
        "server_recv": f.server_recv,
        "target_recv": f.client_target_recv,
        "corrupt": f.corrupt,
        "reset": f.reset,
        "finished_at_us": f.finished_at_ns.map(|n| n / 1000),
        "sim_latency_us": f.latencies_ns.iter().map(|n| n / 1000).collect::<Vec<_>>(),
        "write_wait_us": f.write_wait_ns.iter().map(|n| n / 1000).collect::<Vec<_>>(),
        "saw_zero_wnd": f.saw_zero_wnd,
        "saw_wnd_reopen": f.saw_wnd_reopen,
        "min_snd_wnd": f.min_snd_wnd,
        "sent_at_block": f.sent_at_block,
        "queued_during_block": f.queued_during_block,
        "progress_after_block": f.progress_after_block,
        "stack_released": f.stack_released,
    })
}

fn latency_json(sorted_ns: &[u64]) -> Value {
    let n = sorted_ns.len();
    let us = |p: f64| pct(sorted_ns, p).map(|v| v / 1000);
    json!({
        "n": n,
        "unit": "virtual_us",
        "p50_us": us(50.0),
        "p95_us": if n >= 20 { us(95.0) } else { None },
        "p99_us": if n >= 100 { us(99.0) } else { None },
        "p999_us": if n >= 1000 { us(99.9) } else { None },
        "p999_note": if n >= 1000 { None } else { Some(format!("omitted: {n} < 1000 virtual samples")) },
        "max_us": sorted_ns.last().map(|v| v / 1000),
    })
}

fn pct(sorted: &[u64], p: f64) -> Option<u64> {
    if sorted.is_empty() {
        return None;
    }
    let i = ((p / 100.0) * (sorted.len() as f64 - 1.0)).round() as usize;
    sorted.get(i.min(sorted.len() - 1)).copied()
}

pub fn matrix_value(scenarios: Vec<Value>) -> Value {
    let ok = scenarios.iter().all(|s| s["ok"].as_bool().unwrap_or(false));
    json!({
        "scenario": "matrix",
        "ok": ok,
        "clock": "virtual",
        "layer": "L0",
        "comparability": COMPARABILITY,
        "scenarios": scenarios,
    })
}

pub fn to_markdown(v: &Value) -> String {
    if v["scenario"] == "matrix" {
        return matrix_markdown(v);
    }
    let mut out = String::new();
    out.push_str(&format!("# L0 `{}`\n\n", v["scenario"].as_str().unwrap_or("?")));
    out.push_str(&format!("- ok: {}\n", v["ok"]));
    out.push_str(&format!("- topology: {}  ports: {}  shards: {}\n", v["topology"], v["ports"], v["server_shards"]));
    out.push_str(&format!("- seed: {}  steps: {}  wall_sec: {}  process_cpu_sec: {}\n", v["seed"], v["steps"], v["wall_sec"], v["process_cpu_sec"]));
    out.push_str(&format!("- reserved peak/end/high: {} / {} / {}\n", v["budget"]["reserved_peak"], v["budget"]["reserved_end"], v["budget"]["global_high"]));
    out.push_str(&format!("\n{}\n", v["clock_note"].as_str().unwrap_or("")));
    if let Some(notes) = v["notes"].as_array() {
        for n in notes {
            if let Some(s) = n.as_str() {
                out.push_str(&format!("\n- {s}\n"));
            }
        }
    }
    if let Some(errs) = v["errors"].as_array() {
        if !errs.is_empty() {
            out.push_str("\n## errors\n\n");
            for e in errs {
                out.push_str(&format!("- {}\n", e.as_str().unwrap_or("?")));
            }
        }
    }
    out
}

fn matrix_markdown(v: &Value) -> String {
    let mut out = String::new();
    out.push_str("# L0 multi-tenant matrix\n\n");
    out.push_str("Layer **L0** (sans-IO, virtual clock, no TUN, no WireGuard). ");
    out.push_str("These numbers are not wall-clock P99 and are not comparable to L1 or L2.\n\n");
    out.push_str("| scenario | ok | ports | topology | steps | wall_s | reserved peak | note |\n");
    out.push_str("|---|---|---:|---|---:|---:|---:|---|\n");
    let rows = v["scenarios"].as_array().cloned().unwrap_or_default();
    for s in rows {
        let note = s["notes"].as_array().and_then(|a| a.first()).and_then(|n| n.as_str()).unwrap_or("").replace('|', "/");
        let note: String = note.chars().take(160).collect();
        out.push_str(&format!(
            "| {} | {} | {} | {} | {} | {} | {} | {} |\n",
            s["scenario"].as_str().unwrap_or("?"),
            s["ok"].as_bool().unwrap_or(false),
            s["ports"].as_u64().unwrap_or(0),
            s["topology"].as_str().unwrap_or("?"),
            s["steps"].as_u64().unwrap_or(0),
            s["wall_sec"].as_f64().map(|x| format!("{x:.3}")).unwrap_or_else(|| "?".into()),
            s["budget"]["reserved_peak"].as_u64().unwrap_or(0),
            note,
        ));
    }
    out.push('\n');
    out.push_str(COMPARABILITY);
    out.push('\n');
    out
}
