//! Benchmark application protocol (shared by the client, the kernel server and
//! every userspace-stack adapter).
//!
//! Request header = 16 bytes: `b'Z'`, cmd u8, 6 zero bytes, u64 LE param.
//! - `D` download: server streams the pattern until the peer closes/resets.
//! - `U` upload: server reads/verifies until EOF, replies u64 LE byte count, closes.
//!   (param = client flow index, used only for per-flow accounting.)
//! - `E` echo: server echoes everything back (param = message size, informative).
//! - `C` connect: server closes right after reading the header.
//!
//! Stream pattern: byte i of a stream == (i % 251) as u8.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering::Relaxed};
use std::sync::{Arc, LazyLock};

pub const HDR_LEN: usize = 16;
pub const CMD_DOWN: u8 = b'D';
pub const CMD_UP: u8 = b'U';
pub const CMD_ECHO: u8 = b'E';
pub const CMD_CONNECT: u8 = b'C';
pub const MAX_FLOWS: usize = 1024;
const PAT_MOD: usize = 251;
/// Largest chunk `pattern_at` may return.
pub const PAT_CHUNK: usize = 64 * 1024;

pub fn encode_header(cmd: u8, param: u64) -> [u8; HDR_LEN] {
    let mut h = [0u8; HDR_LEN];
    h[0] = b'Z';
    h[1] = cmd;
    h[8..].copy_from_slice(&param.to_le_bytes());
    h
}

static PATTERN: LazyLock<Vec<u8>> = LazyLock::new(|| (0..PAT_MOD + PAT_CHUNK).map(|i| (i % PAT_MOD) as u8).collect());

/// Pattern bytes for stream positions `[pos, pos + n)`, `n <= PAT_CHUNK`.
#[inline]
pub fn pattern_at(pos: u64, n: usize) -> &'static [u8] {
    debug_assert!(n <= PAT_CHUNK);
    let off = (pos % PAT_MOD as u64) as usize;
    &PATTERN[off..off + n]
}

/// Fill `buf` with the pattern starting at stream position `pos`.
pub fn fill_pattern(pos: u64, buf: &mut [u8]) {
    let mut done = 0;
    while done < buf.len() {
        let n = (buf.len() - done).min(PAT_CHUNK);
        buf[done..done + n].copy_from_slice(pattern_at(pos + done as u64, n));
        done += n;
    }
}

/// True if `data` equals the pattern at stream position `pos`.
pub fn verify_pattern(pos: u64, data: &[u8]) -> bool {
    let mut done = 0;
    while done < data.len() {
        let n = (data.len() - done).min(PAT_CHUNK);
        if data[done..done + n] != *pattern_at(pos + done as u64, n) {
            return false;
        }
        done += n;
    }
    true
}

/// Counters shared between the server-side app (running inside the stack
/// thread / kernel server threads) and the orchestrator.
pub struct ServerCounters {
    /// Payload bytes delivered to the server app (all commands, excl. headers).
    pub rx_bytes: AtomicU64,
    /// Payload bytes handed by the server app to the stack for sending.
    pub tx_bytes: AtomicU64,
    /// Upload bytes received, per client flow index (header param).
    pub up_flow_bytes: Vec<AtomicU64>,
    pub pattern_errors: AtomicU64,
    pub bad_headers: AtomicU64,
    pub conns_accepted: AtomicU64,
    pub conns_finished: AtomicU64,
    /// Server CPU seconds * 1e6 contributed by threads that have exited
    /// (kernel mode only: per-connection handler threads).
    pub thread_cpu_us: AtomicU64,
}

impl ServerCounters {
    pub fn new() -> Arc<Self> {
        Arc::new(ServerCounters {
            rx_bytes: AtomicU64::new(0),
            tx_bytes: AtomicU64::new(0),
            up_flow_bytes: (0..MAX_FLOWS).map(|_| AtomicU64::new(0)).collect(),
            pattern_errors: AtomicU64::new(0),
            bad_headers: AtomicU64::new(0),
            conns_accepted: AtomicU64::new(0),
            conns_finished: AtomicU64::new(0),
            thread_cpu_us: AtomicU64::new(0),
        })
    }

    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "rx_bytes": self.rx_bytes.load(Relaxed),
            "tx_bytes": self.tx_bytes.load(Relaxed),
            "pattern_errors": self.pattern_errors.load(Relaxed),
            "bad_headers": self.bad_headers.load(Relaxed),
            "conns_accepted": self.conns_accepted.load(Relaxed),
            "conns_finished": self.conns_finished.load(Relaxed),
        })
    }
}

