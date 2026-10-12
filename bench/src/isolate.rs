//! L1 isolate mode: kernel TCP clients, one TUN, many logical zfstack ports.
//!
//! There is no WireGuard on this path. A logical port is an `IfaceId` selected
//! from the IPv4 destination `10.211.<port>.2`. A peer is `10.210.<peer>.1`.
//! The TUN device is not itself an iface: one device can carry every port, and
//! that is a weaker isolation claim than one TUN per port. See harness/README.md.

use std::io::{self, Read, Write};
use std::net::{IpAddr, Ipv4Addr, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering::Relaxed};
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use clap::ValueEnum;
use serde_json::{json, Value};

use crate::app;
use crate::client::{Mark, TestOpts, TestOutcome};
use crate::tun;
use crate::util;

pub const PORTS_MAX: u16 = 128;

#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum Placement {
    /// Heavy and light users share iface 0 and differ by peer id.
    SameIface,
    /// Heavy is iface 0; each light user is another iface of the same shard.
    CrossIface,
    /// Same port split as cross-iface, but one shard per port.
    CrossShard,
}

impl Placement {
    pub fn as_str(self) -> &'static str {
        match self {
            Placement::SameIface => "same-iface",
            Placement::CrossIface => "cross-iface",
            Placement::CrossShard => "cross-shard",
        }
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, ValueEnum)]
pub enum Topology {
    /// All logical ports are ifaces of one shard (docs/design/0001).
    SharedShard,
    /// One shard per logical port, one shared global budget, one driver thread.
    PerPortShard,
}

impl Topology {
    pub fn as_str(self) -> &'static str {
        match self {
            Topology::SharedShard => "shared-shard",
            Topology::PerPortShard => "per-port-shard",
        }
    }
}

#[derive(Clone, Debug)]
pub struct IsolatePlan {
    pub ports: u16,
    pub per_port_shard: bool,
}

#[derive(Clone, Debug)]
pub struct Iso {
    pub ports: u16,
    pub lights: u16,
    pub heavy_conns: u16,
    pub placement: Placement,
    pub topology: Topology,
}

impl Iso {
    pub fn plan(&self) -> IsolatePlan {
        IsolatePlan { ports: self.ports, per_port_shard: self.topology == Topology::PerPortShard }
    }
}

pub fn resolve(ports: u16, lights: u16, heavy_conns: u16, placement: Placement, topology: Topology) -> Result<Iso, String> {
    if ports == 0 || ports > PORTS_MAX {
        return Err(format!("--ports must be 1..={PORTS_MAX}"));
    }
    if lights > 200 {
        return Err("--lights must be <= 200".into());
    }
    if heavy_conns > 256 {
        return Err("--heavy-conns must be <= 256".into());
    }
    // Peer ids are the third octet of 10.210.<peer>.1. Peer 1 is the heavy user.
    if u32::from(lights) + 2 > 255 {
        return Err("not enough IPv4 peer addresses in 10.210.0.0/16".into());
    }
    match (placement, topology) {
        (Placement::CrossIface, _) if ports < 2 => return Err("cross-iface placement needs --ports >= 2".into()),
        (Placement::CrossIface, Topology::PerPortShard) => {
            return Err("cross-iface stays on one shard; use --placement cross-shard --topology per-port-shard".into());
        }
        (Placement::CrossShard, _) if ports < 2 => return Err("cross-shard placement needs --ports >= 2".into()),
        (Placement::CrossShard, Topology::SharedShard) => {
            return Err("cross-shard placement needs --topology per-port-shard".into());
        }
        _ => {}
    }
    Ok(Iso { ports, lights, heavy_conns, placement, topology })
}

pub fn server_ip(port: u16) -> Ipv4Addr {
    Ipv4Addr::new(10, 211, port as u8, 2)
}

pub fn client_ip(peer: u16) -> Ipv4Addr {
    Ipv4Addr::new(10, 210, peer as u8, 1)
}

/// `(port, peer)` from a 10.210/10.211 isolate address pair. `None` if the
/// packet is not part of this mapping (the legacy single-address bench).
pub fn classify(src: [u8; 4], dst: [u8; 4]) -> Option<(u16, u64)> {
    if src[0] != 10 || src[1] != 210 || dst[0] != 10 || dst[1] != 211 || dst[3] != 2 {
        return None;
    }
    Some((u16::from(dst[2]), u64::from(src[2])))
}

