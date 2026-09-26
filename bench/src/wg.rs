//! WireGuard pieces of the WG link mode: keys, a single-peer wrapper around
//! boringtun's `Tunn`, batched UDP I/O (recvmmsg / sendmmsg), the userspace
//! TUN <-> UDP bridge and kernel WireGuard interface setup.
//!
//! The server side of a userspace stack does not use a TUN at all: the stack
//! thread owns the UDP socket and the `Tunn`, decrypts into `ingress` and
//! encrypts what `poll` emits (see `stack::run_stack_thread_wg`).

use std::io;
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, UdpSocket};
use std::os::fd::{AsRawFd, RawFd};
use std::sync::atomic::{AtomicBool, Ordering::Relaxed};
use std::sync::Arc;
use std::time::{Duration, Instant};

use boringtun::noise::{Tunn, TunnResult};
use boringtun::x25519::{PublicKey, StaticSecret};

use crate::{tun, util};

/// Inner (tunnel) interface names.
pub const WG_CLIENT_IF: &str = "zfbwg";
pub const WG_SERVER_IF: &str = "zfbwgS";
/// How often boringtun's timers are driven (handshake retry, rekey, keepalive).
pub const TIMER_TICK: Duration = Duration::from_millis(250);
/// WireGuard data overhead: 16 B header + 16 B tag.
pub const WG_OVERHEAD: usize = 32;

// ---------------------------------------------------------------- keys

pub struct KeyPair {
    pub secret: [u8; 32],
    pub public: [u8; 32],
}

