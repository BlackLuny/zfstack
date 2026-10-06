//! zfbench: S0 real-machine benchmark harness (see bench/README.md and
//! docs/design/0002-s0-benchmark-and-falsification.md).
//!
//! kernel TCP client (root ns) <-> TUN zfbA <-> userspace link emulator <-> server
//! where server = kernel (TUN zfbB in netns zfbns) | smoltcp fork | zfstack.

mod adapters;
mod app;
mod client;
mod climode;
mod kernel_server;
mod link;
mod stack;
mod tun;
mod tunproxy;
mod tunproxy_legacy;
mod util;
mod wg;
mod wgmode;

use std::os::unix::thread::JoinHandleExt;
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::Arc;
use std::time::{Duration, Instant};

use clap::{Parser, ValueEnum};
use serde_json::json;

use crate::app::ServerCounters;
use crate::link::{DirParams, Sink, Source};
use crate::stack::{StackLive, UserStack};

#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum StackKind {
    Kernel,
    SmoltcpCubic,
    SmoltcpBbr,
    SmoltcpReno,
    ZfstackCubic,
    ZfstackBbr,
    /// zfstack CUBIC without pacing (isolates the pacing effect).
    ZfstackNopace,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum TestKind {
    Down,
    Up,
    Mixed,
    Connect,
    /// Client mode only: hold `--conns` idle connections, report proxy memory.
    Idle,
}

#[derive(Parser, Debug)]
#[command(name = "zfbench", about = "userspace TCP stack benchmark harness (needs root)")]
pub struct Args {
    /// Server-side stack.
    #[arg(long, value_enum, default_value = "smoltcp-cubic")]
    stack: StackKind,
    /// Test to run.
    #[arg(long, value_enum, default_value = "down")]
    test: TestKind,
    /// Bottleneck rate (Mbit/s, IP bytes) of the down direction; 0 = unlimited.
    #[arg(long, default_value_t = 200.0)]
    rate_mbps: f64,
    /// Bottleneck rate of the up direction (default: same as --rate-mbps).
    #[arg(long)]
    rate_up_mbps: Option<f64>,
    /// Base RTT in ms (one-way propagation delay = RTT/2 in each direction).
    #[arg(long, default_value_t = 12.0)]
    rtt_ms: f64,
    /// Random loss probability on the down direction (server -> client), e.g. 0.01.
    #[arg(long, default_value_t = 0.0)]
    loss: f64,
    /// Random loss probability on the up direction.
    #[arg(long, default_value_t = 0.0)]
    loss_up: f64,
    /// Bottleneck queue = k x R x RTT bytes (per direction).
    #[arg(long, default_value_t = 2.0)]
    queue_bdp: f64,
    /// Override the bottleneck queue limit in bytes (both directions).
    #[arg(long)]
    queue_bytes: Option<usize>,
    /// Parallel flows for down/up.
    #[arg(long, default_value_t = 1)]
    flows: usize,
    /// Test duration in seconds (down/up/mixed).
    #[arg(long, default_value_t = 10)]
    secs: u64,
    /// Seconds discarded at the start.
    #[arg(long, default_value_t = 2)]
    warmup: u64,
    /// connect test: total connections.
    #[arg(long, default_value_t = 2000)]
    conns: usize,
    /// connect test: concurrency.
    #[arg(long, default_value_t = 64)]
    concurrency: usize,
    /// connect test: connect / read timeout in ms.
    #[arg(long, default_value_t = 3000)]
    connect_timeout_ms: u64,
    /// mixed test: RR message size.
    #[arg(long, default_value_t = 1024)]
    rr_size: usize,
    /// mixed test: RR interval.
    #[arg(long, default_value_t = 100)]
    rr_interval_ms: u64,
    /// mixed test: idle RR baseline duration.
    #[arg(long, default_value_t = 3)]
    idle_rr_secs: u64,
    /// Userspace stack socket rx/tx buffer size (KiB).
    #[arg(long, default_value_t = 4096)]
    sock_buf_kb: usize,
    /// smoltcp: number of sockets kept in Listen.
    #[arg(long, default_value_t = 8)]
    listen_pool: usize,
    /// smoltcp: set_pacing_max_backlog_us (default: fork default).
    #[arg(long)]
    smol_pacing_backlog_us: Option<i64>,
    /// smoltcp: enable TCP timestamps.
    #[arg(long)]
    smol_timestamps: bool,
    /// TCP congestion control of the kernel server sockets (kernel mode); default = system.
    #[arg(long)]
    kernel_cc: Option<String>,
    /// TCP congestion control of the kernel client sockets (sender in `up`); default = system.
    #[arg(long)]
    client_cc: Option<String>,
    /// RNG seed for the loss processes.
    #[arg(long, default_value_t = 1)]
    seed: u64,
    /// Also write the JSON result to this file.
    #[arg(long)]
    json: Option<std::path::PathBuf>,
    /// Free-form label copied into the output.
    #[arg(long, default_value = "")]
    label: String,
    /// Only remove leftover TUN/netns state and exit.
    #[arg(long)]
    cleanup: bool,