/// Parse an IP packet and classify it. Invalid or non-isolate packets are `None`
/// and are counted as demux drops rather than delivered to iface 0.
#[cfg(feature = "zfstack")]
pub fn classify_packet(pkt: &[u8]) -> Option<(u16, u64)> {
    let ip = zfstack::wire::parse_ip(pkt).ok()?;
    let (IpAddr::V4(src), IpAddr::V4(dst)) = (ip.src, ip.dst) else { return None };
    classify(src.octets(), dst.octets())
}

pub fn heavy_user() -> (u16, u16) {
    (0, 1)
}

pub fn light_user(index: u16, iso: &Iso) -> (u16, u16) {
    let peer = 2 + index;
    let port = match iso.placement {
        Placement::SameIface => 0,
        Placement::CrossIface | Placement::CrossShard => 1 + (index % (iso.ports - 1)),
    };
    (port, peer)
}

/// Install one local address per peer and a route toward every logical server.
///
/// A connected /16 does not make `bind(10.210.<peer>.1)` succeed, so each peer
/// address is installed as a /32. The device itself is created by `tun::setup_a`;
/// deleting `zfbA` removes the addresses and the route.
pub fn setup_routes(iso: &Iso) -> io::Result<()> {
    tun::sh(&format!("ip route replace 10.211.0.0/16 dev {}", tun::TUN_A))?;
    let mut peers = vec![heavy_user().1];
    for i in 0..iso.lights {
        peers.push(light_user(i, iso).1);
    }
    peers.sort_unstable();
    peers.dedup();
    for peer in peers {
        let ip = client_ip(peer);
        tun::sh(&format!("ip addr replace {ip}/32 dev {}", tun::TUN_A))?;
    }
    Ok(())
}

