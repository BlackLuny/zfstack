//! Client-scenario mode (`--client-proxy`): compare proxy-client TUN stacks.
//!
//! ```text
//! kernel TCP client ──▶ 198.18.0.2:5201 ──TUN zfcT──▶ proxy process ──▶ 127.0.0.1:5201 kernel server
//! ```
//!
//! The proxy is a separate process (zfstack `--tun-proxy`, sing-box, the C
//! lwIP TUN proxy, or hev-socks5-tunnel), so its CPU time and memory are
//! measured the same way for every implementation: utime+stime and
//! VmRSS/VmHWM from /proc/<pid>. `direct` skips the proxy and connects to
//! the server over loopback (kernel-only reference).

use std::io;
use std::net::SocketAddr;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::Arc;
use std::time::{Duration, Instant};

use clap::ValueEnum;
use serde_json::json;

use crate::app::ServerCounters;
use crate::tunproxy::{PROXY_ROUTE, PROXY_TUN, PROXY_TUN_ADDR};
use crate::{client, kernel_server, tun, util, Args, TestKind};

#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum ProxyKind {
    /// No proxy: loopback to the server (kernel reference).
    Direct,
    /// zfstack TUN proxy with the default (server) configuration.
    Zfstack,
    /// zfstack TUN proxy with `StackConfig::client()`.
    ZfstackClient,
    /// sing-box TUN inbound, `go` stack (1.15 default).
    SingboxGo,
    SingboxGvisor,
    SingboxSystem,
    /// Official lwIP 2.2.1 C TUN proxy (`bench/c-stacks/lwip-tun-proxy`).
    Lwip,
    /// hev-socks5-tunnel (C tun2socks + lwIP) with a local SOCKS5 override.
    Hev,
}

pub fn proxy_name(k: ProxyKind) -> &'static str {
    match k {
        ProxyKind::Direct => "direct",
        ProxyKind::Zfstack => "zfstack",
        ProxyKind::ZfstackClient => "zfstack-client",
        ProxyKind::SingboxGo => "singbox-go",
        ProxyKind::SingboxGvisor => "singbox-gvisor",
        ProxyKind::SingboxSystem => "singbox-system",
        ProxyKind::Lwip => "lwip",
        ProxyKind::Hev => "hev",
    }
}

const UPSTREAM: &str = "127.0.0.1:5201";
const TARGET: &str = "198.18.0.2:5201";

#[derive(Clone, Copy)]
struct Snap {
    t: Instant,
    proxy: Option<ProcSample>,
    sys: util::ProcStat,
    harness_cpu: f64,
}

#[derive(Clone, Copy, Default)]
struct ProcSample {
    cpu_sec: f64,
    rss_kb: u64,
    hwm_kb: u64,
}

fn proc_sample(pid: u32) -> ProcSample {
    let mut s = ProcSample::default();
    if let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) {
        // Fields after the ")" of comm: state is field 3; utime/stime are 14/15.
        if let Some(rest) = stat.rsplit_once(')').map(|x| x.1) {
            let f: Vec<&str> = rest.split_whitespace().collect();
            let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) } as f64;
            let ut: f64 = f.get(11).and_then(|v| v.parse().ok()).unwrap_or(0.0);
            let st: f64 = f.get(12).and_then(|v| v.parse().ok()).unwrap_or(0.0);
            s.cpu_sec = (ut + st) / hz;
        }
    }
    if let Ok(status) = std::fs::read_to_string(format!("/proc/{pid}/status")) {
        for l in status.lines() {
            let v = || l.split_whitespace().nth(1).and_then(|x| x.parse().ok()).unwrap_or(0);
            if l.starts_with("VmRSS:") {
                s.rss_kb = v();
            } else if l.starts_with("VmHWM:") {
                s.hwm_kb = v();
            }
        }
    }
    s
}

fn singbox_config(stack: &str, mtu: u16) -> String {
    json!({
        "log": {"level": "error"},
        "inbounds": [{
            "type": "tun", "tag": "tun-in", "interface_name": PROXY_TUN,
            "address": [PROXY_TUN_ADDR], "mtu": mtu, "auto_route": false, "stack": stack, "dns_mode": "disabled",
        }],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {"rules": [{"inbound": "tun-in", "action": "route", "outbound": "direct", "override_address": "127.0.0.1"}], "final": "direct"},
    })
    .to_string()
}

