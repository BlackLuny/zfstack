//! Kernel-socket client (root netns) and the test drivers.

use std::io::{self, Read, Write};
use std::net::{Shutdown, SocketAddr, TcpStream};
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering::Relaxed};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::json;

use crate::app::{self, ServerCounters};
use crate::{tun, util};

pub struct TestOpts {
    pub secs: u64,
    pub warmup: u64,
    pub flows: usize,
    pub rr_size: usize,
    pub rr_interval_ms: u64,
    pub idle_rr_secs: u64,
    pub conns: usize,
    pub concurrency: usize,
    pub connect_timeout: Duration,
    /// TCP_CONGESTION for client sockets (None = system default).
    pub client_cc: Option<String>,
}

pub struct TestOutcome {
    pub results: serde_json::Value,
    /// Correctness failures (pattern mismatch, byte-count mismatch, flow errors).
    pub errors: Vec<String>,
    /// Application bytes delivered to the receiver inside the measurement window.
    pub window_bytes: u64,
    pub window_secs: f64,
}

pub type Mark<'a> = &'a mut dyn FnMut(&'static str);

/// Read access to the server-side application counters. The emulator mode
/// shares [`ServerCounters`] in-process; the WG mode fetches them from the
/// remote server over the control channel.
pub trait ServerView {
    /// Bytes received by the server app on upload flows `0..n`.
    fn up_flow_bytes(&self, n: usize) -> Vec<u64>;
}

impl ServerView for Arc<ServerCounters> {
    fn up_flow_bytes(&self, n: usize) -> Vec<u64> {
        self.up_flow_bytes[..n].iter().map(|c| c.load(Relaxed)).collect()
    }
}

static SERVER_OVERRIDE: std::sync::OnceLock<SocketAddr> = std::sync::OnceLock::new();

/// Client mode connects to a proxied destination instead of the emulator's server.
pub fn set_server_addr(a: SocketAddr) {
    let _ = SERVER_OVERRIDE.set(a);
}

pub fn server_addr() -> SocketAddr {
    SERVER_OVERRIDE.get().copied().unwrap_or_else(|| SocketAddr::new(tun::SERVER_ADDR.parse().unwrap(), tun::SERVER_PORT))
}

fn connect(o: &TestOpts, timeout: Duration) -> io::Result<TcpStream> {
    let s = TcpStream::connect_timeout(&server_addr(), timeout)?;
    s.set_nodelay(true)?;
    if let Some(cc) = &o.client_cc {
        util::set_tcp_cc(s.as_raw_fd(), cc)?;
    }
    Ok(s)
}

fn sleep_until(t: Instant) {
    let now = Instant::now();
    if t > now {
        std::thread::sleep(t - now);
    }
}

fn is_timeout(e: &io::Error) -> bool {
    matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut)
}

fn tcp_info(s: &TcpStream) -> Option<libc::tcp_info> {
    let mut ti: libc::tcp_info = unsafe { std::mem::zeroed() };
    let mut len = std::mem::size_of::<libc::tcp_info>() as libc::socklen_t;
    let rc = unsafe { libc::getsockopt(s.as_raw_fd(), libc::IPPROTO_TCP, libc::TCP_INFO, &mut ti as *mut _ as *mut _, &mut len) };
    (rc == 0).then_some(ti)
}

#[derive(Default)]
struct FlowReport {
    bytes: u64,
    errors: Vec<String>,
    info: serde_json::Value,
}

