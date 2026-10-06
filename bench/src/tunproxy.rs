//! Client-scenario proxy process (`--tun-proxy`): a TUN device terminated by
//! zfstack, every accepted connection relayed to a fixed upstream address with
//! the kernel's TCP (like a proxy client's TUN inbound + direct outbound).
//!
//! ```text
//! app (kernel TCP) ──TUN zfcT──▶ reader thread ──▶ zfstack driver task ──▶ relay (task or splice) ──▶ kernel TCP upstream
//!                  ◀── writev ──────────────────── egress callback ◀──────
//! ```
//!
//! `--vnet-hdr` opens the TUN with `IFF_VNET_HDR` and `TUN_F_CSUM`, so neither
//! the kernel nor zfstack sums payload bytes on this hop (zfstack::offload).

use std::io;
use std::net::SocketAddr;
use std::os::fd::RawFd;

use tokio::io::AsyncWriteExt;
use zfstack::offload::{VirtioNetHdr, VIRTIO_NET_HDR_LEN};
use zfstack::pktpool::{PacketBuf, PacketPool};
use zfstack::tokio_adapter::{self, ResourceLimits, StreamConfig};
use zfstack::{IfaceConfig, IfaceId, OutPacket, PeerId, RxChecksum, SendResult, StackConfig};

use crate::tun;

pub const PROXY_TUN: &str = "zfcT";
pub const PROXY_TUN_ADDR: &str = "172.19.0.1/30";
/// Destinations routed into the TUN (outside the device subnet: sing-box answers
/// DNS on the device's next address).
pub const PROXY_ROUTE: &str = "198.18.0.0/15";

const IFF_VNET_HDR: libc::c_short = 0x4000;
const TUNSETOFFLOAD: libc::c_ulong = 0x4004_54d0;
const TUN_F_CSUM: libc::c_uint = 0x01;
const TUN_F_TSO4: libc::c_uint = 0x02;
const TUN_F_TSO6: libc::c_uint = 0x04;

/// Ingress batches handed to the driver are bounded in bytes, so a slow driver
/// pushes back on the reader (and the TUN queue) instead of buffering packets.
const BATCH_BYTES: usize = 512 * 1024;
const BATCH_QUEUE: usize = 4;

pub struct ProxyOpts {
    pub mtu: u16,
    pub upstream: SocketAddr,
    pub workers: usize,
    pub relay_buf: usize,
    pub client_profile: bool,
    /// Relay in the stack driver (`TcpStream::splice`) instead of a copy task.
    pub splice: bool,
    /// virtio-net header + checksum offload (+ TSO receive when `tso`).
    pub vnet_hdr: bool,
    pub tso: bool,
    /// Run the stack driver on its own current-thread runtime.
    pub driver_thread: bool,
}

fn write_packet(fd: RawFd, p: &OutPacket<'_>, vnet: bool) -> SendResult {
    let vh = VirtioNetHdr::for_packet(p).encode();
    let mut iov = [libc::iovec { iov_base: std::ptr::null_mut(), iov_len: 0 }; 4];
    let mut n = 0;
    let parts: [&[u8]; 4] = [if vnet { &vh[..] } else { &[] }, p.header, p.payload[0], p.payload[1]];
    for part in parts {
        if !part.is_empty() {
            iov[n] = libc::iovec { iov_base: part.as_ptr() as *mut _, iov_len: part.len() };
            n += 1;
        }
    }
    loop {
        let rc = unsafe { libc::writev(fd, iov.as_ptr(), n as i32) };
        if rc >= 0 {
            return SendResult::Accepted;
        }
        match io::Error::last_os_error().raw_os_error() {
            Some(libc::EINTR) => continue,
            // A TUN write only fails like this when the device is going away
            // or the kernel backlog overflows; drop like a lossy link.
            _ => return SendResult::Accepted,
        }
    }
}

type Batch = Vec<(PacketBuf, usize, RxChecksum)>;

/// Idle packet buffers kept (and charged) by the reader's pool.
const POOL_CACHED: usize = 128;

/// Blocking TUN reader: reads each packet straight into a pooled, charged
/// buffer (zero-copy ingress) and batches them for the driver.
fn reader(fd: RawFd, pool: PacketPool, vnet: bool, trust: bool, tx: tokio::sync::mpsc::Sender<Batch>) {
    let start = if vnet { VIRTIO_NET_HDR_LEN } else { 0 };
    let mut spare: Option<PacketBuf> = None;
    loop {
        let mut pfd = libc::pollfd { fd, events: libc::POLLIN, revents: 0 };
        let rc = unsafe { libc::poll(&mut pfd, 1, 1000) };
        if rc < 0 {
            if io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return;
        }
        let mut batch = Vec::with_capacity(16);
        let mut bytes = 0;
        while bytes < BATCH_BYTES && batch.len() < 256 {
            let mut b = match spare.take().or_else(|| pool.get()) {
                Some(b) => b,
                None => {
                    // Out of budget: let the driver drain before reading on.
                    std::thread::sleep(std::time::Duration::from_millis(1));
                    break;
                }
            };
            let room = b.spare_capacity_mut();
            let n = unsafe { libc::read(fd, room.as_mut_ptr() as *mut _, room.len()) };
            if n < 0 {
                spare = Some(b);
                match io::Error::last_os_error().raw_os_error() {
                    Some(libc::EAGAIN) => break,
                    Some(libc::EINTR) => continue,
                    _ => return,
                }
            }
            if n == 0 {
                return;
            }
            unsafe { b.set_len(n as usize) };
            let mut csum = if trust { RxChecksum::Trusted } else { RxChecksum::Verify };
            if vnet {
                let Some(h) = VirtioNetHdr::decode(&b) else { continue };
                csum = h.rx_checksum();
            }
            bytes += b.len();
            batch.push((b, start, csum));
        }
        if !batch.is_empty() && tx.blocking_send(batch).is_err() {
            return;
        }
    }
}

