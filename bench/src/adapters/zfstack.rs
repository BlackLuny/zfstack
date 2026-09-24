//! zfstack adapter (feature `zfstack`). STUB: to be filled in once the zfstack
//! API settles.
//!
//! Contract (see `crate::stack::UserStack`):
//! - `ingress` gets one IPv4 packet sent by the client (10.201.0.1 -> 10.201.0.2:5201).
//! - `poll` runs the stack, accepts new connections on 10.201.0.2:5201 (passive
//!   open only), drives one [`AppConn`] per connection with the stream API
//!   (on_recv / on_peer_eof / wants_send+produce / close_action, see
//!   `crate::app::AppConn` docs), and emits every outgoing IP packet via `out`.
//!   MTU is `crate::tun::MTU` (1420).
//! - `next_deadline` returns the earliest timer (RTO, delayed ACK, pacing ...).
//! - `stats` may return any JSON (loss/recovery counters, connection counts...).
//!
//! Everything runs on the single stack thread; there is no locking.

use std::sync::Arc;
use std::time::Instant;

use crate::app::{AppConn, ServerCounters};
use crate::stack::UserStack;
#[allow(unused_imports)]
use zfstack as _zfstack;

#[derive(Clone, Debug, Default)]
#[allow(dead_code)]
pub struct ZfOpts {
    /// Socket buffer size hint (bytes), from `--sock-buf-kb`.
    pub sock_buf: usize,
}

pub struct ZfStack {
    #[allow(dead_code)]
    opts: ZfOpts,
    #[allow(dead_code)]
    counters: Arc<ServerCounters>,
    #[allow(dead_code)]
    conns: Vec<AppConn>,
}

impl ZfStack {
    pub fn new(opts: ZfOpts, counters: Arc<ServerCounters>) -> Self {
        // TODO(zfstack): construct the zfstack instance (listen on
        // tun::SERVER_ADDR:tun::SERVER_PORT, MTU tun::MTU).
        ZfStack { opts, counters, conns: Vec::new() }
    }
}

impl UserStack for ZfStack {
    fn ingress(&mut self, _now: Instant, _pkt: &[u8]) {
        unimplemented!("zfstack adapter: ingress")
    }
    fn poll(&mut self, _now: Instant, _out: &mut dyn FnMut(&[u8])) {
        unimplemented!("zfstack adapter: poll")
    }
    fn next_deadline(&mut self, _now: Instant) -> Option<Instant> {
        None
    }
    fn stats(&self) -> serde_json::Value {
        serde_json::json!({ "impl": "zfstack" })
    }
}
