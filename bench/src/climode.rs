//! Client-scenario mode (`--client-proxy`): compare proxy-client TUN stacks.
//!
//! ```text
//! kernel TCP client ──▶ 198.18.0.2:5201 ──TUN zfcT──▶ proxy process ──▶ 127.0.0.1:5201 kernel server
//! ```
//!
//! The proxy is a separate process (zfstack `--tun-proxy`, or sing-box with a
//! TUN inbound and a direct outbound overriding the destination), so its CPU
//! time and memory are measured the same way for every implementation:
//! utime+stime and VmRSS/VmHWM from /proc/<pid>. `direct` skips the proxy and
//! connects to the server over loopback (kernel-only reference).

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
}

pub fn proxy_name(k: ProxyKind) -> &'static str {
    match k {
        ProxyKind::Direct => "direct",
        ProxyKind::Zfstack => "zfstack",
        ProxyKind::ZfstackClient => "zfstack-client",
        ProxyKind::SingboxGo => "singbox-go",
        ProxyKind::SingboxGvisor => "singbox-gvisor",
        ProxyKind::SingboxSystem => "singbox-system",
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

fn spawn_proxy(a: &Args, kind: ProxyKind) -> io::Result<Option<Child>> {
    tun::sh_quiet(&format!("ip link del {PROXY_TUN}"));
    let mtu = a.tun_mtu;
    let child = match kind {
        ProxyKind::Direct => return Ok(None),
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
            c.stdin(Stdio::null()).spawn()?
        }
        ProxyKind::SingboxGo | ProxyKind::SingboxGvisor | ProxyKind::SingboxSystem => {
            let stack = match kind {
                ProxyKind::SingboxGo => "go",
                ProxyKind::SingboxGvisor => "gvisor",
                _ => "system",
            };
            let path = std::env::temp_dir().join(format!("zfbench-singbox-{}.json", std::process::id()));
            std::fs::write(&path, singbox_config(stack, mtu))?;
            let bin = a.singbox.clone().unwrap_or_else(|| "sing-box".into());
            let mut c = Command::new(bin);
            c.arg("run").arg("-c").arg(&path).stdin(Stdio::null());
            if a.proxy_workers > 0 {
                c.env("GOMAXPROCS", a.proxy_workers.to_string());
            }
            c.spawn()?
        }
    };
    // Wait for the device, then for the proxy to accept a connection.
    let t = Instant::now();
    let target: SocketAddr = TARGET.parse().unwrap();
    loop {
        if t.elapsed() > Duration::from_secs(15) {
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
    Ok(Some(child))
}

fn stop_proxy(child: Option<Child>) {
    if let Some(mut c) = child {
        unsafe { libc::kill(c.id() as i32, libc::SIGTERM) };
        let t = Instant::now();
        while t.elapsed() < Duration::from_secs(3) {
            if let Ok(Some(_)) = c.try_wait() {
                break;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = c.kill();
        let _ = c.wait();
    }
    tun::sh_quiet(&format!("ip link del {PROXY_TUN}"));
}

/// Hold `n` idle connections open (header not sent: the server waits) and
/// report the proxy's memory per connection.
fn test_idle(n: usize, pid: Option<u32>, mark: &mut dyn FnMut(&'static str)) -> client::TestOutcome {
    let target = client::server_addr();
    let before = pid.map(proc_sample);
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
    let connect_secs = t0.elapsed().as_secs_f64();
    // Let the proxy finish dialing upstream and settle.
    std::thread::sleep(Duration::from_secs(2));
    let after = pid.map(proc_sample);
    mark("window_end");
    drop(conns);
    let per_conn = match (before, after) {
        (Some(b), Some(a)) if n > 0 => Some((a.rss_kb as f64 - b.rss_kb as f64) * 1024.0 / n as f64),
        _ => None,
    };
    client::TestOutcome {
        results: json!({
            "conns": n,
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
    let child = spawn_proxy(a, kind)?;
    let pid = child.as_ref().map(|c| c.id());
    std::thread::sleep(Duration::from_millis(300));
    let base = pid.map(proc_sample);

    let snap = || Snap { t: Instant::now(), proxy: pid.map(proc_sample), sys: util::proc_stat(), harness_cpu: util::process_cpu_now() };
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
    let s_begin = snap();
    let outcome = {
        let mut mark = |name: &'static str| marks.push((name, snap()));
        match a.test {
            TestKind::Down => client::test_down(&o, &counters, &mut mark),
            TestKind::Up => client::test_up(&o, &counters, &mut mark),
            TestKind::Mixed => client::test_mixed(&o, &counters, &mut mark),
            TestKind::Connect => client::test_connect(&o, &counters, &mut mark),
            TestKind::Idle => test_idle(a.conns, pid, &mut mark),
        }
    };
    std::thread::sleep(Duration::from_millis(300));
    let s_end = snap();
    let end_sample = pid.map(proc_sample);
    let mut child = child;
    let child_exit = child.as_mut().and_then(|c| c.try_wait().ok().flatten()).map(|st| st.to_string());
    stop_proxy(child);
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
    if matches!(a.test, TestKind::Down | TestKind::Up) && outcome.window_bytes == 0 {
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
            "pid": pid,
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
