//! Send records (§9.3): one compact record per transmitted segment, ordered by
//! sequence offset. Payload lives in the TX buffer; records carry only metadata
//! (flags, send time, delivery-rate snapshot).
//!
//! Pipe accounting follows RFC 6675 / Linux:
//! `pipe = (snd_nxt - snd_una) - sacked - lost + retrans_out`.

use crate::time::Instant;
use std::collections::VecDeque;

pub const F_SACKED: u8 = 1;
pub const F_LOST: u8 = 2;
/// Retransmitted since last marked lost (counted in `retrans_out`).
pub const F_RETRANS: u8 = 4;
/// Retransmitted at least once (Karn: no RTT samples).
pub const F_EVER_RETRANS: u8 = 8;
pub const F_FIN: u8 = 16;
pub const F_SYN: u8 = 32;

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
    pub sacked: u64,
    pub lost: u64,
    pub retrans_out: u64,
    /// Index hint: no record before this index is (LOST && !RETRANS && !SACKED).
    rtx_hint: usize,
}

impl Scoreboard {
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
        self.recs.push_back(r);
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
    }

    /// Mark records fully inside `[l, r)` as sacked. Calls `f` for each newly sacked record.
    /// Returns the number of bytes newly sacked.
    pub fn sack(&mut self, l: u64, r: u64, mut f: impl FnMut(&Rec)) -> u64 {
        let mut i = self.recs.partition_point(|x| x.end <= l);
        let mut n = 0;
        while i < self.recs.len() {
            let rec = self.recs[i];
            if rec.start >= r {
                break;
            }
            if rec.start < l {
                // Partially covered at the left edge: split so the covered part can be marked.
                self.split(i, l);
                i += 1;
                continue;
            }
            if rec.end > r {
                self.split(i, r);
            }
            let rec = self.recs[i];
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
        // Partial sack splitting.
        sb.sack(750, 851, |_| {});
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
}
