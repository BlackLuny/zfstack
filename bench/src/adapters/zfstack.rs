//! zfstack adapter (feature `zfstack`).
//!
//! One `Shard` with one iface (MTU `tun::MTU`), every packet attributed to a single
//! WG peer. The app server is driven from shard events only (Accepted / Readable /
//! Writable / Closed), so idle connections cost nothing per poll.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Instant;

use bytes::Bytes;
use zfstack::{
    CcAlgo, CloseReason, ConnId, Event, IfaceConfig, IfaceId, OutPacket, PeerId, ReadResult, SendResult, Shard, StackConfig,
    WriteResult,
};

use crate::app::{AppConn, CloseAction, ServerCounters};
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

pub struct ZfStack {
    opts: ZfOpts,
    shard: Shard,
    iface: IfaceId,
    start: Instant,
    conns: HashMap<ConnId, C>,
    active: HashSet<ConnId>,
    counters: Arc<ServerCounters>,
    buf: Vec<u8>,
    pkt: Vec<u8>,
    closed_agg: Agg,
    max_conns: usize,
    t_ingress: std::time::Duration,
    t_run: std::time::Duration,
    t_app: std::time::Duration,
}

impl ZfStack {
    pub fn new(opts: ZfOpts, counters: Arc<ServerCounters>) -> Self {
        let cfg = StackConfig {
            cc: opts.cc,
            pacing: opts.pacing,
            max_rcv_buf: opts.sock_buf as u32,
            max_snd_inflight: opts.sock_buf as u32,
            timestamps: std::env::var("ZF_TS").map_or(true, |v| v != "0"),
            ..Default::default()
        };
        let mut shard = Shard::new(cfg);
        let iface = shard.add_iface(IfaceConfig { mtu: tun::MTU as u16 });
        ZfStack {
            opts,
            shard,
            iface,
            start: Instant::now(),
            conns: HashMap::new(),
            active: HashSet::new(),
            counters,
            buf: vec![0u8; 256 * 1024],
            pkt: Vec::with_capacity(2048),
            closed_agg: Agg::default(),
            max_conns: 0,
            t_ingress: Default::default(),
            t_run: Default::default(),
            t_app: Default::default(),
        }
    }

    fn zt(&self, now: Instant) -> zfstack::Instant {
        zfstack::Instant::from_nanos(now.saturating_duration_since(self.start).as_nanos() as u64 + 1)
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

    /// Drive the app for one connection. Returns true if anything happened.
    fn pump(&mut self, id: ConnId, zn: zfstack::Instant) -> bool {
        let mut progressed = false;
        // Receive.
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
        // Send.
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
                    WriteResult::WouldBlock => break,
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
                // Arms the Writable event.
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
                WriteResult::WouldBlock => {
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
        // Close.
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

    fn handle_events(&mut self) -> bool {
        let mut any = false;
        while let Some(ev) = self.shard.poll_event() {
            any = true;
            match ev {
                Event::Accepted(id) => {
                    self.conns.insert(
                        id,
                        C { app: AppConn::new(self.counters.clone()), eof: false, pending: Vec::new(), pending_off: 0 },
                    );
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
                        // Let the app see any remaining data first.
                        self.active.insert(id);
                        let _: Option<CloseReason> = None;
                    }
                }
                Event::Connected(_) => {}
            }
        }
        any
    }
}

impl UserStack for ZfStack {
    fn ingress(&mut self, now: Instant, pkt: &[u8]) {
        let zn = self.zt(now);
        let t0 = Instant::now();
        self.shard.ingress(zn, self.iface, PeerId(1), Bytes::copy_from_slice(pkt));
        self.t_ingress += t0.elapsed();
    }

    fn poll(&mut self, now: Instant, out: &mut dyn FnMut(&[u8])) {
        let zn = self.zt(now);
        for _ in 0..64 {
            let pkt = &mut self.pkt;
            let mut sink = |_: IfaceId, p: &OutPacket<'_>| {
                pkt.clear();
                p.write_to(pkt);
                out(pkt);
                SendResult::Accepted
            };
            let t0 = Instant::now();
            let o = self.shard.run(zn, &mut sink);
            let t1 = Instant::now();
            self.t_run += t1 - t0;
            let ev = self.handle_events();
            let mut progressed = false;
            if !self.active.is_empty() {
                let ids: Vec<ConnId> = self.active.drain().collect();
                for id in ids {
                    progressed |= self.pump(id, zn);
                }
            }
            self.t_app += t1.elapsed();
            if !ev && !progressed && !o.more {
                break;
            }
        }
    }

    fn next_deadline(&mut self, now: Instant) -> Option<Instant> {
        let d = self.shard.next_deadline()?;
        let ns = d.as_nanos();
        let t = self.start + std::time::Duration::from_nanos(ns.saturating_sub(1));
        Some(if t < now { now } else { t })
    }

    fn stats(&self) -> serde_json::Value {
        let mut live = Agg::default();
        for id in self.shard.conn_ids() {
            if let Some(i) = self.shard.info(id) {
                live.add(&i);
            }
        }
        let st = self.shard.stats();
        serde_json::json!({
            "impl": "zfstack",
            "cc": format!("{:?}", self.opts.cc),
            "pacing": self.opts.pacing,
            "sock_buf": self.opts.sock_buf,
            "pacing_credit_us": self.shard.pacing_credit().as_micros() as u64,
            "max_conns": self.max_conns,
            "time_ms": {"ingress": self.t_ingress.as_secs_f64() * 1e3, "run": self.t_run.as_secs_f64() * 1e3, "app": self.t_app.as_secs_f64() * 1e3},
            "live_conns": self.shard.conn_count(),
            "closed": self.closed_agg.json(),
            "live": live.json(),
            "shard": {
                "rx_packets": st.rx_packets, "tx_packets": st.tx_packets, "rst_sent": st.rst_sent,
                "syn_received": st.syn_received, "syn_dropped": st.syn_dropped,
                "syn_cookies_sent": st.syn_cookies_sent, "conns_created": st.conns_created,
                "conns_freed": st.conns_freed, "runs": st.runs, "idle_rounds": st.idle_rounds,
                "round_budget_exhausted": st.round_budget_exhausted,
                "pacing_credit_dropped": st.pacing_credit_dropped, "rx_dropped_parse": st.rx_dropped_parse,
            },
        })
    }
}
