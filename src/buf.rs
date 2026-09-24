//! Byte storage for connections.
//!
//! * [`TxBuf`]: unacknowledged + unsent bytes in a ring of fixed-size blocks. Any
//!   stream offset maps to (block, position) in O(1), so segmentation and
//!   re-segmentation after an MSS change never copy (§10.4). A segment spans at
//!   most two blocks.
//! * [`RxQueue`]: in-order bytes held as zero-copy slices of ingress packets (§5).
//! * [`OooQueue`]: out-of-order ranges, allocated only when reordering happens (§6.1).

use bytes::Bytes;
use std::collections::{BTreeMap, VecDeque};

pub const TX_BLOCK: usize = 64 * 1024;

/// Recycles TX blocks between connections of one shard.
#[derive(Default)]
pub struct BlockPool {
    free: Vec<Box<[u8]>>,
    pub max_cached: usize,
}

impl BlockPool {
    pub fn new(max_cached: usize) -> Self {
        BlockPool { free: Vec::new(), max_cached }
    }
    fn get(&mut self) -> Box<[u8]> {
        self.free.pop().unwrap_or_else(|| vec![0u8; TX_BLOCK].into_boxed_slice())
    }
    fn put(&mut self, b: Box<[u8]>) {
        if self.free.len() < self.max_cached {
            self.free.push(b);
        }
    }
    pub fn cached(&self) -> usize {
        self.free.len()
    }
}

/// Send buffer. Offset 0 is the oldest unacknowledged byte.
#[derive(Default)]
pub struct TxBuf {
    blocks: VecDeque<Box<[u8]>>,
    /// Position of offset 0 inside `blocks[0]`.
    head: usize,
    len: usize,
}

impl TxBuf {
    pub fn len(&self) -> usize {
        self.len
    }
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
    /// Bytes of block memory held.
    pub fn allocated(&self) -> usize {
        self.blocks.len() * TX_BLOCK
    }

    pub fn push(&mut self, pool: &mut BlockPool, mut src: &[u8]) {
        while !src.is_empty() {
            let end = self.head + self.len;
            let cap = self.blocks.len() * TX_BLOCK;
            if end == cap {
                self.blocks.push_back(pool.get());
            }
            let bi = end / TX_BLOCK;
            let pos = end % TX_BLOCK;
            let n = (TX_BLOCK - pos).min(src.len());
            self.blocks[bi][pos..pos + n].copy_from_slice(&src[..n]);
            self.len += n;
            src = &src[n..];
        }
    }

    /// Drop `n` bytes from the front (acknowledged).
    pub fn consume(&mut self, pool: &mut BlockPool, n: usize) {
        let n = n.min(self.len);
        self.head += n;
        self.len -= n;
        while self.head >= TX_BLOCK {
            let b = self.blocks.pop_front().unwrap();
            pool.put(b);
            self.head -= TX_BLOCK;
        }
        if self.len == 0 {
            // Keep at most one block for reuse by this connection.
            while self.blocks.len() > 1 {
                pool.put(self.blocks.pop_back().unwrap());
            }
            self.head = 0;
        }
    }

    pub fn release_all(&mut self, pool: &mut BlockPool) {
        for b in self.blocks.drain(..) {
            pool.put(b);
        }
        self.head = 0;
        self.len = 0;
    }

    /// Up to two slices covering `[off, off+len)`.
    pub fn slices(&self, off: usize, len: usize) -> [&[u8]; 2] {
        debug_assert!(off + len <= self.len);
        if len == 0 {
            return [&[], &[]];
        }
        let abs = self.head + off;
        let bi = abs / TX_BLOCK;
        let pos = abs % TX_BLOCK;
        let first = (TX_BLOCK - pos).min(len);
        let a = &self.blocks[bi][pos..pos + first];
        if first == len {
            [a, &[]]
        } else {
            debug_assert!(len - first <= TX_BLOCK);
            [a, &self.blocks[bi + 1][..len - first]]
        }
    }
}