/// Download flow: read and verify until `stop`.
fn down_flow(s: TcpStream, ctr: Arc<AtomicU64>, stop: Arc<AtomicBool>) -> FlowReport {
    let mut s = s;
    let mut r = FlowReport::default();
    let mut buf = vec![0u8; 256 * 1024];
    let mut pos = 0u64;
    let mut mismatches = 0u64;
    s.set_read_timeout(Some(Duration::from_millis(50))).ok();
    while !stop.load(Relaxed) {
        match s.read(&mut buf) {
            Ok(0) => {
                r.errors.push("download: unexpected EOF".into());
                break;
            }
            Ok(n) => {
                if !app::verify_pattern(pos, &buf[..n]) {
                    mismatches += 1;
                }
                pos += n as u64;
                ctr.fetch_add(n as u64, Relaxed);
            }
            Err(e) if is_timeout(&e) => {}
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => {
                r.errors.push(format!("download: read error: {e}"));
                break;
            }
        }
    }
    if mismatches > 0 {
        r.errors.push(format!("download: {mismatches} pattern mismatches"));
    }
    r.bytes = pos;
    r.info = json!({ "pattern_mismatch_reads": mismatches });
    r
}

/// Upload flow: write the pattern until `stop`, then half-close and check the count.
fn up_flow(s: TcpStream, ctr: Arc<AtomicU64>, stop: Arc<AtomicBool>) -> FlowReport {
    let mut s = s;
    let mut r = FlowReport::default();
    let mut pos = 0u64;
    s.set_write_timeout(Some(Duration::from_millis(50))).ok();
    while !stop.load(Relaxed) {
        match s.write(app::pattern_at(pos, app::PAT_CHUNK)) {
            Ok(n) => {
                pos += n as u64;
                ctr.fetch_add(n as u64, Relaxed);
            }
            Err(e) if is_timeout(&e) => {}
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => {
                r.errors.push(format!("upload: write error: {e}"));
                break;
            }
        }
    }
    let ti = tcp_info(&s);
    let t_end = Instant::now();
    let _ = s.shutdown(Shutdown::Write);
    s.set_read_timeout(Some(Duration::from_secs(120))).ok();
    let mut cnt = [0u8; 8];
    let reply = match s.read_exact(&mut cnt) {
        Ok(()) => {
            let got = u64::from_le_bytes(cnt);
            if got != pos {
                r.errors.push(format!("upload: server counted {got}, client sent {pos}"));
            }
            Some(got)
        }
        Err(e) => {
            if r.errors.is_empty() {
                r.errors.push(format!("upload: no count reply: {e}"));
            }
            None
        }
    };
    r.bytes = pos;
    r.info = json!({
        "sent_bytes": pos,
        "server_count": reply,
        "drain_ms": t_end.elapsed().as_secs_f64() * 1e3,
        "total_retrans": ti.map(|t| t.tcpi_total_retrans),
        "srtt_us_at_end": ti.map(|t| t.tcpi_rtt),
        "cwnd_at_end": ti.map(|t| t.tcpi_snd_cwnd),
        "data_segs_out": ti.map(|t| t.tcpi_data_segs_out),
    });
    r
}

/// One RR exchange; returns RTT in ms.
fn rr_once(s: &mut TcpStream, msg: &[u8], rbuf: &mut [u8]) -> io::Result<f64> {
    let t = Instant::now();
    s.write_all(msg)?;
    s.read_exact(rbuf)?;
    let dt = t.elapsed().as_secs_f64() * 1e3;
    if rbuf != msg {
        return Err(io::Error::other("echo mismatch"));
    }
    Ok(dt)
}

/// Extra task run alongside bulk flows from t0; returns (results, errors).
type SideTask = Box<dyn FnOnce(Instant, Arc<AtomicBool>) -> (serde_json::Value, Vec<String>) + Send>;