pub fn test_isolate(o: &TestOpts, iso: &Iso, mark: Mark) -> TestOutcome {
    let mut errors = Vec::new();
    let msg = app::pattern_at(7, o.rr_size.min(app::PAT_CHUNK)).to_vec();
    let baseline = if o.idle_rr_secs > 0 { run_lights(o, iso, &msg, o.idle_rr_secs, 0, &mut errors) } else { Vec::new() };

    let heavy_bytes = Arc::new(AtomicU64::new(0));
    let stop = Arc::new(AtomicBool::new(false));
    let mut heavy_handles: Vec<JoinHandle<Vec<String>>> = Vec::new();
    let (hport, hpeer) = heavy_user();
    for i in 0..iso.heavy_conns {
        match connect_bound(o, hpeer, hport) {
            Ok(mut s) => {
                if let Err(e) = s.write_all(&app::encode_header(app::CMD_DOWN, i as u64)) {
                    errors.push(format!("heavy {i}: header: {e}"));
                    continue;
                }
                let (stop, ctr) = (stop.clone(), heavy_bytes.clone());
                heavy_handles.push(std::thread::spawn(move || heavy_read(s, stop, ctr)));
            }
            Err(e) => errors.push(format!("heavy {i}: connect: {e}")),
        }
    }
    // Heavies run alone through warmup so their congestion window is up before
    // the measured interval. Connecting the lights is a separate phase: it is
    // not part of the goodput denominator.
    if o.warmup > 0 {
        std::thread::sleep(Duration::from_secs(o.warmup));
    }
    let t_connect = Instant::now();
    let mut connected = Vec::new();
    for i in 0..iso.lights {
        let (port, peer) = light_user(i, iso);
        match connect_bound(o, peer, port) {
            Ok(mut s) => {
                if let Err(e) = s.write_all(&app::encode_header(app::CMD_ECHO, msg.len() as u64)) {
                    errors.push(format!("light {i} loaded port {port} peer {peer}: header: {e}"));
                    connected.push(None);
                } else {
                    connected.push(Some(s));
                }
            }
            Err(e) => {
                errors.push(format!("light {i} loaded port {port} peer {peer}: connect: {e}"));
                connected.push(None);
            }
        }
    }
    let connect_phase_sec = t_connect.elapsed().as_secs_f64();
    let measure = o.secs.saturating_sub(o.warmup).max(1);
    let window_start = Instant::now();
    let window_end_at = window_start + Duration::from_secs(measure);
    let bytes_at_window = heavy_bytes.load(Relaxed);
    mark("window_start");
    let loaded_handles = spawn_loaded(&msg, o.rr_interval_ms, window_end_at, connected);
    let mut loaded = Vec::new();
    for (i, h) in loaded_handles.into_iter().enumerate() {
        let i = i as u16;
        let (port, peer) = light_user(i, iso);
        match h.join() {
            Ok((samples, errs)) => {
                for e in errs {
                    errors.push(format!("light {i} loaded port {port} peer {peer}: {e}"));
                }
                loaded.push(UserRow { index: i, port, peer, samples });
            }
            Err(_) => errors.push(format!("light {i} loaded: thread panicked")),
        }
    }
    // Lights may be absent or exit early. Their lifetime cannot shorten the
    // configured heavy-only measurement window.
    std::thread::sleep(window_end_at.saturating_duration_since(Instant::now()));
    let window_secs = window_start.elapsed().as_secs_f64().max(1e-6);
    let bytes_at_end = heavy_bytes.load(Relaxed);
    mark("window_end");
    stop.store(true, Relaxed);
    for (i, h) in heavy_handles.into_iter().enumerate() {
        match h.join() {
            Ok(errs) => {
                for e in errs {
                    errors.push(format!("heavy {i}: {e}"));
                }
            }
            Err(_) => errors.push(format!("heavy {i}: thread panicked")),
        }
    }
    let window_bytes = bytes_at_end.saturating_sub(bytes_at_window);
    let deltas = loaded.iter().map(|row| {
        let base = baseline.iter().find(|b| b.index == row.index);
        let b50 = base.and_then(|b| pct(&b.samples, 50.0));
        let l50 = pct(&row.samples, 50.0);
        json!({
            "user": row.index,
            "port": row.port,
            "peer": row.peer,
            "baseline_p50_ms": b50,
            "loaded_p50_ms": l50,
            "delta_p50_ms": match (b50, l50) {
                (Some(a), Some(b)) => Some(b - a),
                _ => None,
            },
            "loaded_n": row.samples.len(),
        })
    });
    let results = json!({
        "test": "isolate",
        "layer": "L1",
        "wireguard": false,
        "clock": "wall",
        "topology": iso.topology.as_str(),
        "placement": iso.placement.as_str(),
        "ports": iso.ports,
        "lights": iso.lights,
        "heavy_conns": iso.heavy_conns,
        "heavy_port": hport,
        "heavy_peer": hpeer,
        "mapping": "destination 10.211.<port>.2 selects IfaceId; source 10.210.<peer>.1 selects PeerId. One TUN carries every port; that is not one TUN device per port.",
        "scheduler": if iso.topology == Topology::PerPortShard {
            "one thread co-schedules one shard per port; they share a GlobalBudget and are not separate cores"
        } else {
            "one shard, one thread, one iface per logical port"
        },
        "comparability": "L1 wall-clock on this TUN path is not L0 virtual time and not an L2 WireGuard measurement. Kernel and smoltcp are not in this mode: their adapters are single-homed, so a 'faster than kernel' claim is not available here.",
        "heavy": {
            "window_bytes": window_bytes,
            "window_nominal_sec": measure,
            "window_wall_sec": window_secs,
            "connect_phase_sec": connect_phase_sec,
            "goodput_mbps_window": window_bytes as f64 * 8.0 / 1e6 / window_secs,
            "goodput_note": "window_wall_sec is the measured Instant interval after connects completed, not secs-warmup",
        },
        "baseline_users": baseline.iter().map(user_json).collect::<Vec<_>>(),
        "loaded_users": loaded.iter().map(user_json).collect::<Vec<_>>(),
        "latency_delta_ms": deltas.collect::<Vec<_>>(),
    });
    TestOutcome { results, errors, window_bytes, window_secs }
}

