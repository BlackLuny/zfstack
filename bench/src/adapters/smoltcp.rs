//! smoltcp (BlackLuny fork) adapter, modelled after how a proxy uses it:
//! Medium::Ip device fed from a channel, a pool of pre-armed listeners on the
//! service port, app logic run in the same thread after every `iface.poll`.

use std::collections::VecDeque;
use std::sync::Arc;
use std::time::Instant;

use smoltcp::iface::{Config, Interface, SocketHandle, SocketSet};
use smoltcp::phy::{self, Device, DeviceCapabilities, Medium};
use smoltcp::socket::tcp::{self, CongestionControl, State};
use smoltcp::time::Instant as SInstant;
use smoltcp::wire::{HardwareAddress, IpAddress, IpCidr, Ipv4Address};

use crate::app::{AppConn, CloseAction, ServerCounters};
use crate::stack::UserStack;
use crate::tun;

#[derive(Clone, Debug)]
pub struct SmolOpts {
    pub cc: CongestionControl,
    pub rx_buf: usize,
    pub tx_buf: usize,
    pub listen_pool: usize,
    pub pacing_backlog_us: Option<i64>,
    pub timestamps: bool,
}

struct ChanDevice {
    rx: VecDeque<Vec<u8>>,
    tx: Vec<Vec<u8>>,
    mtu: usize,
}

struct RxTok(Vec<u8>);
struct TxTok<'a>(&'a mut Vec<Vec<u8>>);

impl phy::RxToken for RxTok {
    fn consume<R, F: FnOnce(&[u8]) -> R>(self, f: F) -> R {
        f(&self.0)
    }
}

impl phy::TxToken for TxTok<'_> {
    fn consume<R, F: FnOnce(&mut [u8]) -> R>(self, len: usize, f: F) -> R {
        let mut v = vec![0u8; len];
        let r = f(&mut v);
        self.0.push(v);
        r
    }
}

impl Device for ChanDevice {
    type RxToken<'a> = RxTok;
    type TxToken<'a> = TxTok<'a>;

    fn receive(&mut self, _t: SInstant) -> Option<(RxTok, TxTok<'_>)> {
        let p = self.rx.pop_front()?;
        Some((RxTok(p), TxTok(&mut self.tx)))
    }
    fn transmit(&mut self, _t: SInstant) -> Option<TxTok<'_>> {
        Some(TxTok(&mut self.tx))
    }
    fn capabilities(&self) -> DeviceCapabilities {
        let mut c = DeviceCapabilities::default();
        c.medium = Medium::Ip;
        c.max_transmission_unit = self.mtu;
        c
    }
}

struct Conn {
    h: SocketHandle,
    app: Option<AppConn>,
    eof_seen: bool,
    close_called: bool,
    aborted: bool,
}

#[derive(Default)]
struct Agg {
    rto: u64,
    fast_retransmit: u64,
    sack_retransmit: u64,
    partial_ack_retransmit: u64,
    lost_retransmit_rescan: u64,
    sack_reneging_fallback: u64,
    sack_triggered_recovery: u64,
}

impl Agg {
    fn add(&mut self, s: tcp::LossStats) {
        self.rto += s.rto as u64;
        self.fast_retransmit += s.fast_retransmit as u64;
        self.sack_retransmit += s.sack_retransmit as u64;
        self.partial_ack_retransmit += s.partial_ack_retransmit as u64;
        self.lost_retransmit_rescan += s.lost_retransmit_rescan as u64;
        self.sack_reneging_fallback += s.sack_reneging_fallback as u64;
        self.sack_triggered_recovery += s.sack_triggered_recovery as u64;
    }
    fn json(&self) -> serde_json::Value {
        serde_json::json!({
            "rto": self.rto, "fast_retransmit": self.fast_retransmit,
            "sack_retransmit": self.sack_retransmit,
            "partial_ack_retransmit": self.partial_ack_retransmit,
            "lost_retransmit_rescan": self.lost_retransmit_rescan,
            "sack_reneging_fallback": self.sack_reneging_fallback,
            "sack_triggered_recovery": self.sack_triggered_recovery,
        })
    }
}