pub fn client_config(profile: bool) -> StackConfig {
    if profile {
        StackConfig::client()
    } else {
        StackConfig::default()
    }
}

fn open_proxy_tun(vnet: bool, tso: bool) -> io::Result<RawFd> {
    if !vnet {
        return tun::open_tun(PROXY_TUN);
    }
    let fd = tun::open_tun_flags(PROXY_TUN, IFF_VNET_HDR)?;
    let mut flags = TUN_F_CSUM;
    if tso {
        flags |= TUN_F_TSO4 | TUN_F_TSO6;
    }
    if unsafe { libc::ioctl(fd, TUNSETOFFLOAD as _, flags as libc::c_ulong) } < 0 {
        let e = io::Error::last_os_error();
        unsafe { libc::close(fd) };
        return Err(e);
    }
    Ok(fd)
}

struct Stack {
    acceptor: tokio_adapter::Acceptor,
    // Dropping these stops the driver.
    _handle: tokio_adapter::StackHandle,
    _task: tokio_adapter::DriverTask,
}

/// Spawn the stack driver on the current runtime and start the TUN reader.
fn start_stack(o: &ProxyOpts, fd: RawFd) -> Stack {
    let (mtu, vnet, tso) = (o.mtu, o.vnet_hdr, o.tso);
    // TSO super-segments can exceed the MTU (up to 64 KiB of IP packet).
    let max_pkt = if tso { 65535 } else { mtu as usize };
    // Without a vnet header the client profile trusts the local kernel's
    // checksums like sing-box does; with one, the header says per packet.
    let trust = o.client_profile && !vnet;
    let (tx, rx) = tokio::sync::mpsc::channel::<Batch>(BATCH_QUEUE);
    let global = zfstack::budget::GlobalBudget::from_system();
    let high = global.high();
    let limits = ResourceLimits { port_bytes: high, peer_bytes: high, peer_max_connections: u32::MAX };
    let egress = move |_: IfaceId, _: PeerId, p: &OutPacket<'_>| write_packet(fd, p, vnet);
    let (handle, acceptor, ids, task, memory) = tokio_adapter::spawn_with_source_factory(
        client_config(o.client_profile),
        StreamConfig::default(),
        vec![IfaceConfig { mtu }],
        move |shard| (egress, shard.memory_handle(PeerId(0))),
        global,
        limits,
        rx,
        move |shard, now, batch: Batch| {
            for (b, start, csum) in batch {
                shard.ingress_buf(now, IfaceId(0), PeerId(0), b, start, csum);
            }
        },
    );
    let pool = PacketPool::new(memory, max_pkt + if vnet { VIRTIO_NET_HDR_LEN } else { 0 }, POOL_CACHED);
    assert_eq!(ids[0], IfaceId(0));
    if vnet {
        handle.set_iface_tx_checksum_offload(ids[0], true);
    }
    if tso {
        handle.set_iface_tso(ids[0], 65_000);
    }
    std::thread::Builder::new().name("zfc-tun-rx".into()).spawn(move || reader(fd, pool, vnet, trust, tx)).unwrap();
    Stack { acceptor, _handle: handle, _task: task }
}

pub fn run(o: ProxyOpts) -> io::Result<()> {
    tun::sh_quiet(&format!("ip link del {PROXY_TUN}"));
    let fd = open_proxy_tun(o.vnet_hdr, o.tso)?;
    tun::sh(&format!("ip addr add {PROXY_TUN_ADDR} dev {PROXY_TUN}"))?;
    tun::sh(&format!("ip link set {PROXY_TUN} mtu {} txqueuelen 500 up", o.mtu))?;
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(o.workers).enable_all().build()?;
    let (upstream, relay_buf, splice) = (o.upstream, o.relay_buf, o.splice);
    let stack = if o.driver_thread {
        // The driver gets a current-thread runtime of its own; spliced
        // sockets move to its reactor, so relay I/O never leaves that thread.
        let (stx, srx) = std::sync::mpsc::channel();
        std::thread::Builder::new().name("zfc-driver".into()).spawn(move || {
            let drt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
            drt.block_on(async move {
                let stack = start_stack(&o, fd);
                let _ = stx.send(stack);
                std::future::pending::<()>().await
            })
        })?;
        srx.recv().map_err(|_| io::Error::other("driver thread failed"))?
    } else {
        let _g = rt.enter();
        start_stack(&o, fd)
    };
    let mut acceptor = stack.acceptor;
    eprintln!("zfbench tun-proxy: ready on {PROXY_TUN} splice {splice} -> {upstream}");
    rt.block_on(async move {
        while let Some(mut down) = acceptor.accept().await {
            tokio::spawn(async move {
                let Ok(mut up) = tokio::net::TcpStream::connect(upstream).await else {
                    let _ = down.shutdown().await;
                    return;
                };
                let _ = up.set_nodelay(true);
                if splice {
                    let _ = down.splice(up).await;
                } else {
                    let _ = tokio::io::copy_bidirectional_with_sizes(&mut down, &mut up, relay_buf, relay_buf).await;
                }
            });
        }
    });
    Ok(())
}