pub fn gen_keypair() -> io::Result<KeyPair> {
    let mut secret = [0u8; 32];
    let n = unsafe { libc::getrandom(secret.as_mut_ptr() as *mut _, 32, 0) };
    if n != 32 {
        return Err(io::Error::last_os_error());
    }
    let s = StaticSecret::from(secret);
    Ok(KeyPair { secret: s.to_bytes(), public: *PublicKey::from(&s).as_bytes() })
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

pub fn b64_encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for c in data.chunks(3) {
        let v = (c[0] as u32) << 16 | (*c.get(1).unwrap_or(&0) as u32) << 8 | *c.get(2).unwrap_or(&0) as u32;
        for i in 0..4 {
            if i <= c.len() {
                out.push(B64[(v >> (18 - 6 * i) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}

pub fn b64_decode_key(s: &str) -> io::Result<[u8; 32]> {
    let bad = || io::Error::new(io::ErrorKind::InvalidData, format!("bad base64 key {s:?}"));
    let mut bits = 0u32;
    let mut nbits = 0;
    let mut out = Vec::with_capacity(33);
    for ch in s.trim().trim_end_matches('=').bytes() {
        let v = B64.iter().position(|&b| b == ch).ok_or_else(bad)? as u32;
        bits = bits << 6 | v;
        nbits += 6;
        if nbits >= 8 {
            nbits -= 8;
            out.push((bits >> nbits) as u8);
            bits &= (1 << nbits) - 1;
        }
    }
    out.try_into().map_err(|_| bad())
}

// ---------------------------------------------------------------- UDP batching

pub const BATCH: usize = 64;
const SLOT: usize = 2048;

#[derive(Default, Debug, Clone)]
pub struct UdpStats {
    pub rx_pkts: u64,
    pub rx_bytes: u64,
    pub rx_calls: u64,
    pub tx_pkts: u64,
    pub tx_bytes: u64,
    pub tx_calls: u64,
    pub tx_errors: u64,
    pub tx_eagain_waits: u64,
}

impl UdpStats {
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "rx_pkts": self.rx_pkts, "rx_bytes": self.rx_bytes, "rx_calls": self.rx_calls,
            "tx_pkts": self.tx_pkts, "tx_bytes": self.tx_bytes, "tx_calls": self.tx_calls,
            "tx_errors": self.tx_errors, "tx_eagain_waits": self.tx_eagain_waits,
        })
    }
}

fn to_sockaddr(a: &SocketAddr) -> (libc::sockaddr_storage, libc::socklen_t) {
    let mut ss: libc::sockaddr_storage = unsafe { std::mem::zeroed() };
    let len = match a {
        SocketAddr::V4(v4) => {
            let sin = unsafe { &mut *(&mut ss as *mut _ as *mut libc::sockaddr_in) };
            sin.sin_family = libc::AF_INET as _;
            sin.sin_port = v4.port().to_be();
            sin.sin_addr = libc::in_addr { s_addr: u32::from_ne_bytes(v4.ip().octets()) };
            std::mem::size_of::<libc::sockaddr_in>()
        }
        SocketAddr::V6(v6) => {
            let sin6 = unsafe { &mut *(&mut ss as *mut _ as *mut libc::sockaddr_in6) };
            sin6.sin6_family = libc::AF_INET6 as _;
            sin6.sin6_port = v6.port().to_be();
            sin6.sin6_addr = libc::in6_addr { s6_addr: v6.ip().octets() };
            sin6.sin6_scope_id = v6.scope_id();
            std::mem::size_of::<libc::sockaddr_in6>()
        }
    };
    (ss, len as libc::socklen_t)
}

fn from_sockaddr(ss: &libc::sockaddr_storage) -> Option<SocketAddr> {
    match ss.ss_family as i32 {
        libc::AF_INET => {
            let sin = unsafe { &*(ss as *const _ as *const libc::sockaddr_in) };
            Some(SocketAddr::new(IpAddr::V4(Ipv4Addr::from(sin.sin_addr.s_addr.to_ne_bytes())), u16::from_be(sin.sin_port)))
        }
        libc::AF_INET6 => {
            let sin6 = unsafe { &*(ss as *const _ as *const libc::sockaddr_in6) };
            Some(SocketAddr::new(IpAddr::V6(Ipv6Addr::from(sin6.sin6_addr.s6_addr)), u16::from_be(sin6.sin6_port)))
        }
        _ => None,
    }
}

/// Non-blocking UDP socket with large buffers.
pub fn udp_socket(bind: SocketAddr) -> io::Result<UdpSocket> {
    let s = UdpSocket::bind(bind)?;
    s.set_nonblocking(true)?;
    let sz: libc::c_int = 8 << 20;
    for (force, plain) in [(libc::SO_RCVBUFFORCE, libc::SO_RCVBUF), (libc::SO_SNDBUFFORCE, libc::SO_SNDBUF)] {
        let p = &sz as *const _ as *const libc::c_void;
        let l = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
        if unsafe { libc::setsockopt(s.as_raw_fd(), libc::SOL_SOCKET, force, p, l) } != 0 {
            unsafe { libc::setsockopt(s.as_raw_fd(), libc::SOL_SOCKET, plain, p, l) };
        }
    }
    Ok(s)
}

/// Receive side: up to [`BATCH`] datagrams per `recvmmsg`.
pub struct RxBatch {
    bufs: Vec<u8>,
    lens: Vec<usize>,
    addrs: Vec<libc::sockaddr_storage>,
    n: usize,
}

impl Default for RxBatch {
    fn default() -> Self {
        RxBatch { bufs: vec![0; BATCH * SLOT], lens: vec![0; BATCH], addrs: vec![unsafe { std::mem::zeroed() }; BATCH], n: 0 }
    }
}

impl RxBatch {
    /// One non-blocking `recvmmsg`. Returns the number of datagrams (0 when
    /// the socket is empty).
    pub fn recv(&mut self, fd: RawFd, st: &mut UdpStats) -> usize {
        let mut iov: Vec<libc::iovec> = (0..BATCH).map(|i| libc::iovec { iov_base: self.bufs[i * SLOT..].as_mut_ptr() as *mut _, iov_len: SLOT }).collect();
        let mut msgs: Vec<libc::mmsghdr> = (0..BATCH)
            .map(|i| {
                let mut h: libc::mmsghdr = unsafe { std::mem::zeroed() };
                h.msg_hdr.msg_name = &mut self.addrs[i] as *mut _ as *mut _;
                h.msg_hdr.msg_namelen = std::mem::size_of::<libc::sockaddr_storage>() as _;
                h.msg_hdr.msg_iov = &mut iov[i];
                h.msg_hdr.msg_iovlen = 1;
                h
            })
            .collect();
        let n = unsafe { libc::recvmmsg(fd, msgs.as_mut_ptr(), BATCH as _, libc::MSG_DONTWAIT as _, std::ptr::null_mut()) };
        st.rx_calls += 1;
        self.n = n.max(0) as usize;
        for (i, m) in msgs.iter().enumerate().take(self.n) {
            self.lens[i] = m.msg_len as usize;
            st.rx_pkts += 1;
            st.rx_bytes += m.msg_len as u64;
        }
        self.n
    }

    pub fn get(&self, i: usize) -> (&[u8], Option<SocketAddr>) {
        (&self.bufs[i * SLOT..i * SLOT + self.lens[i]], from_sockaddr(&self.addrs[i]))
    }
}

/// Send side: datagrams queued into one arena, flushed with `sendmmsg`.
#[derive(Default)]
pub struct TxBatch {
    arena: Vec<u8>,
    meta: Vec<(usize, usize)>,
}

impl TxBatch {
    pub fn push(&mut self, d: &[u8]) {
        self.meta.push((self.arena.len(), d.len()));
        self.arena.extend_from_slice(d);
    }

    /// Send everything queued to `dst`, blocking briefly on EAGAIN. Errors
    /// other than EAGAIN drop the datagram and are counted.
    pub fn flush(&mut self, fd: RawFd, dst: SocketAddr, st: &mut UdpStats) {
        if self.meta.is_empty() {
            return;
        }
        let (mut ss, sslen) = to_sockaddr(&dst);
        let mut iov: Vec<libc::iovec> = self.meta.iter().map(|&(o, l)| libc::iovec { iov_base: self.arena[o..].as_mut_ptr() as *mut _, iov_len: l }).collect();
        let mut msgs: Vec<libc::mmsghdr> = iov
            .iter_mut()
            .map(|v| {
                let mut h: libc::mmsghdr = unsafe { std::mem::zeroed() };
                h.msg_hdr.msg_name = &mut ss as *mut _ as *mut _;
                h.msg_hdr.msg_namelen = sslen;
                h.msg_hdr.msg_iov = v;
                h.msg_hdr.msg_iovlen = 1;
                h
            })
            .collect();
        let mut off = 0;
        while off < msgs.len() {
            let chunk = (msgs.len() - off).min(1024);
            let n = unsafe { libc::sendmmsg(fd, msgs[off..].as_mut_ptr(), chunk as _, 0) };
            st.tx_calls += 1;
            if n > 0 {
                for m in &msgs[off..off + n as usize] {
                    st.tx_pkts += 1;
                    st.tx_bytes += m.msg_len as u64;
                }
                off += n as usize;
                continue;
            }
            let e = io::Error::last_os_error();
            if e.kind() == io::ErrorKind::WouldBlock {
                st.tx_eagain_waits += 1;
                let mut pfd = libc::pollfd { fd, events: libc::POLLOUT, revents: 0 };
                unsafe { libc::poll(&mut pfd, 1, 10) };
            } else {
                st.tx_errors += 1;
                off += 1;
            }
        }
        self.arena.clear();
        self.meta.clear();
    }
}

// ---------------------------------------------------------------- peer

#[derive(Default, Debug, Clone)]
pub struct PeerStats {
    /// Data packets decrypted / encrypted.
    pub rx_data: u64,
    pub tx_data: u64,
    /// Time spent inside boringtun, ns (includes handshake work).
    pub decap_ns: u64,
    pub encap_ns: u64,
    pub handshake_pkts_out: u64,
    pub errors: u64,
    /// Packets that could not be sent because the peer's endpoint is unknown.
    pub no_endpoint: u64,
}

impl PeerStats {
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "rx_data": self.rx_data, "tx_data": self.tx_data,
            "decap_sec": self.decap_ns as f64 / 1e9, "encap_sec": self.encap_ns as f64 / 1e9,
            "handshake_pkts_out": self.handshake_pkts_out, "errors": self.errors, "no_endpoint": self.no_endpoint,
        })
    }
}

