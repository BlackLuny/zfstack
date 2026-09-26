//! Send records (§9.3): one compact record per transmitted segment, ordered by
//! sequence offset. Payload lives in the TX buffer; records carry only metadata
//! (flags, send time, delivery-rate snapshot).
//!
//! Pipe accounting follows RFC 6675 / Linux:
//! `pipe = (snd_nxt - snd_una) - sacked - lost + retrans_out`.

use crate::budget::{Budget, MemoryLease};
use crate::time::Instant;
use crate::PeerId;
use std::collections::VecDeque;

pub const F_SACKED: u8 = 1;
pub const F_LOST: u8 = 2;
/// Retransmitted since last marked lost (counted in `retrans_out`).
pub const F_RETRANS: u8 = 4;
/// Retransmitted at least once (Karn: no RTT samples).
pub const F_EVER_RETRANS: u8 = 8;
pub const F_FIN: u8 = 16;
pub const F_SYN: u8 = 32;

/// Keep a few records for small request/reply bursts, but do not retain a
/// large flight's backing for the rest of a long-lived idle connection.
const IDLE_RECORD_CAP: usize = 8;
/// Charge of the first record backing of a drained connection.
pub(crate) const MIN_RECORD_CHARGE: u64 = (IDLE_RECORD_CAP * std::mem::size_of::<Rec>() * 2) as u64;

#[derive(Clone, Copy, Debug)]
pub struct Rec {
    pub start: u64,
    pub end: u64,
    pub xmit: Instant,
    /// Delivery-rate snapshot at (last) transmission.
    pub delivered: u64,
    pub delivered_ts: Instant,
    pub first_tx_ts: Instant,
    pub tx_in_flight: u64,
    pub lost_at_send: u64,
    pub flags: u8,
    pub app_limited: bool,
}

impl Rec {
    #[inline]
    pub fn len(&self) -> u64 {
        self.end - self.start
    }
    #[inline]
    pub fn has(&self, f: u8) -> bool {
        self.flags & f != 0
    }
}

#[derive(Default)]
pub struct Scoreboard {
    pub recs: VecDeque<Rec>,
    /// Owns the backing of `recs`; replacement holds both old and new leases
    /// until the old allocation has actually been released.
    backing: Option<MemoryLease>,
    pub sacked: u64,
    pub lost: u64,
    pub retrans_out: u64,
    /// Index hint: no record before this index is (LOST && !RETRANS && !SACKED).
    rtx_hint: usize,
}

impl Scoreboard {
    fn next_allocation(&self) -> Option<(usize, u64)> {
        let target = match self.recs.capacity() {
            0 => IDLE_RECORD_CAP,
            n => n.checked_mul(2)?,
        };
        let bytes = target.checked_mul(std::mem::size_of::<Rec>())?.checked_mul(2)?;
        Some((target, u64::try_from(bytes).ok()?))
    }

    /// Physical bytes needed before another record can be appended. An ACK
    /// may free a slot in the existing allocation without releasing memory.
    pub fn reserve_bytes_needed(&self) -> Option<u64> {
        if self.recs.len() < self.recs.capacity() {
            Some(0)
        } else {
            self.next_allocation().map(|(_, bytes)| bytes)
        }
    }

    pub fn is_empty(&self) -> bool {
        self.recs.is_empty()
    }

    /// Bytes considered in the network.
    #[inline]
    pub fn pipe(&self, outstanding: u64) -> u64 {
        (outstanding + self.retrans_out).saturating_sub(self.sacked + self.lost)
    }

    pub fn push(&mut self, r: Rec) {
        debug_assert!(self.recs.back().map_or(true, |b| b.end == r.start));
        debug_assert!(self.backing.is_none() || self.recs.len() < self.recs.capacity(), "send record grew without a lease");
        self.recs.push_back(r);
    }

