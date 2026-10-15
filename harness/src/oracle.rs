//! Assertions consume measured state and reject empty/precondition-only success.
use serde_json::{json, Value};

use crate::world::{Expect, Report};

pub fn assertions(report: &Report) -> Vec<Value> {
    let mut checks = vec![
        assertion("precondition.flows_present", None, !report.flows.is_empty(), json!({"flows": report.flows.len()})),
        assertion(
            "resources.global_hard_limit",
            None,
            report.reserved_peak <= report.global_high,
            json!({"peak": report.reserved_peak, "limit": report.global_high}),
        ),
    ];
    for event in &report.events {
        checks.push(assertion("precondition.event_applied", None, event["status"] == "APPLIED", event.clone()));
    }
    checks.push(assertion("scenario.contracts", None, report.ok, json!({"errors": report.errors})));
    for flow in &report.flows {
        checks.push(assertion(
            "safety.salted_delivery",
            Some(flow.id),
            !flow.corrupt,
            json!({"client_received": flow.client_recv, "server_received": flow.server_recv}),
        ));
        match flow.expect {
            Expect::Complete => {
                checks.push(assertion(
                    "liveness.flow_complete",
                    Some(flow.id),
                    flow.client_recv == flow.client_target_recv
                        && flow.server_sent == flow.client_target_recv
                        && flow.client_sent == flow.client_target_send
                        && flow.server_recv == flow.client_target_send
                        && flow.client_eof
                        && flow.server_eof
                        && flow.finished_at_ns.is_some()
                        && !flow.reset,
                    json!({"client_eof": flow.client_eof, "server_eof": flow.server_eof,
                        "pending_server_bytes": flow.client_target_recv.saturating_sub(flow.server_sent),
                        "server_queued_end": flow.server_queued_end, "server_write_blocked": flow.server_write_blocked}),
                ));
            }
            Expect::Reset => checks.push(assertion("resources.aborted_slot_released", Some(flow.id), flow.stack_released, json!({}))),
        }
    }
    checks
}

fn assertion(id: &str, flow: Option<u64>, pass: bool, evidence: Value) -> Value {
    json!({"id": id, "flow": flow, "status": if pass { "PASS" } else { "FAIL" }, "evidence": evidence})
}

pub fn has_failure(result: &Value, assertion_id: &str) -> bool {
    result["assertions"].as_array().is_some_and(|items| items.iter().any(|a| a["id"] == assertion_id && a["status"] == "FAIL"))
}

pub fn contract_ids(name: &str) -> &'static [&'static str] {
    match name {
        "wall-bulk" => &["C01"],
        "heavy-fanout" | "quota" => &["C02", "C04", "C06"],
        "zero-window" => &["C07"],
        "sink-full" | "sink-full-iface" => &["C08"],
        "lossy" => &["C09"],
        "tuple-isolation" | "peer-reuse" | "same-shard-peer-alias" => &["C10"],
        "port-density" => &["C11"],
        "crowd" | "noisy-same-iface" | "noisy-cross-iface" | "noisy-cross-shard" | "noisy-shared-uplink" => &["C12"],
        "idle-reclaim" | "capacity-recovery" => &["C13"],
        "iface-rebuild" => &["C14", "C19"],
        _ => &[],
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn empty_matrix_is_not_pass() {
        assert_eq!(crate::report::matrix_value(vec![])["ok"], false);
        assert!(!has_failure(&json!({"assertions":[]}), "liveness.flow_complete"));
    }
}