/// One WireGuard peer. The endpoint is either fixed (client) or learned from
/// the source of authenticated datagrams (server, like WireGuard roaming).
pub struct Peer {
    tunn: Tunn,
    pub endpoint: Option<SocketAddr>,
    buf: Vec<u8>,
    last_tick: Instant,
    pub st: PeerStats,
}

impl Peer {
    pub fn new(me: &KeyPair, peer_public: [u8; 32], endpoint: Option<SocketAddr>, index: u32) -> Self {
        let tunn = Tunn::new(StaticSecret::from(me.secret), PublicKey::from(peer_public), None, None, index, None);
        Peer { tunn, endpoint, buf: vec![0; 65536], last_tick: Instant::now(), st: PeerStats::default() }
    }

    fn out(st: &mut PeerStats, endpoint: Option<SocketAddr>, tx: &mut TxBatch, d: &[u8]) {
        if endpoint.is_some() {
            tx.push(d);
        } else {
            st.no_endpoint += 1;
        }
    }

    /// Encrypt one IP packet for the peer.
    pub fn encap(&mut self, pkt: &[u8], tx: &mut TxBatch) {
        let t = Instant::now();
        match self.tunn.encapsulate(pkt, &mut self.buf) {
            TunnResult::WriteToNetwork(d) => {
                if pkt.len() + WG_OVERHEAD == d.len() {
                    self.st.tx_data += 1;
                } else {
                    // no session yet: the packet was queued and a handshake initiation was produced
                    self.st.handshake_pkts_out += 1;
                }
                Self::out(&mut self.st, self.endpoint, tx, d);
            }
            TunnResult::Err(_) => self.st.errors += 1,
            _ => {}
        }
        self.st.encap_ns += t.elapsed().as_nanos() as u64;
    }

