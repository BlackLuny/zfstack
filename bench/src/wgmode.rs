//! WG link mode: the test traffic crosses a real network inside WireGuard
//! instead of the userspace link emulator.
//!
//! ```text
//!  client machine                                   server machine (zfbench --serve-wg)
//! ┌──────────────────────────────┐   UDP 51820    ┌───────────────────────────────────────┐
//! │ kernel TCP client            │ ◀────────────▶ │ userspace stacks: one thread = UDP     │
//! │  └ zfbwg 10.201.0.1          │   (the WAN)    │   recvmmsg → boringtun → stack → app   │
//! │    kernel WG or boringtun    │                │   → boringtun → sendmmsg               │
//! │                              │   TCP 5202     │ kernel: zfbwgS 10.201.0.2 (kernel WG   │
//! │ orchestrator ────────────────┼──── control ──▶│   or boringtun bridge) + TcpListener   │
//! └──────────────────────────────┘                └───────────────────────────────────────┘
//! ```
//!
//! The control channel is newline-delimited JSON over plain TCP, outside the
//! tunnel: `start` (stack options + the client's public key; the reply carries
//! the server's public key), `snap` (server CPU and counters; used for the
//! measurement window and the per-second upload samples) and `stop` (the
//! server-side result). Every request carries the shared token.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream, ToSocketAddrs};
use std::os::fd::RawFd;
use std::os::unix::thread::JoinHandleExt;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::app::{self, ServerCounters};
use crate::client::{self, ServerView};
use crate::stack::StackLive;
use crate::wg::{self, KeyPair, Peer};
use crate::{build_stack, kernel_server, stack_name, sysctl, test_name, tun, util, Args, StackKind, StackOpts, TestKind, WgImpl};

const MAX_LINE: u64 = 1 << 20;

fn resolve_impl(w: WgImpl) -> io::Result<&'static str> {
    Ok(match w {
        WgImpl::Kernel if !wg::kernel_wg_supported() => {
            return Err(io::Error::other("kernel WireGuard requested but `ip link add type wireguard` or `wg` is not available"))
        }
        WgImpl::Kernel => "kernel",
        WgImpl::Userspace => "userspace",
        WgImpl::Auto if wg::kernel_wg_supported() => "kernel",
        WgImpl::Auto => "userspace",
    })
}

fn env_json() -> Value {
    json!({
        "kernel": tun::sh_output("uname -r").trim(),
        "nproc": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
        "zfstack_commit": util::zfstack_commit(),
        "smoltcp_rev": "8014f8b21e12faf89b3b453ceea32027344721af",
        "tcp_congestion_control": sysctl("net.ipv4.tcp_congestion_control"),
    })
}

// ---------------------------------------------------------------- control channel

struct Ctl {
    w: TcpStream,
    r: BufReader<TcpStream>,
}

impl Ctl {
    fn new(s: TcpStream) -> io::Result<Self> {
        s.set_nodelay(true)?;
        Ok(Ctl { r: BufReader::new(s.try_clone()?), w: s })
    }

    fn send(&mut self, v: &Value) -> io::Result<()> {
        let mut line = serde_json::to_vec(v).map_err(io::Error::other)?;
        line.push(b'\n');
        self.w.write_all(&line)
    }

    /// Next JSON line; Ok(None) on EOF.
    fn recv(&mut self) -> io::Result<Option<Value>> {
        let mut line = String::new();
        let n = (&mut self.r).take(MAX_LINE).read_line(&mut line)?;
        if n == 0 {
            return Ok(None);
        }
        serde_json::from_str(&line).map(Some).map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }

    fn call(&mut self, v: &Value) -> io::Result<Value> {
        self.send(v)?;
        let r = self.recv()?.ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "control channel closed"))?;
        if r["ok"].as_bool() != Some(true) {
            return Err(io::Error::other(format!("server: {}", r["error"].as_str().unwrap_or("unknown error"))));
        }
        Ok(r)
    }
}

// ---------------------------------------------------------------- server

struct ServeCfg<'a> {
    token: &'a str,
    wg_listen: SocketAddr,
    server_wg: WgImpl,
}

