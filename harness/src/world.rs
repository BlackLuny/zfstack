//! Deterministic multi-tenant world.
//!
//! One or more server shards (the system under test) face one client shard per
//! logical port. Logical ports are `IfaceId`s. A shared shard is the deployment
//! in docs/design/0001 §3.1; one shard per port is the other topology from
//! issue #24. Peer budgets are whatever the shard implements — this world does
//! not invent a per-iface peer key.
//!
//! The client shards are load generators. Their budgets are separate and ample,
//! so a stall here is the server stack, the shared server budget, or the modelled
//! link. Virtual-clock latencies are not wall-clock P99.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::net::SocketAddr;
use std::time::Duration;

use zfstack::budget::GlobalBudget;
use zfstack::sim::Rng;
use zfstack::{
    CcAlgo, CloseReason, ConnId, Event, IfaceConfig, IfaceId, Instant, OutPacket, PeerId, ReadResult, SendResult, Shard, StackConfig, State, WriteResult,
};

use bytes::Bytes;

use crate::event::{Action, Condition, ScriptEvent, Side};
use crate::link::{Link, LinkParams, LinkStats};
use crate::schedule::{AppDriveMode, Interest, ReadyQueue, Trace};
use serde_json::{json, Value};

const ORIGIN_MS: u64 = 1_000;
const WRITE_CHUNK: usize = 16 * 1024;
const READ_CHUNK: usize = 64 * 1024;
const INNER_ROUNDS: usize = 64;
const SAME_NOW_LIMIT: u32 = 100_000;

pub fn origin() -> Instant {
    Instant::from_millis(ORIGIN_MS)
}