    /// Process one datagram. Decrypted IP packets go to `deliver`; handshake
    /// responses and queued packets go to `tx`.
    pub fn decap(&mut self, from: Option<SocketAddr>, dgram: &[u8], tx: &mut TxBatch, deliver: &mut dyn FnMut(&[u8])) {
        let t = Instant::now();
        let r = self.tunn.decapsulate(from.map(|a| a.ip()), dgram, &mut self.buf);
        // Only boringtun's time is counted, not what `deliver` does with the packet.
        self.st.decap_ns += t.elapsed().as_nanos() as u64;
        match r {
            TunnResult::WriteToTunnelV4(p, _) | TunnResult::WriteToTunnelV6(p, _) => {
                if from.is_some() {
                    self.endpoint = from;
                }
                self.st.rx_data += 1;
                // a zero-length packet is a keepalive
                if !p.is_empty() {
                    deliver(p);
                }
            }
            TunnResult::WriteToNetwork(d) => {
                if from.is_some() {
                    self.endpoint = from;
                }
                self.st.handshake_pkts_out += 1;
                Self::out(&mut self.st, self.endpoint, tx, d);
                // Flush the packets boringtun queued while the handshake was in progress.
                let t = Instant::now();
                while let TunnResult::WriteToNetwork(d) = self.tunn.decapsulate(None, &[], &mut self.buf) {
                    self.st.tx_data += 1;
                    Self::out(&mut self.st, self.endpoint, tx, d);
                }
                self.st.encap_ns += t.elapsed().as_nanos() as u64;
            }
            TunnResult::Err(_) => self.st.errors += 1,
            TunnResult::Done => {}
        }
    }

    pub fn next_tick(&self) -> Instant {
        self.last_tick + TIMER_TICK
    }

