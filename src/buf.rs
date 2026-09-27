//! Byte storage for connections.
//!
//! * [`TxBuf`]: unacknowledged + unsent bytes in 64 KiB blocks, with an optional
//!   2 KiB first block for small writes. Any
//!   stream offset maps to (block, position) in O(1), so segmentation and
//!   re-segmentation after an MSS change never copy (§10.4). A segment spans at
//!   most two blocks.
//! * [`RxQueue`]: in-order bytes; production borrowed ingress is copied into
//!   charged compact chunks so packet-pool owners return immediately (§5).
//! * [`OooQueue`]: out-of-order ranges, allocated only when reordering happens (§6.1).

use crate::budget::{Budget, MemoryLease};
use crate::PeerId;
use bytes::{Bytes, BytesMut};
use std::collections::{BTreeMap, VecDeque};
use std::ops::{Deref, DerefMut};

pub const TX_BLOCK: usize = 64 * 1024;
const SMALL_TX_BLOCK: usize = 2048;
const BLOCK_METADATA_BYTES: u64 = 64;
pub(crate) const TX_BLOCK_CHARGE: u64 = TX_BLOCK as u64 + BLOCK_METADATA_BYTES;
const CHUNK_METADATA_BYTES: u64 = 64;
const OOO_DESCRIPTOR_BYTES: u64 = 128;

struct Block {
    data: Box<[u8]>,
    lease: MemoryLease,
}

impl Deref for Block {
    type Target = [u8];
    fn deref(&self) -> &Self::Target {
        &self.data
    }
}

impl DerefMut for Block {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.data
    }
}

/// Recycles TX blocks between connections of one shard.
#[derive(Default)]
pub struct BlockPool {
    free: Vec<Block>,
    small: Vec<Block>,
    pub max_cached: usize,
}

impl BlockPool {
    pub fn new(max_cached: usize) -> Self {
        BlockPool { free: Vec::new(), small: Vec::new(), max_cached }
    }
    fn get(&mut self, budget: &mut Budget, peer: PeerId, size: usize) -> Option<Block> {
        let free = if size == SMALL_TX_BLOCK { &mut self.small } else { &mut self.free };
        if let Some(mut block) = free.pop() {
            if block.lease.assign(budget, peer) {
                return Some(block);
            }
            free.push(block);
            return None;
        }
        let lease = budget.try_allocate_kind(peer, size as u64 + BLOCK_METADATA_BYTES, crate::budget::AllocationKind::TxBlock)?;
        Some(Block { data: vec![0u8; size].into_boxed_slice(), lease })
    }
    fn put(&mut self, mut b: Block) {
        // A previous short-flow phase must not pin every cache slot and force
        // subsequent bulk transfers to allocate a full block on every write.
        if self.cached() == self.max_cached {
            if b.len() == SMALL_TX_BLOCK { self.free.pop(); } else { self.small.pop(); }
        }
        if self.cached() < self.max_cached && b.lease.park_cached() {
            if b.len() == SMALL_TX_BLOCK { self.small.push(b); } else { self.free.push(b); }
        }
    }
    pub fn cached(&self) -> usize {
        self.free.len() + self.small.len()
    }
    /// Drop every idle block, releasing its global and port share. Returns
    /// the number of blocks and bytes released.
    pub fn reclaim(&mut self) -> (usize, u64) {
        let n = self.cached();
        let bytes = self.free.len() as u64 * TX_BLOCK_CHARGE
            + self.small.len() as u64 * (SMALL_TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
        self.free = Vec::new();
        self.small = Vec::new();
        (n, bytes)
    }
}

/// Send buffer. Offset 0 is the oldest unacknowledged byte.
#[derive(Default)]
pub struct TxBuf {
    blocks: VecDeque<Block>,
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
        self.blocks.front().map_or(0, |b| b.len() + (self.blocks.len() - 1) * TX_BLOCK)
    }