    /// Leave room for the one record that a plan may insert or commit. Allocate
    /// a separate deque so the old and new backing are both charged while the
    /// records move. Charge twice the requested size to cover allocator slack.
    pub fn try_reserve_one(&mut self, budget: &mut Budget, peer: PeerId) -> bool {
        if self.recs.len() < self.recs.capacity() {
            return true;
        }
        let Some((target, bytes)) = self.next_allocation() else {
            return false;
        };
        let Some(lease) = budget.try_allocate_kind(peer, bytes, crate::budget::AllocationKind::SendRecord) else { return false };
        let mut next = VecDeque::new();
        if next.try_reserve_exact(target).is_err() {
            return false;
        }
        if next.capacity().checked_mul(std::mem::size_of::<Rec>()).is_none_or(|n| n as u64 > bytes) {
            return false;
        }
        next.extend(self.recs.drain(..));
        let old = std::mem::replace(&mut self.recs, next);
        drop(old);
        self.backing = Some(lease);
        true
    }

    /// Index of the record containing `off`, if any.
    pub fn find(&self, off: u64) -> Option<usize> {
        let i = self.recs.partition_point(|r| r.end <= off);
        (i < self.recs.len() && self.recs[i].start <= off).then_some(i)
    }

    fn unaccount(&mut self, r: &Rec) {
        let l = r.len();
        if r.has(F_SACKED) {
            self.sacked -= l;
        }
        if r.has(F_LOST) {
            self.lost -= l;
        }
        if r.has(F_RETRANS) {
            self.retrans_out -= l;
        }
    }

    fn account(&mut self, r: &Rec) {
        let l = r.len();
        if r.has(F_SACKED) {
            self.sacked += l;
        }
        if r.has(F_LOST) {
            self.lost += l;
        }
        if r.has(F_RETRANS) {
            self.retrans_out += l;
        }
    }

    /// Split record `i` at `at` (start < at < end). The two halves share metadata.
    pub fn split(&mut self, i: usize, at: u64) {
        debug_assert!(self.backing.is_none() || self.recs.len() < self.recs.capacity(), "split record grew without a lease");
        let r = self.recs[i];
        debug_assert!(r.start < at && at < r.end);
        let mut a = r;
        a.end = at;
        a.flags &= !F_FIN;
        let mut b = r;
        b.start = at;
        b.flags &= !F_SYN;
        self.recs[i] = a;
        self.recs.insert(i + 1, b);
        if self.rtx_hint > i {
            self.rtx_hint += 1;
        }
    }

    /// Remove records fully below `ack`, trimming a partially acked one.
    /// Calls `f` for every record (or part) newly delivered (not previously sacked).
    pub fn ack_to(&mut self, ack: u64, mut f: impl FnMut(&Rec)) {
        while let Some(front) = self.recs.front() {
            if front.end <= ack {
                let r = self.recs.pop_front().unwrap();
                self.unaccount(&r);
                if !r.has(F_SACKED) {
                    f(&r);
                }
                self.rtx_hint = self.rtx_hint.saturating_sub(1);
            } else if front.start < ack {
                let r = *front;
                self.unaccount(&r);
                let mut part = r;
                part.end = ack;
                if !r.has(F_SACKED) {
                    f(&part);
                }
                let mut rest = r;
                rest.start = ack;
                self.account(&rest);
                self.recs[0] = rest;
                break;
            } else {
                break;
            }
        }
        if self.recs.is_empty() && self.recs.capacity() > IDLE_RECORD_CAP {
            self.recs = VecDeque::new();
            self.backing = None;
            self.rtx_hint = 0;
        }
    }

    /// Mark records fully inside `[l, r)` as sacked. Calls `f` for each newly sacked record.
    /// Returns the number of bytes newly sacked. Records are never split on SACK
    /// edges: legitimate blocks align with segment boundaries, and splitting on
    /// arbitrary edges would let a peer fragment the scoreboard (like Linux, which
    /// only splits at MSS multiples).
    pub fn sack(&mut self, l: u64, r: u64, mut f: impl FnMut(&Rec)) -> u64 {
        let mut i = self.recs.partition_point(|x| x.start < l);
        let mut n = 0;
        while i < self.recs.len() {
            let rec = self.recs[i];
            // A FIN occupies one sequence number after the data; accept blocks that
            // cover all of the record's data even if they stop before the FIN.
            let data_end = rec.end - rec.has(F_FIN) as u64;
            if data_end > r || rec.start >= r {
                break;
            }
            if !rec.has(F_SACKED) {
                self.unaccount(&rec);
                let mut m = rec;
                m.flags = (m.flags | F_SACKED) & !(F_LOST | F_RETRANS);
                self.account(&m);
                self.recs[i] = m;
                n += m.len();
                f(&rec);
            }
            i += 1;
        }
        n
    }