    /// Drive boringtun's timers if a tick is due.
    pub fn tick(&mut self, now: Instant, tx: &mut TxBatch) {
        if now < self.next_tick() {
            return;
        }
        self.last_tick = now;
        match self.tunn.update_timers(&mut self.buf) {
            TunnResult::WriteToNetwork(d) => {
                self.st.handshake_pkts_out += 1;
                Self::out(&mut self.st, self.endpoint, tx, d);
            }
            TunnResult::Err(_) => {} // ConnectionExpired etc.: nothing to do for a benchmark peer
            _ => {}
        }
    }
}

// ---------------------------------------------------------------- polling helper

/// Wait until one of `fds` is readable or `until` passes (ns precision).
pub fn wait_readable(fds: &[RawFd], until: Instant) {
    let now = Instant::now();
    if until <= now {
        return;
    }
    let mut pfds: Vec<libc::pollfd> = fds.iter().map(|&fd| libc::pollfd { fd, events: libc::POLLIN, revents: 0 }).collect();
    let ts = util::dur_to_timespec(until - now);
    unsafe { libc::ppoll(pfds.as_mut_ptr(), pfds.len() as _, &ts, std::ptr::null()) };
}

// ---------------------------------------------------------------- userspace bridge

pub struct BridgeResult {
    pub cpu_sec: f64,
    pub peer: PeerStats,
    pub udp: UdpStats,
    pub tun_write_errors: u64,
}

impl BridgeResult {
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "cpu_sec": self.cpu_sec,
            "peer": self.peer.to_json(),
            "udp": self.udp.to_json(),
            "tun_write_errors": self.tun_write_errors,
        })
    }
}

/// Userspace WireGuard endpoint: IP packets read from a TUN are encrypted to
/// the peer, datagrams from the peer are decrypted into the TUN (the job
/// wireguard-go / boringtun-cli do). Used on the client when the kernel has no
/// WireGuard, and for the kernel-TCP baseline on such a server.
pub fn run_bridge(tun_fd: RawFd, udp: UdpSocket, mut peer: Peer, stop: Arc<AtomicBool>) -> BridgeResult {
    util::set_timerslack_ns(1);
    let cpu0 = util::thread_cpu_now();
    let ufd = udp.as_raw_fd();
    let (mut rx, mut tx) = (RxBatch::default(), TxBatch::default());
    let mut ust = UdpStats::default();
    let mut tun_write_errors = 0u64;
    let mut pkt = vec![0u8; 65536];
    // Kick off the handshake right away when we know where the peer is.
    if peer.endpoint.is_some() {
        peer.encap(&[], &mut tx);
    }
    while !stop.load(Relaxed) {
        if let Some(ep) = peer.endpoint {
            tx.flush(ufd, ep, &mut ust);
        }
        wait_readable(&[tun_fd, ufd], peer.next_tick().min(Instant::now() + Duration::from_millis(50)));
        // TUN -> peer
        for _ in 0..BATCH {
            let n = unsafe { libc::read(tun_fd, pkt.as_mut_ptr() as *mut _, pkt.len()) };
            if n <= 0 {
                break;
            }
            peer.encap(&pkt[..n as usize], &mut tx);
        }
        // peer -> TUN
        loop {
            let n = rx.recv(ufd, &mut ust);
            for i in 0..n {
                let (d, from) = rx.get(i);
                peer.decap(from, d, &mut tx, &mut |p| {
                    if unsafe { libc::write(tun_fd, p.as_ptr() as *const _, p.len()) } < 0 {
                        tun_write_errors += 1;
                    }
                });
            }
            if n < BATCH {
                break;
            }
        }
        peer.tick(Instant::now(), &mut tx);
    }
    BridgeResult { cpu_sec: util::thread_cpu_now() - cpu0, peer: peer.st, udp: ust, tun_write_errors }
}

// ---------------------------------------------------------------- interfaces

pub fn kernel_wg_supported() -> bool {
    let probe = "zfbwgprobe";
    let ok = tun::sh(&format!("ip link add {probe} type wireguard")).is_ok();
    if ok {
        tun::sh_quiet(&format!("ip link del {probe}"));
    }
    ok && std::process::Command::new("wg").arg("--version").output().is_ok()
}