/// What the transport should do once everything produced so far is sent.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CloseAction {
    /// Keep the connection open.
    None,
    /// Graceful close (FIN) after all produced bytes have been queued.
    Close,
    /// Abort (RST) now.
    Abort,
}

enum St {
    Header {
        buf: [u8; HDR_LEN],
        got: usize,
    },
    Down {
        pos: u64,
    },
    Up {
        pos: u64,
        flow: usize,
        reply: Option<([u8; 8], usize)>,
    },
    Echo {
        buf: VecDeque<u8>,
        eof: bool,
    },
    /// Connect test: close as soon as the header is in.
    Connect,
    /// Finished: either close or abort.
    Done(CloseAction),
}

/// Transport-agnostic server-side state machine for one connection.
///
/// Adapter usage (stream API):
/// 1. feed every received byte with [`on_recv`](Self::on_recv) (always consumes all);
/// 2. when the peer has sent FIN and all its data has been fed, call
///    [`on_peer_eof`](Self::on_peer_eof) once;
/// 3. while [`wants_send`](Self::wants_send) and the transport has room, call
///    [`produce`](Self::produce) into the transport's send buffer;
/// 4. act on [`close_action`](Self::close_action) (Close only once the produced
///    bytes are queued; Abort immediately);
/// 5. call [`finish`](Self::finish) once when the connection is gone.
pub struct AppConn {
    st: St,
    counters: Arc<ServerCounters>,
    finished: bool,
}

impl AppConn {
    pub fn new(counters: Arc<ServerCounters>) -> Self {
        counters.conns_accepted.fetch_add(1, Relaxed);
        AppConn { st: St::Header { buf: [0; HDR_LEN], got: 0 }, counters, finished: false }
    }

    pub fn on_recv(&mut self, mut data: &[u8]) {
        while !data.is_empty() {
            match &mut self.st {
                St::Header { buf, got } => {
                    let n = (HDR_LEN - *got).min(data.len());
                    buf[*got..*got + n].copy_from_slice(&data[..n]);
                    *got += n;
                    data = &data[n..];
                    if *got == HDR_LEN {
                        let param = u64::from_le_bytes(buf[8..16].try_into().unwrap());
                        self.st = if buf[0] != b'Z' {
                            self.counters.bad_headers.fetch_add(1, Relaxed);
                            St::Done(CloseAction::Abort)
                        } else {
                            match buf[1] {
                                CMD_DOWN => St::Down { pos: 0 },
                                CMD_UP => St::Up { pos: 0, flow: (param as usize).min(MAX_FLOWS - 1), reply: None },
                                CMD_ECHO => St::Echo { buf: VecDeque::new(), eof: false },
                                CMD_CONNECT => St::Connect,
                                _ => {
                                    self.counters.bad_headers.fetch_add(1, Relaxed);
                                    St::Done(CloseAction::Abort)
                                }
                            }
                        };
                    }
                }
                St::Up { pos, flow, reply: None } => {
                    if !verify_pattern(*pos, data) {
                        self.counters.pattern_errors.fetch_add(1, Relaxed);
                    }
                    *pos += data.len() as u64;
                    self.counters.rx_bytes.fetch_add(data.len() as u64, Relaxed);
                    self.counters.up_flow_bytes[*flow].fetch_add(data.len() as u64, Relaxed);
                    return;
                }
                St::Echo { buf, .. } => {
                    self.counters.rx_bytes.fetch_add(data.len() as u64, Relaxed);
                    buf.extend(data);
                    return;
                }
                // Download / connect / done: ignore anything else the client sends.
                _ => {
                    self.counters.rx_bytes.fetch_add(data.len() as u64, Relaxed);
                    return;
                }
            }
        }
    }

