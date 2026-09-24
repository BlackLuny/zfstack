//! Byte storage for connections.
//!
//! * [`TxBuf`]: unacknowledged + unsent bytes in a ring of fixed-size blocks. Any
//!   stream offset maps to (block, position) in O(1), so segmentation and
//!   re-segmentation after an MSS change never copy (§10.4). A segment spans at
//!   most two blocks.
//! * [`RxQueue`]: in-order bytes held as zero-copy slices of ingress packets (§5).
//! * [`OooQueue`]: out-of-order ranges, allocated only when reordering happens (§6.1).

use bytes::{Bytes, BytesMut};
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

/// Segments shorter than this are copied into a shared buffer instead of being kept
/// as zero-copy slices (Linux `tcp_collapse` in spirit). A slice pins its whole
/// ingress packet while the budget charges only the payload, so a peer sending tiny
/// segments could pin far more memory than it is charged for; copying bounds pinned
/// memory to about (packet size / RX_COPY_BELOW) × charged for per-packet buffers.
pub const RX_COPY_BELOW: usize = 1024;
const RX_COPY_BUF: usize = 8 * 1024;

/// In-order received data waiting for the application: zero-copy slices, then
/// `tail` (copied small segments, logically after everything in `q`).
#[derive(Default)]
pub struct RxQueue {
    q: VecDeque<Bytes>,
    tail: BytesMut,
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
        if b.len() < RX_COPY_BELOW {
            if self.tail.capacity() - self.tail.len() < b.len() {
                self.seal_tail();
                self.tail.reserve(RX_COPY_BUF);
            }
            self.tail.extend_from_slice(&b);
            return;
        }
        self.seal_tail();
        self.q.push_back(b);
    }
    /// Move copied bytes into `q` (keeping the tail's spare capacity for later copies).
    fn seal_tail(&mut self) {
        if !self.tail.is_empty() {
            self.q.push_back(self.tail.split().freeze());
        }
    }
    pub fn read(&mut self, dst: &mut [u8]) -> usize {
        if self.q.is_empty() {
            self.seal_tail();
        }
        let mut n = 0;
        while n < dst.len() {
            let Some(front) = self.q.front_mut() else {
                if self.tail.is_empty() {
                    break;
                }
                self.seal_tail();
                continue;
            };
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
        if self.q.is_empty() {
            self.seal_tail();
        }
        let front = self.q.front_mut()?;
        let b = if front.len() <= max { self.q.pop_front().unwrap() } else { front.split_to(max) };
        self.len -= b.len();
        Some(b)
    }
    pub fn clear(&mut self) {
        self.q.clear();
        self.tail = BytesMut::new();
        self.len = 0;
    }
}