    // ---- WG link mode (see bench/README.md "WG link mode") ----
    /// Client: run the test through WireGuard against a `--serve-wg` server at
    /// HOST instead of the link emulator. The emulator flags (rate, RTT, loss,
    /// queue) do not apply; the path is whatever the network between the two
    /// machines is.
    #[arg(long, value_name = "HOST")]
    wg_server: Option<String>,
    /// Client: control port of the WG server.
    #[arg(long, default_value_t = 5202)]
    ctl_port: u16,
    /// Client: WireGuard implementation on the client side.
    #[arg(long, value_enum, default_value = "auto")]
    client_wg: WgImpl,
    /// Server: run the WG benchmark server (one session at a time, until killed).
    #[arg(long)]
    serve_wg: bool,
    /// Server: control listen address.
    #[arg(long, default_value = "0.0.0.0:5202")]
    ctl_listen: std::net::SocketAddr,
    /// Server: WireGuard UDP listen address.
    #[arg(long, default_value = "0.0.0.0:51820")]
    wg_listen: std::net::SocketAddr,
    /// Server: WireGuard implementation in front of the kernel-TCP baseline.
    #[arg(long, value_enum, default_value = "auto")]
    server_wg: WgImpl,
    /// Server: only accept control connections from these addresses (default: any).
    #[arg(long, value_delimiter = ',')]
    allow: Vec<std::net::IpAddr>,
    /// Shared secret for the control channel (both sides; required in WG mode).
    #[arg(long, env = "ZFBENCH_TOKEN", hide_env_values = true)]
    token: Option<String>,

