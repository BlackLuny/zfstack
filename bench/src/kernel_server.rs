//! Kernel TCP server (baseline), running inside netns `zfbns`.
//! Same application protocol, driven by the shared [`AppConn`] state machine.

use std::io::{self, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering::Relaxed};
use std::sync::Arc;
use std::time::Duration;

use crate::app::{AppConn, CloseAction, ServerCounters};
use crate::{tun, util};

pub struct KernelServer {
    pub active: Arc<AtomicUsize>,
}

pub fn start(counters: Arc<ServerCounters>, stop: Arc<AtomicBool>, cc: Option<String>) -> io::Result<KernelServer> {
    let (tx, rx) = std::sync::mpsc::channel::<io::Result<()>>();
    let active = Arc::new(AtomicUsize::new(0));
    let active2 = active.clone();
    std::thread::Builder::new().name("ksrv-accept".into()).spawn(move || {
        let listener = match tun::enter_netns(tun::NETNS)
            .and_then(|_| TcpListener::bind((tun::SERVER_ADDR, tun::SERVER_PORT)))
        {
            Ok(l) => {
                let _ = tx.send(Ok(()));
                l
            }
            Err(e) => {
                let _ = tx.send(Err(e));
                return;
            }
        };
        if let Some(cc) = &cc {
            // Inherited by accepted sockets.
            if let Err(e) = util::set_tcp_cc(listener.as_raw_fd(), cc) {
                eprintln!("zfbench: kernel server TCP_CONGESTION={cc}: {e}");
                std::process::exit(2);
            }
        }
        // Raise the accept backlog (std uses 128).
        unsafe { libc::listen(listener.as_raw_fd(), 4096) };
        listener.set_nonblocking(true).ok();
        while !stop.load(Relaxed) {
            match listener.accept() {
                Ok((s, _)) => {
                    s.set_nonblocking(false).ok();
                    let c = counters.clone();
                    let a = active2.clone();
                    a.fetch_add(1, Relaxed);
                    let spawned = std::thread::Builder::new()
                        .stack_size(256 * 1024)
                        .spawn(move || {
                            let t0 = util::thread_cpu_now();
                            let _ = handle(s, c.clone());
                            let cpu = util::thread_cpu_now() - t0;
                            c.thread_cpu_us.fetch_add((cpu * 1e6) as u64, Relaxed);
                            a.fetch_sub(1, Relaxed);
                        });
                    if spawned.is_err() {
                        active2.fetch_sub(1, Relaxed);
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                    // Poll for new connections without busy-looping.
                    let mut pfd = libc::pollfd {
                        fd: listener.as_raw_fd(),
                        events: libc::POLLIN,
                        revents: 0,
                    };
                    unsafe { libc::poll(&mut pfd, 1, 20) };
                }
                Err(_) => std::thread::sleep(Duration::from_millis(1)),
            }
        }
    })?;
    rx.recv().map_err(|_| io::Error::other("accept thread died"))??;
    Ok(KernelServer { active })
}

fn handle(mut s: TcpStream, counters: Arc<ServerCounters>) -> io::Result<()> {
    s.set_nodelay(true)?;
    let mut app = AppConn::new(counters);
    let mut rbuf = vec![0u8; 256 * 1024];
    let mut wbuf = vec![0u8; 256 * 1024];
    loop {
        if app.wants_send() {
            let n = app.produce(&mut wbuf);
            s.write_all(&wbuf[..n])?;
            continue;
        }
        match app.close_action() {
            CloseAction::Close => {
                let _ = s.shutdown(std::net::Shutdown::Write);
                // Wait for the client's FIN so the server-side close is graceful.
                s.set_read_timeout(Some(Duration::from_secs(5)))?;
                while matches!(s.read(&mut rbuf), Ok(n) if n > 0) {}
                return Ok(());
            }
            CloseAction::Abort => {
                let l = libc::linger { l_onoff: 1, l_linger: 0 };
                unsafe {
                    libc::setsockopt(
                        s.as_raw_fd(),
                        libc::SOL_SOCKET,
                        libc::SO_LINGER,
                        &l as *const _ as *const _,
                        std::mem::size_of::<libc::linger>() as u32,
                    )
                };
                return Ok(());
            }
            CloseAction::None => {}
        }
        let n = s.read(&mut rbuf)?;
        if n == 0 {
            app.on_peer_eof();
        } else {
            app.on_recv(&rbuf[..n]);
        }
    }
}