    pub fn on_peer_eof(&mut self) {
        match &mut self.st {
            St::Up { pos, reply: r @ None, .. } => *r = Some((pos.to_le_bytes(), 0)),
            St::Down { .. } => self.st = St::Done(CloseAction::Abort),
            St::Header { .. } => self.st = St::Done(CloseAction::Abort),
            St::Echo { eof, .. } => *eof = true, // flush first; see close_action
            _ => {}
        }
    }

    pub fn wants_send(&self) -> bool {
        match &self.st {
            St::Down { .. } => true,
            St::Up { reply: Some((_, off)), .. } => *off < 8,
            St::Echo { buf, .. } => !buf.is_empty(),
            _ => false,
        }
    }

    /// Write up to `buf.len()` bytes to send. Returns bytes written.
    pub fn produce(&mut self, out: &mut [u8]) -> usize {
        let n = match &mut self.st {
            St::Down { pos } => {
                fill_pattern(*pos, out);
                *pos += out.len() as u64;
                out.len()
            }
            St::Up { reply: Some((r, off)), .. } => {
                let n = (8 - *off).min(out.len());
                out[..n].copy_from_slice(&r[*off..*off + n]);
                *off += n;
                n
            }
            St::Echo { buf, .. } => {
                let n = buf.len().min(out.len());
                let (a, b) = buf.as_slices();
                let na = a.len().min(n);
                out[..na].copy_from_slice(&a[..na]);
                out[na..n].copy_from_slice(&b[..n - na]);
                buf.drain(..n);
                n
            }
            _ => 0,
        };
        self.counters.tx_bytes.fetch_add(n as u64, Relaxed);
        n
    }

    pub fn close_action(&self) -> CloseAction {
        match &self.st {
            St::Done(a) => *a,
            St::Connect => CloseAction::Close,
            St::Up { reply: Some((_, 8)), .. } => CloseAction::Close,
            St::Echo { buf, eof: true } if buf.is_empty() => CloseAction::Close,
            _ => CloseAction::None,
        }
    }

    /// Mark the connection as gone (idempotent).
    pub fn finish(&mut self) {
        if !self.finished {
            self.finished = true;
            self.counters.conns_finished.fetch_add(1, Relaxed);
        }
    }
}

impl Drop for AppConn {
    fn drop(&mut self) {
        self.finish();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pattern_roundtrip() {
        let mut b = vec![0u8; 200_000];
        fill_pattern(12345, &mut b);
        assert!(verify_pattern(12345, &b));
        assert!(!verify_pattern(12346, &b));
        for (i, x) in b.iter().enumerate() {
            assert_eq!(*x, ((12345 + i) % 251) as u8);
        }
    }

    #[test]
    fn upload_flow() {
        let c = ServerCounters::new();
        let mut a = AppConn::new(c.clone());
        a.on_recv(&encode_header(CMD_UP, 3)[..5]);
        a.on_recv(&encode_header(CMD_UP, 3)[5..]);
        let mut d = vec![0u8; 1000];
        fill_pattern(0, &mut d);
        a.on_recv(&d);
        assert!(!a.wants_send());
        a.on_peer_eof();
        let mut out = [0u8; 16];
        assert_eq!(a.produce(&mut out), 8);
        assert_eq!(u64::from_le_bytes(out[..8].try_into().unwrap()), 1000);
        assert_eq!(a.close_action(), CloseAction::Close);
        assert_eq!(c.up_flow_bytes[3].load(Relaxed), 1000);
        assert_eq!(c.pattern_errors.load(Relaxed), 0);
    }

    #[test]
    fn echo_flow() {
        let c = ServerCounters::new();
        let mut a = AppConn::new(c);
        let mut m = encode_header(CMD_ECHO, 4).to_vec();
        m.extend_from_slice(b"abcd");
        a.on_recv(&m);
        let mut out = [0u8; 3];
        assert_eq!(a.produce(&mut out), 3);
        assert_eq!(&out, b"abc");
        a.on_peer_eof();
        assert_eq!(a.close_action(), CloseAction::None);
        assert_eq!(a.produce(&mut out), 1);
        assert_eq!(a.close_action(), CloseAction::Close);
    }
}
