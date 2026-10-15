//! Per-direction bottleneck used by the L0 world.
//!
//! Propagation delay and the drop-tail queue are separate, matching zfbench and
//! `docs/design/0002`. A tag rides with each packet so one shared pipe can still
//! demux logical ports; a per-port pipe leaves the tag unused.

use std::collections::{BTreeMap, VecDeque};

use crate::schedule::{content_digest, Trace};
use bytes::Bytes;
use serde_json::json;
use zfstack::sim::Rng;
use zfstack::{Instant, PeerId};

#[derive(Clone, Debug)]
pub struct LinkParams {
    /// Bottleneck rate in bits/s. Zero means the queue does not serialize.
    pub rate_bps: u64,
    /// One-way propagation delay.
    pub delay: core::time::Duration,
    /// Drop-tail limit, in bytes still serializing.
    pub queue_bytes: usize,
    pub loss: f64,
    /// Extra delay applied with this probability (reordering).
    pub reorder: f64,
    pub reorder_delay: core::time::Duration,
    pub duplicate: f64,
}

impl Default for LinkParams {
    fn default() -> Self {
        wan(200_000_000, core::time::Duration::from_millis(12), 2.0, 0.0)
    }
}

/// 200 Mbit/s class WAN unless the caller overrides rate, RTT, queue or loss.
pub fn wan(rate_bps: u64, rtt: core::time::Duration, queue_bdp: f64, loss: f64) -> LinkParams {
    let bdp = (rate_bps as f64 / 8.0 * rtt.as_secs_f64()) as usize;
    LinkParams {
        rate_bps,
        delay: rtt / 2,
        queue_bytes: ((queue_bdp * bdp as f64) as usize).max(1500),
        loss,
        reorder: 0.0,
        reorder_delay: core::time::Duration::from_millis(2),
        duplicate: 0.0,
    }
}

#[derive(Clone, Debug, Default)]
pub struct LinkStats {
    pub sent: u64,
    pub delivered: u64,
    pub queue_drops: u64,
    pub random_drops: u64,
    pub bytes_delivered: u64,
}

struct Flight {
    tag: u32,
    peer: PeerId,
    pkt: Bytes,
}

pub struct Link {
    pub p: LinkParams,
    busy_until: Instant,
    queued: VecDeque<(Instant, usize)>,
    queued_bytes: usize,
    inflight: BTreeMap<(Instant, u64), Flight>,
    seq: u64,
    pub stats: LinkStats,
    pub trace: Trace,
}

impl Link {
    pub fn new(p: LinkParams) -> Self {
        Link {
            p,
            busy_until: Instant::ZERO,
            queued: VecDeque::new(),
            queued_bytes: 0,
            inflight: BTreeMap::new(),
            seq: 0,
            stats: LinkStats::default(),
            trace: Trace::default(),
        }
    }

    pub fn enqueue(&mut self, now: Instant, tag: u32, peer: PeerId, pkt: Bytes, rng: &mut Rng) {
        self.trace.push(json!({"event":"enqueue", "at_ns":now.as_nanos(), "port":tag, "peer":peer.0, "len":pkt.len(), "packet":content_digest(&pkt)}));
        self.stats.sent += 1;
        while let Some(&(dep, n)) = self.queued.front() {
            if dep <= now {
                self.queued.pop_front();
                self.queued_bytes -= n;
            } else {
                break;
            }
        }
        let len = pkt.len();
        if self.p.rate_bps > 0 && self.queued_bytes + len > self.p.queue_bytes {
            self.trace.push(json!({"event":"queue_drop", "at_ns":now.as_nanos(), "port":tag}));
            self.stats.queue_drops += 1;
            return;
        }
        if rng.chance(self.p.loss) {
            self.trace.push(json!({"event":"random_drop", "at_ns":now.as_nanos(), "port":tag}));
            self.stats.random_drops += 1;
            return;
        }
        let dep = if self.p.rate_bps > 0 {
            let start = if self.busy_until > now { self.busy_until } else { now };
            let tx = core::time::Duration::from_nanos((len as u128 * 8 * 1_000_000_000 / self.p.rate_bps as u128) as u64);
            self.busy_until = start + tx;
            self.queued.push_back((self.busy_until, len));
            self.queued_bytes += len;
            self.busy_until
        } else {
            now
        };
        let mut at = dep + self.p.delay;
        if rng.chance(self.p.reorder) {
            at += self.p.reorder_delay;
        }
        self.seq += 1;
        self.inflight.insert((at, self.seq), Flight { tag, peer, pkt: pkt.clone() });
        if rng.chance(self.p.duplicate) {
            self.seq += 1;
            self.inflight.insert((at + core::time::Duration::from_micros(10), self.seq), Flight { tag, peer, pkt });
        }
    }

    /// Drop packets already accepted by the pipe. Used when an iface is rebuilt
    /// so a SYN still in flight cannot land on the replacement iface.
    pub fn clear(&mut self) {
        self.queued.clear();
        self.queued_bytes = 0;
        self.inflight.clear();
        self.busy_until = Instant::ZERO;
    }

    pub fn next_delivery(&self) -> Option<Instant> {
        self.inflight.keys().next().map(|k| k.0)
    }

    pub fn pop_due(&mut self, now: Instant) -> Option<(u32, PeerId, Bytes)> {
        let (&k, _) = self.inflight.iter().next()?;
        if k.0 > now {
            return None;
        }
        let v = self.inflight.remove(&k).unwrap();
        self.trace.push(json!({"event":"deliver", "at_ns":now.as_nanos(), "port":v.tag, "packet":content_digest(&v.pkt)}));
        self.stats.delivered += 1;
        self.stats.bytes_delivered += v.pkt.len() as u64;
        Some((v.tag, v.peer, v.pkt))
    }
}
