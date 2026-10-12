//! zfstack adapter (feature `zfstack`).
//!
//! Default: one `Shard`, one iface (MTU `tun::MTU`), every packet attributed to
//! peer 1. The app server is driven from shard events only.
//!
//! `--test isolate` builds several logical ports. Packets are demuxed by
//! destination `10.211.<port>.2` and source `10.210.<peer>.1`. That mapping is
//! the harness's stand-in for "which WireGuard port / peer this packet came
//! from". It is not WireGuard, and one TUN is not N physical devices.
//! `shared-shard` puts every iface on one shard (docs/design/0001). `per-port-shard`
//! uses one shard per port, all sharing one `GlobalBudget`, still driven by this
//! single thread.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

use bytes::Bytes;
use zfstack::budget::GlobalBudget;
use zfstack::{CcAlgo, CloseReason, ConnId, Event, IfaceConfig, IfaceId, OutPacket, PeerId, ReadResult, SendResult, Shard, StackConfig, WriteResult};

use crate::app::{AppConn, CloseAction, ServerCounters};
use crate::isolate::{self, IsolatePlan};
use crate::stack::UserStack;
use crate::tun;

#[derive(Clone, Debug)]
pub struct ZfOpts {
    /// Per-connection receive buffer ceiling / send in-flight cap (bytes), from `--sock-buf-kb`.
    pub sock_buf: usize,
    pub cc: CcAlgo,
    pub pacing: bool,
}

struct C {
    app: AppConn,
    eof: bool,
    /// Produced by the app but not yet accepted by the stack.
    pending: Vec<u8>,
    pending_off: usize,
}

#[derive(Default)]
struct Agg {
    conns: u64,
    bytes_sent: u64,
    bytes_retrans: u64,
    rto: u64,
    tlp: u64,
    fast_recoveries: u64,
    spurious_rtos: u64,
    dsacks: u64,
    ooo_segs: u64,
    rack_scanned: u64,
    small_segs: u64,
    max_cwnd: u64,
    max_rcv_target: u64,
    dropped_no_mem: u64,
    limited_rwnd_ms: f64,
    limited_cwnd_ms: f64,
    limited_pacing_ms: f64,
}

impl Agg {
    fn add(&mut self, i: &zfstack::ConnInfo) {
        let s = &i.stats;
        self.conns += 1;
        self.bytes_sent += s.bytes_sent;
        self.bytes_retrans += s.bytes_retrans;
        self.rto += s.rto_count;
        self.tlp += s.tlp_count;
        self.fast_recoveries += s.fast_recoveries;
        self.spurious_rtos += s.spurious_rtos;
        self.dsacks += s.dsacks;
        self.ooo_segs += s.ooo_segs;
        self.rack_scanned += s.rack_scanned;
        self.small_segs += s.small_segs;
        self.max_cwnd = self.max_cwnd.max(i.cwnd);
        self.max_rcv_target = self.max_rcv_target.max(i.rcv_target);
        self.dropped_no_mem += s.dropped_no_mem;
        self.limited_rwnd_ms += s.limited_rwnd_ns as f64 / 1e6;
        self.limited_cwnd_ms += s.limited_cwnd_ns as f64 / 1e6;
        self.limited_pacing_ms += s.limited_pacing_ns as f64 / 1e6;
    }
    fn json(&self) -> serde_json::Value {
        serde_json::json!({
            "conns": self.conns, "bytes_sent": self.bytes_sent, "bytes_retrans": self.bytes_retrans,
            "rto": self.rto, "tlp": self.tlp, "fast_recoveries": self.fast_recoveries,
            "spurious_rtos": self.spurious_rtos, "dsacks": self.dsacks, "ooo_segs": self.ooo_segs, "rack_scanned": self.rack_scanned, "small_segs": self.small_segs,
            "max_cwnd": self.max_cwnd, "max_rcv_target": self.max_rcv_target, "dropped_no_mem": self.dropped_no_mem, "limited_rwnd_ms": self.limited_rwnd_ms,
            "limited_cwnd_ms": self.limited_cwnd_ms, "limited_pacing_ms": self.limited_pacing_ms,
        })
    }
}