    // ---- client mode (proxy-client TUN stacks, see bench/README.md) ----
    /// Run the client-scenario comparison through this proxy instead of the emulator.
    #[arg(long, value_enum)]
    client_proxy: Option<climode::ProxyKind>,
    /// TUN MTU of the proxy device (sing-box defaults to 65535 on Linux).
    #[arg(long, default_value_t = 65535)]
    tun_mtu: u16,
    /// sing-box binary.
    #[arg(long)]
    singbox: Option<String>,
    /// Proxy runtime worker threads (zfstack tokio workers / sing-box GOMAXPROCS); 0 = default.
    #[arg(long, default_value_t = 0)]
    proxy_workers: usize,
    /// zfstack proxy: relay copy buffer per direction (KiB).
    #[arg(long, default_value_t = 64)]
    relay_buf_kb: usize,
    /// Internal: run the zfstack TUN proxy process.
    #[arg(long, hide = true)]
    tun_proxy: bool,
    #[arg(long, hide = true, default_value = "127.0.0.1:5201")]
    proxy_upstream: std::net::SocketAddr,
    #[arg(long, hide = true)]
    client_profile: bool,
    /// zfstack proxy: relay with `TcpStream::splice` (in the stack driver) instead of a copy task.
    #[arg(long)]
    splice: bool,
    /// zfstack proxy: TUN with IFF_VNET_HDR + checksum offload.
    #[arg(long)]
    vnet_hdr: bool,
    /// zfstack proxy: also accept TSO super-segments from the kernel (implies --vnet-hdr).
    #[arg(long)]
    tso: bool,
    /// zfstack proxy: run the stack driver on a dedicated current-thread runtime.
    #[arg(long)]
    driver_thread: bool,
    /// zfstack proxy: only library APIs that predate the client profile
    /// (A/B of the tokio adapter's server path across library revisions).
    #[arg(long)]
    legacy: bool,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum WgImpl {
    /// Kernel WireGuard if `ip link add type wireguard` and `wg` work, else userspace.
    Auto,
    Kernel,
    /// boringtun on a TUN device (like wireguard-go).
    Userspace,
}

#[derive(Clone, Copy)]
struct Snap {
    t: Instant,
    stack_cpu: Option<f64>,
    link_up_cpu: Option<f64>,
    link_down_cpu: Option<f64>,
    proc_cpu: f64,
    procstat: util::ProcStat,
    stack_wakeups: u64,
}

/// Everything needed to build the server side of one run. In the WG mode it
/// travels to the remote server over the control channel.
#[derive(Clone, Debug)]
pub struct StackOpts {
    pub stack: StackKind,
    pub sock_buf_kb: usize,
    pub listen_pool: usize,
    pub smol_pacing_backlog_us: Option<i64>,
    pub smol_timestamps: bool,
    pub kernel_cc: Option<String>,
}

impl StackOpts {
    fn from_args(a: &Args) -> Self {
        StackOpts {
            stack: a.stack,
            sock_buf_kb: a.sock_buf_kb,
            listen_pool: a.listen_pool,
            smol_pacing_backlog_us: a.smol_pacing_backlog_us,
            smol_timestamps: a.smol_timestamps,
            kernel_cc: a.kernel_cc.clone(),
        }
    }

    pub fn to_json(&self) -> serde_json::Value {
        json!({
            "stack": stack_name(self.stack),
            "sock_buf_kb": self.sock_buf_kb,
            "listen_pool": self.listen_pool,
            "smol_pacing_backlog_us": self.smol_pacing_backlog_us,
            "smol_timestamps": self.smol_timestamps,
            "kernel_cc": self.kernel_cc,
        })
    }

    pub fn from_json(v: &serde_json::Value) -> Result<Self, String> {
        let stack = v["stack"].as_str().ok_or("missing stack")?;
        Ok(StackOpts {
            stack: StackKind::from_str(stack, true)?,
            sock_buf_kb: v["sock_buf_kb"].as_u64().ok_or("missing sock_buf_kb")? as usize,
            listen_pool: v["listen_pool"].as_u64().ok_or("missing listen_pool")? as usize,
            smol_pacing_backlog_us: v["smol_pacing_backlog_us"].as_i64(),
            smol_timestamps: v["smol_timestamps"].as_bool().unwrap_or(false),
            kernel_cc: v["kernel_cc"].as_str().map(str::to_string),
        })
    }
}

pub fn build_stack(o: &StackOpts, counters: Arc<ServerCounters>) -> Box<dyn UserStack> {
    use smoltcp::socket::tcp::CongestionControl as CC;
    let cc = match o.stack {
        StackKind::SmoltcpCubic => CC::Cubic,
        StackKind::SmoltcpBbr => CC::Bbr,
        StackKind::SmoltcpReno => CC::Reno,
        StackKind::ZfstackCubic | StackKind::ZfstackBbr | StackKind::ZfstackNopace => {
            #[cfg(feature = "zfstack")]
            {
                let (cc, pacing) = match o.stack {
                    StackKind::ZfstackBbr => (zfstack::CcAlgo::Bbr, true),
                    StackKind::ZfstackNopace => (zfstack::CcAlgo::Cubic, false),
                    _ => (zfstack::CcAlgo::Cubic, true),
                };
                return Box::new(adapters::zfstack::ZfStack::new(adapters::zfstack::ZfOpts { sock_buf: o.sock_buf_kb * 1024, cc, pacing }, counters));
            }
            #[cfg(not(feature = "zfstack"))]
            {
                eprintln!("zfbench was built without the `zfstack` feature");
                std::process::exit(2);
            }
        }
        StackKind::Kernel => unreachable!(),
    };
    Box::new(adapters::smoltcp::SmolStack::new(
        adapters::smoltcp::SmolOpts {
            cc,
            rx_buf: o.sock_buf_kb * 1024,
            tx_buf: o.sock_buf_kb * 1024,
            listen_pool: o.listen_pool,
            pacing_backlog_us: o.smol_pacing_backlog_us,
            timestamps: o.smol_timestamps,
        },
        counters,
    ))
}

pub fn stack_name(s: StackKind) -> &'static str {
    match s {
        StackKind::Kernel => "kernel",
        StackKind::SmoltcpCubic => "smoltcp-cubic",
        StackKind::SmoltcpBbr => "smoltcp-bbr",
        StackKind::SmoltcpReno => "smoltcp-reno",
        StackKind::ZfstackCubic => "zfstack-cubic",
        StackKind::ZfstackBbr => "zfstack-bbr",
        StackKind::ZfstackNopace => "zfstack-nopace",
    }
}