pub struct SmolStack {
    opts: SmolOpts,
    start: Instant,
    iface: Interface,
    dev: ChanDevice,
    sockets: SocketSet<'static>,
    conns: Vec<Conn>,
    counters: Arc<ServerCounters>,
    // stats
    listeners_created: u64,
    conns_removed: u64,
    max_conns: usize,
    loss_closed: Agg,
    max_cwnd: usize,
    iface_polls: u64,
}

fn tsval() -> u32 {
    static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_millis() as u32
}

impl SmolStack {
    pub fn new(opts: SmolOpts, counters: Arc<ServerCounters>) -> Self {
        let start = Instant::now();
        let mut dev = ChanDevice { rx: VecDeque::new(), tx: Vec::new(), mtu: tun::MTU };
        let mut iface =
            Interface::new(Config::new(HardwareAddress::Ip), &mut dev, SInstant::from_micros(0));
        let srv: Ipv4Address = tun::SERVER_ADDR.parse().unwrap();
        let cli: Ipv4Address = tun::CLIENT_ADDR.parse().unwrap();
        iface.update_ip_addrs(|a| {
            a.push(IpCidr::new(IpAddress::Ipv4(srv), 24)).unwrap();
        });
        iface.set_any_ip(true);
        iface.routes_mut().add_default_ipv4_route(cli).unwrap();
        let mut s = SmolStack {
            opts,
            start,
            iface,
            dev,
            sockets: SocketSet::new(Vec::new()),
            conns: Vec::new(),
            counters,
            listeners_created: 0,
            conns_removed: 0,
            max_conns: 0,
            loss_closed: Agg::default(),
            max_cwnd: 0,
            iface_polls: 0,
        };
        for _ in 0..s.opts.listen_pool {
            s.add_listener();
        }
        s
    }

    fn ts(&self, now: Instant) -> SInstant {
        SInstant::from_micros(now.saturating_duration_since(self.start).as_micros() as i64)
    }

    fn add_listener(&mut self) {
        let mut s = tcp::Socket::new(
            tcp::SocketBuffer::new(vec![0u8; self.opts.rx_buf]),
            tcp::SocketBuffer::new(vec![0u8; self.opts.tx_buf]),
        );
        s.set_congestion_control(self.opts.cc);
        s.set_nagle_enabled(false);
        if let Some(us) = self.opts.pacing_backlog_us {
            s.set_pacing_max_backlog_us(us);
        }
        if self.opts.timestamps {
            s.set_tsval_generator(Some(tsval));
        }
        s.listen(tun::SERVER_PORT).expect("listen");
        let h = self.sockets.add(s);
        self.conns.push(Conn { h, app: None, eof_seen: false, close_called: false, aborted: false });
        self.listeners_created += 1;
    }