/// Kernel WireGuard interface `name` with address `addr`/24, one peer allowed
/// to use `peer_ip`/32.
pub fn kernel_iface(
    name: &str,
    addr: &str,
    me: &KeyPair,
    peer_public: &[u8; 32],
    peer_ip: &str,
    endpoint: Option<SocketAddr>,
    listen_port: Option<u16>,
) -> io::Result<()> {
    tun::sh(&format!("ip link add {name} type wireguard"))?;
    let key_path = format!("/run/zfbench-{name}.key");
    {
        use std::os::unix::fs::OpenOptionsExt;
        let mut f = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&key_path)?;
        io::Write::write_all(&mut f, b64_encode(&me.secret).as_bytes())?;
    }
    let mut cmd = format!("wg set {name} private-key {key_path}");
    if let Some(p) = listen_port {
        cmd += &format!(" listen-port {p}");
    }
    cmd += &format!(" peer {} allowed-ips {peer_ip}/32", b64_encode(peer_public));
    if let Some(ep) = endpoint {
        cmd += &format!(" endpoint {ep}");
    }
    let r = tun::sh(&cmd);
    let _ = std::fs::remove_file(&key_path);
    r?;
    tun::configure_dev("", name, addr)
}

/// TUN interface `name` with address `addr`/24 for a userspace bridge.
pub fn tun_iface(name: &str, addr: &str) -> io::Result<RawFd> {
    let fd = tun::open_tun(name)?;
    tun::configure_dev("", name, addr)?;
    Ok(fd)
}

pub fn cleanup() {
    tun::sh_quiet(&format!("ip link del {WG_CLIENT_IF}"));
    tun::sh_quiet(&format!("ip link del {WG_SERVER_IF}"));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn b64_roundtrip() {
        let k = gen_keypair().unwrap();
        for key in [k.secret, k.public, [0u8; 32], [255u8; 32]] {
            let s = b64_encode(&key);
            assert_eq!(s.len(), 44);
            assert_eq!(b64_decode_key(&s).unwrap(), key);
        }
        assert_eq!(b64_encode(b"foob"), "Zm9vYg==");
    }

    /// Two peers exchange a handshake and one data packet through TxBatch
    /// arenas (no sockets).
    #[test]
    fn peers_handshake_and_carry_data() {
        let (a, b) = (gen_keypair().unwrap(), gen_keypair().unwrap());
        let ea: SocketAddr = "192.0.2.1:1000".parse().unwrap();
        let eb: SocketAddr = "192.0.2.2:2000".parse().unwrap();
        let mut pa = Peer::new(&a, b.public, Some(eb), 1);
        let mut pb = Peer::new(&b, a.public, None, 2);
        // minimal IPv4 header, 10.201.0.1 -> 10.201.0.2
        let mut ip = vec![0u8; 40];
        ip[0] = 0x45;
        ip[2..4].copy_from_slice(&40u16.to_be_bytes());
        ip[8] = 64;
        ip[9] = 6;
        ip[12..16].copy_from_slice(&[10, 201, 0, 1]);
        ip[16..20].copy_from_slice(&[10, 201, 0, 2]);
        let mut got = Vec::new();
        let mut ta = TxBatch::default();
        pa.encap(&ip, &mut ta); // queued + handshake init
        for _ in 0..4 {
            let mut tb = TxBatch::default();
            for &(o, l) in &ta.meta {
                pb.decap(Some(ea), &ta.arena[o..o + l], &mut tb, &mut |p| got.push(p.to_vec()));
            }
            ta = TxBatch::default();
            for &(o, l) in &tb.meta {
                pa.decap(Some(eb), &tb.arena[o..o + l], &mut ta, &mut |_| {});
            }
            if !got.is_empty() {
                break;
            }
        }
        assert_eq!(got, vec![ip]);
        assert_eq!(pb.endpoint, Some(ea));
    }
}
