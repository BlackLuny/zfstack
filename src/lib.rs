//! zfstack — userspace TCP stack for proxy/forwarding dataplanes.
//!
//! Passive-open TCP termination: raw IP packets in, byte streams out. The core is
//! sans-IO (the caller supplies time and moves packets); see
//! `docs/design/0001-architecture.md`.

pub mod budget;
pub mod buf;
pub mod cc;
pub mod config;
mod conn;
mod heap;
pub mod rtt;
pub mod scoreboard;
pub mod seq;
mod shard;
pub mod time;
pub mod wire;

#[cfg(any(test, feature = "test-peer"))]
pub mod sim;

pub use cc::CcAlgo;
pub use config::StackConfig;
pub use conn::{ConnStats, ReadResult, State, WriteResult};
pub use shard::*;
pub use time::Instant;

use core::time::Duration;
use std::net::SocketAddr;

/// Interface (WG port) handle.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct IfaceId(pub u16);

/// WG peer identity, supplied by the caller with every ingress packet (§3.1).
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub struct PeerId(pub u64);

/// Connection handle: slot index + generation.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ConnId(pub u64);

impl ConnId {
    pub(crate) fn new(idx: u32, gen: u32) -> Self {
        ConnId(((gen as u64) << 32) | idx as u64)
    }
    pub(crate) fn idx(self) -> usize {
        (self.0 & 0xffff_ffff) as usize
    }
    pub(crate) fn gen(self) -> u32 {
        (self.0 >> 32) as u32
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum CloseReason {
    /// Orderly close completed.
    Normal,
    /// Peer sent RST.
    Reset,
    /// Retransmission / keepalive / user / orphan timeout.
    Timeout,
    /// Locally aborted.
    Aborted,
    /// Active open refused (test peer).
    Refused,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
pub enum Event {
    /// Handshake completed on a passive connection.
    Accepted(ConnId),
    /// Active open completed (test peer).
    Connected(ConnId),
    /// Data (or EOF) available. Re-armed when `read` returns `WouldBlock`.
    Readable(ConnId),
    /// Send space ≥ low watermark. Re-armed when `write` is short.
    Writable(ConnId),
    /// Connection terminated (not emitted for local `abort`/`close` completion).
    Closed(ConnId, CloseReason),
}

#[derive(Clone, Debug)]
pub struct ConnInfo {
    pub state: State,
    pub iface: IfaceId,
    pub peer: PeerId,
    pub local: SocketAddr,
    pub remote: SocketAddr,
    pub srtt: Duration,
    pub rttvar: Duration,
    pub min_rtt: Duration,
    pub rto: Duration,
    pub cwnd: u64,
    pub ssthresh: u64,
    pub pipe: u64,
    pub pacing_rate: Option<u64>,
    pub mss: u32,
    pub snd_wnd: u64,
    pub rcv_wnd: u64,
    pub rcv_target: u64,
    pub tx_queued: usize,
    pub tx_unsent: u64,
    pub rx_queued: usize,
    pub ooo_bytes: usize,
    pub sacked: u64,
    pub lost: u64,
    pub retrans_out: u64,
    pub in_recovery: bool,
    pub delivered: u64,
    pub cc: &'static str,
    pub stats: ConnStats,
}