/// One shard plus the connections it owns. ConnIds are not unique across shards.
struct Slot {
    shard: Shard,
    /// `iface_port[iface] = Some(logical port)` for isolate mode.
    iface_port: Vec<Option<u16>>,
    tx_pkts: Vec<u64>,
    tx_bytes: Vec<u64>,
    conns: HashMap<ConnId, C>,
    active: HashSet<ConnId>,
    buf: Vec<u8>,
    pkt: Vec<u8>,
    closed_agg: Agg,
    max_conns: usize,
    t_ingress: std::time::Duration,
    t_run: std::time::Duration,
    t_app: std::time::Duration,
}

impl Slot {
    fn new(shard: Shard) -> Self {
        Slot {
            shard,
            iface_port: Vec::new(),
            tx_pkts: Vec::new(),
            tx_bytes: Vec::new(),
            conns: HashMap::new(),
            active: HashSet::new(),
            buf: vec![0u8; 256 * 1024],
            pkt: Vec::with_capacity(2048),
            closed_agg: Agg::default(),
            max_conns: 0,
            t_ingress: Default::default(),
            t_run: Default::default(),
            t_app: Default::default(),
        }
    }

    fn note_iface(&mut self, iface: IfaceId, port: u16) {
        let i = iface.0 as usize;
        if self.iface_port.len() <= i {
            self.iface_port.resize(i + 1, None);
            self.tx_pkts.resize(i + 1, 0);
            self.tx_bytes.resize(i + 1, 0);
        }
        self.iface_port[i] = Some(port);
    }

    fn drop_conn(&mut self, id: ConnId, zn: zfstack::Instant, abort: bool) {
        if let Some(info) = self.shard.info(id) {
            self.closed_agg.add(&info);
        }
        if abort {
            self.shard.abort(id);
        } else {
            self.shard.close(zn, id);
        }
        if let Some(mut c) = self.conns.remove(&id) {
            c.app.finish();
        }
        self.active.remove(&id);
    }

    fn pump(&mut self, id: ConnId, zn: zfstack::Instant) -> bool {
        let mut progressed = false;
        loop {
            let Some(c) = self.conns.get_mut(&id) else { return progressed };
            if c.eof {
                break;
            }
            match self.shard.read(zn, id, &mut self.buf) {
                ReadResult::Data(n) => {
                    c.app.on_recv(&self.buf[..n]);
                    progressed = true;
                }
                ReadResult::Eof => {
                    c.eof = true;
                    c.app.on_peer_eof();
                    progressed = true;
                }
                ReadResult::WouldBlock => break,
                ReadResult::Closed(_) => {
                    self.drop_conn(id, zn, false);
                    return true;
                }
            }
        }
        loop {
            let Some(c) = self.conns.get_mut(&id) else { return progressed };
            if c.pending_off < c.pending.len() {
                match self.shard.write(id, &c.pending[c.pending_off..]) {
                    WriteResult::Written(n) => {
                        c.pending_off += n;
                        progressed = true;
                        if c.pending_off < c.pending.len() {
                            break;
                        }
                    }
                    WriteResult::WouldBlock | WriteResult::QuotaBlocked | WriteResult::MemoryBlocked => break,
                    WriteResult::Closed => {
                        self.drop_conn(id, zn, true);
                        return true;
                    }
                }
                continue;
            }
            if !c.app.wants_send() {
                break;
            }
            let space = self.shard.send_space(id).min(self.buf.len());
            if space == 0 {
                let _ = self.shard.write(id, &[]);
                break;
            }
            let c = self.conns.get_mut(&id).unwrap();
            let n = c.app.produce(&mut self.buf[..space]);
            if n == 0 {
                break;
            }
            match self.shard.write(id, &self.buf[..n]) {
                WriteResult::Written(w) => {
                    progressed = true;
                    if w < n {
                        c.pending.clear();
                        c.pending.extend_from_slice(&self.buf[w..n]);
                        c.pending_off = 0;
                        break;
                    }
                }
                WriteResult::WouldBlock | WriteResult::QuotaBlocked | WriteResult::MemoryBlocked => {
                    c.pending.clear();
                    c.pending.extend_from_slice(&self.buf[..n]);
                    c.pending_off = 0;
                    break;
                }
                WriteResult::Closed => {
                    self.drop_conn(id, zn, true);
                    return true;
                }
            }
        }
        let Some(c) = self.conns.get(&id) else { return progressed };
        let flushed = c.pending_off >= c.pending.len();
        match c.app.close_action() {
            CloseAction::Close if flushed && !c.app.wants_send() => {
                self.drop_conn(id, zn, false);
                progressed = true;
            }
            CloseAction::Abort => {
                self.drop_conn(id, zn, true);
                progressed = true;
            }
            _ => {}
        }
        progressed
    }