/// Out-of-order segments keyed by 64-bit stream offset. Segments never overlap.
/// `ranges` holds the merged intervals so SACK generation and block lookup are
/// O(log n) regardless of how the data was fragmented.
#[derive(Default)]
pub struct OooQueue {
    segs: BTreeMap<u64, Bytes>,
    ranges: BTreeMap<u64, u64>,
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
        self.ranges.len()
    }
    pub fn segments(&self) -> usize {
        self.segs.len()
    }
    /// Whether storing another segment would exceed `limit` descriptors (§6.7).
    pub fn is_full(&self, limit: usize) -> bool {
        self.segs.len() >= limit
    }

    /// Insert `[off, off+data.len())`, keeping only bytes not already present.
    /// Returns number of new bytes stored.
    pub fn insert(&mut self, off: u64, data: Bytes) -> usize {
        let end = off + data.len() as u64;
        if data.is_empty() {
            return 0;
        }
        // Gaps of [off, end) not covered by existing ranges.
        let mut gaps: Vec<(u64, u64)> = Vec::new();
        let mut cur = off;
        if let Some((_, &e)) = self.ranges.range(..=off).next_back() {
            if e > cur {
                cur = e.min(end);
            }
        }
        for (&s, &e) in self.ranges.range(off..end) {
            if s > cur {
                gaps.push((cur, s));
            }
            cur = cur.max(e);
            if cur >= end {
                break;
            }
        }
        if cur < end {
            gaps.push((cur, end));
        }
        let mut added = 0;
        for (s, e) in gaps {
            let piece = data.slice((s - off) as usize..(e - off) as usize);
            added += piece.len();
            self.segs.insert(s, piece);
        }
        if added == 0 {
            return 0;
        }
        self.bytes += added;
        // Merge [off, end) into the interval map.
        let mut ns = off;
        let mut ne = end;
        if let Some((&s, &e)) = self.ranges.range(..=off).next_back() {
            if e >= off {
                ns = s;
                ne = ne.max(e);
                self.ranges.remove(&s);
            }
        }
        let overl: Vec<(u64, u64)> = self.ranges.range(ns..=ne).map(|(&s, &e)| (s, e)).collect();
        for (s, e) in overl {
            ne = ne.max(e);
            self.ranges.remove(&s);
        }
        self.ranges.insert(ns, ne);
        self.last = Some((ns, ne));
        added
    }

    /// Remove and return data contiguous from `off` (the new rcv_nxt).
    /// Returns (new rcv_nxt, bytes discarded because they were below `off`).
    pub fn pop_contiguous(&mut self, off: u64, out: &mut RxQueue) -> (u64, u64) {
        let mut next = off;
        let mut discarded = 0u64;
        while let Some((&s, _)) = self.segs.iter().next() {
            if s > next {
                break;
            }
            let b = self.segs.remove(&s).unwrap();
            self.bytes -= b.len();
            let e = s + b.len() as u64;
            if e > next {
                discarded += next - s;
                out.push(b.slice((next - s) as usize..));
                next = e;
            } else {
                discarded += b.len() as u64;
            }
        }
        // Trim the interval map.
        while let Some((&s, &e)) = self.ranges.iter().next() {
            if s >= next {
                break;
            }
            self.ranges.remove(&s);
            if e > next {
                self.ranges.insert(next, e);
                break;
            }
        }
        if let Some((ls, le)) = self.last {
            if le <= next {
                self.last = None;
            } else if ls < next {
                self.last = Some((next, le));
            }
        }
        (next, discarded)
    }

    /// Merged blocks, most recent first, at most `max`.
    pub fn sack_blocks(&self, max: usize, out: &mut Vec<(u64, u64)>) {
        out.clear();
        if max == 0 {
            return;
        }
        if let Some(l) = self.last {
            out.push(l);
        }
        for (&s, &e) in &self.ranges {
            if out.len() >= max {
                break;
            }
            if Some((s, e)) != self.last {
                out.push((s, e));
            }
        }
        out.truncate(max);
    }

    /// Drop everything (last-resort reneging, §6.7).
    pub fn clear(&mut self) -> usize {
        let b = self.bytes;
        self.segs.clear();
        self.ranges.clear();
        self.bytes = 0;
        self.last = None;
        b
    }

    /// Highest offset held.
    pub fn end(&self) -> Option<u64> {
        self.ranges.iter().next_back().map(|(_, &e)| e)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rx_queue_copies_small_segments_in_order() {
        // Tiny segments must not each keep their own (packet-pinning) slice.
        let mut q = RxQueue::default();
        let mut expect = Vec::new();
        let mut k = 0u8;
        for i in 0..5000usize {
            let n = if i % 100 == 99 { 1400 } else { 1 + i % 7 };
            let v: Vec<u8> = (0..n).map(|_| { k = k.wrapping_add(1); k }).collect();
            expect.extend_from_slice(&v);
            q.push(Bytes::from(v));
        }
        assert_eq!(q.len(), expect.len());
        // 50 zero-copy big segments plus one sealed copy slice before each (slices of a
        // few shared copy buffers), not ~5000 slices.
        assert!(q.q.len() <= 2 * 50 + expect.len() / RX_COPY_BUF + 2, "{} slices", q.q.len());
        let mut got = Vec::new();
        let mut buf = [0u8; 333];
        let mut flip = false;
        while !q.is_empty() {
            flip = !flip;
            if flip {
                let n = q.read(&mut buf);
                got.extend_from_slice(&buf[..n]);
            } else {
                got.extend_from_slice(&q.read_chunk(777).unwrap());
            }
            // Interleave new data with reads.
            if got.len() < 20_000 && got.len() % 3 == 0 {
                let v = vec![k; 3];
                expect.extend_from_slice(&v);
                q.push(Bytes::from(v));
            }
        }
        assert_eq!(got, expect);
        assert!(q.read_chunk(10).is_none());
    }

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
        let (next, disc) = q.pop_contiguous(100, &mut rx);
        assert_eq!((next, disc), (130, 0));
        assert_eq!(rx.len(), 30);
        q.sack_blocks(3, &mut v);
        assert_eq!(v, vec![(200, 205)]);
        // Overlap before rcv_nxt is discarded.
        q.insert(198, b(4));
        let (next, disc) = q.pop_contiguous(199, &mut rx);
        assert_eq!((next, disc), (205, 1));
        assert_eq!(rx.len(), 30 + 6);
    }
}