pub fn test_name(t: TestKind) -> &'static str {
    match t {
        TestKind::Down => "down",
        TestKind::Up => "up",
        TestKind::Mixed => "mixed",
        TestKind::Connect => "connect",
        TestKind::Idle => "idle",
    }
}

pub fn sysctl(name: &str) -> String {
    std::fs::read_to_string(format!("/proc/sys/{}", name.replace('.', "/"))).map(|s| s.split_whitespace().collect::<Vec<_>>().join(" ")).unwrap_or_default()
}

fn main() {
    let a = Args::parse();
    if a.cleanup {
        tun::cleanup();
        return;
    }
    if a.tun_proxy {
        let workers = if a.proxy_workers == 0 { std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1) } else { a.proxy_workers };
        let o = tunproxy::ProxyOpts {
            mtu: a.tun_mtu,
            upstream: a.proxy_upstream,
            workers,
            relay_buf: a.relay_buf_kb * 1024,
            client_profile: a.client_profile,
            splice: a.splice,
            vnet_hdr: a.vnet_hdr || a.tso,
            tso: a.tso,
            driver_thread: a.driver_thread,
        };
        let r = if a.legacy { tunproxy_legacy::run(o) } else { tunproxy::run(o) };
        if let Err(e) = r {
            eprintln!("zfbench tun-proxy: {e}");
            std::process::exit(3);
        }
        return;
    }
    if let Some(kind) = a.client_proxy {
        install_signal_cleanup();
        let code = climode::run(&a, kind).unwrap_or_else(|e| {
            eprintln!("zfbench: client mode: {e}");
            3
        });
        tun::sh_quiet(&format!("ip link del {}", tunproxy::PROXY_TUN));
        std::process::exit(code);
    }
    if a.serve_wg {
        install_signal_cleanup();
        match wgmode::serve(&a) {
            Ok(()) => std::process::exit(0),
            Err(e) => {
                eprintln!("zfbench: {e}");
                tun::cleanup();
                std::process::exit(3);
            }
        }
    }
    if a.warmup >= a.secs && matches!(a.test, TestKind::Down | TestKind::Up | TestKind::Mixed) {
        eprintln!("--warmup must be < --secs");
        std::process::exit(2);
    }
    if a.flows == 0 || a.flows > app::MAX_FLOWS {
        eprintln!("--flows must be in 1..={}", app::MAX_FLOWS);
        std::process::exit(2);
    }
    // Make sure teardown happens on panic too (TUN fds die with the process,
    // the netns needs an explicit delete).
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        default_hook(info);
        tun::cleanup();
        std::process::exit(101);
    }));
    install_signal_cleanup();

    let code = match if a.wg_server.is_some() { wgmode::run_client(&a) } else { run(&a) } {
        Ok(code) => code,
        Err(e) => {
            eprintln!("zfbench: setup error: {e}");
            3
        }
    };
    tun::cleanup();
    std::process::exit(code);
}

