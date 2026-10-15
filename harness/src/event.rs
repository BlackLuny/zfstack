//! Versioned, bounded replay input. Logical targets never contain live ConnIds.
use std::collections::BTreeSet;
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::scenario::{build, Overrides};
use crate::schedule::AppDriveMode;
use crate::world::Config;

pub const MAX_EVENTS: usize = 4096;
pub const MAX_SCRIPT_BYTES: usize = 1 << 20;
pub const MAX_DURATION_MS: u64 = 3_600_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Client,
    Server,
}

#[derive(Clone, Debug)]
pub enum Condition {
    Always,
    Accepted(u64),
    WriteBlocked(u64),
    MemoryBlocked(u64),
    Received { flow: u64, bytes: u64 },
}

#[derive(Clone, Debug)]
pub enum Action {
    StartFlow { flow: u64 },
    AppWrite { flow: u64, side: Side, bytes: u64 },
    PauseRead { flow: u64, side: Side },
    ResumeRead { flow: u64, side: Side },
    CloseWrite { flow: u64, side: Side },
    Abort { flow: u64 },
    RemoveIface { port: u16 },
    RebuildIface { port: u16 },
    SinkFull { port: u16 },
    SinkRelease { port: u16 },
    LinkBlackhole { port: u16, enabled: bool },
    LossBurst { port: u16, packets: u64 },
    HoldMemory { lease: String, port: u16, peer: u64, bytes: u64 },
    ReleaseMemory { lease: String },
    ExecutionGap { millis: u64 },
    SetMtu { port: u16, bytes: u16 },
}

#[derive(Clone, Debug)]
pub struct ScriptEvent {
    pub id: String,
    pub at: Duration,
    pub deadline: Duration,
    pub priority: u64,
    pub sequence: usize,
    pub condition: Condition,
    pub action: Action,
}

#[derive(Clone, Debug)]
pub struct Replay {
    pub scenario: String,
    pub config: Config,
    pub input: Value,
}

fn object<'a>(v: &'a Value, where_: &str) -> Result<&'a Map<String, Value>, String> {
    v.as_object().ok_or_else(|| format!("{where_}: expected object"))
}
fn fields(o: &Map<String, Value>, allowed: &[&str]) -> Result<(), String> {
    for key in o.keys() {
        if !allowed.contains(&key.as_str()) {
            return Err(format!("unknown field {key}"));
        }
    }
    Ok(())
}
fn uint(o: &Map<String, Value>, key: &str) -> Result<u64, String> {
    o.get(key).and_then(Value::as_u64).ok_or_else(|| format!("{key}: expected unsigned integer"))
}
fn text<'a>(o: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    o.get(key).and_then(Value::as_str).filter(|s| !s.is_empty() && s.len() <= 128).ok_or_else(|| format!("{key}: expected nonempty string <= 128 bytes"))
}
fn time(o: &Map<String, Value>, key: &str) -> Result<Duration, String> {
    let n = uint(o, key)?;
    if n > MAX_DURATION_MS {
        return Err(format!("{key} exceeds bounded duration"));
    }
    Ok(Duration::from_millis(n))
}
fn side(o: &Map<String, Value>) -> Result<Side, String> {
    match text(o, "side")? {
        "client" => Ok(Side::Client),
        "server" => Ok(Side::Server),
        _ => Err("side must be client or server".into()),
    }
}
fn port(o: &Map<String, Value>) -> Result<u16, String> {
    u16::try_from(uint(o, "port")?).map_err(|_| "port exceeds u16".into())
}