    /// Run the app on every connection. Returns true if anything was queued
    /// that needs another egress pass.
    fn app_pass(&mut self) -> bool {
        let mut progressed = false;
        let mut listening = 0;
        let mut i = 0;
        while i < self.conns.len() {
            let c = &mut self.conns[i];
            let s = self.sockets.get_mut::<tcp::Socket>(c.h);
            let st = s.state();
            match st {
                State::Listen => {
                    listening += 1;
                    i += 1;
                    continue;
                }
                State::SynReceived => {
                    i += 1;
                    continue;
                }
                _ => {}
            }
            let app = c.app.get_or_insert_with(|| AppConn::new(self.counters.clone()));
            // receive
            while s.can_recv() {
                let n = s
                    .recv(|b| {
                        app.on_recv(b);
                        (b.len(), b.len())
                    })
                    .unwrap_or(0);
                if n == 0 {
                    break;
                }
            }
            if !c.eof_seen
                && matches!(st, State::CloseWait | State::LastAck | State::Closing | State::TimeWait)
                && !s.can_recv()
            {
                c.eof_seen = true;
                app.on_peer_eof();
            }
            // send
            while app.wants_send() && s.can_send() {
                let n = s.send(|b| {
                    let n = app.produce(b);
                    (n, n)
                });
                match n {
                    Ok(n) if n > 0 => progressed = true,
                    _ => break,
                }
            }
            self.max_cwnd = self.max_cwnd.max(s.congestion_window());
            match app.close_action() {
                CloseAction::Close if !c.close_called && !app.wants_send() => {
                    s.close();
                    c.close_called = true;
                    progressed = true;
                }
                CloseAction::Abort if !c.aborted => {
                    if s.state() != State::Closed {
                        s.abort();
                        progressed = true;
                    }
                    c.aborted = true;
                    i += 1;
                    continue; // removed on the next pass, after the RST went out
                }
                _ => {}
            }
            let st = s.state();
            // Closed (RST / fully done) or TimeWait: drop the socket like a proxy would.
            if st == State::Closed || st == State::TimeWait {
                let c = self.conns.swap_remove(i);
                self.loss_closed.add(self.sockets.get::<tcp::Socket>(c.h).loss_stats());
                self.sockets.remove(c.h);
                self.conns_removed += 1;
                continue;
            }
            i += 1;
        }
        for _ in listening..self.opts.listen_pool {
            self.add_listener();
        }
        self.max_conns = self.max_conns.max(self.conns.len());
        progressed
    }
}

impl UserStack for SmolStack {
    fn ingress(&mut self, _now: Instant, pkt: &[u8]) {
        self.dev.rx.push_back(pkt.to_vec());
    }

    fn poll(&mut self, now: Instant, out: &mut dyn FnMut(&[u8])) {
        let ts = self.ts(now);
        for _ in 0..4 {
            self.iface.poll(ts, &mut self.dev, &mut self.sockets);
            self.iface_polls += 1;
            let progressed = self.app_pass();
            for p in self.dev.tx.drain(..) {
                out(&p);
            }
            if !progressed {
                break;
            }
        }
        for p in self.dev.tx.drain(..) {
            out(&p);
        }
    }

    fn next_deadline(&mut self, now: Instant) -> Option<Instant> {
        let ts = self.ts(now);
        self.iface
            .poll_delay(ts, &self.sockets)
            .map(|d| now + std::time::Duration::from_micros(d.total_micros()))
    }

    fn stats(&self) -> serde_json::Value {
        let mut live = Agg::default();
        for c in &self.conns {
            live.add(self.sockets.get::<tcp::Socket>(c.h).loss_stats());
        }
        let mut all = Agg::default();
        {
            let (a, b) = (&mut all, &self.loss_closed);
            a.rto = b.rto + live.rto;
            a.fast_retransmit = b.fast_retransmit + live.fast_retransmit;
            a.sack_retransmit = b.sack_retransmit + live.sack_retransmit;
            a.partial_ack_retransmit = b.partial_ack_retransmit + live.partial_ack_retransmit;
            a.lost_retransmit_rescan = b.lost_retransmit_rescan + live.lost_retransmit_rescan;
            a.sack_reneging_fallback = b.sack_reneging_fallback + live.sack_reneging_fallback;
            a.sack_triggered_recovery = b.sack_triggered_recovery + live.sack_triggered_recovery;
        }
        serde_json::json!({
            "impl": "smoltcp",
            "cc": format!("{:?}", self.opts.cc),
            "rx_buf": self.opts.rx_buf,
            "tx_buf": self.opts.tx_buf,
            "listen_pool": self.opts.listen_pool,
            "pacing_backlog_us": self.opts.pacing_backlog_us,
            "timestamps": self.opts.timestamps,
            "listeners_created": self.listeners_created,
            "conns_removed": self.conns_removed,
            "max_sockets": self.max_conns,
            "max_cwnd": self.max_cwnd,
            "iface_polls": self.iface_polls,
            "loss_stats": all.json(),
        })
    }
}