    pub fn push(&mut self, pool: &mut BlockPool, budget: &mut Budget, peer: PeerId, mut src: &[u8]) -> bool {
        let before = self.blocks.len();
        let Some(total) = self.head.checked_add(self.len).and_then(|n| n.checked_add(src.len())) else { return false };
        let first_size = self.blocks.front().map_or_else(
            || if src.len() <= SMALL_TX_BLOCK { SMALL_TX_BLOCK } else { TX_BLOCK }, |b| b.len());
        let needed = if total == 0 { 0 } else { 1 + total.saturating_sub(first_size).div_ceil(TX_BLOCK) };
        while self.blocks.len() < needed {
            let size = if self.blocks.is_empty() { first_size } else { TX_BLOCK };
            let Some(block) = pool.get(budget, peer, size) else {
                while self.blocks.len() > before {
                    pool.put(self.blocks.pop_back().unwrap());
                }
                self.trim_empty_index();
                return false;
            };
            self.blocks.push_back(block);
        }
        while !src.is_empty() {
            let end = self.head + self.len;
            let (bi, pos) = self.position(end);
            let n = (self.blocks[bi].len() - pos).min(src.len());
            self.blocks[bi][pos..pos + n].copy_from_slice(&src[..n]);
            self.len += n;
            src = &src[n..];
        }
        true
    }

    /// Drop `n` bytes from the front (acknowledged).
    pub fn consume(&mut self, pool: &mut BlockPool, n: usize) {
        let n = n.min(self.len);
        self.head += n;
        self.len -= n;
        while self.blocks.front().is_some_and(|b| self.head >= b.len()) {
            let b = self.blocks.pop_front().unwrap();
            self.head -= b.len();
            pool.put(b);
        }
        if self.len == 0 {
            // An idle connection, including TIME_WAIT, must not pin a 64 KiB
            // block. The shard pool retains only its bounded cache.
            while let Some(block) = self.blocks.pop_back() {
                pool.put(block);
            }
            self.head = 0;
            self.trim_empty_index();
        }
    }

    pub fn release_all(&mut self, pool: &mut BlockPool) {
        for b in self.blocks.drain(..) {
            pool.put(b);
        }
        self.head = 0;
        self.len = 0;
        self.trim_empty_index();
    }

    fn trim_empty_index(&mut self) {
        if self.blocks.is_empty() && self.blocks.capacity() > 8 {
            self.blocks = VecDeque::new();
        }
    }

    // Only the first block may be small. Every following block can hold a
    // maximum-size TCP segment, preserving the two-slice output contract.
    fn position(&self, offset: usize) -> (usize, usize) {
        let first = self.blocks.front().unwrap().len();
        if offset < first { (0, offset) } else { (1 + (offset - first) / TX_BLOCK, (offset - first) % TX_BLOCK) }
    }

    #[cfg(any(test, feature = "tokio"))]
    pub(crate) fn write_allocation_charge(&self, bytes: usize) -> u64 {
        if self.blocks.is_empty() && bytes <= SMALL_TX_BLOCK {
            SMALL_TX_BLOCK as u64 + BLOCK_METADATA_BYTES
        } else {
            TX_BLOCK_CHARGE
        }
    }