/// In-order received data waiting for the application.
#[derive(Default)]
pub struct RxQueue {
    q: VecDeque<Bytes>,
    len: usize,
}

impl RxQueue {
    pub fn len(&self) -> usize {
        self.len
    }
    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
    pub fn push(&mut self, b: Bytes) {
        if b.is_empty() {
            return;
        }
        self.len += b.len();
        self.q.push_back(b);
    }
    pub fn read(&mut self, dst: &mut [u8]) -> usize {
        let mut n = 0;
        while n < dst.len() {
            let Some(front) = self.q.front_mut() else { break };
            let k = front.len().min(dst.len() - n);
            dst[n..n + k].copy_from_slice(&front[..k]);
            n += k;
            if k == front.len() {
                self.q.pop_front();
            } else {
                let _ = front.split_to(k);
            }
        }
        self.len -= n;
        n
    }
    pub fn read_chunk(&mut self, max: usize) -> Option<Bytes> {
        let front = self.q.front_mut()?;
        let b = if front.len() <= max { self.q.pop_front().unwrap() } else { front.split_to(max) };
        self.len -= b.len();
        Some(b)
    }
    pub fn clear(&mut self) {
        self.q.clear();
        self.len = 0;
    }
}

/// Out-of-order segments keyed by 64-bit stream offset. Ranges never overlap.
#[derive(Default)]
pub struct OooQueue {
    segs: BTreeMap<u64, Bytes>,
    bytes: usize,
    /// Most recently changed range (reported first in SACK, RFC 2018 §4).
    pub last: Option<(u64, u64)>,
}

impl OooQueue {
    pub fn bytes(&self) -> usize {
        self.bytes
    }
    pub fn is_empty(&self) -> bool {
        self.segs.is_empty()
    }
    pub fn ranges(&self) -> usize {
        self.segs.len()
    }

    /// Insert `[off, off+data.len())`, keeping only bytes not already present.
    /// Returns number of new bytes stored.
    pub fn insert(&mut self, off: u64, data: Bytes) -> usize {
        let end = off + data.len() as u64;
        let mut added = 0;
        let mut cur = off;
        // Walk existing ranges overlapping [off, end) and fill gaps.
        let mut gaps: Vec<(u64, u64)> = Vec::new();
        if let Some((&s, b)) = self.segs.range(..=off).next_back() {
            let e = s + b.len() as u64;
            if e > cur {
                cur = e.min(end);
            }
        }
        for (&s, b) in self.segs.range(off..end) {
            if s > cur {
                gaps.push((cur, s));
            }
            cur = cur.max(s + b.len() as u64);
            if cur >= end {
                break;
            }
        }
        if cur < end {
            gaps.push((cur, end));
        }
        for (s, e) in gaps {
            let piece = data.slice((s - off) as usize..(e - off) as usize);
            added += piece.len();
            self.segs.insert(s, piece);
        }
        self.bytes += added;
        if added > 0 {
            let (bs, be) = self.block_containing(off.max(self.first_start().unwrap_or(off)));
            self.last = Some((bs, be));
        }
        let _ = end;
        added
    }

    fn first_start(&self) -> Option<u64> {
        self.segs.keys().next().copied()
    }

    /// The maximal contiguous block containing offset `at` (must be present).
    fn block_containing(&self, at: u64) -> (u64, u64) {
        let mut start = at;
        let mut end = at;
        // Extend backwards.
        let mut iter = self.segs.range(..=at).rev();
        if let Some((&s, b)) = iter.next() {
            start = s;
            end = s + b.len() as u64;
            for (&ps, pb) in iter {
                if ps + pb.len() as u64 == start {
                    start = ps;
                } else {
                    break;
                }
            }
        }
        for (&s, b) in self.segs.range(end..) {
            if s == end {
                end = s + b.len() as u64;
            } else {
                break;
            }
        }
        (start, end)
    }