/// Run N bulk flows (down or up) with per-second sampling. `side` runs in its
/// own thread from t0 (used by `mixed`) and returns extra results.
fn run_bulk(o: &TestOpts, up: bool, flows: usize, counters: &dyn ServerView, mark: Mark, side: Option<SideTask>) -> TestOutcome {
    let stop = Arc::new(AtomicBool::new(false));
    let mut errors = Vec::new();
    // Connect all flows first.
    let mut streams = Vec::new();
    for i in 0..flows {
        let r = connect(o, Duration::from_secs(5)).and_then(|mut s| {
            let h = app::encode_header(if up { app::CMD_UP } else { app::CMD_DOWN }, if up { i as u64 } else { 0 });
            s.write_all(&h)?;
            Ok(s)
        });
        match r {
            Ok(s) => streams.push(s),
            Err(e) => errors.push(format!("flow {i}: connect failed: {e}")),
        }
    }
    if streams.len() != flows {
        return TestOutcome { results: json!({ "error": "connect failed" }), errors, window_bytes: 0, window_secs: 0.0 };
    }
    // Receiver-side per-flow byte counters.
    let recv_ctrs: Vec<Arc<AtomicU64>> = (0..flows).map(|_| Arc::new(AtomicU64::new(0))).collect();
    let base_up: Vec<u64> = if up { counters.up_flow_bytes(flows) } else { vec![0; flows] };
    // One sample of every flow's receiver-side byte count (one control round
    // trip in WG mode).
    let read_all = || -> Vec<u64> {
        if up {
            counters.up_flow_bytes(flows).iter().zip(&base_up).map(|(v, b)| v.saturating_sub(*b)).collect()
        } else {
            recv_ctrs.iter().map(|c| c.load(Relaxed)).collect()
        }
    };
    let t0 = Instant::now();
    let mut handles = Vec::new();
    for (i, s) in streams.into_iter().enumerate() {
        let st = stop.clone();
        let c = recv_ctrs[i].clone();
        handles.push(std::thread::spawn(move || if up { up_flow(s, c, st) } else { down_flow(s, c, st) }));
    }
    let side_h = side.map(|f| {
        let st = stop.clone();
        std::thread::spawn(move || f(t0, st))
    });
    let mut series: Vec<Vec<u64>> = vec![Vec::new(); flows];
    let mut last: Vec<u64> = vec![0; flows];
    let mut window_start_bytes = 0u64;
    for sec in 1..=o.secs {
        if sec - 1 == o.warmup {
            // measurement window starts now (== t0 + warmup)
            window_start_bytes = read_all().iter().sum();
            mark("window_start");
        }
        sleep_until(t0 + Duration::from_secs(sec));
        let now = read_all();
        for i in 0..flows {
            series[i].push(now[i].saturating_sub(last[i]));
            last[i] = now[i];
        }
    }
    let window_end_bytes: u64 = read_all().iter().sum();
    mark("window_end");
    stop.store(true, Relaxed);
    let reports: Vec<FlowReport> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    let side_res = side_h.map(|h| h.join().unwrap());
    let final_recv = read_all();

    let w = o.warmup as usize;
    let agg: Vec<u64> = (0..o.secs as usize).map(|s| series.iter().map(|f| f[s]).sum()).collect();
    let post: Vec<f64> = agg[w.min(agg.len())..].iter().map(|b| *b as f64 * 8.0 / 1e6).collect();
    let zero_secs = agg[w.min(agg.len())..].iter().filter(|b| **b == 0).count();
    let window_bytes = window_end_bytes.saturating_sub(window_start_bytes);
    let window_secs = (o.secs - o.warmup) as f64;
    let mut flows_json = Vec::new();
    for (i, r) in reports.into_iter().enumerate() {
        for e in &r.errors {
            errors.push(format!("flow {i}: {e}"));
        }
        let fz = series[i][w.min(series[i].len())..].iter().filter(|b| **b == 0).count();
        flows_json.push(json!({
            "flow": i,
            "total_bytes": r.bytes,
            "receiver_bytes": final_recv[i],
            "zero_secs": fz,
            "mbps_per_sec": series[i].iter().map(|b| *b as f64 * 8.0 / 1e6).collect::<Vec<_>>(),
            "info": r.info,
        }));
    }
    let mut results = json!({
        "direction": if up { "up" } else { "down" },
        "flows": flows,
        "goodput_mbps_mean": util::mean(&post),
        "goodput_mbps_window": window_bytes as f64 * 8.0 / 1e6 / window_secs,
        "agg_mbps_per_sec": agg.iter().map(|b| *b as f64 * 8.0 / 1e6).collect::<Vec<_>>(),
        "zero_throughput_secs": zero_secs,
        "per_flow": flows_json,
    });
    if let Some((v, e)) = side_res {
        results["side"] = v;
        errors.extend(e);
    }
    TestOutcome { results, errors, window_bytes, window_secs }
}