    /// Up to two slices covering `[off, off+len)`.
    pub fn slices(&self, off: usize, len: usize) -> [&[u8]; 2] {
        debug_assert!(off + len <= self.len);
        if len == 0 {
            return [&[], &[]];
        }
        let abs = self.head + off;
        let (bi, pos) = self.position(abs);
        let first = (self.blocks[bi].len() - pos).min(len);
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

struct ChargedChunk {
    data: Box<[u8]>,
    len: usize,
    _lease: MemoryLease,
}

struct ChargedPayload {
    data: Box<[u8]>,
    _lease: MemoryLease,
}

impl AsRef<[u8]> for ChargedPayload {
    fn as_ref(&self) -> &[u8] {
        &self.data
    }
}

impl AsRef<[u8]> for ChargedChunk {
    fn as_ref(&self) -> &[u8] {
        &self.data[..self.len]
    }
}

/// In-order received data waiting for the application: owned slices or charged
/// compact chunks, then `tail` (copied small segments, logically after `q`).
#[derive(Default)]
pub struct RxQueue {
    q: VecDeque<Bytes>,
    tail: BytesMut,
    charged_tail: Option<ChargedChunk>,
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
        self.seal_charged_tail();
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

    /// Copy borrowed ingress into reusable, compact chunks. The caller's IP
    /// buffer can return to its pool immediately after this call.
    pub fn push_borrowed(&mut self, mut data: &[u8]) {
        self.seal_charged_tail();
        self.len += data.len();
        while !data.is_empty() {
            if self.tail.capacity() == self.tail.len() {
                self.seal_tail();
                self.tail.reserve(RX_COPY_BUF);
            }
            let n = data.len().min(self.tail.capacity() - self.tail.len());
            self.tail.extend_from_slice(&data[..n]);
            data = &data[n..];
        }
    }

    /// Reserve the full backing of every new chunk before retaining any input.
    /// A failed reservation leaves the queue and advertised receive edge intact.
    pub fn push_charged(&mut self, mut data: &[u8], budget: &mut Budget, peer: PeerId) -> bool {
        if data.is_empty() {
            return true;
        }
        let current_free = self.charged_tail.as_ref().map_or(0, |c| c.data.len() - c.len);
        let mut remaining = data.len().saturating_sub(current_free);
        let mut fresh = VecDeque::new();
        while remaining > 0 {
            // A driver may transfer each MSS to the adapter immediately. A
            // fixed 8 KiB chunk would then pin 8 KiB for ~1.4 KiB payload.
            let cap = remaining.min(RX_COPY_BUF).next_power_of_two().max(64);
            let Some(lease) = budget.try_allocate_kind(peer, cap as u64 + CHUNK_METADATA_BYTES, crate::budget::AllocationKind::RxChunk) else { return false };
            fresh.push_back(ChargedChunk { data: vec![0u8; cap].into_boxed_slice(), len: 0, _lease: lease });
            remaining = remaining.saturating_sub(cap);
        }
        self.seal_tail();
        self.len += data.len();
        while !data.is_empty() {
            if self.charged_tail.as_ref().is_none_or(|c| c.len == c.data.len()) {
                self.seal_charged_tail();
                self.charged_tail = fresh.pop_front();
            }
            let chunk = self.charged_tail.as_mut().expect("pre-reserved RX chunk");
            let n = data.len().min(chunk.data.len() - chunk.len);
            chunk.data[chunk.len..chunk.len + n].copy_from_slice(&data[..n]);
            chunk.len += n;
            data = &data[n..];
        }
        true
    }

    fn seal_charged_tail(&mut self) {
        if let Some(chunk) = self.charged_tail.take() {
            if chunk.len != 0 {
                self.q.push_back(Bytes::from_owner(chunk));
            }
        }
    }

    /// Transfer an already accounted owner from the out-of-order queue.
    pub fn push_existing(&mut self, b: Bytes) {
        if b.is_empty() {
            return;
        }
        self.seal_tail();
        self.seal_charged_tail();
        self.len += b.len();
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
            self.seal_charged_tail();
        }
        let mut n = 0;
        while n < dst.len() {
            let Some(front) = self.q.front_mut() else {
                if self.tail.is_empty() {
                    break;
                }
                self.seal_tail();
                self.seal_charged_tail();
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
        self.trim_empty_index();
        n
    }
    pub fn read_chunk(&mut self, max: usize) -> Option<Bytes> {
        if self.q.is_empty() {
            self.seal_tail();
            self.seal_charged_tail();
        }
        let front = self.q.front_mut()?;
        let b = if front.len() <= max { self.q.pop_front().unwrap() } else { front.split_to(max) };
        self.len -= b.len();
        self.trim_empty_index();
        Some(b)
    }
    pub fn clear(&mut self) {
        self.q = VecDeque::new();
        self.tail = BytesMut::new();
        self.charged_tail = None;
        self.len = 0;
    }

    fn trim_empty_index(&mut self) {
        if self.q.is_empty() && self.q.capacity() > 8 {
            self.q = VecDeque::new();
        }
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
        self.insert_with(off, &data, |start, end| data.slice(start..end))
    }

    /// Borrowed ingress copies only uncovered gaps. A duplicate out-of-order
    /// segment allocates no payload storage.
    pub fn insert_borrowed(&mut self, off: u64, data: &[u8]) -> usize {
        self.insert_with(off, data, |start, end| Bytes::copy_from_slice(&data[start..end]))
    }

    /// Copy only uncovered bytes into one leased allocation. All resulting
    /// segment slices keep the lease until the final owner is consumed.
    pub fn insert_charged(&mut self, off: u64, data: &[u8], budget: &mut Budget, peer: PeerId) -> Option<usize> {
        let gaps = self.uncovered(off, data.len());
        if gaps.is_empty() {
            return Some(0);
        }
        let total: usize = gaps.iter().map(|(s, e)| (e - s) as usize).sum();
        let descriptors = (gaps.len() as u64).checked_mul(OOO_DESCRIPTOR_BYTES)?;
        let lease = budget.try_allocate_kind(peer, (total as u64).checked_add(descriptors)?, crate::budget::AllocationKind::Ooo)?;
        let mut packed = Vec::with_capacity(total);
        let mut offsets = VecDeque::with_capacity(gaps.len());
        for &(s, e) in &gaps {
            let start = packed.len();
            packed.extend_from_slice(&data[(s - off) as usize..(e - off) as usize]);
            offsets.push_back((start, packed.len()));
        }
        let owner = Bytes::from_owner(ChargedPayload { data: packed.into_boxed_slice(), _lease: lease });
        Some(self.insert_gaps(off, data.len(), gaps, |_, _| {
            let (start, end) = offsets.pop_front().expect("one slice per uncovered range");
            owner.slice(start..end)
        }))
    }

    fn insert_with(&mut self, off: u64, data: &[u8], make_piece: impl Fn(usize, usize) -> Bytes) -> usize {
        let gaps = self.uncovered(off, data.len());
        self.insert_gaps(off, data.len(), gaps, make_piece)
    }

    fn uncovered(&self, off: u64, len: usize) -> Vec<(u64, u64)> {
        let end = off + len as u64;
        if len == 0 {
            return Vec::new();
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
        gaps
    }

    fn insert_gaps(&mut self, off: u64, len: usize, gaps: Vec<(u64, u64)>, mut make_piece: impl FnMut(usize, usize) -> Bytes) -> usize {
        let end = off + len as u64;
        let mut added = 0;
        for (s, e) in gaps {
            let piece = make_piece((s - off) as usize, (e - off) as usize);
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
                out.push_existing(b.slice((next - s) as usize..));
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
    fn borrowed_ingress_chunks_outlive_source_and_only_stores_ooo_gaps() {
        let mut q = RxQueue::default();
        let mut packet = vec![0x5a; 1400];
        for _ in 0..20 {
            q.push_borrowed(&packet);
        }
        packet.fill(0);
        assert!(q.q.len() <= 4, "20 packets should be packed into a few chunks");
        assert_eq!(q.len(), 28_000);
        let mut data = vec![0; 28_000];
        assert_eq!(q.read(&mut data), data.len());
        assert!(data.iter().all(|&b| b == 0x5a));

        let mut ooo = OooQueue::default();
        let mut source = vec![7u8; 1400];
        assert_eq!(ooo.insert_borrowed(1400, &source), 1400);
        assert_eq!(ooo.insert_borrowed(1400, &source), 0);
        source.fill(0);
        let mut recv = RxQueue::default();
        let (next, discarded) = ooo.pop_contiguous(1400, &mut recv);
        assert_eq!((next, discarded), (2800, 0));
        let mut got = vec![0; 1400];
        assert_eq!(recv.read(&mut got), 1400);
        assert!(got.iter().all(|&b| b == 7));
    }

    #[test]
    fn rx_queue_copies_small_segments_in_order() {
        // Tiny segments must not each keep their own (packet-pinning) slice.
        let mut q = RxQueue::default();
        let mut expect = Vec::new();
        let mut k = 0u8;
        for i in 0..5000usize {
            let n = if i % 100 == 99 { 1400 } else { 1 + i % 7 };
            let v: Vec<u8> = (0..n)
                .map(|_| {
                    k = k.wrapping_add(1);
                    k
                })
                .collect();
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
    fn small_first_block_crosses_into_full_blocks_and_survives_partial_ack() {
        let mut pool = BlockPool::new(4);
        let global = crate::budget::GlobalBudget::new(2 << 20);
        let mut budget = Budget::new(global.clone());
        let mut tx = TxBuf::default();
        let mut expected: Vec<u8> = (0..1900).map(|n| (n % 251) as u8).collect();
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &expected));
        assert_eq!(tx.allocated(), SMALL_TX_BLOCK);
        assert_eq!(budget.physical_used(), SMALL_TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
        let tail: Vec<u8> = (0..2 * TX_BLOCK).map(|n| (n % 239) as u8).collect();
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &tail));
        expected.extend_from_slice(&tail);
        // A maximum segment beginning at the last byte of the small block
        // must still fit in exactly two slices, including on retransmission.
        for off in [0, SMALL_TX_BLOCK - 1, SMALL_TX_BLOCK, SMALL_TX_BLOCK + TX_BLOCK - 1] {
            let n = TX_BLOCK.min(expected.len() - off);
            let [a, b] = tx.slices(off, n);
            assert_eq!([a, b].concat(), expected[off..off + n]);
        }
        for consumed in [17, SMALL_TX_BLOCK - 18, 2, TX_BLOCK - 1] {
            tx.consume(&mut pool, consumed);
            expected.drain(..consumed);
            let n = TX_BLOCK.min(expected.len());
            let [a, b] = tx.slices(0, n);
            assert_eq!([a, b].concat(), expected[..n]);
        }
        tx.release_all(&mut pool);
        let (count, bytes) = pool.reclaim();
        assert_eq!(count, 3);
        assert_eq!(bytes, 2 * TX_BLOCK_CHARGE + SMALL_TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn small_tx_growth_failure_preserves_data_and_wait_size() {
        let small_charge = SMALL_TX_BLOCK as u64 + BLOCK_METADATA_BYTES;
        let global = crate::budget::GlobalBudget::new(small_charge);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(small_charge, small_charge, 4);
        let mut pool = BlockPool::new(0);
        let mut tx = TxBuf::default();
        assert_eq!(tx.write_allocation_charge(8), small_charge);
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), b"response"));
        assert_eq!(tx.write_allocation_charge(TX_BLOCK), TX_BLOCK_CHARGE);
        assert!(!tx.push(&mut pool, &mut budget, PeerId(1), &vec![7; TX_BLOCK]));
        assert_eq!(tx.len(), 8);
        assert_eq!(tx.slices(0, 8)[0], b"response");
        assert_eq!(global.reserved(), small_charge);
        tx.release_all(&mut pool);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn small_cached_block_moves_between_peers_and_waiter_fits_its_actual_size() {
        use std::sync::{Arc, atomic::{AtomicUsize, Ordering}};
        struct Wake(AtomicUsize);
        impl std::task::Wake for Wake { fn wake(self: Arc<Self>) { self.0.fetch_add(1, Ordering::Relaxed); } }
        let charge = SMALL_TX_BLOCK as u64 + BLOCK_METADATA_BYTES;
        let global = crate::budget::GlobalBudget::new(2 * charge);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(2 * charge, charge, 4);
        let mut pool = BlockPool::new(1);
        let mut tx = TxBuf::default();
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &[3]));
        tx.consume(&mut pool, 1);
        assert!(tx.push(&mut pool, &mut budget, PeerId(2), &[4]));
        assert_eq!(budget.physical_used(), charge);
        let memory = budget.memory_handle(PeerId(2));
        let wake = Arc::new(Wake(AtomicUsize::new(0)));
        let waiter = global.new_waiter_id();
        let waiting = TxBuf::default();
        assert!(memory.try_allocate_kind(charge, crate::budget::AllocationKind::TxBlock).is_none());
        global.register_physical_waiter(waiter, global.release_epoch(), &std::task::Waker::from(wake.clone()), &memory,
            waiting.write_allocation_charge(1), crate::budget::AllocationKind::TxBlock);
        assert_eq!(wake.0.load(Ordering::Relaxed), 0);
        tx.release_all(&mut pool);
        assert!(wake.0.load(Ordering::Relaxed) > 0);
        global.remove_waiter(waiter);
        assert!(tx.push(&mut pool, &mut budget, PeerId(2), &[5]));
    }

    #[test]
    fn cache_switches_size_classes_without_growing_its_slot_limit() {
        let global = crate::budget::GlobalBudget::new(1 << 20);
        let mut budget = Budget::new(global.clone());
        let mut pool = BlockPool::new(1);
        let mut tx = TxBuf::default();
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &[1]));
        tx.consume(&mut pool, 1);
        assert_eq!(pool.small.len(), 1);
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &vec![2; TX_BLOCK]));
        tx.consume(&mut pool, TX_BLOCK);
        assert_eq!((pool.small.len(), pool.free.len()), (0, 1));
        let ptr = pool.free[0].as_ptr();
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &vec![3; TX_BLOCK]));
        assert_eq!(tx.blocks[0].as_ptr(), ptr, "bulk block must be reused after short-flow cache occupancy");
        tx.consume(&mut pool, TX_BLOCK);
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &[4]));
        tx.consume(&mut pool, 1);
        assert_eq!((pool.small.len(), pool.free.len()), (1, 0));
        assert_eq!(pool.cached(), 1);
        pool.reclaim();
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn tx_ring() {
        let mut pool = BlockPool::new(4);
        let mut tx = TxBuf::default();
        let global = crate::budget::GlobalBudget::new(2 << 20);
        let mut budget = Budget::new(global);
        let data: Vec<u8> = (0..200_000u32).map(|i| (i % 251) as u8).collect();
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &data));
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
    fn tx_block_capacity_stays_charged_in_pool_and_changes_peer() {
        let block_charge = TX_BLOCK as u64 + BLOCK_METADATA_BYTES;
        let global = crate::budget::GlobalBudget::new(4 * TX_BLOCK as u64);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(2 * block_charge, block_charge, 4);
        let mut pool = BlockPool::new(1);
        let mut first = TxBuf::default();
        assert!(first.push(&mut pool, &mut budget, PeerId(1), &vec![7; TX_BLOCK]));
        assert_eq!(budget.physical_used(), TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
        first.consume(&mut pool, TX_BLOCK);
        assert_eq!(pool.cached(), 1);
        assert_eq!(budget.physical_used(), TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
        let mut second = TxBuf::default();
        assert!(second.push(&mut pool, &mut budget, PeerId(2), &vec![9; TX_BLOCK]));
        assert_eq!(budget.physical_used(), TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
        second.consume(&mut pool, TX_BLOCK);
        drop(pool);
        assert_eq!(budget.physical_used(), 0);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn idle_block_cache_is_shared_across_shards() {
        let global = crate::budget::GlobalBudget::new(1 << 20);
        let mut first_budget = Budget::new(global.clone());
        let mut second_budget = Budget::new(global.clone());
        let mut first_pool = BlockPool::new(16);
        let mut second_pool = BlockPool::new(16);
        let mut first = TxBuf::default();
        let mut second = TxBuf::default();
        assert!(first.push(&mut first_pool, &mut first_budget, PeerId(1), &vec![1; TX_BLOCK]));
        assert!(second.push(&mut second_pool, &mut second_budget, PeerId(2), &vec![2; TX_BLOCK]));
        first.consume(&mut first_pool, TX_BLOCK);
        second.consume(&mut second_pool, TX_BLOCK);
        assert_eq!(first_pool.cached() + second_pool.cached(), 1);
        assert_eq!(global.cached_bytes(), TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
        drop((first_pool, second_pool));
        assert_eq!(global.cached_bytes(), 0);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn hundred_idle_ports_do_not_pin_a_block_each() {
        let global = crate::budget::GlobalBudget::new(8 << 20);
        let mut ports = Vec::new();
        for peer in 0..100 {
            let mut budget = Budget::new(global.clone());
            let mut pool = BlockPool::new(16);
            let mut tx = TxBuf::default();
            assert!(tx.push(&mut pool, &mut budget, PeerId(peer), &[1]));
            tx.consume(&mut pool, 1);
            ports.push((budget, pool));
        }
        assert!(global.cached_bytes() <= 1 << 20);
        assert_eq!(global.reserved(), global.cached_bytes());
        drop(ports);
        assert_eq!(global.cached_bytes(), 0);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn tx_block_quota_failure_does_not_append_partial_data() {
        let global = crate::budget::GlobalBudget::new(TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(global.high(), global.high(), 4);
        let mut pool = BlockPool::new(0);
        let mut tx = TxBuf::default();
        assert!(!tx.push(&mut pool, &mut budget, PeerId(1), &vec![1; TX_BLOCK + 1]));
        assert_eq!(tx.len(), 0);
        assert_eq!(tx.allocated(), 0);
        assert_eq!(budget.physical_used(), 0);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn large_flow_indexes_release_high_water_after_drain() {
        let global = crate::budget::GlobalBudget::new(4 << 20);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(global.high(), global.high(), 4);
        let mut rx = RxQueue::default();
        for _ in 0..128 {
            assert!(rx.push_charged(&[7; 1400], &mut budget, PeerId(1)));
        }
        let mut received = 0;
        while !rx.is_empty() {
            received += rx.read_chunk(1400).unwrap().len();
        }
        assert_eq!(received, 128 * 1400);
        assert!(rx.q.capacity() <= 8, "drained RX retained its peak descriptor backing");
        assert_eq!(global.reserved(), 0);

        let mut pool = BlockPool::new(0);
        let mut tx = TxBuf::default();
        assert!(tx.push(&mut pool, &mut budget, PeerId(1), &vec![9; 32 * TX_BLOCK]));
        assert!(tx.blocks.capacity() > 8);
        tx.consume(&mut pool, tx.len());
        assert!(tx.blocks.capacity() <= 8, "drained TX retained its peak descriptor backing");
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn rx_chunk_lease_follows_partial_adapter_read() {
        let global = crate::budget::GlobalBudget::new(4096);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(4096, 4096, 4);
        let mut rx = RxQueue::default();
        assert!(rx.push_charged(&vec![4; 1400], &mut budget, PeerId(1)));
        assert_eq!(budget.physical_used(), 2048 + CHUNK_METADATA_BYTES);
        let one = rx.read_chunk(1).unwrap();
        assert_eq!(one.len(), 1);
        drop(rx);
        assert_eq!(budget.physical_used(), 2048 + CHUNK_METADATA_BYTES);
        drop(one);
        assert_eq!(budget.physical_used(), 0);
        assert_eq!(global.reserved(), 0);

        let small = crate::budget::GlobalBudget::new(1024);
        let mut budget = Budget::new(small.clone());
        let mut rx = RxQueue::default();
        assert!(!rx.push_charged(&vec![4; 1400], &mut budget, PeerId(1)));
        assert_eq!(rx.len(), 0);
        assert_eq!(small.reserved(), 0);
    }

    #[test]
    fn ooo_duplicate_does_not_allocate_and_owner_moves_to_rx() {
        let global = crate::budget::GlobalBudget::new(4096);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(4096, 4096, 4);
        let mut ooo = OooQueue::default();
        let payload = vec![5; 1400];
        assert_eq!(ooo.insert_charged(100, &payload, &mut budget, PeerId(1)), Some(1400));
        assert_eq!(budget.physical_used(), 1400 + OOO_DESCRIPTOR_BYTES);
        assert_eq!(ooo.insert_charged(100, &payload, &mut budget, PeerId(1)), Some(0));
        assert_eq!(budget.physical_used(), 1400 + OOO_DESCRIPTOR_BYTES);
        let mut rx = RxQueue::default();
        assert_eq!(ooo.pop_contiguous(100, &mut rx), (1500, 0));
        assert_eq!(rx.read_chunk(1400).unwrap().len(), 1400);
        assert_eq!(budget.physical_used(), 0);
        assert_eq!(global.reserved(), 0);
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