/// Server profile. This is `StackConfig::default()`, never `StackConfig::client()`:
/// client() is a lossless sub-millisecond TUN peer and is the wrong budget for a
/// WAN multi-tenant proxy.
pub fn server_stack(cc: CcAlgo, time_wait: Option<Duration>) -> StackConfig {
    let base = StackConfig::default();
    StackConfig { cc, time_wait: time_wait.unwrap_or(base.time_wait), ..base }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Topology {
    /// Every logical port is an iface on shard 0. This is the 0001 default.
    SharedShard,
    /// Each logical port is its own shard. Shards share one `GlobalBudget`.
    ShardPerPort,
}

impl Topology {
    pub fn as_str(self) -> &'static str {
        match self {
            Topology::SharedShard => "shared-shard",
            Topology::ShardPerPort => "shard-per-port",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Expect {
    /// Byte counts match and the close is orderly.
    Complete,
    /// The port was removed or the peer aborted. In-flight bytes must still match
    /// the salted pattern; short counts are expected.
    Reset,
}

#[derive(Clone, Debug)]
pub struct Limits {
    pub global: u64,
    pub port: u64,
    pub peer: u64,
    pub peer_max_conns: u32,
}

impl Limits {
    pub fn ample() -> Self {
        Limits { global: 64 << 20, port: 32 << 20, peer: 16 << 20, peer_max_conns: 4096 }
    }

    /// Still above the progress-reserve floor (~2·H4) so a stall is not "budget
    /// too small to admit one connection".
    pub fn tight() -> Self {
        Limits { global: 4 << 20, port: 2 << 20, peer: 1 << 20, peer_max_conns: 128 }
    }
}

#[derive(Clone, Debug)]
pub enum Kind {
    /// Application bytes each side writes. Zero means that side only reads.
    Bulk { from_server: u64, from_client: u64 },
    /// `rounds` request/response exchanges. `pause` is the client gap between
    /// a finished response and the next request. Latencies are virtual time.
    Rr { req: u64, resp: u64, rounds: u32, pause: Duration },
}

#[derive(Clone, Debug)]
pub struct FlowSpec {
    pub port: u16,
    pub peer: u64,
    pub class: &'static str,
    /// Client local port. `None` allocates a unique port. The same value on two
    /// ifaces is the overlapping-tuple case.
    pub tuple: Option<u16>,
    pub start_after: Duration,
    pub expect: Expect,
    pub kind: Kind,
    /// Client does not read server bytes until `origin + read_pause`. That closes
    /// the server's send window (a zero-window neighbor).
    pub read_pause: Option<Duration>,
}

#[derive(Clone, Debug)]
pub struct FullFault {
    /// `None` blocks every logical port.
    pub port: Option<u16>,
    pub from: Duration,
    pub until: Duration,
}

#[derive(Clone, Debug)]
pub struct Config {
    pub app_drive: AppDriveMode,
    pub events: Vec<ScriptEvent>,
    pub resource_samples: Vec<(String, Duration)>,
    /// Suppress one post-block Writable for a negative control (server flow id).
    pub suppress_writable: Option<u64>,
    pub seed: u64,
    pub topology: Topology,
    pub ports: u16,
    pub limits: Limits,
    pub link: LinkParams,
    pub shared_bottleneck: bool,
    pub cc: CcAlgo,
    pub time_wait: Option<Duration>,
    pub gap: Duration,
    pub flows: Vec<FlowSpec>,
    pub until: Duration,
    pub full: Option<FullFault>,
    /// At this offset, drop and recreate `port`'s iface. Flows with `Expect::Reset`
    /// should be the ones already started on it.
    pub rebuild: Option<(u16, Duration)>,
    pub step_cap: u64,
    /// Cap the load-generator's receive buffer so a paused reader actually
    /// advertises a zero window. `None` keeps the default server profile.
    pub client_max_rcv: Option<u32>,
    pub tx_cache_trim: Option<zfstack::TxCacheTrimPolicy>,
    /// Experimental server config; clients retain their reference profile.
    pub stack_override: Option<StackConfig>,
    /// Per-shard local sink capacity, replenished every virtual millisecond.
    pub local_egress_burst: Option<usize>,
    /// Experimental sink refill period; world event deadlines have nanosecond resolution.
    pub local_egress_period: Duration,
}

impl Config {
    pub fn skeleton(ports: u16, topology: Topology) -> Self {
        Config {
            app_drive: AppDriveMode::Eager,
            events: Vec::new(),
            resource_samples: Vec::new(),
            suppress_writable: None,
            seed: 1,
            topology,
            ports,
            limits: Limits::ample(),
            link: LinkParams::default(),
            shared_bottleneck: false,
            cc: CcAlgo::Cubic,
            time_wait: None,
            gap: Duration::ZERO,
            flows: Vec::new(),
            until: Duration::from_secs(2),
            full: None,
            rebuild: None,
            step_cap: 2_000_000,
            client_max_rcv: None,
            tx_cache_trim: None,
            stack_override: None,
            local_egress_burst: None,
            local_egress_period: Duration::from_millis(1),
        }
    }
}

#[derive(Clone, Debug)]
struct RrState {
    req: u64,
    resp: u64,
    rounds: u32,
    round: u32,
    pause: Duration,
    pause_until: Instant,
    /// When the app was ready to send this round, including time spent blocked
    /// on a full window or a quota. This is the start of the end-to-end latency.
    ready: Option<Instant>,
    /// When `write` first accepted a byte of this round.
    first_write: Option<Instant>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Role {
    Client,
    Server,
}

struct App {
    flow: u16,
    role: Role,
    send_salt: u64,
    recv_salt: u64,
    send_target: u64,
    recv_target: u64,
    sent: u64,
    recv: u64,
    fin_sent: bool,
    eof: bool,
    released: bool,
    closed: Option<CloseReason>,
    corrupt: bool,
    short: bool,
    reset_seen: bool,
    port: u16,
    /// The shard no longer has this conn (`info` is `None`) or, for a local
    /// abort that keeps the slot, `ConnInfo.state` is `Closed`. Set only after
    /// reading the stack, never as a stand-in for that read.
    stack_released: bool,
    saw_zero_wnd: bool,
    saw_wnd_reopen: bool,
    min_snd_wnd: Option<u64>,
    max_tx_unsent: u64,
    stall_wnd: Option<u64>,
    /// Client read-resume time, copied onto the sender so the window probe
    /// knows which samples are still inside the pause.
    resume_at: Instant,
    unsent_at_resume: Option<u64>,
    /// App bytes already accepted when the sink fault was first observed.
    sent_at_block: Option<u64>,
    recv_at_block: Option<u64>,
    /// During the fault the stack still held payload it had not emitted.
    queued_during_block: bool,
    progress_after_block: bool,
    read_pause_until: Instant,
    rr: Option<RrState>,
    latencies_ns: Vec<u64>,
    /// `first_write - ready` for each round. Zero means the stack accepted the
    /// byte in the same virtual instant the app became ready.
    write_wait_ns: Vec<u64>,
    finished_at: Option<Instant>,
    offered: Option<u64>,
    manual_fin: bool,
    write_blocked: bool,
    memory_blocked: bool,
    writes: u64,
    short_writes: u64,
    reads: u64,
}

struct Route {
    app: App,
}

struct Endpoint {
    shard: Shard,
    apps: BTreeMap<ConnId, App>,
    routes: BTreeMap<(IfaceId, PeerId, u16), Route>,
    buf: Vec<u8>,
    pattern: Vec<u8>,
    unexpected: u64,
    /// iface index → logical port. Holes are removed ifaces.
    iface_port: Vec<Option<usize>>,
    ready: ReadyQueue,
    app_calls: u64,
    suppressed_writable: u64,
    suppress_writable: Option<u64>,
    trace: Trace,
    local_egress: Option<LocalEgress>,
    service: BTreeMap<PeerId, ServiceObservation>,
}

#[derive(Default)]
struct ServiceObservation {
    pending: Option<Instant>,
    max_gap_ns: u64,
    packets: u64,
    bytes: u64,
}

struct LocalEgress {
    period: Duration,
    burst: usize,
    left: usize,
    refill: Instant,
    blocked: BTreeSet<IfaceId>,
}

struct Port {
    server: usize,
    iface: IfaceId,
}

struct Flow {
    id: u64,
    port: u16,
    peer: u64,
    class: &'static str,
    tuple_port: u16,
    salt_c2s: u64,
    salt_s2c: u64,
    start_at: Instant,
    expect: Expect,
    kind: Kind,
    read_pause_until: Instant,
    started: bool,
}

struct Links {
    down_rng: Vec<Rng>,
    up_rng: Vec<Rng>,
    blackholes: BTreeSet<usize>,
    loss_burst: BTreeMap<usize, u64>,
    scripted_drops: BTreeMap<usize, u64>,
    full_hits: BTreeMap<usize, u64>,
    shared: bool,
    down: Vec<Link>,
    up: Vec<Link>,
}

impl Links {
    fn drop_scripted(&mut self, port: usize) -> bool {
        if self.blackholes.contains(&port) {
            return true;
        }
        if let Some(n) = self.loss_burst.get_mut(&port) {
            if *n > 0 {
                *n -= 1;
                return true;
            }
        }
        false
    }
    fn enqueue_down(&mut self, port: usize, now: Instant, peer: PeerId, pkt: bytes::Bytes) {
        let i = if self.shared { 0 } else { port };
        if self.drop_scripted(port) {
            *self.scripted_drops.entry(port).or_default() += 1;
            self.down[i].trace.push(json!({"event":"scripted_drop", "at_ns":now.as_nanos(), "port":port,"packet":crate::schedule::content_digest(&pkt)}));
            self.down[i].stats.sent += 1;
            self.down[i].stats.random_drops += 1;
            return;
        }
        self.down[i].enqueue(now, port as u32, peer, pkt, &mut self.down_rng[i]);
    }

    fn enqueue_up(&mut self, port: usize, now: Instant, peer: PeerId, pkt: bytes::Bytes) {
        let i = if self.shared { 0 } else { port };
        if self.drop_scripted(port) {
            *self.scripted_drops.entry(port).or_default() += 1;
            self.up[i].trace.push(json!({"event":"scripted_drop", "at_ns":now.as_nanos(), "port":port,"packet":crate::schedule::content_digest(&pkt)}));
            self.up[i].stats.sent += 1;
            self.up[i].stats.random_drops += 1;
            return;
        }
        self.up[i].enqueue(now, port as u32, peer, pkt, &mut self.up_rng[i]);
    }
}

pub struct FlowReport {
    pub id: u64,
    pub port: u16,
    pub peer: u64,
    pub class: &'static str,
    pub expect: Expect,
    pub client_sent: u64,
    pub client_recv: u64,
    pub server_sent: u64,
    pub server_recv: u64,
    pub client_target_recv: u64,
    pub client_target_send: u64,
    pub client_eof: bool,
    pub server_eof: bool,
    pub server_write_blocked: bool,
    pub server_writes: u64,
    pub server_queued_end: u64,
    pub corrupt: bool,
    pub reset: bool,
    pub finished_at_ns: Option<u64>,
    pub planned_rr_rounds: u32,
    pub censored_rr_rounds: u32,
    pub censored_latency_lower_bound_ns: u64,
    pub latencies_ns: Vec<u64>,
    pub write_wait_ns: Vec<u64>,
    pub saw_zero_wnd: bool,
    pub saw_wnd_reopen: bool,
    pub min_snd_wnd: Option<u64>,
    pub max_tx_unsent: u64,
    pub unsent_at_resume: Option<u64>,
    pub sent_at_block: Option<u64>,
    pub queued_during_block: bool,
    pub progress_after_block: bool,
    pub stack_released: bool,
}

/// What `remove_iface` did to the stack, read back from the shard rather than
/// from a flag the harness set on the app.
#[derive(Clone, Debug, Default)]
pub struct RebuildStats {
    pub happened: bool,
    pub live_before: usize,
    pub still_on_old: usize,
    pub freed_delta: u64,
    pub reserved_before: u64,
    pub reserved_after: u64,
    pub server_slots_gone: bool,
    pub client_closed: bool,
    pub untouched_live_before: usize,
    pub untouched_live_after: usize,
}

pub struct Report {
    pub app_drive: AppDriveMode,
    pub scheduler: Value,
    pub events: Vec<Value>,
    pub resources: Vec<Value>,
    pub trace: Value,
    pub ok: bool,
    pub errors: Vec<String>,
    pub steps: u64,
    pub wall_sec: f64,
    pub cpu_sec: f64,
    pub seed: u64,
    pub topology: Topology,
    pub ports: u16,
    pub shared_bottleneck: bool,
    pub server_shards: usize,
    pub flows: Vec<FlowReport>,
    pub reserved_peak: u64,
    pub reserved_end: u64,
    pub reserved_at_complete: Option<u64>,
    pub global_high: u64,
    pub physical_peaks: Vec<u64>,
    pub peer_peaks: Vec<(usize, u64, u64)>,
    pub share_blocked: u64,
    pub allocation_failures_by_level: [u64; 3],
    pub conns_end: usize,
    pub progress_reserve: bool,
    pub link_sent: u64,
    pub link_delivered: u64,
    pub link_queue_drops: u64,
    pub link_random_drops: u64,
    pub sink_full_before: Option<u64>,
    pub sink_full_total: u64,
    pub sink_window_ns: Option<(u64, u64)>,
    pub rebuild: RebuildStats,
    /// Virtual clock for every latency and `finished_at`. `wall_sec` / `cpu_sec`
    /// are only the harness process, not a host PPS measurement.
    pub clock: &'static str,
}

pub fn execute(cfg: Config) -> Report {
    let wall0 = std::time::Instant::now();
    let cpu0 = process_cpu();
    let built = match World::new(cfg) {
        Ok(w) => w,
        Err(errors) => {
            return Report {
                app_drive: AppDriveMode::Eager,
                scheduler: json!({}),
                events: Vec::new(),
                resources: Vec::new(),
                trace: json!({}),
                ok: false,
                errors,
                steps: 0,
                wall_sec: wall0.elapsed().as_secs_f64(),
                cpu_sec: (process_cpu() - cpu0).max(0.0),
                seed: 0,
                topology: Topology::SharedShard,
                ports: 0,
                shared_bottleneck: false,
                server_shards: 0,
                flows: Vec::new(),
                reserved_peak: 0,
                reserved_end: 0,
                reserved_at_complete: None,
                global_high: 0,
                physical_peaks: Vec::new(),
                peer_peaks: Vec::new(),
                share_blocked: 0,
                allocation_failures_by_level: [0; 3],
                conns_end: 0,
                progress_reserve: false,
                link_sent: 0,
                link_delivered: 0,
                link_queue_drops: 0,
                link_random_drops: 0,
                sink_full_before: None,
                sink_full_total: 0,
                sink_window_ns: None,
                rebuild: RebuildStats::default(),
                clock: "virtual",
            };
        }
    };
    let mut world = built;
    world.run();
    let mut report = world.finish();
    report.wall_sec = wall0.elapsed().as_secs_f64();
    report.cpu_sec = (process_cpu() - cpu0).max(0.0);
    report
}

struct World {
    app_drive: AppDriveMode,
    events: Vec<(ScriptEvent, bool)>,
    event_results: Vec<Value>,
    resource_samples: Vec<(String, Instant, bool)>,
    resource_results: Vec<Value>,
    leases: BTreeMap<String, zfstack::budget::MemoryLease>,
    script_full: BTreeSet<usize>,
    sink_hit_start: BTreeMap<usize, u64>,
    sink_window_ns: Option<(u64, u64)>,
    blackhole_drop_start: BTreeMap<usize, u64>,
    inactive_ports: BTreeSet<usize>,
    execution_resume: Instant,
    cfg_seed: u64,
    topology: Topology,
    now: Instant,
    until: Instant,
    global: std::sync::Arc<GlobalBudget>,
    servers: Vec<Endpoint>,
    clients: Vec<Endpoint>,
    ports: Vec<Port>,
    links: Links,
    flows: Vec<Flow>,
    gap: Duration,
    full: Option<FullFault>,
    full_released: bool,
    rebuild: Option<(u16, Instant)>,
    rebuilt: bool,
    step_cap: u64,
    steps: u64,
    errors: Vec<String>,
    reserved_peak: u64,
    reserved_at_complete: Option<u64>,
    physical_peaks: Vec<u64>,
    peer_peaks: HashMap<(usize, u64), u64>,
    allow_no_iface: bool,
    sink_full_before: Option<u64>,
    rebuild_stats: RebuildStats,
}

impl World {
    fn configure_script_app(&self, app: &mut App) {
        for (event, _) in &self.events {
            match event.action {
                Action::AppWrite { flow, side, .. } if flow == u64::from(app.flow) && side_matches(side, app.role) => app.offered = Some(0),
                Action::CloseWrite { flow, side } if flow == u64::from(app.flow) && side_matches(side, app.role) => app.manual_fin = true,
                _ => {}
            }
        }
    }

    fn app_location(&self, flow: u64, side: Side) -> Option<(usize, ConnId)> {
        let f = self.flows.get(flow as usize)?;
        let (index, endpoint) = match side {
            Side::Client => (f.port as usize, self.clients.get(f.port as usize)?),
            Side::Server => {
                let index = self.ports[f.port as usize].server;
                (index, &self.servers[index])
            }
        };
        endpoint.apps.iter().find(|(_, a)| u64::from(a.flow) == flow).map(|(id, _)| (index, *id))
    }

    fn condition_reached(&self, condition: &Condition) -> bool {
        let server = |flow| self.app_location(flow, Side::Server).and_then(|(i, id)| self.servers[i].apps.get(&id));
        match *condition {
            Condition::Always => true,
            Condition::Accepted(flow) => self.app_location(flow, Side::Server).is_some(),
            Condition::WriteBlocked(flow) => server(flow).is_some_and(|a| !a.released && a.write_blocked && a.sent < a.send_target),
            Condition::MemoryBlocked(flow) => server(flow).is_some_and(|a| !a.released && a.memory_blocked && a.sent < a.send_target),
            Condition::Received { flow, bytes } => {
                self.flows.get(flow as usize).is_some_and(|f| self.clients[f.port as usize].apps.values().any(|a| u64::from(a.flow) == flow && a.recv >= bytes))
            }
        }
    }

    fn process_events(&mut self) {
        // At most MAX_EVENTS; each event applies once. A condition is observed
        // between driver turns and can never create an unbounded same-now loop.
        for i in 0..self.events.len() {
            let (event, done) = &self.events[i];
            if *done || self.now < origin() + event.at {
                continue;
            }
            let reached = self.condition_reached(&event.condition);
            if !reached && self.now < origin() + event.deadline {
                continue;
            }
            let event = event.clone();
            self.events[i].1 = true;
            let before = self.global.reserved();
            let result = if reached { self.apply_action(&event.action) } else { Err("trigger condition was not reached by deadline".into()) };
            let (status, error) = match result {
                Ok(()) => ("APPLIED", None),
                Err(error) => {
                    self.errors.push(format!("PRECONDITION_NOT_REACHED: event {}: {error}", event.id));
                    ("PRECONDITION_NOT_REACHED", Some(error))
                }
            };
            self.event_results.push(json!({"id": event.id, "sequence": event.sequence, "priority": event.priority,
                "at_ns": self.now.saturating_since(origin()).as_nanos() as u64, "action": format!("{:?}", event.action),
                "status": status, "error": error, "reserved_before": before, "reserved_after": self.global.reserved()}));
        }
    }

    fn apply_action(&mut self, action: &Action) -> Result<(), String> {
        match action {
            Action::StartFlow { flow } => {
                let f = self.flows.get(*flow as usize).ok_or("flow does not exist")?;
                if f.started || self.inactive_ports.contains(&(f.port as usize)) {
                    return Err("flow already started or iface absent".into());
                }
                self.start_flow(*flow as usize);
            }
            Action::AppWrite { flow, side, .. } | Action::PauseRead { flow, side } | Action::ResumeRead { flow, side } | Action::CloseWrite { flow, side } => {
                let (index, id) = self.app_location(*flow, *side).ok_or("flow is not live")?;
                let ep = match side {
                    Side::Client => &mut self.clients[index],
                    Side::Server => &mut self.servers[index],
                };
                if !ep.shard.info(id).is_some_and(|c| !matches!(c.state, State::Closed | State::SynSent | State::SynReceived)) {
                    return Err("connection is not established/live".into());
                }
                let app = ep.apps.get_mut(&id).unwrap();
                match action {
                    Action::AppWrite { bytes, .. } => {
                        if app.fin_sent || *bytes == 0 {
                            return Err("write after FIN or empty application arrival".into());
                        }
                        let next = app.offered.unwrap_or(0).checked_add(*bytes).ok_or("application byte overflow")?;
                        if next > app.send_target {
                            return Err("application arrivals exceed declared flow bytes".into());
                        }
                        app.offered = Some(next);
                        ep.ready.insert(id, Interest::WRITE);
                    }
                    Action::PauseRead { .. } => {
                        if app.eof || app.read_pause_until > self.now {
                            return Err("reader is already paused or EOF".into());
                        }
                        app.read_pause_until = self.until + Duration::from_millis(1);
                    }
                    Action::ResumeRead { .. } => {
                        if app.read_pause_until <= self.now {
                            return Err("reader is not paused".into());
                        }
                        app.read_pause_until = Instant::ZERO;
                        ep.ready.insert(id, Interest::READ);
                    }
                    Action::CloseWrite { .. } => {
                        if app.fin_sent || app.sent != app.send_target {
                            return Err("FIN requires every declared byte accepted and no previous FIN".into());
                        }
                        app.fin_sent = true;
                        ep.shard.shutdown_write(id);
                        ep.ready.insert(id, Interest::READ);
                    }
                    _ => unreachable!(),
                }
            }
            Action::Abort { flow } => {
                let client = self.app_location(*flow, Side::Client).ok_or("client is not live")?;
                let server = self.app_location(*flow, Side::Server).ok_or("server is not live")?;
                if self.clients[client.0].shard.info(client.1).is_none_or(|c| c.state == State::Closed)
                    || self.servers[server.0].shard.info(server.1).is_none_or(|c| c.state == State::Closed)
                {
                    return Err("abort requires live core connections".into());
                }
                for (ep, id) in [(&mut self.clients[client.0], client.1), (&mut self.servers[server.0], server.1)] {
                    ep.ready.remove(id);
                    ep.shard.abort(id);
                    let app = ep.apps.get_mut(&id).unwrap();
                    app.stack_released = ep.shard.info(id).is_none_or(|c| c.state == State::Closed);
                    app.released = true;
                    app.reset_seen = true;
                    app.finished_at = Some(self.now);
                }
                self.flows[*flow as usize].expect = Expect::Reset;
            }
            Action::RemoveIface { port } => {
                let port = *port as usize;
                if self.links.shared || self.inactive_ports.contains(&port) {
                    return Err("iface is absent or links are shared".into());
                }
                let p = &self.ports[port];
                let ep = &mut self.servers[p.server];
                let live: Vec<_> = ep.shard.conn_ids().into_iter().filter(|id| ep.shard.info(*id).is_some_and(|c| c.iface == p.iface)).collect();
                if live.is_empty() {
                    return Err("remove requires live connections".into());
                }
                for f in &mut self.flows {
                    if f.port as usize == port && f.started {
                        f.expect = Expect::Reset;
                    }
                }
                take_down_iface(ep, p.iface, &live, self.now);
                abort_apps(&mut self.clients[port], self.now);
                self.links.up[port].clear();
                self.links.down[port].clear();
                self.inactive_ports.insert(port);
                self.allow_no_iface = true;
            }
            Action::RebuildIface { port } => {
                let port = *port as usize;
                if !self.inactive_ports.remove(&port) {
                    return Err("rebuild requires a removed iface".into());
                }
                let ep = &mut self.servers[self.ports[port].server];
                let iface = ep.shard.add_iface(IfaceConfig::default());
                ep.note_iface(iface, port);
                self.ports[port].iface = iface;
            }
            Action::SinkFull { port } => {
                if !self.script_full.insert(*port as usize) {
                    return Err("sink is already Full".into());
                }
                self.sink_full_before.get_or_insert_with(|| self.servers.iter().map(|s| s.shard.stats().sink_full).sum());
                self.sink_window_ns = Some((self.now.saturating_since(origin()).as_nanos() as u64, u64::MAX));
                self.sink_hit_start.insert(*port as usize, self.links.full_hits.get(&(*port as usize)).copied().unwrap_or(0));
            }
            Action::SinkRelease { port } => {
                let port = *port as usize;
                let before = self.sink_hit_start.get(&port).copied().ok_or("sink was not Full")?;
                if self.links.full_hits.get(&port).copied().unwrap_or(0) <= before {
                    return Err("sink fault never rejected a packet".into());
                }
                if !self.script_full.remove(&port) {
                    return Err("sink was not Full".into());
                }
                self.sink_hit_start.remove(&port);
                if let Some((_, end)) = &mut self.sink_window_ns {
                    *end = self.now.saturating_since(origin()).as_nanos() as u64;
                }
                let p = &self.ports[port];
                self.servers[p.server].shard.egress_released(p.iface);
                self.clients[port].shard.egress_released(IfaceId(0));
            }
            Action::LinkBlackhole { port, enabled } => {
                if *enabled {
                    self.blackhole_drop_start.insert(*port as usize, self.links.scripted_drops.get(&(*port as usize)).copied().unwrap_or(0));
                } else {
                    let before = self.blackhole_drop_start.get(&(*port as usize)).copied().ok_or("blackhole was not enabled")?;
                    if self.links.scripted_drops.get(&(*port as usize)).copied().unwrap_or(0) <= before {
                        return Err("blackhole never dropped a packet".into());
                    }
                    self.blackhole_drop_start.remove(&(*port as usize));
                }
                let changed = if *enabled { self.links.blackholes.insert(*port as usize) } else { self.links.blackholes.remove(&(*port as usize)) };
                if !changed {
                    return Err("blackhole state did not change".into());
                }
            }
            Action::LossBurst { port, packets } => {
                if *packets == 0 || *packets > 1_000_000 || self.links.loss_burst.get(&(*port as usize)).is_some_and(|n| *n > 0) {
                    return Err("loss burst must be bounded, nonempty and not overlap a pending burst".into());
                }
                self.links.loss_burst.insert(*port as usize, *packets);
            }
            Action::HoldMemory { lease, port, peer, bytes } => {
                if self.leases.contains_key(lease) || *bytes == 0 {
                    return Err("duplicate lease or empty allocation".into());
                }
                let server = self.ports[*port as usize].server;
                let owner = self.servers[server].shard.memory_handle(PeerId(*peer)).try_allocate(*bytes).ok_or("memory hold was not admitted")?;
                self.leases.insert(lease.clone(), owner);
            }
            Action::ReleaseMemory { lease } => {
                let owner = self.leases.remove(lease).ok_or("lease does not exist or was already released")?;
                drop(owner);
                // Explicit external-owner release is an application wake source.
                // It is recorded separately from core Writable, never inferred
                // merely from running the world another time.
                for ep in &mut self.servers {
                    for (&id, app) in &ep.apps {
                        if app.memory_blocked && !app.released {
                            ep.ready.insert(id, Interest::WRITE);
                        }
                    }
                }
            }
            Action::ExecutionGap { millis } => {
                if self.execution_resume > self.now {
                    return Err("execution gap is already active".into());
                }
                self.execution_resume = self.now + Duration::from_millis(*millis);
            }
            Action::SetMtu { port, bytes } => {
                let port = *port as usize;
                if self.inactive_ports.contains(&port) {
                    return Err("iface is absent".into());
                }
                let p = &self.ports[port];
                self.servers[p.server].shard.set_iface_mtu(p.iface, *bytes);
                self.clients[port].shard.set_iface_mtu(IfaceId(0), *bytes);
            }
        }
        Ok(())
    }

    fn new(cfg: Config) -> Result<Self, Vec<String>> {
        let mut errors = Vec::new();
        if cfg.flows.is_empty() {
            errors.push("PRECONDITION_NOT_REACHED: no flows configured".into());
        }
        if cfg.flows.len() > u16::MAX as usize {
            errors.push("too many flows".into());
        }
        if cfg.ports == 0 {
            errors.push("ports must be >= 1".into());
        }
        if cfg.limits.peer > cfg.limits.port || cfg.limits.port > cfg.limits.global {
            errors.push(format!("limits must satisfy peer <= port <= global ({} / {} / {})", cfg.limits.peer, cfg.limits.port, cfg.limits.global));
        }
        if !errors.is_empty() {
            return Err(errors);
        }
        let origin = origin();
        let n = cfg.ports as usize;
        let nservers = match cfg.topology {
            Topology::SharedShard => 1,
            Topology::ShardPerPort => n,
        };
        let global = GlobalBudget::new(cfg.limits.global);
        let stack = cfg.stack_override.clone().unwrap_or_else(|| server_stack(cfg.cc, cfg.time_wait));
        let mut servers = Vec::with_capacity(nservers);
        for i in 0..nservers {
            let mut shard = Shard::with_budget_seeded(stack.clone(), std::sync::Arc::clone(&global), cfg.seed ^ (0xA5A5_0000_0000_0000 | i as u64));
            shard.set_budget_limits(cfg.limits.port, cfg.limits.peer, cfg.limits.peer_max_conns);
            if let Err(error) = shard.set_tx_cache_trim(cfg.tx_cache_trim) {
                errors.push(format!("shard {i}: {error}"));
            }
            if !shard.budget().progress_reserve_active() {
                errors.push(format!("shard {i} progress reserve is inactive; raise port/peer limits above 2·H4"));
            }
            let mut endpoint = Endpoint::new(shard);
            endpoint.local_egress = cfg.local_egress_burst.map(|burst| LocalEgress {
                period: cfg.local_egress_period,
                burst,
                left: burst,
                refill: origin + cfg.local_egress_period,
                blocked: BTreeSet::new(),
            });
            endpoint.suppress_writable = cfg.suppress_writable;
            servers.push(endpoint);
        }
        let mut ports = Vec::with_capacity(n);
        for p in 0..n {
            let server = match cfg.topology {
                Topology::SharedShard => 0,
                Topology::ShardPerPort => p,
            };
            let iface = servers[server].shard.add_iface(IfaceConfig::default());
            servers[server].note_iface(iface, p);
            ports.push(Port { server, iface });
        }
        let mut client_stack = server_stack(CcAlgo::Cubic, None);
        if let Some(n) = cfg.client_max_rcv {
            client_stack.max_rcv_buf = n;
            client_stack.init_rcv_wnd = n;
        }
        let mut clients = Vec::with_capacity(n);
        for i in 0..n {
            // Separate budgets so the load generator cannot share a ceiling with
            // the server or with another logical port's client.
            let g = GlobalBudget::new(1 << 30);
            let mut shard = Shard::with_budget_seeded(client_stack.clone(), std::sync::Arc::clone(&g), cfg.seed ^ (0x5A5A_0000_0000_0000 | i as u64));
            shard.set_budget_limits(g.high(), g.high(), 100_000);
            let iface = shard.add_iface(IfaceConfig::default());
            let mut ep = Endpoint::new(shard);
            ep.note_iface(iface, i);
            clients.push(ep);
        }
        let pipes = if cfg.shared_bottleneck { 1 } else { n };
        let links = Links {
            down_rng: (0..pipes).map(|i| Rng::new(cfg.seed ^ 0xD001_0000_0000_0000 ^ i as u64)).collect(),
            up_rng: (0..pipes).map(|i| Rng::new(cfg.seed ^ 0xA001_0000_0000_0000 ^ i as u64)).collect(),
            blackholes: BTreeSet::new(),
            loss_burst: BTreeMap::new(),
            scripted_drops: BTreeMap::new(),
            full_hits: BTreeMap::new(),
            shared: cfg.shared_bottleneck,
            down: (0..pipes).map(|_| Link::new(cfg.link.clone())).collect(),
            up: (0..pipes).map(|_| Link::new(cfg.link.clone())).collect(),
        };
        let mut next_tuple = 20_000u16;
        let mut flows = Vec::with_capacity(cfg.flows.len());
        for (i, spec) in cfg.flows.iter().enumerate() {
            if spec.port as usize >= n {
                errors.push(format!("flow {i} port {} >= {}", spec.port, n));
                continue;
            }
            if let Kind::Rr { req, resp, rounds, .. } = spec.kind {
                if req == 0 || resp == 0 || rounds == 0 {
                    errors.push(format!("flow {i} RR needs non-zero req, resp and rounds"));
                }
            }
            let tuple = match spec.tuple {
                Some(t) => t,
                None => {
                    let t = next_tuple;
                    next_tuple = next_tuple.saturating_add(1);
                    t
                }
            };
            flows.push(Flow {
                id: i as u64,
                port: spec.port,
                peer: spec.peer,
                class: spec.class,
                tuple_port: tuple,
                salt_c2s: i as u64 * 2 + 1,
                salt_s2c: i as u64 * 2 + 2,
                start_at: origin + spec.start_after,
                expect: spec.expect,
                kind: spec.kind.clone(),
                read_pause_until: spec.read_pause.map(|d| origin + d).unwrap_or(Instant::ZERO),
                started: false,
            });
        }
        if !errors.is_empty() {
            return Err(errors);
        }
        let physical_peaks = vec![0; nservers];
        Ok(World {
            app_drive: cfg.app_drive,
            events: cfg.events.into_iter().map(|e| (e, false)).collect(),
            event_results: Vec::new(),
            resource_samples: cfg.resource_samples.into_iter().map(|(label, at)| (label, origin + at, false)).collect(),
            resource_results: Vec::new(),
            leases: BTreeMap::new(),
            script_full: BTreeSet::new(),
            sink_hit_start: BTreeMap::new(),
            sink_window_ns: cfg.full.as_ref().map(|f| (f.from.as_nanos() as u64, f.until.as_nanos() as u64)),
            blackhole_drop_start: BTreeMap::new(),
            inactive_ports: BTreeSet::new(),
            execution_resume: Instant::ZERO,
            cfg_seed: cfg.seed,
            topology: cfg.topology,
            now: origin,
            until: origin + cfg.until,
            global,
            servers,
            clients,
            ports,
            links,
            flows,
            gap: cfg.gap,
            full: cfg.full,
            full_released: false,
            rebuild: cfg.rebuild.map(|(p, d)| (p, origin + d)),
            rebuilt: false,
            step_cap: cfg.step_cap,
            steps: 0,
            errors,
            reserved_peak: 0,
            reserved_at_complete: None,
            physical_peaks,
            peer_peaks: HashMap::new(),
            allow_no_iface: false,
            sink_full_before: None,
            rebuild_stats: RebuildStats::default(),
        })
    }

    fn run(&mut self) {
        self.process_events();
        self.start_due();
        let mut same = 0u32;
        let mut last = self.now;
        loop {
            if self.steps >= self.step_cap {
                self.errors.push(format!("step cap {} hit at {:?}", self.step_cap, self.now));
                break;
            }
            if self.now == last {
                same += 1;
                if same > SAME_NOW_LIMIT {
                    self.errors.push(format!("simulation stuck at {:?}", self.now));
                    break;
                }
            } else {
                same = 0;
                last = self.now;
            }
            let Some(t) = self.next_time() else {
                self.now = self.until;
                self.step();
                if self.next_time().is_some_and(|t| t <= self.until) {
                    continue;
                }
                break;
            };
            if t > self.until {
                self.now = self.until;
                self.step();
                break;
            }
            if t > self.now {
                self.now = t;
            }
            self.step();
        }
    }

    fn quantize(&self, t: Instant) -> Instant {
        if self.gap.is_zero() {
            return t;
        }
        let g = self.gap.as_nanos() as u64;
        Instant::from_nanos(t.as_nanos().div_ceil(g) * g)
    }

    fn server_may_run(&self, now: Instant) -> bool {
        now >= self.execution_resume && (self.gap.is_zero() || self.quantize(now) == now)
    }

    fn next_time(&self) -> Option<Instant> {
        let mut t: Option<Instant> = None;
        let mut take = |x: Option<Instant>| {
            if let Some(x) = x {
                t = Some(t.map_or(x, |y: Instant| y.min(x)));
            }
        };
        for s in &self.servers {
            if let Some(local) = &s.local_egress {
                if !local.blocked.is_empty() {
                    take(Some(local.refill));
                }
            }
            if !s.ready.is_empty() {
                take(Some(self.quantize(self.now.max(self.execution_resume))));
            }
            take(s.shard.next_deadline().map(|d| self.quantize(d.max(self.now).max(self.execution_resume))));
        }
        for c in &self.clients {
            if !c.ready.is_empty() {
                take(Some(self.now));
            }
            take(c.shard.next_deadline());
        }
        for link in &self.links.down {
            take(link.next_delivery());
        }
        for link in &self.links.up {
            take(link.next_delivery().map(|d| self.quantize(d.max(self.now).max(self.execution_resume))));
        }
        for ep in self.servers.iter().chain(self.clients.iter()) {
            for app in ep.apps.values() {
                if app.released {
                    continue;
                }
                if app.read_pause_until > self.now {
                    take(Some(app.read_pause_until));
                }
                if let Some(rr) = &app.rr {
                    if rr.pause_until > self.now {
                        take(Some(rr.pause_until));
                    }
                }
            }
        }
        for (_, at, done) in &self.resource_samples {
            if !done {
                take(Some(*at));
            }
        }
        if self.execution_resume > self.now {
            take(Some(self.execution_resume));
        }
        for (event, done) in &self.events {
            if !done {
                if origin() + event.at > self.now {
                    take(Some(origin() + event.at));
                } else {
                    take(Some(origin() + event.deadline));
                }
            }
        }
        for f in &self.flows {
            if !f.started && f.start_at > self.now {
                take(Some(f.start_at));
            }
        }
        if let Some(full) = &self.full {
            let from = origin() + full.from;
            let until = origin() + full.until;
            if from > self.now {
                take(Some(from));
            }
            if until > self.now {
                take(Some(until));
            }
        }
        if let Some((_, at)) = self.rebuild {
            if !self.rebuilt && at > self.now {
                take(Some(at));
            }
        }
        t
    }

    fn step(&mut self) {
        self.steps += 1;
        let now = self.now;
        self.process_events();
        self.maybe_rebuild(now);
        self.start_due();
        self.note_full_window();
        self.release_full(now);
        self.deliver(now);
        let server_ok = self.server_may_run(now);
        for _ in 0..INNER_ROUNDS {
            let mut progressed = false;
            if server_ok {
                for i in 0..self.servers.len() {
                    progressed |= pump_endpoint(&mut self.servers[i], now, self.app_drive);
                    progressed |= emit_server(i, &mut self.servers, &mut self.links, now, &self.full, &self.script_full);
                }
            }
            for i in 0..self.clients.len() {
                progressed |= pump_endpoint(&mut self.clients[i], now, self.app_drive);
                progressed |= emit_client(i, &mut self.clients, &mut self.links, now, &self.full, &self.script_full);
            }
            if server_ok {
                for s in &self.servers {
                    s.shard.check_invariants();
                }
            }
            for c in &self.clients {
                c.shard.check_invariants();
            }
            if !progressed {
                break;
            }
        }
        self.process_events();
        self.note_budget(now);
        self.sample_resources();
        self.note_block();
        self.note_window_after_emit();
        for ep in &mut self.servers {
            for (&id, app) in &mut ep.apps {
                observe_window(&ep.shard, id, app, now);
            }
        }
    }

    /// `tx_unsent` shrinks in `run`, which happens after the app pump. Look
    /// again at the end of the step so a window that opens and drains in one
    /// step is visible.
    fn note_window_after_emit(&mut self) {
        let now = self.now;
        for s in &mut self.servers {
            let ids: Vec<ConnId> = s.apps.keys().copied().collect();
            for id in ids {
                let Some(info) = s.shard.info(id) else { continue };
                let unsent = info.tx_unsent;
                let Some(app) = s.apps.get_mut(&id) else { continue };
                if app.role != Role::Server || app.resume_at == Instant::ZERO || now < app.resume_at {
                    continue;
                }
                if app.saw_zero_wnd && app.unsent_at_resume.is_some_and(|at| unsent < at) {
                    app.saw_wnd_reopen = true;
                }
            }
        }
    }

    /// Remember the sink-full counter the instant the fault window opens, before
    /// this step's `run` can increment it.
    fn note_full_window(&mut self) {
        if self.sink_full_before.is_some() || self.full.is_none() {
            return;
        }
        let now = self.now;
        let covers = match self.full.as_ref().and_then(|f| f.port) {
            Some(p) => port_blocked(&self.full, p as usize, now),
            None => !self.ports.is_empty() && port_blocked(&self.full, 0, now),
        };
        if !covers {
            return;
        }
        let n = self.servers.iter().map(|s| s.shard.stats().sink_full).sum();
        self.sink_full_before = Some(n);
    }

    fn note_block(&mut self) {
        let now = self.now;
        let full = self.full.clone();
        for s in &mut self.servers {
            let ids: Vec<ConnId> = s.apps.keys().copied().collect();
            for id in ids {
                let queued = s.shard.info(id).is_some_and(|info| info.tx_unsent > 0 || info.tx_queued > 0);
                let Some(app) = s.apps.get_mut(&id) else { continue };
                if app.role != Role::Server || app.send_target == 0 {
                    continue;
                }
                if !port_blocked(&full, app.port as usize, now) && !self.script_full.contains(&(app.port as usize)) {
                    if app.queued_during_block && app.sent > app.sent_at_block.unwrap_or(0) {
                        app.progress_after_block = true;
                    }
                    continue;
                }
                if app.sent_at_block.is_none() {
                    app.sent_at_block = Some(app.sent);
                }
                if queued {
                    app.queued_during_block = true;
                }
            }
        }
        // The receiver's byte count is what proves the transfer resumed, including
        // the case where `write` had already accepted every byte and only the
        // sink was holding it.
        for (port, c) in self.clients.iter_mut().enumerate() {
            let blocked = port_blocked(&full, port, now) || self.script_full.contains(&port);
            for app in c.apps.values_mut() {
                if app.role != Role::Client || app.recv_target == 0 {
                    continue;
                }
                if blocked {
                    if app.recv_at_block.is_none() {
                        app.recv_at_block = Some(app.recv);
                    }
                } else if app.recv > app.recv_at_block.unwrap_or(u64::MAX) {
                    app.progress_after_block = true;
                }
            }
        }
    }

    fn deliver(&mut self, now: Instant) {
        let server_ok = self.server_may_run(now);
        if self.links.shared {
            loop {
                let Some((tag, peer, pkt)) = self.links.down[0].pop_due(now) else { break };
                let port = tag as usize;
                if let Some(c) = self.clients.get_mut(port) {
                    c.shard.ingress(now, IfaceId(0), peer, pkt);
                }
            }
            if server_ok {
                loop {
                    let Some((tag, peer, pkt)) = self.links.up[0].pop_due(now) else { break };
                    let port = tag as usize;
                    if let Some(p) = self.ports.get(port) {
                        let server = p.server;
                        let iface = p.iface;
                        self.servers[server].shard.ingress(now, iface, peer, pkt);
                    }
                }
            }
            return;
        }
        for port in 0..self.ports.len() {
            loop {
                let Some((_tag, peer, pkt)) = self.links.down[port].pop_due(now) else { break };
                self.clients[port].shard.ingress(now, IfaceId(0), peer, pkt);
            }
            if server_ok {
                loop {
                    let Some((_tag, peer, pkt)) = self.links.up[port].pop_due(now) else { break };
                    let server = self.ports[port].server;
                    let iface = self.ports[port].iface;
                    self.servers[server].shard.ingress(now, iface, peer, pkt);
                }
            }
        }
    }

    fn release_full(&mut self, now: Instant) {
        let Some(full) = &self.full else { return };
        if self.full_released || now < origin() + full.until {
            return;
        }
        self.full_released = true;
        let ports: Vec<usize> = match full.port {
            Some(p) => vec![p as usize],
            None => (0..self.ports.len()).collect(),
        };
        for port in ports {
            if let Some(p) = self.ports.get(port) {
                let server = p.server;
                let iface = p.iface;
                self.servers[server].shard.egress_released(iface);
                self.clients[port].shard.egress_released(IfaceId(0));
            }
        }
    }

    fn maybe_rebuild(&mut self, now: Instant) {
        let Some((port, at)) = self.rebuild else { return };
        if self.rebuilt || now < at {
            return;
        }
        self.rebuilt = true;
        self.allow_no_iface = true;
        let port = port as usize;
        if port >= self.ports.len() {
            self.errors.push(format!("rebuild port {port} out of range"));
            return;
        }
        if self.links.shared {
            self.errors.push("iface rebuild is only defined for per-port links".into());
            return;
        }
        self.links.down[port].clear();
        self.links.up[port].clear();
        let server = self.ports[port].server;
        let old = self.ports[port].iface;
        let reserved_before = self.global.reserved();
        let freed_before = self.servers[server].shard.stats().conns_freed;
        let live_before: Vec<ConnId> =
            self.servers[server].shard.conn_ids().into_iter().filter(|id| self.servers[server].shard.info(*id).is_some_and(|info| info.iface == old)).collect();
        let untouched: Vec<ConnId> = self.servers[server]
            .shard
            .conn_ids()
            .into_iter()
            .filter(|id| self.servers[server].shard.info(*id).is_some_and(|info| info.iface != old && info.state.can_send_data()))
            .collect();
        let client_closed = abort_apps(&mut self.clients[port], now);
        let server_slots_gone = take_down_iface(&mut self.servers[server], old, &live_before, now);
        let still_on_old =
            self.servers[server].shard.conn_ids().into_iter().filter(|id| self.servers[server].shard.info(*id).is_some_and(|info| info.iface == old)).count();
        let freed_delta = self.servers[server].shard.stats().conns_freed.saturating_sub(freed_before);
        let reserved_after = self.global.reserved();
        self.rebuild_stats = RebuildStats {
            happened: true,
            live_before: live_before.len(),
            still_on_old,
            freed_delta,
            reserved_before,
            reserved_after,
            server_slots_gone,
            client_closed,
            untouched_live_before: untouched.len(),
            untouched_live_after: untouched.iter().filter(|id| self.servers[server].shard.info(**id).is_some_and(|info| info.state.can_send_data())).count(),
        };
        let iface = self.servers[server].shard.add_iface(IfaceConfig::default());
        self.servers[server].note_iface(iface, port);
        self.ports[port].iface = iface;
    }

    fn start_due(&mut self) {
        let now = self.now;
        let due: Vec<usize> = self.flows.iter().enumerate().filter(|(_, f)| !f.started && f.start_at <= now).map(|(i, _)| i).collect();
        for idx in due {
            self.start_flow(idx);
        }
    }

    fn start_flow(&mut self, idx: usize) {
        let now = self.now;
        let port_i = self.flows[idx].port as usize;
        if self.inactive_ports.contains(&port_i) {
            self.errors.push(format!("PRECONDITION_NOT_REACHED: flow {idx} starts on removed iface"));
            self.flows[idx].started = true;
            return;
        }
        let peer = PeerId(self.flows[idx].peer);
        let tuple = self.flows[idx].tuple_port;
        let (c_send, c_recv, s_send, s_recv, rr) = targets(&self.flows[idx].kind);
        let read_pause = self.flows[idx].read_pause_until;
        let salt_c2s = self.flows[idx].salt_c2s;
        let salt_s2c = self.flows[idx].salt_s2c;
        let server = self.ports[port_i].server;
        let iface = self.ports[port_i].iface;
        let local: SocketAddr = format!("10.0.0.2:{tuple}").parse().unwrap();
        let remote: SocketAddr = "10.0.0.1:80".parse().unwrap();
        let id = self.clients[port_i].shard.connect(now, IfaceId(0), peer, local, remote);
        let mut client_app = App::new(idx as u16, Role::Client, (salt_c2s, salt_s2c), (c_send, c_recv), read_pause, rr.clone());
        client_app.port = self.flows[idx].port;
        self.configure_script_app(&mut client_app);
        self.clients[port_i].apps.insert(id, client_app);
        let mut server_app = App::new(idx as u16, Role::Server, (salt_s2c, salt_c2s), (s_send, s_recv), Instant::ZERO, rr);
        server_app.port = self.flows[idx].port;
        server_app.resume_at = read_pause;
        self.configure_script_app(&mut server_app);
        self.servers[server].routes.insert((iface, peer, tuple), Route { app: server_app });
        self.flows[idx].started = true;
    }

    fn note_budget(&mut self, now: Instant) {
        let reserved = self.global.reserved();
        if reserved > self.global.high() {
            self.errors.push(format!("reserved {reserved} exceeds high {} at {now:?}", self.global.high()));
        }
        self.reserved_peak = self.reserved_peak.max(reserved);
        for (i, s) in self.servers.iter().enumerate() {
            let phys = s.shard.budget().physical_used();
            self.physical_peaks[i] = self.physical_peaks[i].max(phys);
            for (peer, usage) in &s.shard.budget().peers {
                let e = self.peer_peaks.entry((i, peer.0)).or_insert(0);
                *e = (*e).max(usage.bytes);
            }
        }
        if self.reserved_at_complete.is_none() && self.all_settled() {
            self.reserved_at_complete = Some(reserved);
        }
    }

    fn sample_resources(&mut self) {
        for (label, at, done) in &mut self.resource_samples {
            if *done || self.now < *at {
                continue;
            }
            *done = true;
            self.resource_results.push(json!({"label": label, "at_ns": self.now.saturating_since(origin()).as_nanos() as u64,
                "server_connections": self.servers.iter().map(|e| e.shard.conn_count()).sum::<usize>(),
                "reserved_bytes": self.global.reserved(),
                "physical_bytes": self.servers.iter().map(|e| e.shard.budget().physical_used()).sum::<u64>(),
                "ready_items": self.servers.iter().chain(self.clients.iter()).map(|e| e.ready.len()).sum::<usize>(),
                "external_owners": self.leases.len(),
                "measurement": "budget ledger and live slots; excludes harness app history and is not RSS or retained container capacity"}));
        }
    }

    fn all_settled(&self) -> bool {
        self.flows.iter().all(|f| f.started)
            && self.servers.iter().chain(self.clients.iter()).all(|ep| ep.routes.is_empty() && ep.apps.values().all(|a| a.released))
    }

    fn finish(self) -> Report {
        let mut errors = self.errors;
        for (event, done) in &self.events {
            if !done {
                errors.push(format!("PRECONDITION_NOT_REACHED: event {} was not applied", event.id));
            }
        }
        for (port, remaining) in &self.links.loss_burst {
            if *remaining > 0 {
                errors.push(format!("PRECONDITION_NOT_REACHED: port {port} loss burst has {remaining} unobserved packet drops"));
            }
        }
        if !self.script_full.is_empty() || !self.links.blackholes.is_empty() {
            errors.push("PRECONDITION_NOT_REACHED: scripted sink/network outage was not released".into());
        }
        if !self.leases.is_empty() {
            errors.push(format!("resources.external_owners: {} unreleased memory leases", self.leases.len()));
        }
        for ep in self.servers.iter().chain(self.clients.iter()) {
            let st = ep.shard.stats();
            if st.rx_dropped_peer_mismatch > 0 {
                errors.push(format!("peer mismatch drops: {}", st.rx_dropped_peer_mismatch));
            }
            if st.rx_dropped_parse > 0 {
                errors.push(format!("parse drops: {}", st.rx_dropped_parse));
            }
            if st.rx_dropped_no_iface > 0 && !self.allow_no_iface {
                errors.push(format!("packets for a missing iface: {}", st.rx_dropped_no_iface));
            }
            if ep.unexpected > 0 {
                errors.push(format!("{} unexpected accepts", ep.unexpected));
            }
        }
        let mut flows = Vec::with_capacity(self.flows.len());
        for (idx, flow) in self.flows.iter().enumerate() {
            let client = self.clients.get(flow.port as usize).and_then(|ep| ep.apps.values().find(|a| a.flow as usize == idx && a.role == Role::Client));
            let server_ep = self.servers.get(self.ports[flow.port as usize].server);
            let server = server_ep.and_then(|ep| ep.apps.values().find(|a| a.flow as usize == idx && a.role == Role::Server));
            let leftover = server_ep.is_some_and(|ep| ep.routes.keys().any(|k| k.2 == flow.tuple_port && k.1 == PeerId(flow.peer)));
            let corrupt = client.is_some_and(|a| a.corrupt) || server.is_some_and(|a| a.corrupt);
            let reset = client.is_some_and(|a| a.reset_seen) || server.is_some_and(|a| a.reset_seen);
            if corrupt {
                errors.push(format!("flow {} port {} peer {} class {}: salted pattern mismatch", flow.id, flow.port, flow.peer, flow.class));
            }
            match flow.expect {
                Expect::Complete => {
                    if !flow.started {
                        errors.push(format!("flow {} never started", flow.id));
                    }
                    if leftover || server.is_none() {
                        errors.push(format!("flow {} port {} peer {} was not accepted", flow.id, flow.port, flow.peer));
                    }
                    if reset {
                        errors.push(format!("flow {} reset (class {})", flow.id, flow.class));
                    }
                    if let Some(c) = client {
                        if !c.released || c.short || c.recv != c.recv_target || c.sent != c.send_target {
                            errors.push(format!(
                                "flow {} client short or open: sent {}/{} recv {}/{} released {}",
                                flow.id, c.sent, c.send_target, c.recv, c.recv_target, c.released
                            ));
                        }
                    } else {
                        errors.push(format!("flow {} missing client app", flow.id));
                    }
                    if let Some(s) = server {
                        if !s.released || s.short || s.recv != s.recv_target || s.sent != s.send_target {
                            errors.push(format!(
                                "flow {} server short or open: sent {}/{} recv {}/{} released {}",
                                flow.id, s.sent, s.send_target, s.recv, s.recv_target, s.released
                            ));
                        }
                    }
                }
                Expect::Reset => {
                    // `reset_seen` is not evidence: local abort does not emit
                    // `Closed`. The slot has to be gone, or the stack has to
                    // report `State::Closed`, which `stack_released` records.
                    if !flow.started {
                        errors.push(format!("flow {} never started before rebuild", flow.id));
                    }
                    if !client.is_some_and(|c| c.stack_released) {
                        errors.push(format!("flow {} client conn was not closed by the stack", flow.id));
                    }
                    if !server.is_some_and(|s| s.stack_released) {
                        errors.push(format!("flow {} server conn was still in the shard after remove_iface", flow.id));
                    }
                }
            }
            let finished = client.and_then(|c| c.finished_at).map(|t| t.saturating_since(origin()).as_nanos() as u64);
            flows.push(FlowReport {
                id: flow.id,
                port: flow.port,
                peer: flow.peer,
                class: flow.class,
                expect: flow.expect,
                client_sent: client.map(|c| c.sent).unwrap_or(0),
                client_recv: client.map(|c| c.recv).unwrap_or(0),
                server_sent: server.map(|s| s.sent).unwrap_or(0),
                server_recv: server.map(|s| s.recv).unwrap_or(0),
                client_target_recv: client.map(|c| c.recv_target).unwrap_or(0),
                client_target_send: client.map(|c| c.send_target).unwrap_or(0),
                client_eof: client.is_some_and(|c| c.eof),
                server_eof: server.is_some_and(|c| c.eof),
                server_write_blocked: server.is_some_and(|c| c.write_blocked),
                server_writes: server.map_or(0, |c| c.writes),
                server_queued_end: server_ep.map_or(0, |ep| {
                    ep.apps.iter().find(|(_, a)| a.flow as usize == idx).and_then(|(id, _)| ep.shard.info(*id)).map_or(0, |i| i.tx_queued as u64)
                }),
                corrupt,
                reset,
                finished_at_ns: finished,
                planned_rr_rounds: match flow.kind {
                    Kind::Rr { rounds, .. } => rounds,
                    _ => 0,
                },
                censored_rr_rounds: match flow.kind {
                    Kind::Rr { rounds, .. } => rounds.saturating_sub(client.map_or(0, |c| c.latencies_ns.len() as u32)),
                    _ => 0,
                },
                censored_latency_lower_bound_ns: client
                    .and_then(|c| c.rr.as_ref())
                    .and_then(|r| r.ready)
                    .map_or(0, |t| self.now.saturating_since(t).as_nanos() as u64),
                latencies_ns: client.map(|c| c.latencies_ns.clone()).unwrap_or_default(),
                write_wait_ns: client.map(|c| c.write_wait_ns.clone()).unwrap_or_default(),
                saw_zero_wnd: server.is_some_and(|s| s.saw_zero_wnd),
                saw_wnd_reopen: server.is_some_and(|s| s.saw_wnd_reopen),
                min_snd_wnd: server.and_then(|s| s.min_snd_wnd),
                max_tx_unsent: server.map(|s| s.max_tx_unsent).unwrap_or(0),
                unsent_at_resume: server.and_then(|s| s.unsent_at_resume),
                sent_at_block: server.and_then(|s| s.sent_at_block),
                queued_during_block: server.is_some_and(|s| s.queued_during_block),
                progress_after_block: client.is_some_and(|c| c.progress_after_block),
                stack_released: server.is_some_and(|s| s.stack_released) || client.is_some_and(|c| c.stack_released),
            });
        }
        let share_blocked: u64 = self.servers.iter().map(|s| s.shard.budget().share_blocked()).sum();
        let mut allocation_failures_by_level = [0; 3];
        for s in &self.servers {
            for (total, failures) in allocation_failures_by_level.iter_mut().zip(s.shard.budget().stats().failures_by_level()) {
                *total += failures;
            }
        }
        let conns_end: usize = self.servers.iter().map(|s| s.shard.conn_count()).sum();
        let progress_reserve = self.servers.iter().all(|s| s.shard.budget().progress_reserve_active());
        let mut link_sent = 0;
        let mut link_delivered = 0;
        let mut link_queue_drops = 0;
        let mut link_random_drops = 0;
        let fold = |st: &LinkStats, sent: &mut u64, del: &mut u64, qd: &mut u64, rd: &mut u64| {
            *sent += st.sent;
            *del += st.delivered;
            *qd += st.queue_drops;
            *rd += st.random_drops;
        };
        for l in self.links.down.iter().chain(self.links.up.iter()) {
            fold(&l.stats, &mut link_sent, &mut link_delivered, &mut link_queue_drops, &mut link_random_drops);
        }
        let sink_full_total = self.servers.iter().map(|s| s.shard.stats().sink_full).sum();
        let rebuild = self.rebuild_stats.clone();
        let sink_full_before = self.sink_full_before;
        let mut peer_peaks: Vec<_> = self.peer_peaks.into_iter().map(|((shard, peer), bytes)| (shard, peer, bytes)).collect();
        peer_peaks.sort_unstable();
        let finish_now = self.now;
        let scheduler = json!({
            "serve_calls": self.servers.iter().map(|e| e.shard.stats().serve_calls).sum::<u64>(),
            "serve_zero": self.servers.iter().map(|e| e.shard.stats().serve_zero).sum::<u64>(),
            "runs": self.servers.iter().map(|e| e.shard.stats().runs).sum::<u64>(),
            "sink_full": self.servers.iter().map(|e| e.shard.stats().sink_full).sum::<u64>(),
            "round_budget_exhausted": self.servers.iter().map(|e| e.shard.stats().round_budget_exhausted).sum::<u64>(),
            "share_retry_visits": self.servers.iter().map(|e| e.shard.stats().share_retry_visits).sum::<u64>(),
            "share_retry_blocked": self.servers.iter().map(|e| e.shard.stats().share_retry_blocked).sum::<u64>(),
            "share_wakes": self.servers.iter().map(|e| e.shard.stats().share_wakes).sum::<u64>(),
            "share_wake_noop": self.servers.iter().map(|e| e.shard.stats().share_wake_noop).sum::<u64>(),
            "share_wake_pending": self.servers.iter().map(|e| e.shard.share_wake_pending()).sum::<usize>(),
            "peer_service": self.servers.iter().enumerate().flat_map(|(shard, e)| e.service.iter().map(move |(peer, s)| json!({"shard":shard,"peer":peer.0,"data_packets":s.packets,"wire_data_bytes":s.bytes,"max_pending_gap_ns":s.max_gap_ns.max(s.pending.map_or(0, |t| finish_now.saturating_since(t).as_nanos() as u64))}))).collect::<Vec<_>>(),
            "short_writes": self.servers.iter().flat_map(|e| e.apps.values()).map(|a| a.short_writes).sum::<u64>(),
            "app_calls": self.servers.iter().chain(self.clients.iter()).map(|e| e.app_calls).sum::<u64>(),
            "suppressed_writable": self.servers.iter().map(|e| e.suppressed_writable).sum::<u64>(),
            "ready_budget_per_endpoint_round": 1024, "inner_round_limit": INNER_ROUNDS, "same_time_limit": SAME_NOW_LIMIT,
        });
        let trace = json!({"version": 1, "servers": self.servers.iter().map(|e| e.trace.value(true)).collect::<Vec<_>>(),
            "clients": self.clients.iter().map(|e| e.trace.value(true)).collect::<Vec<_>>(),
            "link_down": self.links.down.iter().map(|e| e.trace.value(true)).collect::<Vec<_>>(),
            "link_up": self.links.up.iter().map(|e| e.trace.value(true)).collect::<Vec<_>>(),
            "random_policy": "independent link/direction streams, packet-index consumption; not identical loss sequence numbers across code revisions"});
        Report {
            app_drive: self.app_drive,
            scheduler,
            events: self.event_results,
            resources: self.resource_results,
            trace,
            ok: errors.is_empty(),
            errors,
            steps: self.steps,
            wall_sec: 0.0,
            cpu_sec: 0.0,
            seed: self.cfg_seed,
            topology: self.topology,
            ports: self.ports.len() as u16,
            shared_bottleneck: self.links.shared,
            server_shards: self.servers.len(),
            flows,
            reserved_peak: self.reserved_peak,
            reserved_end: self.global.reserved(),
            reserved_at_complete: self.reserved_at_complete,
            global_high: self.global.high(),
            physical_peaks: self.physical_peaks,
            peer_peaks,
            share_blocked,
            allocation_failures_by_level,
            conns_end,
            progress_reserve,
            link_sent,
            link_delivered,
            link_queue_drops,
            link_random_drops,
            sink_full_before,
            sink_full_total,
            sink_window_ns: self.sink_window_ns,
            rebuild,
            clock: "virtual",
        }
    }
}

fn side_matches(side: Side, role: Role) -> bool {
    matches!((side, role), (Side::Client, Role::Client) | (Side::Server, Role::Server))
}

fn port_blocked(full: &Option<FullFault>, port: usize, now: Instant) -> bool {
    let Some(full) = full else { return false };
    if full.port.is_some_and(|p| p as usize != port) {
        return false;
    }
    now >= origin() + full.from && now < origin() + full.until
}

fn emit_server(si: usize, servers: &mut [Endpoint], links: &mut Links, now: Instant, full: &Option<FullFault>, script_full: &BTreeSet<usize>) -> bool {
    let ep = &mut servers[si];
    if let Some(local) = &mut ep.local_egress {
        if now >= local.refill {
            local.left = local.burst;
            local.refill = now + local.period;
            for iface in std::mem::take(&mut local.blocked) {
                ep.shard.egress_released(iface);
            }
        }
    }
    // Observe waiting only while the core actually has unsent payload. This
    // includes transport/pacing waits, and excludes application think time.
    let pending: BTreeSet<_> = ep.apps.keys().filter_map(|id| ep.shard.info(*id)).filter(|i| i.tx_unsent > 0).map(|i| i.peer).collect();
    for peer in &pending {
        ep.service.entry(*peer).or_default().pending.get_or_insert(now);
    }
    let map = ep.iface_port.clone();
    let outcome = ep.shard.run(now, &mut |iface: IfaceId, pkt: &OutPacket<'_>| {
        let Some(port) = map.get(iface.0 as usize).copied().flatten() else {
            return SendResult::Accepted;
        };
        if script_full.contains(&port) {
            *links.full_hits.entry(port).or_default() += 1;
        }
        if port_blocked(full, port, now) || script_full.contains(&port) {
            return SendResult::Full;
        }
        if let Some(local) = &mut ep.local_egress {
            if pkt.len() > local.left {
                local.blocked.insert(iface);
                return SendResult::Full;
            }
            local.left -= pkt.len();
        }
        if pkt.payload.iter().any(|p| !p.is_empty()) {
            let s = ep.service.entry(pkt.peer).or_default();
            if let Some(t) = s.pending {
                s.max_gap_ns = s.max_gap_ns.max(now.saturating_since(t).as_nanos() as u64);
            }
            s.pending = Some(now);
            s.packets += 1;
            s.bytes += pkt.len() as u64;
        }
        links.enqueue_down(port, now, pkt.peer, Bytes::from(pkt.to_vec()));
        SendResult::Accepted
    });
    let pending: BTreeSet<_> = ep.apps.keys().filter_map(|id| ep.shard.info(*id)).filter(|i| i.tx_unsent > 0).map(|i| i.peer).collect();
    for (peer, s) in &mut ep.service {
        if !pending.contains(peer) {
            s.pending = None;
        }
    }
    outcome.packets > 0 || outcome.more
}

fn emit_client(port: usize, clients: &mut [Endpoint], links: &mut Links, now: Instant, full: &Option<FullFault>, script_full: &BTreeSet<usize>) -> bool {
    let outcome = clients[port].shard.run(now, &mut |_iface: IfaceId, pkt: &OutPacket<'_>| {
        if script_full.contains(&port) {
            *links.full_hits.entry(port).or_default() += 1;
        }
        if port_blocked(full, port, now) || script_full.contains(&port) {
            return SendResult::Full;
        }
        links.enqueue_up(port, now, pkt.peer, Bytes::from(pkt.to_vec()));
        SendResult::Accepted
    });
    outcome.packets > 0 || outcome.more
}

fn targets(kind: &Kind) -> (u64, u64, u64, u64, Option<RrState>) {
    match *kind {
        Kind::Bulk { from_server, from_client } => (from_client, from_server, from_server, from_client, None),
        Kind::Rr { req, resp, rounds, pause } => {
            let rr = RrState { req, resp, rounds, round: 0, pause, pause_until: Instant::ZERO, ready: None, first_write: None };
            (req * rounds as u64, resp * rounds as u64, resp * rounds as u64, req * rounds as u64, Some(rr))
        }
    }
}

impl Endpoint {
    fn new(shard: Shard) -> Self {
        Endpoint {
            shard,
            apps: BTreeMap::new(),
            routes: BTreeMap::new(),
            buf: vec![0; READ_CHUNK],
            pattern: vec![0; READ_CHUNK],
            ready: ReadyQueue::default(),
            app_calls: 0,
            suppressed_writable: 0,
            suppress_writable: None,
            trace: Trace::default(),
            local_egress: None,
            service: BTreeMap::new(),
            unexpected: 0,
            iface_port: Vec::new(),
        }
    }

    fn note_iface(&mut self, iface: IfaceId, port: usize) {
        let i = iface.0 as usize;
        if self.iface_port.len() <= i {
            self.iface_port.resize(i + 1, None);
        }
        self.iface_port[i] = Some(port);
    }
}

/// Flow/direction salt and absolute byte position both participate. A repeated
/// 251-byte shift cannot distinguish flow 0 from flow 251 at scale.
fn pattern_at(pattern: &mut [u8], salt: u64, off: u64, len: usize) -> &[u8] {
    for (i, byte) in pattern[..len].iter_mut().enumerate() {
        let position = off + i as u64;
        let mut word = salt.wrapping_mul(0x9e3779b97f4a7c15) ^ (position / 8);
        word = (word ^ (word >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
        word = (word ^ (word >> 27)).wrapping_mul(0x94d049bb133111eb);
        word ^= word >> 31;
        *byte = (word >> ((position % 8) * 8)) as u8;
    }
    &pattern[..len]
}

impl App {
    fn new(flow: u16, role: Role, salts: (u64, u64), targets: (u64, u64), read_pause_until: Instant, rr: Option<RrState>) -> Self {
        let (send_salt, recv_salt) = salts;
        let (send_target, recv_target) = targets;
        App {
            flow,
            role,
            send_salt,
            recv_salt,
            send_target,
            recv_target,
            sent: 0,
            recv: 0,
            fin_sent: false,
            eof: false,
            released: false,
            closed: None,
            corrupt: false,
            short: false,
            reset_seen: false,
            port: 0,
            stack_released: false,
            saw_zero_wnd: false,
            saw_wnd_reopen: false,
            min_snd_wnd: None,
            max_tx_unsent: 0,
            stall_wnd: None,
            resume_at: Instant::ZERO,
            unsent_at_resume: None,
            sent_at_block: None,
            recv_at_block: None,
            queued_during_block: false,
            progress_after_block: false,
            read_pause_until,
            rr,
            latencies_ns: Vec::new(),
            write_wait_ns: Vec::new(),
            finished_at: None,
            offered: None,
            manual_fin: false,
            write_blocked: false,
            memory_blocked: false,
            writes: 0,
            short_writes: 0,
            reads: 0,
        }
    }

    fn send_limit(&self, now: Instant) -> u64 {
        let allowed = if let Some(rr) = &self.rr {
            if self.role == Role::Client {
                if now < rr.pause_until {
                    rr.round as u64 * rr.req
                } else {
                    (rr.round as u64 + u64::from(rr.round < rr.rounds)) * rr.req
                }
            } else {
                (self.recv / rr.req).min(rr.rounds as u64) * rr.resp
            }
        } else {
            self.send_target
        };
        allowed.min(self.offered.unwrap_or(self.send_target))
    }
}

fn pump_endpoint(ep: &mut Endpoint, now: Instant, mode: AppDriveMode) -> bool {
    let mut progressed = false;
    while let Some(ev) = ep.shard.poll_event() {
        progressed = true;
        ep.trace.push(json!({"at_ns": now.as_nanos(), "event": format!("{ev:?}")}));
        match ev {
            Event::Accepted(id) => {
                on_accept(ep, id);
                ep.ready.insert(id, Interest::BOTH);
            }
            Event::Connected(id) => ep.ready.insert(id, Interest::BOTH),
            Event::Readable(id) => ep.ready.insert(id, Interest::READ),
            Event::Writable(id) => {
                let suppress =
                    ep.suppress_writable.is_some_and(|f| ep.apps.get(&id).is_some_and(|a| a.flow as u64 == f && (a.write_blocked || a.short_writes > 0)));
                if suppress {
                    ep.suppress_writable = None;
                    ep.suppressed_writable += 1;
                    ep.trace.push(json!({"at_ns": now.as_nanos(), "event": "suppress_writable", "conn": id.0}));
                } else {
                    ep.ready.insert(id, Interest::WRITE);
                }
            }
            Event::Closed(id, reason) => {
                ep.ready.remove(id);
                if let Some(app) = ep.apps.get_mut(&id) {
                    app.closed = Some(reason);
                    if reason != CloseReason::Normal {
                        app.reset_seen = true;
                    }
                    // Read any final data and perform lifecycle cleanup. A
                    // close notification never retries a blocked writer.
                    ep.ready.insert(id, Interest::READ);
                }
            }
        }
    }
    // These are application timers, not rescue polls. Consume each timer once.
    for (&id, app) in &mut ep.apps {
        if app.released {
            ep.ready.remove(id);
            continue;
        }
        if app.read_pause_until != Instant::ZERO && now >= app.read_pause_until {
            app.read_pause_until = Instant::ZERO;
            ep.ready.insert(id, Interest::READ);
        }
        if let Some(rr) = &app.rr {
            if app.role == Role::Client
                && rr.ready.is_none()
                && rr.round < rr.rounds
                && app.sent == rr.round as u64 * rr.req
                && now >= rr.pause_until
                && app.offered.is_none_or(|offered| offered > app.sent)
                && ep.shard.info(id).is_some_and(|i| i.state.can_send_data())
            {
                ep.ready.insert(id, Interest::WRITE);
            }
        }
    }
    let ready = match mode {
        AppDriveMode::Eager => {
            ep.ready.take(usize::MAX);
            ep.apps.keys().copied().map(|id| (id, Interest::BOTH)).collect::<Vec<_>>()
        }
        AppDriveMode::EventOnly => ep.ready.take(1024),
    };
    for (id, interest) in ready {
        if ep.apps.get(&id).is_none_or(|a| a.released) {
            continue;
        }
        ep.app_calls += 1;
        let advanced = pump_one(ep, id, now, interest);
        if let Some(app) = ep.apps.get(&id) {
            if advanced || app.write_blocked {
                ep.trace.push(json!({"event":"app_progress", "at_ns":now.as_nanos(), "conn":id.0,"flow":app.flow,
                    "read_ready":interest.read,"write_ready":interest.write,"sent":app.sent,"received":app.recv,
                    "write_blocked":app.write_blocked,"memory_blocked":app.memory_blocked,"eof":app.eof}));
            }
        }
        progressed |= advanced;
    }
    progressed
}

fn on_accept(ep: &mut Endpoint, id: ConnId) {
    let Some(info) = ep.shard.info(id) else {
        ep.unexpected += 1;
        ep.shard.abort(id);
        return;
    };
    let key = (info.iface, info.peer, info.remote.port());
    if let Some(route) = ep.routes.remove(&key) {
        ep.apps.insert(id, route.app);
    } else {
        ep.unexpected += 1;
        ep.shard.abort(id);
    }
}

fn pump_one(ep: &mut Endpoint, id: ConnId, now: Instant, interest: Interest) -> bool {
    let Endpoint { shard, apps, buf, pattern, .. } = ep;
    let Some(app) = apps.get_mut(&id) else { return false };
    if app.released {
        return false;
    }
    let mut progressed = false;
    if let Some(reason) = app.closed {
        if reason != CloseReason::Normal || app.eof {
            app.released = true;
            app.finished_at = Some(now);
            if reason != CloseReason::Normal {
                app.reset_seen = true;
            }
            shard.close(now, id);
            return true;
        }
    }
    let established = shard.info(id).is_some_and(|i| i.state.can_send_data());
    let recv_before = app.recv;
    if interest.read && now >= app.read_pause_until && !app.eof {
        loop {
            app.reads += 1;
            match shard.read(now, id, buf) {
                ReadResult::Data(n) => {
                    let salt = app.recv_salt;
                    let off = app.recv;
                    if app.recv_target == 0 || off + n as u64 > app.recv_target || buf[..n] != *pattern_at(pattern, salt, off, n) {
                        app.corrupt = true;
                    }
                    app.recv += n as u64;
                    progressed = true;
                }
                ReadResult::Eof => {
                    app.eof = true;
                    progressed = true;
                    break;
                }
                ReadResult::WouldBlock => break,
                ReadResult::Closed(reason) => {
                    app.closed = Some(reason);
                    app.reset_seen = reason != CloseReason::Normal;
                    app.released = true;
                    app.finished_at = Some(now);
                    shard.close(now, id);
                    return true;
                }
            }
        }
    }
    if let Some(rr) = app.rr.as_mut() {
        if app.role == Role::Client {
            while rr.round < rr.rounds && app.recv >= (rr.round as u64 + 1) * rr.resp {
                let ready = rr.ready.take();
                let first = rr.first_write.take();
                if let Some(t0) = ready {
                    app.latencies_ns.push(now.saturating_since(t0).as_nanos() as u64);
                    let waited = first.map(|t| t.saturating_since(t0).as_nanos() as u64).unwrap_or(0);
                    app.write_wait_ns.push(waited);
                }
                rr.round += 1;
                if rr.round < rr.rounds {
                    rr.pause_until = now + rr.pause;
                }
            }
        }
    }
    if established && (interest.write || (app.rr.is_some() && app.recv > recv_before)) {
        let limit = app.send_limit(now);
        if let Some(rr) = app.rr.as_mut() {
            if app.role == Role::Client
                && rr.ready.is_none()
                && rr.round < rr.rounds
                && app.sent == rr.round as u64 * rr.req
                && now >= rr.pause_until
                && limit > app.sent
            {
                rr.ready = Some(now);
            }
        }
        while app.sent < limit {
            let n = ((limit - app.sent) as usize).min(WRITE_CHUNK);
            let before = app.sent;
            let salt = app.send_salt;
            app.writes += 1;
            match shard.write(id, pattern_at(pattern, salt, before, n)) {
                WriteResult::Written(w) => {
                    if let Some(rr) = app.rr.as_mut() {
                        if app.role == Role::Client && before == rr.round as u64 * rr.req && rr.first_write.is_none() {
                            rr.first_write = Some(now);
                        }
                    }
                    app.memory_blocked = false;
                    app.write_blocked = w < n;
                    app.sent += w as u64;
                    progressed = true;
                    if w < n {
                        app.short_writes += 1;
                        break;
                    }
                }
                WriteResult::WouldBlock | WriteResult::QuotaBlocked => {
                    app.write_blocked = true;
                    app.memory_blocked = false;
                    break;
                }
                WriteResult::MemoryBlocked => {
                    app.write_blocked = true;
                    app.memory_blocked = true;
                    break;
                }
                WriteResult::Closed => {
                    app.closed = Some(CloseReason::Aborted);
                    app.reset_seen = true;
                    app.released = true;
                    app.finished_at = Some(now);
                    return true;
                }
            }
        }
        if should_fin(app) {
            app.fin_sent = true;
            shard.shutdown_write(id);
            progressed = true;
        }
    }
    if app.eof && app.fin_sent {
        if app.recv < app.recv_target || app.sent < app.send_target {
            app.short = true;
        }
        app.released = true;
        app.finished_at = Some(now);
        shard.close(now, id);
        progressed = true;
    }
    progressed
}

fn observe_window(shard: &Shard, id: ConnId, app: &mut App, now: Instant) {
    if app.role == Role::Server {
        if let Some(info) = shard.info(id) {
            app.min_snd_wnd = Some(app.min_snd_wnd.map_or(info.snd_wnd, |m| m.min(info.snd_wnd)));
            // `sent` is what the app handed the stack, which can run ahead of the
            // window. Zero-window means the peer's advertised window is closed
            // while payload is still sitting in the send buffer.
            app.max_tx_unsent = app.max_tx_unsent.max(info.tx_unsent);
            // Receiver SWS can leave a non-zero advertisement while more payload
            // is waiting than that advertisement can hold. That is the stall.
            // A later larger snd_wnd is the window update after the app reads.
            if info.snd_wnd == 0 || (info.tx_unsent > info.snd_wnd && info.tx_unsent > 0) {
                app.saw_zero_wnd = true;
                app.stall_wnd = Some(app.stall_wnd.map_or(info.snd_wnd, |w| w.min(info.snd_wnd)));
            }
            if app.resume_at > Instant::ZERO {
                if now < app.resume_at {
                    app.unsent_at_resume = Some(info.tx_unsent);
                } else if app.saw_zero_wnd && app.unsent_at_resume.is_some_and(|at| info.tx_unsent < at) {
                    app.saw_wnd_reopen = true;
                }
            } else if app.saw_zero_wnd && info.snd_wnd > app.stall_wnd.unwrap_or(u64::MAX) {
                app.saw_wnd_reopen = true;
            }
        }
    }
}

fn should_fin(app: &App) -> bool {
    if app.manual_fin || app.fin_sent || app.sent < app.send_target {
        return false;
    }
    if let Some(rr) = &app.rr {
        if app.role == Role::Client {
            return app.recv >= app.recv_target && rr.round >= rr.rounds;
        }
        return app.recv >= app.recv_target;
    }
    true
}

/// Abort every app conn. Returns whether each one is `Closed` or gone afterwards.
/// Local abort does not emit `Closed`; the state is read back from `info`.
fn abort_apps(ep: &mut Endpoint, now: Instant) -> bool {
    let ids: Vec<ConnId> = ep.apps.keys().copied().collect();
    let mut all_closed = !ids.is_empty();
    for id in ids {
        ep.ready.remove(id);
        ep.shard.abort(id);
        let gone = ep.shard.info(id).is_none_or(|info| info.state == State::Closed);
        if !gone {
            all_closed = false;
        }
        if let Some(app) = ep.apps.get_mut(&id) {
            if !app.released {
                app.stack_released = gone;
                app.released = true;
                app.finished_at = Some(now);
            }
        }
    }
    while ep.shard.poll_event().is_some() {}
    all_closed
}

/// `remove_iface` frees the slots. Returns whether every previously live id is gone.
fn take_down_iface(ep: &mut Endpoint, iface: IfaceId, live: &[ConnId], now: Instant) -> bool {
    ep.shard.remove_iface(iface);
    if (iface.0 as usize) < ep.iface_port.len() {
        ep.iface_port[iface.0 as usize] = None;
    }
    ep.routes.retain(|k, _| k.0 != iface);
    let gone = live.iter().all(|id| ep.shard.info(*id).is_none());
    let ids: Vec<ConnId> = ep.apps.keys().copied().collect();
    for id in ids {
        let slot_gone = ep.shard.info(id).is_none();
        if let Some(app) = ep.apps.get_mut(&id) {
            if slot_gone {
                ep.ready.remove(id);
                app.stack_released = true;
                app.released = true;
                app.finished_at.get_or_insert(now);
            }
        }
    }
    gone && live.iter().all(|id| ep.shard.info(*id).is_none())
}

/// User + kernel CPU time of this process, excluding child processes.
#[cfg(unix)]
fn process_cpu() -> f64 {
    let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
    // SAFETY: getrusage initializes the output on success; it is read only then.
    if unsafe { libc::getrusage(libc::RUSAGE_SELF, usage.as_mut_ptr()) } != 0 {
        return 0.0;
    }
    let usage = unsafe { usage.assume_init() };
    let seconds = |t: libc::timeval| t.tv_sec as f64 + t.tv_usec as f64 * 1e-6;
    seconds(usage.ru_utime) + seconds(usage.ru_stime)
}

#[cfg(windows)]
fn process_cpu() -> f64 {
    use windows_sys::Win32::Foundation::FILETIME;
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, GetProcessTimes};

    let mut creation = FILETIME::default();
    let mut exit = FILETIME::default();
    let mut kernel = FILETIME::default();
    let mut user = FILETIME::default();
    // SAFETY: the pseudo-handle refers to this process and all outputs are valid
    // FILETIME pointers. The pseudo-handle does not need to be closed.
    if unsafe { GetProcessTimes(GetCurrentProcess(), &mut creation, &mut exit, &mut kernel, &mut user) } == 0 {
        return 0.0;
    }
    let seconds = |t: FILETIME| ((u64::from(t.dwHighDateTime) << 32) | u64::from(t.dwLowDateTime)) as f64 * 1e-7;
    seconds(kernel) + seconds(user)
}

#[cfg(test)]
mod tests {
    use super::*;
    use zfstack::budget::{AllocationKind, Level, Tier};

    #[test]
    fn process_cpu_records_work() {
        let before = process_cpu();
        let mut value = 1u64;
        for _ in 0..32_000_000 {
            value = std::hint::black_box(value.wrapping_mul(6364136223846793005).wrapping_add(1));
        }
        std::hint::black_box(value);
        let after = process_cpu();
        assert!(before.is_finite() && before >= 0.0);
        assert!(after.is_finite() && after > before, "process CPU clock did not advance: {before} -> {after}");
    }

    fn physical_level(level: Level) {
        let mut cfg = crate::scenario::build("wall-bulk", &crate::scenario::Overrides::default()).unwrap();
        cfg.app_drive = AppDriveMode::EventOnly;
        let limit = 1 << 20;
        cfg.limits = Limits {
            global: if level == Level::Global { limit } else { 2 * limit },
            port: if level == Level::Peer { 2 * limit } else { limit },
            peer: limit,
            peer_max_conns: 128,
        };
        let mut world = World::new(cfg).unwrap();
        let memory = world.servers[0].shard.memory_handle(PeerId(1));
        let used = match level {
            Level::Global => world.global.reserved(),
            Level::Port => world.servers[0].shard.budget().physical_used(),
            Level::Peer => memory.peer_physical_used(),
        };
        let admitted = limit - world.global.headroom(limit, Tier::Admit);
        let owner = memory.try_allocate(admitted - used).expect("fill the admission tier exactly");
        let before = world.global.reserved();
        let refusal = memory.try_allocate_level(1, AllocationKind::Other).err();
        assert_eq!(refusal, Some(level));
        assert_eq!(world.global.reserved(), before, "refusal rolls back every partial reservation");
        let mut failures = [0; 3];
        failures[level as usize] = 1;
        assert_eq!(world.servers[0].shard.budget().stats().failures_by_level(), failures);
        drop(owner);
        assert_eq!(memory.peer_physical_used(), 0, "last owner returns the peer charge");
        world.run();
        let result = world.finish();
        assert!(result.ok, "{:?}", result.errors);
        assert_eq!(result.allocation_failures_by_level, failures);
        assert!(result.flows[0].client_recv > 0, "a real connection transfers after release");
    }

    #[test]
    fn c04_global_physical_refusal_then_event_only_recovery() {
        physical_level(Level::Global);
    }
    #[test]
    fn c04_port_physical_refusal_then_event_only_recovery() {
        physical_level(Level::Port);
    }
    #[test]
    fn c04_peer_physical_refusal_then_event_only_recovery() {
        physical_level(Level::Peer);
    }

    #[test]
    fn salted_patterns_distinguish_large_flow_ids_and_absolute_offsets() {
        let mut scratch = vec![0; 4096];
        let first = pattern_at(&mut scratch, 1, 0, 4096).to_vec();
        assert_ne!(first, pattern_at(&mut scratch, 503, 0, 4096));
        assert_ne!(first, pattern_at(&mut scratch, 1, 251, 4096));
        let left = pattern_at(&mut scratch, 8739, 119, 29).to_vec();
        let right = pattern_at(&mut scratch, 8739, 120, 28);
        assert_eq!(&left[1..], right, "validation is independent of I/O chunk boundaries");
    }
}
