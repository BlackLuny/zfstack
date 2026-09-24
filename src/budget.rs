//! Memory accounting and quotas (§6.2, §6.3, §6.7).
//!
//! * [`GlobalBudget`] is shared across shards. A shard reserves in 64 KiB batches
//!   *before* use, so the global `high` is a hard limit (no allocate-then-account window).
//! * [`Budget`] is shard-local: it consumes batch reservations and keeps per-peer usage.

use crate::PeerId;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

pub const BATCH: u64 = 64 * 1024;

#[derive(Debug)]
pub struct GlobalBudget {
    high: u64,
    low: u64,
    pressure: u64,
    reserved: AtomicU64,
}

impl GlobalBudget {
    /// `high` is the hard limit; low/pressure are 3/8 and 5/8 of it (3%/5%/8% of memory, §6.3).
    pub fn new(high: u64) -> Arc<Self> {
        Arc::new(GlobalBudget { high, low: high * 3 / 8, pressure: high * 5 / 8, reserved: AtomicU64::new(0) })
    }

    /// Default: 8% of min(cgroup memory.max, physical memory).
    pub fn from_system() -> Arc<Self> {
        let mem = system_memory().unwrap_or(1 << 30);
        Self::new(mem * 8 / 100)
    }

    pub fn reserved(&self) -> u64 {
        self.reserved.load(Ordering::Relaxed)
    }
    pub fn high(&self) -> u64 {
        self.high
    }

    fn try_reserve(&self, n: u64) -> bool {
        let mut cur = self.reserved.load(Ordering::Relaxed);
        loop {
            if cur + n > self.high {
                return false;
            }
            match self.reserved.compare_exchange_weak(cur, cur + n, Ordering::AcqRel, Ordering::Relaxed) {
                Ok(_) => return true,
                Err(v) => cur = v,
            }
        }
    }

    fn release(&self, n: u64) {
        self.reserved.fetch_sub(n, Ordering::AcqRel);
    }

    pub fn level(&self) -> Pressure {
        let r = self.reserved();
        if r >= self.high {
            Pressure::High
        } else if r >= self.pressure {
            Pressure::Pressure
        } else if r >= self.low {
            Pressure::Low
        } else {
            Pressure::Free
        }
    }
}

fn system_memory() -> Option<u64> {
    let phys = std::fs::read_to_string("/proc/meminfo").ok().and_then(|s| {
        s.lines()
            .find(|l| l.starts_with("MemTotal:"))
            .and_then(|l| l.split_whitespace().nth(1).and_then(|v| v.parse::<u64>().ok()))
            .map(|kb| kb * 1024)
    });
    let cg = std::fs::read_to_string("/sys/fs/cgroup/memory.max").ok().and_then(|s| s.trim().parse::<u64>().ok());
    match (phys, cg) {
        (Some(p), Some(c)) => Some(p.min(c)),
        (p, c) => p.or(c),
    }
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Pressure {
    Free,
    Low,
    Pressure,
    High,
}

#[derive(Default, Debug, Clone, Copy)]
pub struct PeerUsage {
    pub bytes: u64,
    pub conns: u32,
}

/// Shard-local accounting.
pub struct Budget {
    global: Arc<GlobalBudget>,
    /// Bytes reserved from global but not yet used.
    local_free: u64,
    pub used: u64,
    pub peers: HashMap<PeerId, PeerUsage>,
    pub peer_limit: u64,
    pub peer_max_conns: u32,
    pub reserve_failures: u64,
}

impl Budget {
    pub fn new(global: Arc<GlobalBudget>) -> Self {
        let peer_limit = global.high / 4;
        Budget { global, local_free: 0, used: 0, peers: HashMap::new(), peer_limit, peer_max_conns: 4096, reserve_failures: 0 }
    }

    pub fn global(&self) -> &Arc<GlobalBudget> {
        &self.global
    }

    pub fn level(&self) -> Pressure {
        self.global.level()
    }

    /// Reserve `n` bytes for `peer`. `force` bypasses the per-peer limit and pressure
    /// levels (progress reserve, §6.4) but never the global hard limit.
    pub fn try_reserve(&mut self, peer: PeerId, n: u64, force: bool) -> bool {
        if n == 0 {
            return true;
        }
        let pu = self.peers.get(&peer).copied().unwrap_or_default();
        if !force && pu.bytes + n > self.peer_limit {
            self.reserve_failures += 1;
            return false;
        }
        if self.local_free < n {
            let need = (n - self.local_free).div_ceil(BATCH) * BATCH;
            if !self.global.try_reserve(need) {
                // Try the exact amount before failing.
                let exact = n - self.local_free;
                if !self.global.try_reserve(exact) {
                    self.reserve_failures += 1;
                    return false;
                }
                self.local_free += exact;
            } else {
                self.local_free += need;
            }
        }
        self.local_free -= n;
        self.used += n;
        self.peers.entry(peer).or_default().bytes += n;
        true
    }

    pub fn release(&mut self, peer: PeerId, n: u64) {
        if n == 0 {
            return;
        }
        self.used -= n;
        if let Some(p) = self.peers.get_mut(&peer) {
            p.bytes -= n;
            if p.bytes == 0 && p.conns == 0 {
                self.peers.remove(&peer);
            }
        }
        self.local_free += n;
        if self.local_free > 2 * BATCH {
            let give = self.local_free - BATCH;
            self.global.release(give);
            self.local_free -= give;
        }
    }

    pub fn peer_conn_add(&mut self, peer: PeerId) -> bool {
        let e = self.peers.entry(peer).or_default();
        if e.conns >= self.peer_max_conns {
            return false;
        }
        e.conns += 1;
        true
    }

    pub fn peer_conn_del(&mut self, peer: PeerId) {
        if let Some(p) = self.peers.get_mut(&peer) {
            p.conns = p.conns.saturating_sub(1);
            if p.bytes == 0 && p.conns == 0 {
                self.peers.remove(&peer);
            }
        }
    }
}

impl Drop for Budget {
    fn drop(&mut self) {
        self.global.release(self.local_free + self.used);
    }
}