    /// Remove and return data contiguous from `off` (the new rcv_nxt).
    /// Data before `off` is discarded.
    pub fn pop_contiguous(&mut self, off: u64, out: &mut RxQueue) -> u64 {
        let mut next = off;
        while let Some((&s, _)) = self.segs.iter().next() {
            if s > next {
                break;
            }
            let b = self.segs.remove(&s).unwrap();
            self.bytes -= b.len();
            let e = s + b.len() as u64;
            if e > next {
                out.push(b.slice((next - s) as usize..));
                next = e;
            }
        }
        if let Some((ls, le)) = self.last {
            if le <= next {
                self.last = None;
            } else if ls < next {
                self.last = Some((next, le));
            }
        }
        next
    }

    /// Contiguous blocks (merged), most recent first, at most `max`.
    pub fn sack_blocks(&self, max: usize, out: &mut Vec<(u64, u64)>) {
        out.clear();
        if max == 0 {
            return;
        }
        if let Some(l) = self.last {
            out.push(l);
        }
        let mut cur: Option<(u64, u64)> = None;
        for (&s, b) in &self.segs {
            let e = s + b.len() as u64;
            match cur {
                Some((cs, ce)) if ce == s => cur = Some((cs, e)),
                Some(c) => {
                    if Some(c) != self.last {
                        out.push(c);
                    }
                    cur = Some((s, e));
                }
                None => cur = Some((s, e)),
            }
            if out.len() >= max {
                break;
            }
        }
        if let Some(c) = cur {
            if out.len() < max && Some(c) != self.last {
                out.push(c);
            }
        }
        out.truncate(max);
    }

    /// Drop everything (last-resort reneging, §6.7).
    pub fn clear(&mut self) -> usize {
        let b = self.bytes;
        self.segs.clear();
        self.bytes = 0;
        self.last = None;
        b
    }

    /// Highest offset held.
    pub fn end(&self) -> Option<u64> {
        self.segs.iter().next_back().map(|(&s, b)| s + b.len() as u64)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tx_ring() {
        let mut pool = BlockPool::new(4);
        let mut tx = TxBuf::default();
        let data: Vec<u8> = (0..200_000u32).map(|i| (i % 251) as u8).collect();
        tx.push(&mut pool, &data);
        assert_eq!(tx.len(), data.len());
        let [a, b] = tx.slices(TX_BLOCK - 10, 30);
        assert_eq!(a.len(), 10);
        assert_eq!(b.len(), 20);
        assert_eq!(a, &data[TX_BLOCK - 10..TX_BLOCK]);
        assert_eq!(b, &data[TX_BLOCK..TX_BLOCK + 20]);
        tx.consume(&mut pool, TX_BLOCK + 5);
        let [a, _] = tx.slices(0, 5);
        assert_eq!(a, &data[TX_BLOCK + 5..TX_BLOCK + 10]);
        tx.consume(&mut pool, tx.len());
        assert!(tx.is_empty());
        assert!(tx.allocated() <= TX_BLOCK);
    }

    #[test]
    fn ooo_merge_and_sack() {
        let mut q = OooQueue::default();
        let b = |n: usize| Bytes::from(vec![7u8; n]);
        assert_eq!(q.insert(100, b(10)), 10);
        assert_eq!(q.insert(120, b(10)), 10);
        assert_eq!(q.insert(105, b(20)), 10); // fills 110..120
        assert_eq!(q.bytes(), 30);
        let mut v = Vec::new();
        q.sack_blocks(3, &mut v);
        assert_eq!(v, vec![(100, 130)]);
        assert_eq!(q.insert(200, b(5)), 5);
        q.sack_blocks(3, &mut v);
        assert_eq!(v, vec![(200, 205), (100, 130)]);
        let mut rx = RxQueue::default();
        let next = q.pop_contiguous(100, &mut rx);
        assert_eq!(next, 130);
        assert_eq!(rx.len(), 30);
        q.sack_blocks(3, &mut v);
        assert_eq!(v, vec![(200, 205)]);
        // Overlap before rcv_nxt is discarded.
        q.insert(198, b(4));
        let next = q.pop_contiguous(199, &mut rx);
        assert_eq!(next, 205);
        assert_eq!(rx.len(), 30 + 6);
    }
}