    fn handle_events(&mut self, counters: &Arc<ServerCounters>) -> bool {
        let mut any = false;
        while let Some(ev) = self.shard.poll_event() {
            any = true;
            match ev {
                Event::Accepted(id) => {
                    self.conns.insert(id, C { app: AppConn::new(counters.clone()), eof: false, pending: Vec::new(), pending_off: 0 });
                    self.active.insert(id);
                    self.max_conns = self.max_conns.max(self.conns.len());
                }
                Event::Readable(id) | Event::Writable(id) => {
                    if self.conns.contains_key(&id) {
                        self.active.insert(id);
                    }
                }
                Event::Closed(id, _reason) => {
                    if self.conns.contains_key(&id) {
                        self.active.insert(id);
                        let _: Option<CloseReason> = None;
                    }
                }
                Event::Connected(_) => {}
            }
        }
        any
    }

    /// One scheduling round. `true` if the caller should poll again immediately.
    fn poll_once(&mut self, zn: zfstack::Instant, out: &mut dyn FnMut(&[u8]), counters: &Arc<ServerCounters>) -> bool {
        let tx_pkts = &mut self.tx_pkts;
        let tx_bytes = &mut self.tx_bytes;
        let pkt = &mut self.pkt;
        let mut sink = |iface: IfaceId, p: &OutPacket<'_>| {
            let i = iface.0 as usize;
            if i < tx_pkts.len() {
                tx_pkts[i] += 1;
                tx_bytes[i] += p.len() as u64;
            }
            pkt.clear();
            p.write_to(pkt);
            out(pkt);
            SendResult::Accepted
        };
        let t0 = Instant::now();
        let o = self.shard.run(zn, &mut sink);
        let t1 = Instant::now();
        self.t_run += t1 - t0;
        let ev = self.handle_events(counters);
        let mut progressed = false;
        if !self.active.is_empty() {
            let ids: Vec<ConnId> = self.active.drain().collect();
            for id in ids {
                progressed |= self.pump(id, zn);
            }
        }
        self.t_app += t1.elapsed();
        ev || progressed || o.more
    }
}

pub struct ZfStack {
    opts: ZfOpts,
    start: Instant,
    counters: Arc<ServerCounters>,
    slots: Vec<Slot>,
    /// Legacy single-iface mode. Isolate mode demuxes instead.
    legacy: Option<IfaceId>,
    /// `locs[port] = (slot index, iface)`.
    locs: Vec<(usize, IfaceId)>,
    rx_pkts: Vec<u64>,
    demux_drops: u64,
    plan: Option<IsolatePlan>,
}

impl ZfStack {
    pub fn new(opts: ZfOpts, counters: Arc<ServerCounters>) -> Self {
        let cfg = stack_cfg(&opts);
        let mut shard = Shard::new(cfg);
        let iface = shard.add_iface(IfaceConfig { mtu: tun::MTU as u16 });
        ZfStack {
            opts,
            start: Instant::now(),
            counters,
            slots: vec![Slot::new(shard)],
            legacy: Some(iface),
            locs: Vec::new(),
            rx_pkts: Vec::new(),
            demux_drops: 0,
            plan: None,
        }
    }