    pub fn mark_lost(&mut self, i: usize) -> u64 {
        let r = self.recs[i];
        if r.has(F_SACKED) {
            return 0;
        }
        if r.has(F_LOST) && !r.has(F_RETRANS) {
            return 0;
        }
        self.unaccount(&r);
        let mut m = r;
        m.flags = (m.flags | F_LOST) & !F_RETRANS;
        self.account(&m);
        self.recs[i] = m;
        if i < self.rtx_hint {
            self.rtx_hint = i;
        }
        // Newly lost bytes (a re-lost retransmission was already counted in `lost`).
        if r.has(F_LOST) {
            0
        } else {
            m.len()
        }
    }

    /// Record `i` was (re)transmitted at `now`.
    pub fn mark_retransmitted(&mut self, i: usize, now: Instant) {
        let r = self.recs[i];
        self.unaccount(&r);
        let mut m = r;
        m.flags |= F_EVER_RETRANS;
        if m.has(F_LOST) {
            m.flags |= F_RETRANS;
        }
        m.xmit = now;
        self.account(&m);
        self.recs[i] = m;
    }

    /// First record needing retransmission (LOST, not yet retransmitted, not sacked).
    pub fn next_lost(&mut self) -> Option<usize> {
        let mut i = self.rtx_hint.min(self.recs.len());
        if self.lost_pending() == 0 {
            self.rtx_hint = self.recs.len();
            return None;
        }
        while i < self.recs.len() {
            let r = &self.recs[i];
            if r.has(F_LOST) && !r.has(F_RETRANS) && !r.has(F_SACKED) {
                self.rtx_hint = i;
                return Some(i);
            }
            i += 1;
        }
        self.rtx_hint = i;
        None
    }

    /// Lost bytes awaiting retransmission.
    #[inline]
    pub fn lost_pending(&self) -> u64 {
        // Every retransmitted-and-outstanding byte is also counted in `lost`.
        self.lost - self.retrans_out.min(self.lost)
    }

    pub fn clear(&mut self) {
        self.recs.clear();
        self.sacked = 0;
        self.lost = 0;
        self.retrans_out = 0;
        self.rtx_hint = 0;
    }

    /// Mark every un-sacked record lost (RTO, RFC 8985 §6.3). Returns newly lost bytes.
    pub fn mark_all_lost(&mut self) -> u64 {
        let mut n = 0;
        for i in 0..self.recs.len() {
            n += self.mark_lost(i);
        }
        self.rtx_hint = 0;
        n
    }

    /// Forget SACK information and mark everything lost (suspected reneging after
    /// repeated RTO, RFC 2018 §8). Returns newly lost bytes.
    pub fn renege_all(&mut self) -> u64 {
        let mut n = 0;
        for i in 0..self.recs.len() {
            let r = self.recs[i];
            if r.has(F_SACKED) {
                self.unaccount(&r);
                let mut m = r;
                // Its original send time no longer yields a valid RTT sample.
                m.flags = (m.flags & !(F_SACKED | F_RETRANS)) | F_LOST | F_EVER_RETRANS;
                self.account(&m);
                self.recs[i] = m;
                n += m.len();
            } else {
                n += self.mark_lost(i);
            }
        }
        self.rtx_hint = 0;
        n
    }