fn spawn_loaded(msg: &[u8], interval_ms: u64, end: Instant, streams: Vec<Option<TcpStream>>) -> Vec<JoinHandle<(Vec<f64>, Vec<String>)>> {
    let interval = Duration::from_millis(interval_ms.max(1));
    streams
        .into_iter()
        .map(|s| {
            let msg = msg.to_vec();
            std::thread::spawn(move || match s {
                Some(s) => light_until(s, &msg, interval, end),
                None => (Vec::new(), Vec::new()),
            })
        })
        .collect()
}

fn spawn_lights(o: &TestOpts, iso: &Iso, msg: &[u8], secs: u64, warmup: u64) -> Vec<JoinHandle<(Vec<f64>, Vec<String>)>> {
    (0..iso.lights)
        .map(|i| {
            let (port, peer) = light_user(i, iso);
            let msg = msg.to_vec();
            let interval = Duration::from_millis(o.rr_interval_ms.max(1));
            let cc = o.client_cc.clone();
            let timeout = o.connect_timeout;
            std::thread::spawn(move || {
                let mut errors = Vec::new();
                let s = match connect_bound_cc(peer, port, timeout, cc.as_deref()) {
                    Ok(mut s) => {
                        if let Err(e) = s.write_all(&app::encode_header(app::CMD_ECHO, msg.len() as u64)) {
                            return (Vec::new(), vec![format!("header: {e}")]);
                        }
                        s
                    }
                    Err(e) => return (Vec::new(), vec![format!("connect: {e}")]),
                };
                let start = Instant::now();
                let end = start + Duration::from_secs(secs.max(1));
                let warmup_until = start + Duration::from_secs(warmup);
                let (samples, errs) = light_loop(s, &msg, interval, end, warmup_until);
                errors.extend(errs);
                (samples, errors)
            })
        })
        .collect()
}

fn run_lights(o: &TestOpts, iso: &Iso, msg: &[u8], secs: u64, warmup: u64, errors: &mut Vec<String>) -> Vec<UserRow> {
    let handles = spawn_lights(o, iso, msg, secs, warmup);
    let mut rows = Vec::new();
    for (i, h) in handles.into_iter().enumerate() {
        let i = i as u16;
        let (port, peer) = light_user(i, iso);
        match h.join() {
            Ok((samples, errs)) => {
                for e in errs {
                    errors.push(format!("light {i} baseline port {port} peer {peer}: {e}"));
                }
                rows.push(UserRow { index: i, port, peer, samples });
            }
            Err(_) => errors.push(format!("light {i} baseline: thread panicked")),
        }
    }
    rows
}

struct UserRow {
    index: u16,
    port: u16,
    peer: u16,
    samples: Vec<f64>,
}

fn connect_bound(o: &TestOpts, peer: u16, port: u16) -> io::Result<TcpStream> {
    connect_bound_cc(peer, port, o.connect_timeout, o.client_cc.as_deref())
}