extern "C" fn on_signal(_: libc::c_int) {
    // Not async-signal-safe in theory; acceptable for a benchmark tool.
    tun::cleanup();
    unsafe { libc::_exit(130) };
}

fn install_signal_cleanup() {
    unsafe {
        libc::signal(libc::SIGINT, on_signal as *const () as libc::sighandler_t);
        libc::signal(libc::SIGTERM, on_signal as *const () as libc::sighandler_t);
    }
}

fn run(a: &Args) -> std::io::Result<i32> {
    tun::cleanup();
    let rtt = Duration::from_secs_f64(a.rtt_ms / 1e3);
    let one_way = rtt / 2;
    let rate_down = (a.rate_mbps * 1e6) as u64;
    let rate_up = (a.rate_up_mbps.unwrap_or(a.rate_mbps) * 1e6) as u64;
    let qlim = |rate: u64| -> usize {
        match a.queue_bytes {
            Some(b) => b,
            None if rate == 0 => usize::MAX / 2,
            None => ((a.queue_bdp * rate as f64 / 8.0 * rtt.as_secs_f64()) as usize).max(tun::MTU),
        }
    };
    let p_down = DirParams { rate_bps: rate_down, queue_limit: qlim(rate_down), delay: one_way, loss: a.loss, seed: a.seed.wrapping_mul(2) + 1 };
    let p_up = DirParams { rate_bps: rate_up, queue_limit: qlim(rate_up), delay: one_way, loss: a.loss_up, seed: a.seed.wrapping_mul(2) + 2 };

    let counters = ServerCounters::new();
    let stop_link = Arc::new(AtomicBool::new(false));
    let stop_srv = Arc::new(AtomicBool::new(false));
    let softnet0 = util::softnet_dropped();

    let fd_a = tun::setup_a()?;
    let mut fd_b = None;
    let mut kserver = None;
    let mut stack_handle = None;
    let live = Arc::new(StackLive::default());
    let (up_src, up_sink, down_src, down_sink);
    if a.stack == StackKind::Kernel {
        let fb = tun::setup_ns_b()?;
        fd_b = Some(fb);
        kserver = Some(kernel_server::start(counters.clone(), stop_srv.clone(), a.kernel_cc.clone(), Some(tun::NETNS))?);
        up_src = Source::Tun(fd_a);
        up_sink = Sink::Tun(fb);
        down_src = Source::Tun(fb);
        down_sink = Sink::Tun(fd_a);
    } else {
        let st = build_stack(&StackOpts::from_args(a), counters.clone());
        let (up_tx, up_rx) = crossbeam_channel::unbounded();
        let (down_tx, down_rx) = crossbeam_channel::unbounded();
        let (stop, lv) = (stop_link.clone(), live.clone());
        let h = std::thread::Builder::new().name("zfb-stack".into()).spawn(move || stack::run_stack_thread(st, up_rx, down_tx, stop, lv))?;
        stack_handle = Some(h);
        up_src = Source::Tun(fd_a);
        up_sink = Sink::Chan(up_tx);
        down_src = Source::Chan(down_rx);
        down_sink = Sink::Tun(fd_a);
    }
    let (pu, pd) = (p_up.clone(), p_down.clone());
    let (s1, s2) = (stop_link.clone(), stop_link.clone());
    let h_up = std::thread::Builder::new().name("zfb-link-up".into()).spawn(move || link::run_direction(pu, up_src, up_sink, s1))?;
    let h_down = std::thread::Builder::new().name("zfb-link-down".into()).spawn(move || link::run_direction(pd, down_src, down_sink, s2))?;
    let stack_pt = stack_handle.as_ref().map(|h| h.as_pthread_t());
    let (up_pt, down_pt) = (h_up.as_pthread_t(), h_down.as_pthread_t());
    std::thread::sleep(Duration::from_millis(200));

    let snap = || Snap {
        t: Instant::now(),
        stack_cpu: stack_pt.and_then(util::pthread_cpu),
        link_up_cpu: util::pthread_cpu(up_pt),
        link_down_cpu: util::pthread_cpu(down_pt),
        proc_cpu: util::process_cpu_now(),
        procstat: util::proc_stat(),
        stack_wakeups: live.wakeups.load(Relaxed),
    };
    let s_begin = snap();
    let mut marks: Vec<(&'static str, Snap)> = Vec::new();
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
    let t_test = Instant::now();
    let outcome = {
        let mut mark = |name: &'static str| marks.push((name, snap()));
        match a.test {
            TestKind::Down => client::test_down(&o, &counters, &mut mark),
            TestKind::Up => client::test_up(&o, &counters, &mut mark),
            TestKind::Mixed => client::test_mixed(&o, &counters, &mut mark),
            TestKind::Connect => client::test_connect(&o, &counters, &mut mark),
            TestKind::Idle => {
                eprintln!("--test idle needs --client-proxy");
                std::process::exit(2);
            }
        }
    };
    let test_secs = t_test.elapsed().as_secs_f64();
    // Let straggling closes settle before tearing down.
    std::thread::sleep(Duration::from_millis(300));
    let s_end = snap();

    // Tear down.
    stop_link.store(true, Relaxed);
    let c_up = h_up.join().expect("link up panicked");
    let c_down = h_down.join().expect("link down panicked");
    let stack_res = stack_handle.map(|h| h.join().expect("stack thread panicked"));
    let mut kernel_threads_active = None;
    if let Some(ks) = &kserver {
        stop_srv.store(true, Relaxed);
        let t = Instant::now();
        while ks.active.load(Relaxed) > 0 && t.elapsed() < Duration::from_secs(3) {
            std::thread::sleep(Duration::from_millis(10));
        }
        kernel_threads_active = Some(ks.active.load(Relaxed));
    }
    let tun_a_stats = json!({
        "qdisc_drops": tun::qdisc_drops(None, tun::TUN_A),
        "tx_dropped": tun::dev_stat(tun::TUN_A, "tx_dropped"),
        "rx_dropped": tun::dev_stat(tun::TUN_A, "rx_dropped"),
    });
    let tun_b_stats = fd_b.map(|_| json!({ "qdisc_drops": tun::qdisc_drops(Some(tun::NETNS), tun::TUN_B) }));
    let softnet_drops = util::softnet_dropped().saturating_sub(softnet0);
    unsafe {
        libc::close(fd_a);
        if let Some(fb) = fd_b {
            libc::close(fb);
        }
    }

    // CPU accounting.
    let find = |n: &str| marks.iter().find(|(k, _)| *k == n).map(|(_, s)| *s);
    let (w0, w1) = match (find("window_start"), find("window_end")) {
        (Some(x), Some(y)) => (x, y),
        _ => (s_begin, s_end),
    };
    let dsub = |x: Option<f64>, y: Option<f64>| -> Option<f64> { Some(y? - x?) };
    let window_wall = (w1.t - w0.t).as_secs_f64();
    let stack_window_cpu = dsub(w0.stack_cpu, w1.stack_cpu);
    let gb = outcome.window_bytes as f64 / 1e9;
    let per_gb = |c: Option<f64>| -> Option<f64> {
        if gb > 0.0 {
            c.map(|c| c / gb)
        } else {
            None
        }
    };
    let sys_window = util::proc_stat_delta_json(&w0.procstat, &w1.procstat);
    let sys_busy = sys_window["busy_sec"].as_f64();
    let kernel_srv_cpu = kserver.as_ref().map(|_| counters.thread_cpu_us.load(Relaxed) as f64 / 1e6);

    let mut errors = outcome.errors.clone();
    let pe = counters.pattern_errors.load(Relaxed);
    if pe > 0 {
        errors.push(format!("server: {pe} upload pattern mismatches"));
    }
    let bh = counters.bad_headers.load(Relaxed);
    if bh > 0 {
        errors.push(format!("server: {bh} bad headers"));
    }

    let late_mean_us = {
        let n = live.timer_wakeups.load(Relaxed);
        if n > 0 {
            live.deadline_late_sum_ns.load(Relaxed) as f64 / n as f64 / 1e3
        } else {
            0.0
        }
    };
    let result = json!({
        "tool": "zfbench",
        "version": env!("CARGO_PKG_VERSION"),
        "label": a.label,
        "timestamp_unix": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_secs(),
        "config": {
            "stack": stack_name(a.stack),
            "test": test_name(a.test),
            "rate_mbps": a.rate_mbps,
            "rate_up_mbps": a.rate_up_mbps.unwrap_or(a.rate_mbps),
            "rtt_ms": a.rtt_ms,
            "loss": a.loss,
            "loss_up": a.loss_up,
            "queue_bdp": a.queue_bdp,
            "queue_bytes_down": p_down.queue_limit,
            "queue_bytes_up": p_up.queue_limit,
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
            "seed": a.seed,
            "kernel_cc": a.kernel_cc.clone().unwrap_or_else(|| sysctl("net.ipv4.tcp_congestion_control")),
            "client_cc": a.client_cc.clone().unwrap_or_else(|| sysctl("net.ipv4.tcp_congestion_control")),
            "mtu": tun::MTU,
        },
        "env": {
            "kernel": tun::sh_output("uname -r").trim(),
            "nproc": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
            "zfstack_commit": util::zfstack_commit(),
            "smoltcp_rev": "8014f8b21e12faf89b3b453ceea32027344721af",
            "tcp_rmem": sysctl("net.ipv4.tcp_rmem"),
            "tcp_wmem": sysctl("net.ipv4.tcp_wmem"),
            "tcp_congestion_control": sysctl("net.ipv4.tcp_congestion_control"),
        },
        "ok": errors.is_empty(),
        "errors": errors,
        "results": outcome.results,
        "link": {
            "down": c_down.to_json(&p_down),
            "up": c_up.to_json(&p_up),
            "tun_a": tun_a_stats,
            "tun_b": tun_b_stats,
            "softnet_backlog_drops": softnet_drops,
        },
        "server_app": counters.to_json(),
        "cpu": {
            "window_wall_sec": window_wall,
            "window_nominal_sec": outcome.window_secs,
            "window_effective_bytes": outcome.window_bytes,
            "stack_thread_window_sec": stack_window_cpu,
            "stack_thread_total_sec": stack_res.as_ref().map(|r| r.cpu_sec),
            "cpu_sec_per_effective_GB": per_gb(stack_window_cpu),
            "stack_thread_util": stack_window_cpu.map(|c| c / window_wall),
            "kernel_server_threads_total_sec": kernel_srv_cpu,
            "kernel_server_threads_active_at_exit": kernel_threads_active,
            "link_up_window_sec": dsub(w0.link_up_cpu, w1.link_up_cpu),
            "link_down_window_sec": dsub(w0.link_down_cpu, w1.link_down_cpu),
            "process_window_sec": w1.proc_cpu - w0.proc_cpu,
            "system_window": sys_window,
            "system_busy_sec_per_effective_GB": per_gb(sys_busy),
            "test_wall_sec": test_secs,
        },
        "stack_thread": stack_res.as_ref().map(|r| json!({
            "wakeups_total": live.wakeups.load(Relaxed),
            "wakeups_window": w1.stack_wakeups - w0.stack_wakeups,
            "wakeups_per_sec_window": (w1.stack_wakeups - w0.stack_wakeups) as f64 / window_wall,
            "timer_wakeups": live.timer_wakeups.load(Relaxed),
            "deadline_late_mean_us": late_mean_us,
            "deadline_late_max_us": live.deadline_late_max_ns.load(Relaxed) as f64 / 1e3,
            "polls": live.polls.load(Relaxed),
            "pkts_in": live.pkts_in.load(Relaxed),
            "pkts_out": live.pkts_out.load(Relaxed),
            "stack_stats": r.stats,
        })),
    });
    let text = serde_json::to_string_pretty(&result).unwrap();
    println!("{text}");
    if let Some(p) = &a.json {
        std::fs::write(p, &text)?;
    }
    if !errors.is_empty() {
        for e in &errors {
            eprintln!("zfbench: correctness failure: {e}");
        }
        return Ok(1);
    }
    Ok(0)
}