    /// Logical multi-port server. Budgets are ample ceilings so this mode measures
    /// scheduling and demux, not an artificially tight quota. Still `StackConfig::default`
    /// plus the bench's CC / buffer flags — not `StackConfig::client()`.
    pub fn with_isolate(opts: ZfOpts, counters: Arc<ServerCounters>, plan: IsolatePlan) -> Self {
        let cfg = stack_cfg(&opts);
        let nslots = if plan.per_port_shard { plan.ports as usize } else { 1 };
        let global = GlobalBudget::new(512 << 20);
        let mut slots = Vec::with_capacity(nslots);
        for _ in 0..nslots {
            let mut shard = Shard::with_budget(cfg.clone(), std::sync::Arc::clone(&global));
            shard.set_budget_limits(64 << 20, 32 << 20, 4096);
            slots.push(Slot::new(shard));
        }
        let mut locs = Vec::with_capacity(plan.ports as usize);
        for port in 0..plan.ports {
            let slot = if plan.per_port_shard { port as usize } else { 0 };
            let iface = slots[slot].shard.add_iface(IfaceConfig { mtu: tun::MTU as u16 });
            slots[slot].note_iface(iface, port);
            locs.push((slot, iface));
        }
        ZfStack { opts, start: Instant::now(), counters, slots, legacy: None, locs, rx_pkts: vec![0; plan.ports as usize], demux_drops: 0, plan: Some(plan) }
    }

    fn zt(&self, now: Instant) -> zfstack::Instant {
        zfstack::Instant::from_nanos(now.saturating_duration_since(self.start).as_nanos() as u64 + 1)
    }
}

fn stack_cfg(opts: &ZfOpts) -> StackConfig {
    StackConfig {
        cc: opts.cc,
        pacing: opts.pacing,
        max_rcv_buf: opts.sock_buf as u32,
        max_snd_inflight: opts.sock_buf as u32,
        timestamps: std::env::var("ZF_TS").map_or(true, |v| v != "0"),
        ..Default::default()
    }
}

impl UserStack for ZfStack {
    fn ingress(&mut self, now: Instant, pkt: &[u8]) {
        let zn = self.zt(now);
        let t0 = Instant::now();
        if let Some(iface) = self.legacy {
            self.slots[0].shard.ingress(zn, iface, PeerId(1), Bytes::copy_from_slice(pkt));
            self.slots[0].t_ingress += t0.elapsed();
            return;
        }
        match isolate::classify_packet(pkt) {
            Some((port, peer)) if (port as usize) < self.locs.len() => {
                let (slot, iface) = self.locs[port as usize];
                self.slots[slot].shard.ingress(zn, iface, PeerId(peer), Bytes::copy_from_slice(pkt));
                self.slots[slot].t_ingress += t0.elapsed();
                self.rx_pkts[port as usize] += 1;
            }
            _ => self.demux_drops += 1,
        }
    }

    fn poll(&mut self, now: Instant, out: &mut dyn FnMut(&[u8])) {
        let zn = self.zt(now);
        let counters = Arc::clone(&self.counters);
        for _ in 0..64 {
            let mut progress = false;
            for slot in &mut self.slots {
                progress |= slot.poll_once(zn, out, &counters);
            }
            if !progress {
                break;
            }
        }
    }

    fn next_deadline(&mut self, now: Instant) -> Option<Instant> {
        let d = self.slots.iter().filter_map(|s| s.shard.next_deadline()).min()?;
        let ns = d.as_nanos();
        let t = self.start + std::time::Duration::from_nanos(ns.saturating_sub(1));
        Some(if t < now { now } else { t })
    }