pub fn serve(a: &Args) -> io::Result<()> {
    let token = a.token.as_deref().filter(|t| !t.is_empty()).ok_or_else(|| io::Error::other("--serve-wg needs --token (or ZFBENCH_TOKEN)"))?;
    let cfg = ServeCfg { token, wg_listen: a.wg_listen, server_wg: a.server_wg };
    tun::cleanup();
    let l = TcpListener::bind(a.ctl_listen)?;
    eprintln!("zfbench: WG server: control {}, WireGuard udp {}", a.ctl_listen, a.wg_listen);
    for c in l.incoming() {
        let c = match c {
            Ok(c) => c,
            Err(e) => {
                eprintln!("zfbench: accept: {e}");
                continue;
            }
        };
        let peer = c.peer_addr().ok();
        if !a.allow.is_empty() && !peer.is_some_and(|p| a.allow.contains(&p.ip())) {
            eprintln!("zfbench: control connection from {peer:?} rejected (--allow)");
            continue;
        }
        eprintln!("zfbench: session from {peer:?}");
        if let Err(e) = session(c, &cfg) {
            eprintln!("zfbench: session error: {e}");
        }
        tun::cleanup();
        eprintln!("zfbench: session done");
    }
    Ok(())
}

enum Running {
    Stack(std::thread::JoinHandle<crate::stack::WgStackResult>),
    Kernel { bridge: Option<(std::thread::JoinHandle<wg::BridgeResult>, RawFd)>, ks: kernel_server::KernelServer, stop_srv: Arc<AtomicBool> },
}

