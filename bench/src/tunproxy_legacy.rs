//! `--tun-proxy --legacy`: the TUN proxy restricted to library APIs that
//! predate the client profile (owned `ingress`, full checksums, copy relay,
//! `StackConfig::default()`), so one harness can A/B the tokio adapter's
//! server path between library revisions.

use std::io;
use std::os::fd::RawFd;

use bytes::{Bytes, BytesMut};
use tokio::io::AsyncWriteExt;
use zfstack::tokio_adapter::{self, ResourceLimits, StreamConfig};
use zfstack::{IfaceConfig, IfaceId, OutPacket, PeerId, SendResult, StackConfig};

use crate::tun;
use crate::tunproxy::{ProxyOpts, PROXY_TUN, PROXY_TUN_ADDR};

fn write_packet(fd: RawFd, p: &OutPacket<'_>) -> SendResult {
    let iov = [
        libc::iovec { iov_base: p.header.as_ptr() as *mut _, iov_len: p.header.len() },
        libc::iovec { iov_base: p.payload[0].as_ptr() as *mut _, iov_len: p.payload[0].len() },
        libc::iovec { iov_base: p.payload[1].as_ptr() as *mut _, iov_len: p.payload[1].len() },
    ];
    while unsafe { libc::writev(fd, iov.as_ptr(), 3) } < 0 {
        if io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
            break;
        }
    }
    SendResult::Accepted
}

fn reader(fd: RawFd, mtu: usize, tx: tokio::sync::mpsc::Sender<Vec<Bytes>>) {
    let mut buf = BytesMut::new();
    loop {
        let mut pfd = libc::pollfd { fd, events: libc::POLLIN, revents: 0 };
        if unsafe { libc::poll(&mut pfd, 1, 1000) } < 0 && io::Error::last_os_error().raw_os_error() != Some(libc::EINTR) {
            return;
        }
        let mut batch = Vec::new();
        let mut bytes = 0;
        while bytes < 512 * 1024 && batch.len() < 256 {
            if buf.capacity() < mtu {
                buf.reserve((256 * 1024).max(mtu));
            }
            let spare = buf.spare_capacity_mut();
            let n = unsafe { libc::read(fd, spare.as_mut_ptr() as *mut _, spare.len().min(mtu)) };
            if n <= 0 {
                break;
            }
            unsafe { buf.set_len(n as usize) };
            bytes += n as usize;
            batch.push(buf.split().freeze());
        }
        if !batch.is_empty() && tx.blocking_send(batch).is_err() {
            return;
        }
    }
}

pub fn run(o: ProxyOpts) -> io::Result<()> {
    tun::sh_quiet(&format!("ip link del {PROXY_TUN}"));
    let fd = tun::open_tun(PROXY_TUN)?;
    tun::sh(&format!("ip addr add {PROXY_TUN_ADDR} dev {PROXY_TUN}"))?;
    tun::sh(&format!("ip link set {PROXY_TUN} mtu {} txqueuelen 500 up", o.mtu))?;
    let rt = tokio::runtime::Builder::new_multi_thread().worker_threads(o.workers).enable_all().build()?;
    let (mtu, upstream, relay_buf) = (o.mtu, o.upstream, o.relay_buf);
    rt.block_on(async move {
        let (tx, rx) = tokio::sync::mpsc::channel::<Vec<Bytes>>(4);
        let global = zfstack::budget::GlobalBudget::from_system();
        let high = global.high();
        let limits = ResourceLimits { port_bytes: high, peer_bytes: high, peer_max_connections: u32::MAX };
        let egress = move |_: IfaceId, _: PeerId, p: &OutPacket<'_>| write_packet(fd, p);
        let (_handle, mut acceptor, _ids, _task) = tokio_adapter::spawn_with_source(
            StackConfig::default(),
            StreamConfig::default(),
            vec![IfaceConfig { mtu }],
            egress,
            global,
            limits,
            rx,
            move |shard, now, batch: Vec<Bytes>| {
                for p in batch {
                    shard.ingress(now, IfaceId(0), PeerId(0), p);
                }
            },
        );
        std::thread::Builder::new().name("zfc-tun-rx".into()).spawn(move || reader(fd, mtu as usize, tx)).unwrap();
        while let Some(mut down) = acceptor.accept().await {
            tokio::spawn(async move {
                let Ok(mut up) = tokio::net::TcpStream::connect(upstream).await else {
                    let _ = down.shutdown().await;
                    return;
                };
                let _ = up.set_nodelay(true);
                let _ = tokio::io::copy_bidirectional_with_sizes(&mut down, &mut up, relay_buf, relay_buf).await;
            });
        }
    });
    Ok(())
}