fn connect_bound_cc(peer: u16, port: u16, timeout: Duration, cc: Option<&str>) -> io::Result<TcpStream> {
    // Bind the source before connect so the stack can tell peers apart. std has
    // no stable TcpSocket here, so this is a short non-blocking connect.
    let fd = unsafe { libc::socket(libc::AF_INET, libc::SOCK_STREAM | libc::SOCK_CLOEXEC, 0) };
    if fd < 0 {
        return Err(io::Error::last_os_error());
    }
    let fail = |fd| {
        let e = io::Error::last_os_error();
        unsafe { libc::close(fd) };
        e
    };
    let nodelay: libc::c_int = 1;
    unsafe {
        libc::setsockopt(fd, libc::IPPROTO_TCP, libc::TCP_NODELAY, &nodelay as *const _ as *const _, std::mem::size_of_val(&nodelay) as _);
    }
    let src = sockaddr_v4(client_ip(peer), 0);
    if unsafe { libc::bind(fd, &src as *const _ as *const _, std::mem::size_of_val(&src) as _) } < 0 {
        return Err(fail(fd));
    }
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err(fail(fd));
    }
    let dst = sockaddr_v4(server_ip(port), tun::SERVER_PORT);
    let rc = unsafe { libc::connect(fd, &dst as *const _ as *const _, std::mem::size_of_val(&dst) as _) };
    if rc < 0 {
        let err = io::Error::last_os_error();
        if err.raw_os_error() != Some(libc::EINPROGRESS) && err.kind() != io::ErrorKind::WouldBlock {
            unsafe { libc::close(fd) };
            return Err(err);
        }
        let mut pfd = libc::pollfd { fd, events: libc::POLLOUT, revents: 0 };
        let ms = timeout.as_millis().min(i32::MAX as u128) as libc::c_int;
        let pr = unsafe { libc::poll(&mut pfd, 1, ms) };
        if pr <= 0 {
            unsafe { libc::close(fd) };
            return Err(if pr == 0 { io::Error::new(io::ErrorKind::TimedOut, "connect timed out") } else { io::Error::last_os_error() });
        }
        let mut soerr: libc::c_int = 0;
        let mut len = std::mem::size_of_val(&soerr) as libc::socklen_t;
        unsafe { libc::getsockopt(fd, libc::SOL_SOCKET, libc::SO_ERROR, &mut soerr as *mut _ as *mut _, &mut len) };
        if soerr != 0 {
            unsafe { libc::close(fd) };
            return Err(io::Error::from_raw_os_error(soerr));
        }
    }
    if unsafe { libc::fcntl(fd, libc::F_SETFL, flags) } < 0 {
        return Err(fail(fd));
    }
    let s = unsafe { TcpStream::from_raw_fd(fd) };
    if let Some(cc) = cc {
        util::set_tcp_cc(s.as_raw_fd(), cc)?;
    }
    Ok(s)
}

fn sockaddr_v4(ip: Ipv4Addr, port: u16) -> libc::sockaddr_in {
    libc::sockaddr_in {
        sin_family: libc::AF_INET as libc::sa_family_t,
        sin_port: port.to_be(),
        // `s_addr` is already network-order bytes. On little-endian, the octets
        // in memory must be the address itself, which is what `from_ne_bytes` stores.
        sin_addr: libc::in_addr { s_addr: u32::from_ne_bytes(ip.octets()) },
        sin_zero: [0; 8],
    }
}

fn light_until(s: TcpStream, msg: &[u8], interval: Duration, end: Instant) -> (Vec<f64>, Vec<String>) {
    light_loop(s, msg, interval, end, Instant::now())
}

fn light_loop(mut s: TcpStream, msg: &[u8], interval: Duration, end: Instant, warmup_until: Instant) -> (Vec<f64>, Vec<String>) {
    let mut rbuf = vec![0u8; msg.len()];
    let mut samples = Vec::new();
    let mut errors = Vec::new();
    s.set_read_timeout(Some(Duration::from_secs(5))).ok();
    s.set_write_timeout(Some(Duration::from_secs(5))).ok();
    let mut next = Instant::now();
    while Instant::now() < end {
        if next > Instant::now() {
            std::thread::sleep(next - Instant::now());
        }
        if Instant::now() >= end {
            break;
        }
        match rr_once(&mut s, msg, &mut rbuf) {
            Ok(ms) if Instant::now() >= warmup_until => samples.push(ms),
            Ok(_) => {}
            Err(e) => {
                errors.push(e.to_string());
                break;
            }
        }
        next = (next + interval).max(Instant::now());
    }
    (samples, errors)
}

fn rr_once(s: &mut TcpStream, msg: &[u8], rbuf: &mut [u8]) -> io::Result<f64> {
    let t = Instant::now();
    s.write_all(msg)?;
    s.read_exact(rbuf)?;
    if rbuf != msg {
        return Err(io::Error::other("echo mismatch"));
    }
    Ok(t.elapsed().as_secs_f64() * 1e3)
}

fn heavy_read(mut s: TcpStream, stop: Arc<AtomicBool>, ctr: Arc<AtomicU64>) -> Vec<String> {
    let mut buf = vec![0u8; 256 * 1024];
    let mut pos = 0u64;
    let mut errors = Vec::new();
    let mut mismatches = 0u64;
    s.set_read_timeout(Some(Duration::from_millis(50))).ok();
    while !stop.load(Relaxed) {
        match s.read(&mut buf) {
            Ok(0) => {
                errors.push("download: unexpected EOF".into());
                break;
            }
            Ok(n) => {
                if !app::verify_pattern(pos, &buf[..n]) {
                    mismatches += 1;
                }
                pos += n as u64;
                ctr.fetch_add(n as u64, Relaxed);
            }
            Err(e) if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => {}
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => {
                errors.push(format!("download: {e}"));
                break;
            }
        }
    }
    if mismatches > 0 {
        errors.push(format!("download: {mismatches} pattern mismatches"));
    }
    errors
}