pub fn parse(input: &str) -> Result<Replay, String> {
    if input.len() > MAX_SCRIPT_BYTES {
        return Err("script exceeds 1 MiB limit".into());
    }
    let value: Value = serde_json::from_str(input).map_err(|e| format!("invalid replay JSON: {e}"))?;
    let o = object(&value, "script")?;
    fields(o, &["schema_version", "scenario", "seed", "app_drive", "until_ms", "events", "suppress_writable", "description", "source", "triage"])?;
    if uint(o, "schema_version")? != 1 {
        return Err("unsupported replay schema_version".into());
    }
    let name = text(o, "scenario")?;
    if name == "matrix" || name.starts_with("noisy-") {
        return Err("replay requires one concrete scenario, not a paired/matrix run".into());
    }
    let mode = AppDriveMode::parse(text(o, "app_drive")?)?;
    let until = time(o, "until_ms")?;
    if until.is_zero() {
        return Err("until_ms must be positive".into());
    }
    let over = Overrides { seed: Some(uint(o, "seed")?), app_drive: mode, until_ms: Some(until.as_millis() as u64), ..Overrides::default() };
    let mut config = build(name, &over)?;
    config.suppress_writable = o.get("suppress_writable").map(|_| uint(o, "suppress_writable")).transpose()?;
    if config.suppress_writable.is_some_and(|f| f >= config.flows.len() as u64) {
        return Err("suppress_writable flow does not exist".into());
    }
    let events = o.get("events").and_then(Value::as_array).ok_or("events must be an array")?;
    if events.len() > MAX_EVENTS {
        return Err(format!("events exceeds {MAX_EVENTS}"));
    }
    let mut ids = BTreeSet::new();
    for (sequence, event) in events.iter().enumerate() {
        let e = object(event, "event")?;
        fields(e, &["id", "at_ms", "deadline_ms", "priority", "when", "action"])?;
        let id = text(e, "id")?.to_string();
        if !ids.insert(id.clone()) {
            return Err(format!("duplicate event id {id}"));
        }
        let at = time(e, "at_ms")?;
        let condition = if let Some(v) = e.get("when") {
            let c = object(v, "when")?;
            let kind = text(c, "kind")?;
            fields(c, if kind == "received" { &["kind", "flow", "bytes"] } else { &["kind", "flow"] })?;
            let flow = uint(c, "flow")?;
            if flow >= config.flows.len() as u64 {
                return Err("condition flow does not exist".into());
            }
            match kind {
                "accepted" => Condition::Accepted(flow),
                "write-blocked" => Condition::WriteBlocked(flow),
                "memory-blocked" => Condition::MemoryBlocked(flow),
                "received" => Condition::Received { flow, bytes: uint(c, "bytes")? },
                _ => return Err(format!("unknown condition {kind}")),
            }
        } else {
            Condition::Always
        };
        let deadline = if e.contains_key("deadline_ms") {
            time(e, "deadline_ms")?
        } else if matches!(condition, Condition::Always) {
            at
        } else {
            return Err(format!("conditional event {id} requires deadline_ms"));
        };
        if at > deadline || deadline > until {
            return Err(format!("event {id} requires at <= deadline <= until"));
        }
        let a = object(e.get("action").ok_or("missing action")?, "action")?;
        let kind = text(a, "kind")?;
        let allowed: &[&str] = match kind {
            "StartFlow" | "Abort" => &["kind", "flow"],
            "AppWrite" => &["kind", "flow", "side", "bytes"],
            "PauseRead" | "ResumeRead" | "CloseWrite" => &["kind", "flow", "side"],
            "RemoveIface" | "RebuildIface" | "SinkFull" | "SinkRelease" => &["kind", "port"],
            "LinkBlackhole" => &["kind", "port", "enabled"],
            "LossBurst" => &["kind", "port", "packets"],
            "HoldMemory" => &["kind", "lease", "port", "peer", "bytes"],
            "ReleaseMemory" => &["kind", "lease"],
            "ExecutionGap" => &["kind", "millis"],
            "SetMtu" => &["kind", "port", "bytes"],
            _ => return Err(format!("unknown action {kind}")),
        };
        fields(a, allowed)?;
        if a.contains_key("port") && port(a)? >= config.ports {
            return Err("action port does not exist".into());
        }
        if a.contains_key("flow") && uint(a, "flow")? >= config.flows.len() as u64 {
            return Err("action flow does not exist".into());
        }
        let action = match kind {
            "StartFlow" => Action::StartFlow { flow: uint(a, "flow")? },
            "AppWrite" => Action::AppWrite { flow: uint(a, "flow")?, side: side(a)?, bytes: uint(a, "bytes")? },
            "PauseRead" => Action::PauseRead { flow: uint(a, "flow")?, side: side(a)? },
            "ResumeRead" => Action::ResumeRead { flow: uint(a, "flow")?, side: side(a)? },
            "CloseWrite" => Action::CloseWrite { flow: uint(a, "flow")?, side: side(a)? },
            "Abort" => Action::Abort { flow: uint(a, "flow")? },
            "RemoveIface" => Action::RemoveIface { port: port(a)? },
            "RebuildIface" => Action::RebuildIface { port: port(a)? },
            "SinkFull" => Action::SinkFull { port: port(a)? },
            "SinkRelease" => Action::SinkRelease { port: port(a)? },
            "LinkBlackhole" => Action::LinkBlackhole { port: port(a)?, enabled: a.get("enabled").and_then(Value::as_bool).ok_or("enabled must be bool")? },
            "LossBurst" => Action::LossBurst { port: port(a)?, packets: uint(a, "packets")? },
            "HoldMemory" => Action::HoldMemory { lease: text(a, "lease")?.to_owned(), port: port(a)?, peer: uint(a, "peer")?, bytes: uint(a, "bytes")? },
            "ReleaseMemory" => Action::ReleaseMemory { lease: text(a, "lease")?.to_owned() },
            "ExecutionGap" => {
                let millis = uint(a, "millis")?;
                if millis > MAX_DURATION_MS {
                    return Err("execution gap exceeds limit".into());
                }
                Action::ExecutionGap { millis }
            }
            "SetMtu" => {
                let bytes = u16::try_from(uint(a, "bytes")?).map_err(|_| "MTU exceeds u16")?;
                if bytes < 576 {
                    return Err("MTU must be at least 576".into());
                }
                Action::SetMtu { port: port(a)?, bytes }
            }
            _ => unreachable!(),
        };
        if let Action::StartFlow { flow } = action {
            config.flows[flow as usize].start_after = until + Duration::from_millis(1);
        }
        if matches!(action, Action::SinkFull { .. }) {
            config.full = None;
        }
        if matches!(action, Action::RemoveIface { .. } | Action::RebuildIface { .. }) {
            config.rebuild = None;
        }
        config.events.push(ScriptEvent {
            id,
            at,
            deadline,
            priority: e.get("priority").map(|_| uint(e, "priority")).transpose()?.unwrap_or(0),
            sequence,
            condition,
            action,
        });
    }
    config.events.sort_by_key(|e| (e.at, e.priority, e.sequence));
    Ok(Replay { scenario: name.to_owned(), config, input: value })
}