pub fn test_down(o: &TestOpts, c: &dyn ServerView, mark: Mark) -> TestOutcome {
    run_bulk(o, false, o.flows, c, mark, None)
}

pub fn test_up(o: &TestOpts, c: &dyn ServerView, mark: Mark) -> TestOutcome {
    run_bulk(o, true, o.flows, c, mark, None)
}

/// E8: one saturating download + an RR connection (1 KiB every 100 ms).
pub fn test_mixed(o: &TestOpts, c: &dyn ServerView, mark: Mark) -> TestOutcome {
    let mut errors = Vec::new();
    let msg: Vec<u8> = app::pattern_at(7, o.rr_size.min(app::PAT_CHUNK)).to_vec();
    let mut rr = match connect(o, Duration::from_secs(5)).and_then(|mut s| {
        s.write_all(&app::encode_header(app::CMD_ECHO, msg.len() as u64))?;
        s.set_read_timeout(Some(Duration::from_secs(10)))?;
        Ok(s)
    }) {
        Ok(s) => s,
        Err(e) => {
            errors.push(format!("rr connect: {e}"));
            return TestOutcome { results: json!({"error": "rr connect failed"}), errors, window_bytes: 0, window_secs: 0.0 };
        }
    };
    let interval = Duration::from_millis(o.rr_interval_ms);
    // Idle baseline.
    let mut rbuf = vec![0u8; msg.len()];
    let mut idle = Vec::new();
    let n_idle = (o.idle_rr_secs * 1000 / o.rr_interval_ms.max(1)).max(1);
    let t_idle = Instant::now();
    for k in 0..n_idle {
        sleep_until(t_idle + interval * k as u32);
        match rr_once(&mut rr, &msg, &mut rbuf) {
            Ok(ms) => idle.push(ms),
            Err(e) => {
                errors.push(format!("rr idle: {e}"));
                break;
            }
        }
    }
    let warmup = o.warmup;
    let secs = o.secs;
    let side = Box::new(move |t0: Instant, stop: Arc<AtomicBool>| {
        let mut errs = Vec::new();
        let mut loaded = Vec::new();
        let mut all = Vec::new();
        let mut rbuf = vec![0u8; msg.len()];
        let mut next = t0;
        let end = t0 + Duration::from_secs(secs);
        while !stop.load(Relaxed) {
            sleep_until(next);
            if Instant::now() >= end {
                break;
            }
            let at = Instant::now().duration_since(t0).as_secs_f64();
            match rr_once(&mut rr, &msg, &mut rbuf) {
                Ok(ms) => {
                    all.push((at, ms));
                    if at >= warmup as f64 {
                        loaded.push(ms);
                    }
                }
                Err(e) => {
                    errs.push(format!("rr loaded: {e}"));
                    break;
                }
            }
            next = (next + interval).max(Instant::now());
        }
        (
            json!({
                "rr_loaded": util::lat_summary(&loaded),
                "rr_loaded_series": all.iter().map(|(t, ms)| json!([t, ms])).collect::<Vec<_>>(),
            }),
            errs,
        )
    });
    let mut out = run_bulk(o, false, 1, c, mark, Some(side));
    let idle_sum = util::lat_summary(&idle);
    if let Some(side) = out.results.get("side").cloned() {
        let d = |k: &str| -> Option<f64> { Some(side["rr_loaded"][k].as_f64()? - idle_sum[k].as_f64()?) };
        out.results["rr_idle"] = idle_sum.clone();
        out.results["rr_loaded"] = side["rr_loaded"].clone();
        out.results["rr_delta_p50_ms"] = json!(d("p50_ms"));
        out.results["rr_delta_p99_ms"] = json!(d("p99_ms"));
        out.results["rr_loaded_series"] = side["rr_loaded_series"].clone();
        out.results.as_object_mut().unwrap().remove("side");
    }
    out.results["test"] = json!("mixed");
    out.errors.splice(0..0, errors);
    out
}