fn session(c: TcpStream, cfg: &ServeCfg) -> io::Result<()> {
    // A connection that never sends `start` must not hold the (single) session slot.
    c.set_read_timeout(Some(Duration::from_secs(10)))?;
    let mut ctl = Ctl::new(c)?;
    let Some(req) = ctl.recv()? else { return Ok(()) };
    ctl.w.set_read_timeout(Some(Duration::from_secs(900)))?;
    let fail = |ctl: &mut Ctl, e: String| -> io::Result<()> {
        let _ = ctl.send(&json!({ "ok": false, "error": e }));
        Err(io::Error::other(e))
    };
    if req["token"].as_str() != Some(cfg.token) {
        return fail(&mut ctl, "bad token".into());
    }
    if req["cmd"] != "start" {
        return fail(&mut ctl, "expected start".into());
    }
    let opts = match StackOpts::from_json(&req["opts"]) {
        Ok(o) => o,
        Err(e) => return fail(&mut ctl, format!("bad opts: {e}")),
    };
    let client_pub = match wg::b64_decode_key(req["client_pubkey"].as_str().unwrap_or("")) {
        Ok(k) => k,
        Err(e) => return fail(&mut ctl, e.to_string()),
    };
    let me = wg::gen_keypair()?;
    let counters = ServerCounters::new();
    let stop = Arc::new(AtomicBool::new(false));
    let live = Arc::new(StackLive::default());
    let mut wg_mode = "stack-thread";
    let started = (|| -> io::Result<Running> {
        if opts.stack == StackKind::Kernel {
            wg_mode = resolve_impl(cfg.server_wg)?;
            let bridge = if wg_mode == "kernel" {
                wg::kernel_iface(wg::WG_SERVER_IF, tun::SERVER_ADDR, &me, &client_pub, tun::CLIENT_ADDR, None, Some(cfg.wg_listen.port()))?;
                None
            } else {
                let fd = wg::tun_iface(wg::WG_SERVER_IF, tun::SERVER_ADDR)?;
                let udp = wg::udp_socket(cfg.wg_listen)?;
                let peer = Peer::new(&me, client_pub, None, 1);
                let st = stop.clone();
                Some((std::thread::Builder::new().name("zfb-wg-bridge".into()).spawn(move || wg::run_bridge(fd, udp, peer, st))?, fd))
            };
            let stop_srv = Arc::new(AtomicBool::new(false));
            let ks = kernel_server::start(counters.clone(), stop_srv.clone(), opts.kernel_cc.clone(), None)?;
            Ok(Running::Kernel { bridge, ks, stop_srv })
        } else {
            let st = build_stack(&opts, counters.clone());
            let udp = wg::udp_socket(cfg.wg_listen)?;
            let peer = Peer::new(&me, client_pub, None, 1);
            let (s, lv) = (stop.clone(), live.clone());
            Ok(Running::Stack(std::thread::Builder::new().name("zfb-stack".into()).spawn(move || crate::stack::run_stack_thread_wg(st, udp, peer, s, lv))?))
        }
    })();
    let running = match started {
        Ok(r) => r,
        Err(e) => return fail(&mut ctl, format!("setup: {e}")),
    };
    let pt = match &running {
        Running::Stack(h) => Some(h.as_pthread_t()),
        Running::Kernel { bridge: Some((h, _)), .. } => Some(h.as_pthread_t()),
        Running::Kernel { .. } => None,
    };
    ctl.send(&json!({ "ok": true, "server_pubkey": wg::b64_encode(&me.public), "wg_port": cfg.wg_listen.port(), "server_wg": wg_mode }))?;
    let t0 = Instant::now();
    let snap = |flows: usize| -> Value {
        json!({
            "ok": true,
            "t": t0.elapsed().as_secs_f64(),
            "thread_cpu": pt.and_then(util::pthread_cpu),
            "proc_cpu": util::process_cpu_now(),
            "procstat": util::proc_stat().v,
            "wakeups": live.wakeups.load(Relaxed),
            "up_flow_bytes": counters.up_flow_bytes[..flows.min(app::MAX_FLOWS)].iter().map(|c| c.load(Relaxed)).collect::<Vec<_>>(),
        })
    };
    // Serve snapshots until stop or EOF.
    let mut graceful = false;
    while let Ok(Some(req)) = ctl.recv() {
        if req["token"].as_str() != Some(cfg.token) {
            let _ = ctl.send(&json!({ "ok": false, "error": "bad token" }));
            break;
        }
        match req["cmd"].as_str() {
            Some("snap") => ctl.send(&snap(req["flows"].as_u64().unwrap_or(0) as usize))?,
            Some("stop") => {
                graceful = true;
                break;
            }
            _ => ctl.send(&json!({ "ok": false, "error": "unknown command" }))?,
        }
    }
    // Tear down and report.
    stop.store(true, Relaxed);
    let late_mean_us = {
        let n = live.timer_wakeups.load(Relaxed);
        if n > 0 {
            live.deadline_late_sum_ns.load(Relaxed) as f64 / n as f64 / 1e3
        } else {
            0.0
        }
    };
    let (thread_json, wg_json) = match running {
        Running::Stack(h) => {
            let r = h.join().map_err(|_| io::Error::other("stack thread panicked"))?;
            (
                json!({
                    "cpu_sec": r.stack.cpu_sec,
                    "wakeups_total": live.wakeups.load(Relaxed),
                    "timer_wakeups": live.timer_wakeups.load(Relaxed),
                    "deadline_late_mean_us": late_mean_us,
                    "deadline_late_max_us": live.deadline_late_max_ns.load(Relaxed) as f64 / 1e3,
                    "polls": live.polls.load(Relaxed),
                    "pkts_in": live.pkts_in.load(Relaxed),
                    "pkts_out": live.pkts_out.load(Relaxed),
                    "stack_stats": r.stack.stats,
                }),
                json!({ "mode": wg_mode, "peer": r.peer.to_json(), "udp": r.udp.to_json() }),
            )
        }
        Running::Kernel { bridge, ks, stop_srv } => {
            stop_srv.store(true, Relaxed);
            let t = Instant::now();
            while ks.active.load(Relaxed) > 0 && t.elapsed() < Duration::from_secs(3) {
                std::thread::sleep(Duration::from_millis(10));
            }
            let b = bridge.map(|(h, fd)| {
                let r = h.join().ok();
                unsafe { libc::close(fd) };
                r.map(|r| r.to_json())
            });
            (
                json!({ "kernel_server_threads_total_sec": counters.thread_cpu_us.load(Relaxed) as f64 / 1e6, "kernel_server_threads_active_at_exit": ks.active.load(Relaxed) }),
                json!({ "mode": wg_mode, "bridge": b.flatten() }),
            )
        }
    };
    wg::cleanup();
    if graceful {
        ctl.send(&json!({
            "ok": true,
            "server_app": counters.to_json(),
            "pattern_errors": counters.pattern_errors.load(Relaxed),
            "bad_headers": counters.bad_headers.load(Relaxed),
            "thread": thread_json,
            "wg": wg_json,
            "env": env_json(),
        }))?;
    }
    Ok(())
}

// ---------------------------------------------------------------- client