pub fn run(input: &str) -> Result<Value, String> {
    let replay = parse(input)?;
    let replaces_lifecycle =
        replay.config.events.iter().any(|e| matches!(e.action, Action::Abort { .. } | Action::RemoveIface { .. } | Action::RebuildIface { .. }));
    let mut report = crate::world::execute(replay.config);
    if !(replaces_lifecycle && replay.scenario == "iface-rebuild") {
        report.errors.extend(crate::scenario::extra_checks(&replay.scenario, &report));
    }
    report.ok = report.errors.is_empty();
    let mut result = crate::report::scenario_value(&replay.scenario, &report, &[]);
    result["replay"] = json!({"schema_version": 1, "input": replay.input});
    Ok(result)
}

/// Deterministic event deletion. A candidate must keep *all* reached fault
/// preconditions and the same assertion id; a failed handshake is not a
/// smaller reproduction of an established writer's missing notification.
pub fn reduce(input: &str, assertion_id: &str) -> Result<Value, String> {
    let mut value = parse(input)?.input;
    let original = run(&serde_json::to_string(&value).unwrap())?;
    if !crate::oracle::has_failure(&original, assertion_id) {
        return Err(format!("assertion {assertion_id} is not failing"));
    }
    let signature = |result: &Value| {
        result["assertions"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|a| a["id"] == assertion_id && a["status"] == "FAIL")
            .map(|a| (a["flow"].clone(), a["evidence"].clone()))
            .collect::<Vec<_>>()
    };
    let original_signature = signature(&original);
    let precondition_ids = value["events"].as_array().unwrap().iter().filter(|e| e.get("when").is_some()).map(|e| e["id"].clone()).collect::<Vec<_>>();
    let mut i = 0;
    while i < value["events"].as_array().unwrap().len() {
        let mut candidate = value.clone();
        candidate["events"].as_array_mut().unwrap().remove(i);
        if precondition_ids.iter().any(|id| !candidate["events"].as_array().unwrap().iter().any(|e| e["id"] == *id)) {
            i += 1;
            continue;
        }
        let tested = run(&serde_json::to_string(&candidate).unwrap());
        if tested.is_ok_and(|r| {
            crate::oracle::has_failure(&r, assertion_id)
                && signature(&r) == original_signature
                && r["events"].as_array().is_some_and(|es| es.iter().all(|e| e["status"] == "APPLIED"))
                && r["scheduler"]["suppressed_writable"] == original["scheduler"]["suppressed_writable"]
        }) {
            value = candidate;
        } else {
            i += 1;
        }
    }
    Ok(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn permanent_corpus_replays_with_failure_preconditions() {
        for input in [
            include_str!("../regressions/sink-recovery-v1.json"),
            include_str!("../regressions/scripted-arrivals-v1.json"),
            include_str!("../regressions/network-recovery-v1.json"),
            include_str!("../regressions/external-owner-v1.json"),
        ] {
            let result = run(input).unwrap();
            assert_eq!(result["ok"], true, "{}", result["errors"]);
            assert!(result["events"].as_array().unwrap().iter().all(|e| e["status"] == "APPLIED"));
            assert_eq!(result["trace"], run(input).unwrap()["trace"]);
        }
        let missing = run(include_str!("../regressions/missing-writable-v1.json")).unwrap();
        assert!(crate::oracle::has_failure(&missing, "liveness.flow_complete"));
        assert_eq!(missing["scheduler"]["suppressed_writable"], 1);
        let absent = run(include_str!("../regressions/unreached-precondition-v1.json")).unwrap();
        assert_eq!(absent["ok"], false);
        assert_eq!(absent["events"][0]["status"], "PRECONDITION_NOT_REACHED");
    }

    #[test]
    fn double_release_and_unestablished_actions_fail_preconditions() {
        let mut input: Value = serde_json::from_str(include_str!("../regressions/external-owner-v1.json")).unwrap();
        let mut duplicate = input["events"][1].clone();
        duplicate["id"] = json!("release-twice");
        input["events"].as_array_mut().unwrap().push(duplicate);
        let result = run(&input.to_string()).unwrap();
        assert_eq!(result["ok"], false);
        assert_eq!(result["events"][2]["status"], "PRECONDITION_NOT_REACHED");
        input["events"] = json!([{"id":"too-early","at_ms":0,"action":{"kind":"CloseWrite","flow":0,"side":"server"}}]);
        let result = run(&input.to_string()).unwrap();
        assert_eq!(result["events"][0]["status"], "PRECONDITION_NOT_REACHED");
    }

    #[test]
    fn event_reducer_preserves_the_missing_notification_failure() {
        let mut input: Value = serde_json::from_str(include_str!("../regressions/missing-writable-v1.json")).unwrap();
        input["events"] = json!([{"id":"irrelevant-gap","at_ms":0,"action":{"kind":"ExecutionGap","millis":0}}]);
        let minimized = reduce(&input.to_string(), "liveness.flow_complete").unwrap();
        assert_eq!(minimized["events"], json!([]));
        let result = run(&minimized.to_string()).unwrap();
        assert_eq!(result["scheduler"]["suppressed_writable"], 1);
        assert!(crate::oracle::has_failure(&result, "liveness.flow_complete"));
    }

    #[test]
    fn scripted_iface_generation_pause_and_abort_have_real_lifecycle_evidence() {
        let rebuild = json!({"schema_version":1,"scenario":"iface-rebuild","seed":1,"app_drive":"event-only","until_ms":3000,"events":[
            {"id":"remove","at_ms":40,"when":{"kind":"accepted","flow":0},"deadline_ms":50,"action":{"kind":"RemoveIface","port":0}},
            {"id":"rebuild","at_ms":55,"action":{"kind":"RebuildIface","port":0}},
            {"id":"new-flow","at_ms":60,"action":{"kind":"StartFlow","flow":1}}
        ]});
        let result = run(&rebuild.to_string()).unwrap();
        assert_eq!(result["ok"], true, "{}", result["errors"]);
        assert_eq!(result["flows"][0]["stack_released"], true);
        assert!(result["flows"][1]["client_recv"].as_u64().unwrap() > 0);
        let pause = json!({"schema_version":1,"scenario":"wall-bulk","seed":1,"app_drive":"event-only","until_ms":1000,"events":[
            {"id":"pause","at_ms":0,"when":{"kind":"accepted","flow":0},"deadline_ms":100,"action":{"kind":"PauseRead","flow":0,"side":"client"}},
            {"id":"resume","at_ms":200,"action":{"kind":"ResumeRead","flow":0,"side":"client"}}
        ]});
        let result = run(&pause.to_string()).unwrap();
        assert_eq!(result["ok"], true, "{}", result["errors"]);
        assert!(result["flows"][0]["finished_at_us"].as_u64().unwrap() >= 200000);
        let mut abort = pause;
        abort["events"] = json!([
            {"id":"abort","at_ms":0,"when":{"kind":"accepted","flow":0},"deadline_ms":100,"action":{"kind":"Abort","flow":0}}
        ]);
        let result = run(&abort.to_string()).unwrap();
        assert_eq!(result["ok"], true, "{}", result["errors"]);
        assert_eq!(result["flows"][0]["stack_released"], true);
    }

    #[test]
    fn unapplied_faults_cannot_pass_as_recovery_cases() {
        for (set, clear) in [
            (json!({"kind":"SinkFull","port":0}), json!({"kind":"SinkRelease","port":0})),
            (json!({"kind":"LinkBlackhole","port":0,"enabled":true}), json!({"kind":"LinkBlackhole","port":0,"enabled":false})),
        ] {
            let input = json!({"schema_version":1,"scenario":"wall-bulk","seed":1,"app_drive":"event-only","until_ms":1000,"events":[
                {"id":"late-fault","at_ms":500,"action":set},{"id":"no-effect-release","at_ms":600,"action":clear}
            ]});
            let result = run(&input.to_string()).unwrap();
            assert_eq!(result["ok"], false);
            assert_eq!(result["events"][1]["status"], "PRECONDITION_NOT_REACHED");
        }
    }

    #[test]
    fn bounded_seed_and_execution_gap_schedule_scan() {
        for seed in 0..16 {
            for gap in [0, 1, 5, 10] {
                let input = json!({"schema_version":1,"scenario":"sink-full-iface","seed":seed,"app_drive":"event-only","until_ms":3000,"events":[
                    {"id":"gap","at_ms":31,"action":{"kind":"ExecutionGap","millis":gap}}
                ]});
                let result = run(&input.to_string()).unwrap();
                assert_eq!(result["ok"], true, "seed {seed}, gap {gap}: {}", result["errors"]);
            }
        }
    }

    #[test]
    fn request_response_waits_for_scripted_application_arrivals() {
        let input = json!({"schema_version":1,"scenario":"heavy-fanout","seed":1,"app_drive":"event-only","until_ms":4000,"events":[
            {"id":"first-request","at_ms":0,"when":{"kind":"accepted","flow":4},"deadline_ms":100,"action":{"kind":"AppWrite","flow":4,"side":"client","bytes":256}},
            {"id":"later-requests","at_ms":200,"action":{"kind":"AppWrite","flow":4,"side":"client","bytes":512}}
        ]});
        let result = run(&input.to_string()).unwrap();
        assert_eq!(result["ok"], true, "{}", result["errors"]);
        assert_eq!(result["flows"][4]["client_sent"], 768);
        assert!(result["flows"][4]["finished_at_us"].as_u64().unwrap() >= 200000);
    }

    #[test]
    fn parser_fails_closed_on_unknowns_deadlines_and_duplicate_ids() {
        let base = json!({"schema_version":1,"scenario":"wall-bulk","seed":1,"app_drive":"event-only","until_ms":1000,"events":[]});
        let mut bad = base.clone();
        bad["typo"] = json!(true);
        assert!(parse(&bad.to_string()).is_err());
        let event = json!({"id":"x","at_ms":0,"when":{"kind":"accepted","flow":0},"action":{"kind":"Abort","flow":0}});
        bad = base.clone();
        bad["events"] = json!([event]);
        assert!(parse(&bad.to_string()).unwrap_err().contains("deadline"));
        let event = json!({"id":"x","at_ms":0,"action":{"kind":"Abort","flow":0}});
        bad = base;
        bad["events"] = json!([event.clone(), event]);
        assert!(parse(&bad.to_string()).unwrap_err().contains("duplicate"));
    }
}