fn bin_flag(flag: &Option<String>, default: &str) -> String {
    flag.clone().unwrap_or_else(|| default.into())
}

fn spawn_proxy(a: &Args, kind: ProxyKind) -> io::Result<Vec<Child>> {
    tun::sh_quiet(&format!("ip link del {PROXY_TUN}"));
    let mtu = a.tun_mtu;
    let mut children = Vec::new();
    match kind {
        ProxyKind::Direct => return Ok(children),
        ProxyKind::Zfstack | ProxyKind::ZfstackClient => {
            let mut c = Command::new(std::env::current_exe()?);
            c.args(["--tun-proxy", "--tun-mtu", &mtu.to_string(), "--proxy-upstream", UPSTREAM]);
            c.args(["--proxy-workers", &a.proxy_workers.to_string(), "--relay-buf-kb", &a.relay_buf_kb.to_string()]);
            if kind == ProxyKind::ZfstackClient {
                c.arg("--client-profile");
            }
            if a.splice {
                c.arg("--splice");
            }
            if a.vnet_hdr {
                c.arg("--vnet-hdr");
            }
            if a.tso {
                c.arg("--tso");
            }
            if a.driver_thread {
                c.arg("--driver-thread");
            }
            if a.legacy {
                c.arg("--legacy");
            }
            children.push(c.stdin(Stdio::null()).spawn()?);
        }
        ProxyKind::SingboxGo | ProxyKind::SingboxGvisor | ProxyKind::SingboxSystem => {
            let stack = match kind {
                ProxyKind::SingboxGo => "go",
                ProxyKind::SingboxGvisor => "gvisor",
                _ => "system",
            };
            let path = std::env::temp_dir().join(format!("zfbench-singbox-{}.json", std::process::id()));
            std::fs::write(&path, singbox_config(stack, mtu))?;
            let bin = bin_flag(&a.singbox, "sing-box");
            let mut c = Command::new(bin);
            c.arg("run").arg("-c").arg(&path).stdin(Stdio::null());
            if a.proxy_workers > 0 {
                c.env("GOMAXPROCS", a.proxy_workers.to_string());
            }
            children.push(c.spawn()?);
        }
        ProxyKind::Lwip => {
            let bin = bin_flag(&a.lwip, "lwip-tun-proxy");
            let mut c = Command::new(bin);
            c.args([
                "--tun",
                PROXY_TUN,
                "--addr",
                PROXY_TUN_ADDR,
                "--listen",
                "198.18.0.2",
                "--port",
                "5201",
                "--upstream",
                UPSTREAM,
                "--mtu",
                &mtu.to_string(),
            ]);
            children.push(c.stdin(Stdio::null()).spawn()?);
        }
        ProxyKind::Hev => {
            let socks = bin_flag(&a.socks5_override, "socks5-override");
            let hev = bin_flag(&a.hev, "hev-socks5-tunnel");
            let socks_listen = "127.0.0.1:18721";
            let mut s5 = Command::new(socks);
            s5.args(["--listen", socks_listen, "--override", UPSTREAM]);
            children.push(s5.stdin(Stdio::null()).spawn()?);
            std::thread::sleep(Duration::from_millis(150));
            let yaml = format!(
                "tunnel:\n  name: {PROXY_TUN}\n  mtu: {mtu}\n  ipv4: 172.19.0.1\n  icmp: off\n  offload:\n    tso: true\n    jumbo: true\nsocks5:\n  port: 18721\n  address: 127.0.0.1\n  udp: udp\nmisc:\n  log-level: error\n  tcp-buffer-size: 262144\n  limit-nofile: 262144\n"
            );
            let path = std::env::temp_dir().join(format!("zfbench-hev-{}.yml", std::process::id()));
            std::fs::write(&path, yaml)?;
            let mut c = Command::new(hev);
            c.arg(path).stdin(Stdio::null());
            children.push(c.spawn()?);
        }
    }
    let t = Instant::now();
    let target: SocketAddr = TARGET.parse().unwrap();
    loop {
        if t.elapsed() > Duration::from_secs(15) {
            for mut c in children {
                let _ = c.kill();
                let _ = c.wait();
            }
            return Err(io::Error::other("proxy did not come up"));
        }
        let up = std::fs::read_to_string(format!("/sys/class/net/{PROXY_TUN}/operstate")).is_ok();
        if up {
            tun::sh_quiet(&format!("ip route replace {PROXY_ROUTE} dev {PROXY_TUN}"));
        }
        if up && std::net::TcpStream::connect_timeout(&target, Duration::from_millis(300)).is_ok() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    Ok(children)
}

fn stop_proxy(mut children: Vec<Child>) {
    for c in &children {
        unsafe { libc::kill(c.id() as i32, libc::SIGTERM) };
    }
    let t = Instant::now();
    while t.elapsed() < Duration::from_secs(3) {
        let mut alive = false;
        for c in &mut children {
            if c.try_wait().ok().flatten().is_none() {
                alive = true;
            }
        }
        if !alive {
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    for mut c in children {
        let _ = c.kill();
        let _ = c.wait();
    }
    tun::sh_quiet(&format!("ip link del {PROXY_TUN}"));
}

fn sample_pids(pids: &[u32]) -> Option<ProcSample> {
    if pids.is_empty() {
        return None;
    }
    let mut s = ProcSample::default();
    for pid in pids {
        let p = proc_sample(*pid);
        s.cpu_sec += p.cpu_sec;
        s.rss_kb += p.rss_kb;
        s.hwm_kb += p.hwm_kb;
    }
    Some(s)
}

/// Hold `n` idle connections open (header not sent: the server waits) and
/// report the proxy's memory per connection.
fn test_idle(n: usize, pids: &[u32], mark: &mut dyn FnMut(&'static str)) -> client::TestOutcome {
    let target = client::server_addr();
    let before = sample_pids(pids);
    mark("window_start");
    let mut conns = Vec::with_capacity(n);
    let mut errors = Vec::new();
    let t0 = Instant::now();
    for i in 0..n {
        match std::net::TcpStream::connect_timeout(&target, Duration::from_secs(5)) {
            Ok(s) => conns.push(s),
            Err(e) => {
                errors.push(format!("idle conn {i}: {e}"));
                break;
            }
        }
    }
    let established = conns.len();
    if established < n && errors.is_empty() {
        errors.push(format!("idle established {established}/{n}"));
    }
    let connect_secs = t0.elapsed().as_secs_f64();
    // Let the proxy finish dialing upstream and settle.
    std::thread::sleep(Duration::from_secs(2));
    let after = sample_pids(pids);
    mark("window_end");
    drop(conns);
    let per_conn = match (before, after) {
        (Some(b), Some(a)) if established > 0 => Some((a.rss_kb as f64 - b.rss_kb as f64) * 1024.0 / established as f64),
        _ => None,
    };
    client::TestOutcome {
        results: json!({
            "conns": n,
            "established": established,
            "connect_secs": connect_secs,
            "rss_kb_before": before.map(|s| s.rss_kb),
            "rss_kb_after": after.map(|s| s.rss_kb),
            "rss_bytes_per_conn": per_conn,
        }),
        errors,
        window_bytes: 0,
        window_secs: 0.0,
    }
}

pub fn run(a: &Args, kind: ProxyKind) -> io::Result<i32> {
    let counters = ServerCounters::new();
    let stop_srv = Arc::new(AtomicBool::new(false));
    let ks = kernel_server::start_at(counters.clone(), stop_srv.clone(), a.kernel_cc.clone(), None, UPSTREAM.parse().unwrap())?;
    client::set_server_addr(if kind == ProxyKind::Direct { UPSTREAM.parse().unwrap() } else { TARGET.parse().unwrap() });
    let mut children = spawn_proxy(a, kind)?;
    let pids: Vec<u32> = children.iter().map(|c| c.id()).collect();
    std::thread::sleep(Duration::from_millis(300));
    let base = sample_pids(&pids);

    let snap = || Snap { t: Instant::now(), proxy: sample_pids(&pids), sys: util::proc_stat(), harness_cpu: util::process_cpu_now() };
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
        short_bytes: a.short_bytes,
    };
    let s_begin = snap();
    let outcome = {
        let mut mark = |name: &'static str| marks.push((name, snap()));
        match a.test {
            TestKind::Down => client::test_down(&o, &counters, &mut mark),
            TestKind::Up => client::test_up(&o, &counters, &mut mark),
            TestKind::Mixed => client::test_mixed(&o, &counters, &mut mark),
            TestKind::MixedBi => client::test_mixed_bi(&o, &counters, &mut mark),
            TestKind::Connect => client::test_connect(&o, &counters, &mut mark),
            TestKind::Short => client::test_short(&o, &mut mark),
            TestKind::Idle => test_idle(a.conns, &pids, &mut mark),
            TestKind::Isolate => {
                eprintln!("--test isolate uses the emulator path, not --client-proxy");
                std::process::exit(2);
            }
        }
    };
    std::thread::sleep(Duration::from_millis(300));
    let s_end = snap();
    let end_sample = sample_pids(&pids);
    let child_exit = children.iter_mut().find_map(|c| c.try_wait().ok().flatten()).map(|st| st.to_string());
    stop_proxy(children);
    stop_srv.store(true, Relaxed);
    let t = Instant::now();
    while ks.active.load(Relaxed) > 0 && t.elapsed() < Duration::from_secs(3) {
        std::thread::sleep(Duration::from_millis(10));
    }

    let find = |n: &str| marks.iter().find(|(k, _)| *k == n).map(|(_, s)| *s);
    let (w0, w1) = match (find("window_start"), find("window_end")) {
        (Some(x), Some(y)) => (x, y),
        _ => (s_begin, s_end),
    };
    let wall = (w1.t - w0.t).as_secs_f64();
    let proxy_cpu = match (w0.proxy, w1.proxy) {
        (Some(x), Some(y)) => Some(y.cpu_sec - x.cpu_sec),
        _ => None,
    };
    let gb = outcome.window_bytes as f64 / 1e9;
    let per_gb = |c: Option<f64>| if gb > 0.0 { c.map(|c| c / gb) } else { None };
    let sys = util::proc_stat_delta_json(&w0.sys, &w1.sys);
    let sys_busy = sys["busy_sec"].as_f64();
    let mut errors = outcome.errors.clone();
    if matches!(a.test, TestKind::Down | TestKind::Up | TestKind::MixedBi | TestKind::Short) && outcome.window_bytes == 0 {
        errors.push("no data moved in the measurement window".into());
    }
    if let Some(c) = &child_exit {
        errors.push(format!("proxy exited during the run: {c}"));
    }
    let pe = counters.pattern_errors.load(Relaxed);
    if pe > 0 {
        errors.push(format!("server: {pe} upload pattern mismatches"));
    }
    let result = json!({
        "tool": "zfbench",
        "mode": "client",
        "label": a.label,
        "config": {
            "proxy": proxy_name(kind),
            "test": crate::test_name(a.test),
            "tun_mtu": a.tun_mtu,
            "flows": a.flows,
            "secs": a.secs,
            "warmup": a.warmup,
            "conns": a.conns,
            "concurrency": a.concurrency,
            "proxy_workers": a.proxy_workers,
            "relay_buf_kb": a.relay_buf_kb,
            "splice": a.splice,
            "vnet_hdr": a.vnet_hdr || a.tso,
            "tso": a.tso,
            "driver_thread": a.driver_thread,
            "legacy": a.legacy,
            "short_bytes": a.short_bytes,
        },
        "env": {
            "kernel": tun::sh_output("uname -r").trim(),
            "nproc": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
            "zfstack_commit": util::zfstack_commit(),
        },
        "ok": errors.is_empty(),
        "errors": errors,
        "results": outcome.results,
        "proxy": {
            "pid": pids.first(),
            "pids": pids,
            "rss_kb_idle": base.map(|s| s.rss_kb),
            "rss_kb_end": end_sample.map(|s| s.rss_kb),
            "hwm_kb": end_sample.map(|s| s.hwm_kb),
            "cpu_window_sec": proxy_cpu,
            "cpu_util": proxy_cpu.map(|c| c / wall),
            "cpu_sec_per_GB": per_gb(proxy_cpu),
        },
        "cpu": {
            "window_wall_sec": wall,
            "window_effective_bytes": outcome.window_bytes,
            "harness_process_sec": w1.harness_cpu - w0.harness_cpu,
            "system_window": sys,
            "system_busy_sec_per_GB": per_gb(sys_busy),
        },
    });
    let text = serde_json::to_string_pretty(&result).unwrap();
    println!("{text}");
    if let Some(p) = &a.json {
        std::fs::write(p, &text)?;
    }
    if !result["ok"].as_bool().unwrap_or(false) {
        for e in result["errors"].as_array().unwrap() {
            eprintln!("zfbench: correctness failure: {e}");
        }
        return Ok(1);
    }
    Ok(0)
}