/// Remote server counters, fetched over the control channel.
struct RemoteView {
    ctl: Arc<Mutex<Ctl>>,
    token: String,
    errors: Mutex<Vec<String>>,
}

impl RemoteView {
    fn snap(&self, flows: usize) -> Option<Value> {
        let r = self.ctl.lock().unwrap().call(&json!({ "cmd": "snap", "token": self.token, "flows": flows }));
        match r {
            Ok(v) => Some(v),
            Err(e) => {
                self.errors.lock().unwrap().push(format!("control snap: {e}"));
                None
            }
        }
    }
}

impl ServerView for RemoteView {
    fn up_flow_bytes(&self, n: usize) -> Vec<u64> {
        let got: Vec<u64> =
            self.snap(n).and_then(|v| Some(v["up_flow_bytes"].as_array()?.iter().map(|x| x.as_u64().unwrap_or(0)).collect())).unwrap_or_default();
        got.into_iter().chain(std::iter::repeat(0)).take(n).collect()
    }
}

struct ClientSide {
    mode: &'static str,
    bridge: Option<(std::thread::JoinHandle<wg::BridgeResult>, RawFd)>,
    stop: Arc<AtomicBool>,
}

impl ClientSide {
    fn setup(w: WgImpl, me: &KeyPair, server_pub: &[u8; 32], endpoint: SocketAddr) -> io::Result<ClientSide> {
        let mode = resolve_impl(w)?;
        let stop = Arc::new(AtomicBool::new(false));
        let bridge = if mode == "kernel" {
            wg::kernel_iface(wg::WG_CLIENT_IF, tun::CLIENT_ADDR, me, server_pub, tun::SERVER_ADDR, Some(endpoint), None)?;
            None
        } else {
            let fd = wg::tun_iface(wg::WG_CLIENT_IF, tun::CLIENT_ADDR)?;
            let bind: SocketAddr = if endpoint.is_ipv4() { "0.0.0.0:0".parse().unwrap() } else { "[::]:0".parse().unwrap() };
            let udp = wg::udp_socket(bind)?;
            let peer = Peer::new(me, *server_pub, Some(endpoint), 2);
            let st = stop.clone();
            Some((std::thread::Builder::new().name("zfb-wg-client".into()).spawn(move || wg::run_bridge(fd, udp, peer, st))?, fd))
        };
        Ok(ClientSide { mode, bridge, stop })
    }

    fn teardown(self) -> Option<Value> {
        self.stop.store(true, Relaxed);
        let r = self.bridge.map(|(h, fd)| {
            let r = h.join().ok().map(|r| r.to_json());
            unsafe { libc::close(fd) };
            r
        });
        wg::cleanup();
        r.flatten()
    }
}