fn pct(samples: &[f64], p: f64) -> Option<f64> {
    let mut s = samples.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    util::percentile(&s, p)
}

fn lat_json(samples: &[f64]) -> Value {
    let mut v = util::lat_summary(samples);
    if let Some(obj) = v.as_object_mut() {
        // A handful of exchanges is not a tail. Keep the sample count and drop
        // the percentile that the count cannot support.
        if samples.len() < 100 {
            obj.insert("p99_ms".into(), Value::Null);
            obj.insert("p99_note".into(), json!(format!("omitted: {} < 100 samples", samples.len())));
        }
        if samples.len() < 1000 {
            obj.insert("p999_ms".into(), Value::Null);
            obj.insert("p999_note".into(), json!(format!("omitted: {} < 1000 samples", samples.len())));
        }
    }
    v
}

fn user_json(row: &UserRow) -> Value {
    json!({
        "user": row.index,
        "port": row.port,
        "peer": row.peer,
        "class": "light",
        "samples": lat_json(&row.samples),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn measurement_window_does_not_depend_on_light_users() {
        let o = TestOpts {
            secs: 1,
            warmup: 0,
            flows: 1,
            rr_size: 256,
            rr_interval_ms: 20,
            idle_rr_secs: 0,
            conns: 1,
            concurrency: 1,
            connect_timeout: Duration::from_secs(1),
            client_cc: None,
        };
        let iso = resolve(1, 0, 0, Placement::SameIface, Topology::SharedShard).unwrap();
        let out = test_isolate(&o, &iso, &mut |_| {});
        assert!(out.errors.is_empty(), "{:?}", out.errors);
        assert!(out.window_secs >= 1.0, "window was {} seconds", out.window_secs);
    }

    #[test]
    fn classify_isolate_addresses() {
        assert_eq!(classify([10, 210, 3, 1], [10, 211, 4, 2]), Some((4, 3)));
        assert_eq!(classify([10, 201, 0, 1], [10, 201, 0, 2]), None);
        assert_eq!(classify([10, 210, 1, 1], [10, 211, 0, 1]), None);
    }

    #[test]
    #[cfg(feature = "zfstack")]
    fn classify_packet_matches_header() {
        let pkt = ipv4([10, 210, 5, 1], [10, 211, 7, 2]);
        assert_eq!(classify_packet(&pkt), Some((7, 5)));
        let legacy = ipv4([10, 201, 0, 1], [10, 201, 0, 2]);
        assert_eq!(classify_packet(&legacy), None);
    }

    #[test]
    fn cross_iface_lights_leave_port_zero() {
        let iso = resolve(4, 3, 1, Placement::CrossIface, Topology::SharedShard).unwrap();
        assert_eq!(heavy_user(), (0, 1));
        assert_eq!(light_user(0, &iso), (1, 2));
        assert_eq!(light_user(1, &iso), (2, 3));
        assert_eq!(light_user(2, &iso), (3, 4));
        assert!(resolve(1, 1, 1, Placement::CrossIface, Topology::SharedShard).is_err());
        assert!(resolve(4, 1, 1, Placement::CrossShard, Topology::SharedShard).is_err());
        assert!(resolve(4, 1, 1, Placement::CrossShard, Topology::PerPortShard).is_ok());
    }

    fn ipv4(src: [u8; 4], dst: [u8; 4]) -> Vec<u8> {
        let mut p = vec![0u8; 20];
        p[0] = 0x45;
        p[3] = 20;
        p[8] = 64;
        p[9] = 6;
        p[12..16].copy_from_slice(&src);
        p[16..20].copy_from_slice(&dst);
        let sum = zfstack::wire::checksum_fold(zfstack::wire::sum_bytes(0, &p));
        p[10..12].copy_from_slice(&sum.to_be_bytes());
        p
    }
}