/// E7: K connections with concurrency C. Each = connect + header + wait for EOF.
pub fn test_connect(o: &TestOpts, _c: &dyn ServerView, mark: Mark) -> TestOutcome {
    let next = Arc::new(AtomicUsize::new(0));
    let lat = Arc::new(Mutex::new(Vec::<f64>::with_capacity(o.conns)));
    let conn_lat = Arc::new(Mutex::new(Vec::<f64>::with_capacity(o.conns)));
    let fails = Arc::new(Mutex::new(std::collections::BTreeMap::<String, u64>::new()));
    let other_samples = Arc::new(Mutex::new(Vec::<String>::new()));
    let k = o.conns;
    let to = o.connect_timeout;
    mark("window_start");
    let t0 = Instant::now();
    let hs: Vec<_> = (0..o.concurrency.min(k))
        .map(|_| {
            let (next, lat, conn_lat, fails, other) = (next.clone(), lat.clone(), conn_lat.clone(), fails.clone(), other_samples.clone());
            std::thread::spawn(move || {
                let mut buf = [0u8; 64];
                while next.fetch_add(1, Relaxed) < k {
                    let t = Instant::now();
                    let r = (|| -> io::Result<f64> {
                        let mut s = TcpStream::connect_timeout(&server_addr(), to)?;
                        let tc = t.elapsed().as_secs_f64() * 1e3;
                        s.set_read_timeout(Some(to))?;
                        s.set_write_timeout(Some(to))?;
                        s.write_all(&app::encode_header(app::CMD_CONNECT, 0))?;
                        loop {
                            match s.read(&mut buf) {
                                Ok(0) => break,
                                Ok(_) => {}
                                Err(e) => return Err(e),
                            }
                        }
                        Ok(tc)
                    })();
                    match r {
                        Ok(tc) => {
                            lat.lock().unwrap().push(t.elapsed().as_secs_f64() * 1e3);
                            conn_lat.lock().unwrap().push(tc);
                        }
                        Err(e) => {
                            let kind = match e.kind() {
                                io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => "timeout",
                                io::ErrorKind::ConnectionRefused => "refused",
                                io::ErrorKind::ConnectionReset | io::ErrorKind::ConnectionAborted | io::ErrorKind::BrokenPipe => "reset",
                                _ => "other",
                            };
                            *fails.lock().unwrap().entry(kind.into()).or_default() += 1;
                            if kind == "other" {
                                let mut o = other.lock().unwrap();
                                if o.len() < 5 {
                                    o.push(e.to_string());
                                }
                            }
                        }
                    }
                }
            })
        })
        .collect();
    for h in hs {
        h.join().unwrap();
    }
    let elapsed = t0.elapsed().as_secs_f64();
    mark("window_end");
    let lat = lat.lock().unwrap().clone();
    let fails = fails.lock().unwrap().clone();
    let ok = lat.len();
    TestOutcome {
        results: json!({
            "test": "connect",
            "attempted": k,
            "concurrency": o.concurrency,
            "success": ok,
            "failures": fails,
            "failure_samples": other_samples.lock().unwrap().clone(),
            "elapsed_sec": elapsed,
            "conn_per_sec": ok as f64 / elapsed,
            "latency_total": util::lat_summary(&lat),
            "latency_connect": util::lat_summary(&conn_lat.lock().unwrap()),
        }),
        // Failures are a measured outcome here, not a harness correctness error.
        errors: Vec::new(),
        window_bytes: 0,
        window_secs: elapsed,
    }
}