    /// Sanity check of the incremental counters (tests / debug).
    pub fn check(&self) {
        let (mut s, mut l, mut r) = (0, 0, 0);
        let mut prev: Option<u64> = None;
        for x in &self.recs {
            assert!(x.start < x.end, "empty rec");
            if let Some(p) = prev {
                assert_eq!(p, x.start, "records not contiguous");
            }
            prev = Some(x.end);
            if x.has(F_SACKED) {
                s += x.len();
                assert!(!x.has(F_LOST) && !x.has(F_RETRANS));
            }
            if x.has(F_LOST) {
                l += x.len();
            }
            if x.has(F_RETRANS) {
                assert!(x.has(F_LOST));
                r += x.len();
            }
        }
        assert_eq!((s, l, r), (self.sacked, self.lost, self.retrans_out));
        for (i, x) in self.recs.iter().enumerate().take(self.rtx_hint.min(self.recs.len())) {
            assert!(!(x.has(F_LOST) && !x.has(F_RETRANS) && !x.has(F_SACKED)), "rtx hint skips lost rec {i}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::GlobalBudget;

    fn rec(s: u64, e: u64) -> Rec {
        Rec {
            start: s,
            end: e,
            xmit: Instant::ZERO,
            delivered: 0,
            delivered_ts: Instant::ZERO,
            first_tx_ts: Instant::ZERO,
            tx_in_flight: 0,
            lost_at_send: 0,
            flags: 0,
            app_limited: false,
        }
    }

    #[test]
    fn sack_lost_retrans_ack() {
        let mut sb = Scoreboard::default();
        for i in 0..10 {
            sb.push(rec(1 + i * 100, 1 + (i + 1) * 100));
        }
        let out = 1000;
        assert_eq!(sb.pipe(out), 1000);
        let n = sb.sack(301, 601, |_| {});
        assert_eq!(n, 300);
        sb.check();
        assert_eq!(sb.mark_lost(0), 100);
        assert_eq!(sb.mark_lost(1), 100);
        sb.check();
        assert_eq!(sb.pipe(out), 1000 - 300 - 200);
        let i = sb.next_lost().unwrap();
        assert_eq!(i, 0);
        sb.mark_retransmitted(i, Instant::from_millis(1));
        sb.check();
        assert_eq!(sb.pipe(out), 600);
        assert_eq!(sb.next_lost(), Some(1));
        // Partial SACK blocks only mark fully covered records.
        assert_eq!(sb.sack(750, 851, |_| {}), 0);
        assert_eq!(sb.sack(701, 851, |_| {}), 100);
        sb.check();
        let mut delivered = 0;
        sb.ack_to(151, |r| delivered += r.len());
        assert_eq!(delivered, 150);
        sb.check();
        sb.ack_to(1001, |r| delivered += r.len());
        sb.check();
        assert!(sb.is_empty());
        assert_eq!((sb.sacked, sb.lost, sb.retrans_out), (0, 0, 0));
    }

    #[test]
    fn large_flight_releases_record_backing_after_ack() {
        let mut sb = Scoreboard::default();
        for i in 0..2048 {
            sb.push(rec(i, i + 1));
        }
        assert!(sb.recs.capacity() > IDLE_RECORD_CAP);
        sb.ack_to(2048, |_| {});
        assert!(sb.is_empty());
        assert_eq!(sb.recs.capacity(), 0);
        sb.push(rec(2048, 2049));
        sb.check();
    }

    #[test]
    fn small_flight_keeps_record_backing_for_reuse() {
        let mut sb = Scoreboard::default();
        for i in 0..4 {
            sb.push(rec(i, i + 1));
        }
        let capacity = sb.recs.capacity();
        assert!(capacity <= IDLE_RECORD_CAP);
        sb.ack_to(4, |_| {});
        assert_eq!(sb.recs.capacity(), capacity);
    }

    #[test]
    fn record_growth_charges_and_releases_physical_backing() {
        let global = GlobalBudget::new(1 << 20);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(16 << 10, 16 << 10, 1);
        let peer = PeerId(7);
        let mut sb = Scoreboard::default();
        assert!(sb.try_reserve_one(&mut budget, peer));
        let first = global.reserved();
        assert!(first >= sb.recs.capacity() as u64 * std::mem::size_of::<Rec>() as u64);
        let count = sb.recs.capacity();
        for i in 0..count {
            sb.push(rec(i as u64, i as u64 + 1));
        }
        assert!(sb.try_reserve_one(&mut budget, peer));
        assert!(global.reserved() > first);
        sb.ack_to(count as u64, |_| {});
        assert_eq!(global.reserved(), 0);
        assert_eq!(budget.physical_used(), 0);

        let tight = GlobalBudget::new(256);
        let mut tight_budget = Budget::new(tight.clone());
        assert!(!Scoreboard::default().try_reserve_one(&mut tight_budget, peer));
        assert_eq!(tight.reserved(), 0);
    }
}