    fn stats(&self) -> serde_json::Value {
        let mut live = Agg::default();
        let mut closed = Agg::default();
        let mut shard_rows = Vec::new();
        let mut live_conns = 0usize;
        let mut max_conns = 0usize;
        let mut t_ingress = std::time::Duration::ZERO;
        let mut t_run = std::time::Duration::ZERO;
        let mut t_app = std::time::Duration::ZERO;
        let mut reserved = 0u64;
        let mut high = 0u64;
        for (i, slot) in self.slots.iter().enumerate() {
            for id in slot.shard.conn_ids() {
                if let Some(info) = slot.shard.info(id) {
                    live.add(&info);
                }
            }
            closed.conns += slot.closed_agg.conns;
            closed.bytes_sent += slot.closed_agg.bytes_sent;
            closed.bytes_retrans += slot.closed_agg.bytes_retrans;
            closed.rto += slot.closed_agg.rto;
            closed.tlp += slot.closed_agg.tlp;
            closed.fast_recoveries += slot.closed_agg.fast_recoveries;
            closed.spurious_rtos += slot.closed_agg.spurious_rtos;
            closed.dsacks += slot.closed_agg.dsacks;
            closed.ooo_segs += slot.closed_agg.ooo_segs;
            closed.rack_scanned += slot.closed_agg.rack_scanned;
            closed.small_segs += slot.closed_agg.small_segs;
            closed.max_cwnd = closed.max_cwnd.max(slot.closed_agg.max_cwnd);
            closed.max_rcv_target = closed.max_rcv_target.max(slot.closed_agg.max_rcv_target);
            closed.dropped_no_mem += slot.closed_agg.dropped_no_mem;
            closed.limited_rwnd_ms += slot.closed_agg.limited_rwnd_ms;
            closed.limited_cwnd_ms += slot.closed_agg.limited_cwnd_ms;
            closed.limited_pacing_ms += slot.closed_agg.limited_pacing_ms;
            live_conns += slot.shard.conn_count();
            max_conns += slot.max_conns;
            t_ingress += slot.t_ingress;
            t_run += slot.t_run;
            t_app += slot.t_app;
            let st = slot.shard.stats();
            reserved = slot.shard.budget().global().reserved();
            high = slot.shard.budget().global().high();
            shard_rows.push(serde_json::json!({
                "shard": i,
                "physical_used": slot.shard.budget().physical_used(),
                "senders": slot.shard.budget().senders(),
                "share_blocked": slot.shard.budget().share_blocked(),
                "conns": slot.shard.conn_count(),
                "rx_packets": st.rx_packets, "tx_packets": st.tx_packets, "rst_sent": st.rst_sent,
                "syn_received": st.syn_received, "syn_dropped": st.syn_dropped,
                "syn_cookies_sent": st.syn_cookies_sent, "conns_created": st.conns_created,
                "conns_freed": st.conns_freed, "runs": st.runs, "idle_rounds": st.idle_rounds,
                "round_budget_exhausted": st.round_budget_exhausted,
                "pacing_credit_dropped": st.pacing_credit_dropped, "rx_dropped_parse": st.rx_dropped_parse,
                "rx_dropped_no_iface": st.rx_dropped_no_iface,
                "rx_dropped_peer_mismatch": st.rx_dropped_peer_mismatch,
            }));
        }
        let mut ports = Vec::new();
        if self.plan.is_some() {
            for (port, (slot, iface)) in self.locs.iter().enumerate() {
                let s = &self.slots[*slot];
                let i = iface.0 as usize;
                ports.push(serde_json::json!({
                    "port": port,
                    "shard": slot,
                    "iface": iface.0,
                    "rx_pkts": self.rx_pkts.get(port).copied().unwrap_or(0),
                    "tx_pkts": s.tx_pkts.get(i).copied().unwrap_or(0),
                    "tx_bytes": s.tx_bytes.get(i).copied().unwrap_or(0),
                }));
            }
        }
        serde_json::json!({
            "impl": "zfstack",
            "cc": format!("{:?}", self.opts.cc),
            "pacing": self.opts.pacing,
            "sock_buf": self.opts.sock_buf,
            "pacing_credit_us": self.slots.first().map(|s| s.shard.pacing_credit().as_micros() as u64),
            "max_conns": max_conns,
            "time_ms": {"ingress": t_ingress.as_secs_f64() * 1e3, "run": t_run.as_secs_f64() * 1e3, "app": t_app.as_secs_f64() * 1e3},
            "live_conns": live_conns,
            "closed": closed.json(),
            "live": live.json(),
            "legacy_single_iface": self.legacy.is_some(),
            "isolate": self.plan.as_ref().map(|p| serde_json::json!({
                "ports": p.ports,
                "per_port_shard": p.per_port_shard,
                "scheduler": "one thread co-schedules every shard; this is not a multi-core deployment",
                "budget_note": "isolate ceilings are 512 MiB global / 64 MiB port / 32 MiB peer, shared by the shards in this process",
            })),
            "demux_drops": self.demux_drops,
            "ports": ports,
            "budget": {"reserved": reserved, "high": high},
            "shards": shard_rows,
        })
    }
}