/// Base RTT through the tunnel: TCP connect time of `CMD_CONNECT` probes (the
/// first one also carries the WireGuard handshake and is reported apart).
fn probe_rtt(n: usize) -> (Vec<f64>, Vec<String>) {
    let mut out = Vec::new();
    let mut errs = Vec::new();
    for _ in 0..n {
        let t = Instant::now();
        match TcpStream::connect_timeout(&client::server_addr(), Duration::from_secs(5)) {
            Ok(mut s) => {
                out.push(t.elapsed().as_secs_f64() * 1e3);
                let _ = s.write_all(&app::encode_header(app::CMD_CONNECT, 0));
                let _ = s.set_read_timeout(Some(Duration::from_secs(5)));
                let mut b = [0u8; 64];
                while matches!(s.read(&mut b), Ok(n) if n > 0) {}
            }
            Err(e) => {
                errs.push(format!("rtt probe: {e}"));
                break;
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    (out, errs)
}

/// Client-side and server-side snapshots taken at a test mark.
struct Mark {
    name: &'static str,
    t: Instant,
    local: Value,
    server: Value,
}

impl Mark {
    fn side(&self, server: bool) -> &Value {
        if server {
            &self.server
        } else {
            &self.local
        }
    }

    fn procstat(&self, server: bool) -> Option<util::ProcStat> {
        let mut p = util::ProcStat::default();
        for (i, x) in self.side(server)["procstat"].as_array()?.iter().take(8).enumerate() {
            p.v[i] = x.as_u64()?;
        }
        Some(p)
    }
}

pub fn run_client(a: &Args) -> io::Result<i32> {
    let token = a.token.clone().filter(|t| !t.is_empty()).ok_or_else(|| io::Error::other("--wg-server needs --token (or ZFBENCH_TOKEN)"))?;
    let host = a.wg_server.as_deref().unwrap();
    let ctl_addr = (host, a.ctl_port).to_socket_addrs()?.next().ok_or_else(|| io::Error::other(format!("cannot resolve {host}")))?;
    tun::cleanup();
    let s = TcpStream::connect_timeout(&ctl_addr, Duration::from_secs(10))?;
    s.set_read_timeout(Some(Duration::from_secs(120)))?;
    let mut ctl = Ctl::new(s)?;
    let me = wg::gen_keypair()?;
    let opts = StackOpts::from_args(a);
    let start = ctl.call(&json!({ "cmd": "start", "token": token, "opts": opts.to_json(), "client_pubkey": wg::b64_encode(&me.public) }))?;
    let server_pub = wg::b64_decode_key(start["server_pubkey"].as_str().unwrap_or(""))?;
    let endpoint = SocketAddr::new(ctl_addr.ip(), start["wg_port"].as_u64().unwrap_or(51820) as u16);
    let side = ClientSide::setup(a.client_wg, &me, &server_pub, endpoint)?;
    let ctl = Arc::new(Mutex::new(ctl));
    let view = RemoteView { ctl: ctl.clone(), token: token.clone(), errors: Mutex::new(Vec::new()) };

    let (rtts, mut errors) = probe_rtt(6);
    let rtt_ms = if rtts.len() > 1 { rtts[1..].iter().cloned().fold(f64::INFINITY, f64::min) } else { rtts.first().copied().unwrap_or(0.0) };

    let o = client::TestOpts {
        secs: a.secs,
        warmup: a.warmup,
        flows: a.flows,
        rr_size: a.rr_size,
        rr_interval_ms: a.rr_interval_ms,
        idle_rr_secs: a.idle_rr_secs,
        conns: a.conns,
        concurrency: a.concurrency,
        connect_timeout: Duration::from_millis(a.connect_timeout_ms),
        client_cc: a.client_cc.clone(),
    };
    let bridge_pt = side.bridge.as_ref().map(|(h, _)| h.as_pthread_t());
    let local = || json!({ "proc_cpu": util::process_cpu_now(), "procstat": util::proc_stat().v, "bridge_cpu": bridge_pt.and_then(util::pthread_cpu) });
    let mut marks: Vec<Mark> = Vec::new();
    let t_test = Instant::now();
    let outcome = if errors.is_empty() {
        let mut mark = |name: &'static str| marks.push(Mark { name, t: Instant::now(), local: local(), server: view.snap(0).unwrap_or(Value::Null) });
        match a.test {
            TestKind::Down => client::test_down(&o, &view, &mut mark),
            TestKind::Up => client::test_up(&o, &view, &mut mark),
            TestKind::Mixed => client::test_mixed(&o, &view, &mut mark),
            TestKind::Connect => client::test_connect(&o, &view, &mut mark),
        }
    } else {
        client::TestOutcome { results: json!({ "error": "tunnel not usable" }), errors: Vec::new(), window_bytes: 0, window_secs: 0.0 }
    };
    let test_secs = t_test.elapsed().as_secs_f64();
    std::thread::sleep(Duration::from_millis(300));
    let fin = ctl.lock().unwrap().call(&json!({ "cmd": "stop", "token": token }));
    let client_wg = side.mode;
    let bridge = side.teardown();

    errors.extend(outcome.errors.iter().cloned());
    errors.extend(view.errors.lock().unwrap().drain(..));
    let fin = match fin {
        Ok(v) => v,
        Err(e) => {
            errors.push(format!("control stop: {e}"));
            Value::Null
        }
    };
    for (k, what) in [("pattern_errors", "upload pattern mismatches"), ("bad_headers", "bad headers")] {
        let n = fin[k].as_u64().unwrap_or(0);
        if n > 0 {
            errors.push(format!("server: {n} {what}"));
        }
    }

    // CPU over the measurement window, from the server snapshots.
    let find = |n: &str| marks.iter().find(|m| m.name == n);
    let (w0, w1) = (find("window_start"), find("window_end"));
    let window_wall = match (w0, w1) {
        (Some(x), Some(y)) => (y.t - x.t).as_secs_f64(),
        _ => 0.0,
    };
    let rd = |k: &str, server: bool| -> Option<f64> { Some(w1?.side(server)[k].as_f64()? - w0?.side(server)[k].as_f64()?) };
    let sys = |server: bool| -> Value {
        match (w0.and_then(|m| m.procstat(server)), w1.and_then(|m| m.procstat(server))) {
            (Some(x), Some(y)) => util::proc_stat_delta_json(&x, &y),
            _ => Value::Null,
        }
    };
    let gb = outcome.window_bytes as f64 / 1e9;
    let per_gb = |c: Option<f64>| c.filter(|_| gb > 0.0).map(|c| c / gb);
    let thread_window = rd("thread_cpu", true);
    let server_sys = sys(true);
    let is_kernel = a.stack == StackKind::Kernel;

    let result = json!({
        "tool": "zfbench",
        "version": env!("CARGO_PKG_VERSION"),
        "label": a.label,
        "timestamp_unix": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs(),
        "config": {
            "link_mode": "wg",
            "stack": stack_name(a.stack),
            "test": test_name(a.test),
            "wg_server": host,
            "client_wg": client_wg,
            "server_wg": start["server_wg"],
            // Emulator fields kept for the report: the RTT is the measured
            // tunnel RTT, there is no emulated loss or queue.
            "rtt_ms": (rtt_ms * 10.0).round() / 10.0,
            "rate_mbps": 0.0,
            "loss": 0.0,
            "loss_up": 0.0,
            "queue_bdp": 0.0,
            "flows": a.flows,
            "secs": a.secs,
            "warmup": a.warmup,
            "conns": a.conns,
            "concurrency": a.concurrency,
            "rr_size": a.rr_size,
            "sock_buf_kb": a.sock_buf_kb,
            "listen_pool": a.listen_pool,
            "smol_pacing_backlog_us": a.smol_pacing_backlog_us,
            "smol_timestamps": a.smol_timestamps,
            "kernel_cc": a.kernel_cc,
            "client_cc": a.client_cc.clone().unwrap_or_else(|| sysctl("net.ipv4.tcp_congestion_control")),
            "mtu": tun::MTU,
        },
        "env": { "client": env_json(), "server": fin["env"] },
        "ok": errors.is_empty(),
        "errors": errors,
        "results": outcome.results,
        "link": {
            "mode": "wg",
            "rtt_probe_ms": rtts,
            "client_bridge": bridge,
            "server_wg": fin["wg"],
        },
        "server_app": fin["server_app"],
        "cpu": {
            "window_wall_sec": window_wall,
            "window_nominal_sec": outcome.window_secs,
            "window_effective_bytes": outcome.window_bytes,
            // Userspace stacks: the server thread that does UDP + WireGuard + TCP + app.
            // Kernel: the server's boringtun bridge thread (userspace WG) or null (kernel WG);
            // compare kernel runs through `server_system_busy_sec_per_effective_GB`.
            "stack_thread_window_sec": thread_window.filter(|_| !is_kernel),
            "cpu_sec_per_effective_GB": per_gb(thread_window).filter(|_| !is_kernel),
            "stack_thread_util": thread_window.filter(|_| !is_kernel && window_wall > 0.0).map(|c| c / window_wall),
            "server_wg_bridge_window_sec": thread_window.filter(|_| is_kernel),
            "server_process_window_sec": rd("proc_cpu", true),
            "server_system_window": server_sys,
            "server_system_busy_sec_per_effective_GB": per_gb(server_sys["busy_sec"].as_f64()),
            "client_bridge_window_sec": rd("bridge_cpu", false),
            "client_process_window_sec": rd("proc_cpu", false),
            "client_system_window": sys(false),
            "test_wall_sec": test_secs,
        },
        "stack_thread": fin["thread"],
    });
    let text = serde_json::to_string_pretty(&result).unwrap();
    println!("{text}");
    if let Some(p) = &a.json {
        std::fs::write(p, &text)?;
    }
    let errs = result["errors"].as_array().cloned().unwrap_or_default();
    if !errs.is_empty() {
        for e in &errs {
            eprintln!("zfbench: correctness failure: {}", e.as_str().unwrap_or(""));
        }
        return Ok(1);
    }
    Ok(0)
}
