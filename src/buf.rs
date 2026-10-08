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
            if b.len() == SMALL_TX_BLOCK {
                self.free.pop();
            } else {
                self.small.pop();
            }
        }
        if self.cached() < self.max_cached && b.lease.park_cached() {
            if b.len() == SMALL_TX_BLOCK {
                self.small.push(b);
            } else {
                self.free.push(b);
            }
        }
    }
    pub fn cached(&self) -> usize {
        self.free.len() + self.small.len()
    }
    /// Drop every idle block, releasing its global and port share. Returns
    /// the number of blocks and bytes released.
    pub fn reclaim(&mut self) -> (usize, u64) {
        let n = self.cached();
        let bytes = self.free.len() as u64 * TX_BLOCK_CHARGE + self.small.len() as u64 * (SMALL_TX_BLOCK as u64 + BLOCK_METADATA_BYTES);
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
        let first_size = self.blocks.front().map_or_else(|| if src.len() <= SMALL_TX_BLOCK { SMALL_TX_BLOCK } else { TX_BLOCK }, |b| b.len());
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

    /// Let `fill` write up to `max` bytes in place at the tail and return how
    /// many it wrote. It gets the rest of the last block and, when that is
    /// short, one fresh block, so a socket `readv` lands directly in the send
    /// buffer. Returns `Ok(None)` when no block could be allocated; a fresh
    /// block that `fill` leaves empty goes straight back to the pool.
    pub fn write_with<E>(
        &mut self,
        pool: &mut BlockPool,
        budget: &mut Budget,
        peer: PeerId,
        max: usize,
        fill: impl FnOnce([&mut [u8]; 2]) -> Result<usize, E>,
    ) -> Result<Option<usize>, E> {
        const SECOND_BELOW: usize = TX_BLOCK;
        if max == 0 {
            return fill([&mut [], &mut []]).map(|_| Some(0));
        }
        let before = self.blocks.len();
        let end = self.head + self.len;
        let spare = self.allocated().saturating_sub(end);
        if spare < max.min(SECOND_BELOW) {
            let size = if self.blocks.is_empty() && max <= SMALL_TX_BLOCK { SMALL_TX_BLOCK } else { TX_BLOCK };
            match pool.get(budget, peer, size) {
                Some(block) => self.blocks.push_back(block),
                None if spare == 0 => {
                    self.trim_empty_index();
                    return Ok(None);
                }
                None => {}
            }
        }
        // `end` lies inside the old tail block, or at the start of the fresh one.
        let (bi, pos) = self.position(end);
        let mut it = self.blocks.range_mut(bi..);
        let a = it.next().map_or(&mut [][..], |blk| &mut blk[pos..]);
        let b = it.next().map_or(&mut [][..], |blk| &mut blk[..]);
        // Bytes land in `a` first, then `b` (readv order); `b` is offered
        // only when `a` is offered whole, so filled bytes stay contiguous.
        let la = a.len().min(max);
        let lb = b.len().min(max - la);
        let result = fill([&mut a[..la], &mut b[..lb]]);
        let n = *result.as_ref().unwrap_or(&0);
        assert!(n <= la + lb, "write_with filled more than offered");
        self.len += n;
        while self.blocks.len() > before && self.head + self.len <= self.allocated() - self.blocks.back().map_or(0, |b| b.len()) {
            pool.put(self.blocks.pop_back().unwrap());
        }
        if self.len == 0 {
            while let Some(block) = self.blocks.pop_back() {
                pool.put(block);
            }
            self.head = 0;
            self.trim_empty_index();
        }
        result.map(Some)
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
        if offset < first {
            (0, offset)
        } else {
            (1 + (offset - first) / TX_BLOCK, (offset - first) % TX_BLOCK)
        }
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
/// Largest single RX chunk: a maximum-size IP payload fits one.
const RX_JUMBO_CHUNK: usize = 64 * 1024;
// The IP parser accepts only ordinary 16-bit IP lengths (no jumbograms).
// Keep room for the packet that fills an OOO hole, not just the OOO owners.
const RX_GAP_SLOTS: usize = (u16::MAX as usize).div_ceil(RX_COPY_BUF);

#[cfg(test)]
pub(crate) fn rx_ooo_payload_failure_budget(payload_len: usize) -> u64 {
    let slots = (1 + RX_GAP_SLOTS).max(4).next_power_of_two();
    let index_reservation = (slots * std::mem::size_of::<Bytes>() * 2) as u64;
    // The future RX index fits, but the following OOO owner misses by one byte.
    index_reservation + payload_len as u64 + OOO_DESCRIPTOR_BYTES - 1
}

/// A prepared replacement is unpublished until all payload reservations pass.
/// Field order releases the allocation before its lease, also on rollback.
struct RxIndex {
    items: VecDeque<Bytes>,
    lease: MemoryLease,
}

#[cfg(test)]
thread_local! {
    // Fail exactly the next descriptor allocation on this test thread.
    static FAIL_RX_INDEX_ALLOCATION: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Filled bytes are `data[..]`; `data.capacity()` is the charged backing, so
/// a fresh chunk is never zero-filled before the copy overwrites it.
struct ChargedChunk {
    data: Vec<u8>,
    _lease: MemoryLease,
}

impl ChargedChunk {
    fn free(&self) -> usize {
        self.data.capacity() - self.data.len()
    }
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
        &self.data
    }
}

/// In-order received data waiting for the application: owned slices or charged
/// compact chunks, then `tail` (copied small segments, logically after `q`).
#[derive(Default)]
pub struct RxQueue {
    q: VecDeque<Bytes>,
    /// Covers retained descriptor capacity, independently of payload owners.
    index_lease: Option<MemoryLease>,
    /// Slots promised to OOO owners at admission, before they can be SACKed.
    ooo_slots: usize,
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
    /// Unaccounted standalone helper. Do not mix with charged queue storage.
    pub fn push(&mut self, b: Bytes) {
        assert!(self.index_lease.is_none(), "use charged RX insertion");
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
        assert!(self.index_lease.is_none(), "use charged RX insertion");
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
        let current_free = self.charged_tail.as_ref().map_or(0, ChargedChunk::free);
        let mut remaining = data.len().saturating_sub(current_free);
        // Ordinary IP payloads need at most one new chunk. Keep that chunk
        // inline; larger public-API inputs use the overflow deque. Declaring
        // overflow first preserves first-to-last release order on rollback.
        let mut overflow = VecDeque::new();
        let mut first = None;
        while remaining > 0 {
            // A driver may transfer each MSS to the adapter immediately. A
            // fixed 8 KiB chunk would then pin 8 KiB for ~1.4 KiB payload.
            // A jumbo segment (64 KiB TUN MTU) gets one chunk of its own size
            // rather than eight small allocations and leases.
            let cap = if remaining > RX_COPY_BUF { remaining.min(RX_JUMBO_CHUNK) } else { remaining.next_power_of_two().max(64) };
            let Some(lease) = budget.try_allocate_kind(peer, cap as u64 + CHUNK_METADATA_BYTES, crate::budget::AllocationKind::RxChunk) else { return false };
            let mut data = Vec::new();
            if data.try_reserve_exact(cap).is_err() || data.capacity() > cap {
                return false;
            }
            let chunk = ChargedChunk { data, _lease: lease };
            if first.is_none() {
                first = Some(chunk);
            } else {
                overflow.push_back(chunk);
            }
            remaining = remaining.saturating_sub(cap);
        }
        let Some(slots) = self
            .retained_slots()
            .checked_add(usize::from(first.is_some()))
            .and_then(|n| n.checked_add(overflow.len()))
            .and_then(|n| n.checked_add(self.ooo_slots))
        else {
            return false;
        };
        let Ok(index) = self.prepare_index(slots, budget, peer) else { return false };
        self.commit_index(index);
        self.seal_tail();
        self.len += data.len();
        while !data.is_empty() {
            if self.charged_tail.as_ref().is_none_or(|c| c.free() == 0) {
                self.seal_charged_tail();
                self.charged_tail = first.take().or_else(|| overflow.pop_front());
            }
            let chunk = self.charged_tail.as_mut().expect("pre-reserved RX chunk");
            let n = data.len().min(chunk.free());
            chunk.data.extend_from_slice(&data[..n]);
            data = &data[n..];
        }
        true
    }

    /// Keep `b` itself: its owner already holds a lease for its whole
    /// backing (a `PacketBuf`). Only the descriptor slot is reserved, so a
    /// failure leaves the queue and the receive edge as they were.
    pub fn push_leased(&mut self, b: Bytes, budget: &mut Budget, peer: PeerId) -> bool {
        if b.is_empty() {
            return true;
        }
        let Some(slots) = self.retained_slots().checked_add(1).and_then(|n| n.checked_add(self.ooo_slots)) else { return false };
        let Ok(index) = self.prepare_index(slots, budget, peer) else { return false };
        self.commit_index(index);
        self.push_existing_reserved(b);
        true
    }

    fn seal_charged_tail(&mut self) {
        if let Some(chunk) = self.charged_tail.take() {
            if !chunk.data.is_empty() {
                self.push_indexed(Bytes::from_owner(chunk));
            }
        }
    }

    /// Standalone owner transfer without descriptor accounting. Production
    /// OOO transfers use the slots reserved by `insert_charged_for_rx`.
    pub fn push_existing(&mut self, b: Bytes) {
        assert!(self.index_lease.is_none(), "use reserved OOO transfer");
        self.push_existing_reserved(b);
    }

    fn push_existing_reserved(&mut self, b: Bytes) {
        if b.is_empty() {
            return;
        }
        self.seal_tail();
        self.seal_charged_tail();
        self.len += b.len();
        self.push_indexed(b);
    }
    /// Move copied bytes into `q` (keeping the tail's spare capacity for later copies).
    fn seal_tail(&mut self) {
        if !self.tail.is_empty() {
            let tail = self.tail.split().freeze();
            self.push_indexed(tail);
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
        self.index_lease = None;
        self.ooo_slots = 0;
        self.tail = BytesMut::new();
        self.charged_tail = None;
        self.len = 0;
    }

    fn trim_empty_index(&mut self) {
        if self.retained_slots() == 0 && self.ooo_slots == 0 && self.q.capacity() > 8 {
            self.q = VecDeque::new();
            self.index_lease = None;
        }
    }

    fn retained_slots(&self) -> usize {
        self.q.len() + usize::from(!self.tail.is_empty()) + usize::from(self.charged_tail.is_some())
    }

    fn push_indexed(&mut self, b: Bytes) {
        // Reads and OOO transfer cannot grow RX descriptor backing: slots
        // were reserved before accepting the bytes. Never consume promises.
        assert!(self.index_lease.is_none() || self.q.len() + self.ooo_slots < self.q.capacity(), "RX descriptor grew without a reservation");
        self.q.push_back(b);
    }

    fn prepare_index(&self, slots: usize, budget: &mut Budget, peer: PeerId) -> Result<Option<RxIndex>, ()> {
        if slots <= self.q.capacity() && (self.q.capacity() == 0 || self.index_lease.is_some()) {
            return Ok(None);
        }
        let target = slots.max(self.q.capacity()).max(4).checked_next_power_of_two().ok_or(())?;
        // As with the send scoreboard, reserve allocator slack before the
        // allocation and check its actual capacity. Both old and replacement
        // leases remain live until the old descriptor backing is freed.
        let bytes = target.checked_mul(std::mem::size_of::<Bytes>()).and_then(|n| n.checked_mul(2)).ok_or(())?;
        let lease = budget.try_allocate_kind(peer, u64::try_from(bytes).map_err(|_| ())?, crate::budget::AllocationKind::RxChunk).ok_or(())?;
        #[cfg(test)]
        if FAIL_RX_INDEX_ALLOCATION.with(|fail| fail.replace(false)) {
            return Err(());
        }
        let mut items = VecDeque::new();
        items.try_reserve_exact(target).map_err(|_| ())?;
        if items.capacity().checked_mul(std::mem::size_of::<Bytes>()).is_none_or(|actual| actual > bytes) {
            return Err(());
        }
        Ok(Some(RxIndex { items, lease }))
    }

    fn commit_index(&mut self, index: Option<RxIndex>) {
        if let Some(mut index) = index {
            index.items.extend(self.q.drain(..));
            let old = std::mem::replace(&mut self.q, index.items);
            drop(old);
            self.index_lease = Some(index.lease);
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
        self.insert_charged_gaps(off, data, gaps, budget, peer)
    }

    fn insert_charged_gaps(&mut self, off: u64, data: &[u8], gaps: Vec<(u64, u64)>, budget: &mut Budget, peer: PeerId) -> Option<usize> {
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

    /// Production admission also reserves the future RX descriptor slots.
    /// No OOO owner is published/SACKed until both reservations have passed.
    pub(crate) fn insert_charged_for_rx(&mut self, off: u64, data: &[u8], budget: &mut Budget, peer: PeerId, rx: &mut RxQueue) -> Option<usize> {
        debug_assert_eq!(rx.ooo_slots, self.segs.len());
        let gaps = self.uncovered(off, data.len());
        if gaps.is_empty() {
            return Some(0);
        }
        let promised = self.segs.len().checked_add(gaps.len())?;
        let slots = rx.retained_slots().checked_add(promised)?.checked_add(RX_GAP_SLOTS)?;
        let index = rx.prepare_index(slots, budget, peer).ok()?;
        // No RX metadata changes until the payload/OOO-descriptor
        // reservation also succeeds. Reuse the gaps computed above.
        let added = self.insert_charged_gaps(off, data, gaps, budget, peer)?;
        rx.commit_index(index);
        rx.ooo_slots = promised;
        Some(added)
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
        assert!(out.index_lease.is_none(), "use reserved OOO transfer");
        self.pop_contiguous_inner(off, out, false)
    }

    /// All RX slots were promised at OOO admission: no descriptor growth or
    /// quota failure remains after an in-order receive edge is committed.
    pub(crate) fn pop_contiguous_reserved(&mut self, off: u64, out: &mut RxQueue) -> (u64, u64) {
        debug_assert_eq!(out.ooo_slots, self.segs.len());
        self.pop_contiguous_inner(off, out, true)
    }

    fn pop_contiguous_inner(&mut self, off: u64, out: &mut RxQueue, reserved: bool) -> (u64, u64) {
        let mut next = off;
        let mut discarded = 0u64;
        while let Some((&s, _)) = self.segs.iter().next() {
            if s > next {
                break;
            }
            if reserved {
                out.ooo_slots -= 1;
            }
            let b = self.segs.remove(&s).unwrap();
            self.bytes -= b.len();
            let e = s + b.len() as u64;
            if e > next {
                discarded += next - s;
                out.push_existing_reserved(b.slice((next - s) as usize..));
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
        out.trim_empty_index();
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
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        struct Wake(AtomicUsize);
        impl std::task::Wake for Wake {
            fn wake(self: Arc<Self>) {
                self.0.fetch_add(1, Ordering::Relaxed);
            }
        }
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
        global.register_physical_waiter(
            waiter,
            global.release_epoch(),
            &std::task::Waker::from(wake.clone()),
            &memory,
            waiting.write_allocation_charge(1),
            crate::budget::AllocationKind::TxBlock,
        );
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
    fn tx_write_with_fills_in_place_and_keeps_layout() {
        let global = crate::budget::GlobalBudget::new(4 << 20);
        let mut budget = Budget::new(global.clone());
        let mut pool = BlockPool::new(4);
        let mut tx = TxBuf::default();
        let data: Vec<u8> = (0..600_000u32).map(|i| (i % 251) as u8).collect();
        let mut written = 0usize;
        let mut acked = 0usize;
        // Mix in-place fills of odd sizes (as a socket readv returns them)
        // with plain pushes, partial fills and acknowledgements.
        let sizes = [1usize, 700, 2048, 2049, 65_535, 65_536, 100_000, 3, 40_000, 70_000];
        for (round, &want) in sizes.iter().cycle().take(30).enumerate() {
            if round % 4 == 3 {
                let n = want.min(data.len() - written).min(5000);
                assert!(tx.push(&mut pool, &mut budget, PeerId(1), &data[written..written + n]));
                written += n;
                continue;
            }
            let want = want.min(data.len() - written);
            let fill_to = if round % 5 == 1 { want / 2 } else { want };
            let n = tx
                .write_with(&mut pool, &mut budget, PeerId(1), want, |[a, b]| -> Result<usize, ()> {
                    assert!(a.len() + b.len() <= want);
                    let mut src = &data[written..written + fill_to];
                    let mut n = 0;
                    for s in [a, b] {
                        let k = s.len().min(src.len());
                        s[..k].copy_from_slice(&src[..k]);
                        src = &src[k..];
                        n += k;
                    }
                    Ok(n)
                })
                .unwrap()
                .unwrap();
            assert!(n <= fill_to);
            assert!(n > 0 || want == 0);
            written += n;
            assert_eq!(tx.len(), written - acked);
            // Every byte reads back through the two-slice segment view.
            let mut off = 0;
            while off < tx.len() {
                let len = (tx.len() - off).min(TX_BLOCK);
                let [a, b] = tx.slices(off, len);
                assert_eq!([a, b].concat(), &data[acked + off..acked + off + len]);
                off += len;
            }
            if round % 3 == 2 {
                let n = tx.len() / 2 + 1;
                tx.consume(&mut pool, n.min(tx.len()));
                acked += n.min(written - acked);
            }
            // No allocated block beyond the one holding the tail.
            assert!(tx.allocated() < tx.head + tx.len + TX_BLOCK || tx.is_empty());
        }
        // A fill that writes nothing leaves no block behind.
        tx.consume(&mut pool, tx.len());
        let n = tx.write_with(&mut pool, &mut budget, PeerId(1), 10_000, |_| -> Result<usize, ()> { Ok(0) }).unwrap();
        assert_eq!(n, Some(0));
        assert_eq!(tx.allocated(), 0);
        assert!(tx.write_with(&mut pool, &mut budget, PeerId(1), 10, |_| Err::<usize, _>("io")).is_err());
        assert_eq!(tx.allocated(), 0);
        tx.release_all(&mut pool);
        pool.reclaim();
        assert_eq!(global.reserved(), 0);
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
        assert!(rx.index_lease.is_some());
        assert!(budget.physical_used() >= 2048 + CHUNK_METADATA_BYTES + (rx.q.capacity() * std::mem::size_of::<Bytes>()) as u64);
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

#[cfg(test)]
mod rx_descriptor_tests {
    use super::*;
    use crate::budget::{GlobalBudget, Level};

    fn budget(high: u64) -> (std::sync::Arc<GlobalBudget>, Budget) {
        let global = GlobalBudget::new(high);
        let mut budget = Budget::new(global.clone());
        budget.set_limits(high, high, 8);
        (global, budget)
    }

    #[test]
    fn rx_staged_chunks_preserve_tail_order_and_exported_owner_charge() {
        for chunks in 1..=3 {
            let (global, mut budget) = budget(1 << 20);
            let mut rx = RxQueue::default();
            let prefix = [0xa5; 63];
            assert!(rx.push_charged(&prefix, &mut budget, PeerId(1)));
            // Fill the old tail's final byte, then stage one, two or three
            // chunks. Larger inputs exercise the public API beyond IP MTU.
            let payload: Vec<u8> = (0..1 + (chunks - 1) * RX_JUMBO_CHUNK + 1379).map(|i| (i % 251) as u8).collect();
            assert!(rx.push_charged(&payload, &mut budget, PeerId(1)));
            let owner = rx.read_chunk(17).unwrap();
            let mut got = owner.to_vec();
            while let Some(chunk) = rx.read_chunk(997) {
                got.extend_from_slice(&chunk);
            }
            let mut expected = prefix.to_vec();
            expected.extend_from_slice(&payload);
            assert_eq!(got, expected);
            assert_eq!(rx.len(), 0);
            rx.clear();
            assert_eq!(global.reserved(), 64 + CHUNK_METADATA_BYTES, "the exported prefix retains its whole owner");
            drop(owner);
            assert_eq!(global.reserved(), 0);
        }
    }

    #[test]
    fn rx_second_staged_chunk_quota_failure_rolls_back_at_every_level() {
        const LIMIT: u64 = 1 << 20;
        for (level, (high, port, peer)) in
            [(Level::Global, (LIMIT, LIMIT, LIMIT)), (Level::Port, (2 * LIMIT, LIMIT, LIMIT)), (Level::Peer, (2 * LIMIT, 2 * LIMIT, LIMIT))]
        {
            let global = GlobalBudget::new(high);
            let mut budget = Budget::new(global.clone());
            budget.set_limits(port, peer, 8);
            let mut rx = RxQueue::default();
            assert!(rx.push_charged(&[7; 63], &mut budget, PeerId(1)));
            let first_charge = RX_JUMBO_CHUNK as u64 + CHUNK_METADATA_BYTES;
            let second_charge = 64 + CHUNK_METADATA_BYTES;
            let ceiling = LIMIT - global.headroom(LIMIT, crate::budget::Tier::Bulk);
            // The first provisional chunk fits; the second misses by one.
            let blocker = budget
                .try_allocate_kind(PeerId(1), ceiling - budget.physical_used() - first_charge - second_charge + 1, crate::budget::AllocationKind::RxChunk)
                .unwrap();
            let before = global.reserved();
            let payload: Vec<u8> = (0..1 + RX_JUMBO_CHUNK + 32).map(|i| (i % 251) as u8).collect();
            assert!(!rx.push_charged(&payload, &mut budget, PeerId(1)));
            assert_eq!(rx.len(), 63);
            assert_eq!(rx.q.capacity(), 4);
            assert_eq!(rx.charged_tail.as_ref().unwrap().data, [7; 63]);
            assert_eq!(global.reserved(), before, "failed staging must release every provisional owner");
            assert_eq!(budget.stats().failures().rx_chunk, 1);
            let mut failures = [0; 3];
            failures[level as usize] = 1;
            assert_eq!(budget.stats().failures_by_level(), failures);
            drop(blocker);
            assert!(rx.push_charged(&payload, &mut budget, PeerId(1)));
            let mut got = Vec::new();
            while let Some(chunk) = rx.read_chunk(usize::MAX) {
                got.extend_from_slice(&chunk);
            }
            let mut expected = vec![7; 63];
            expected.extend_from_slice(&payload);
            assert_eq!(got, expected);
            rx.clear();
            assert_eq!(global.reserved(), 0);
        }
    }

    #[test]
    fn retained_rx_descriptor_capacity_remains_funded_after_partial_drain() {
        let (global, mut budget) = budget(1 << 20);
        let mut rx = RxQueue::default();
        for _ in 0..128 {
            assert!(rx.push_charged(&[7; 64], &mut budget, PeerId(1)));
        }
        let cap = rx.q.capacity();
        let backing = (cap * std::mem::size_of::<Bytes>()) as u64;
        assert_eq!(backing, 4096);
        for _ in 0..126 {
            drop(rx.read_chunk(64).unwrap());
        }
        assert_eq!(rx.len(), 128);
        assert_eq!(rx.q.capacity(), cap);
        // Payload leases still cover two 64-byte owners and their metadata.
        let owners = 2 * (64 + CHUNK_METADATA_BYTES);
        assert!(
            budget.physical_used() >= owners + backing,
            "retained descriptor backing is not funded: used={}, owners={owners}, backing={backing}",
            budget.physical_used()
        );
        println!("RX_CAPACITY payload={} actual_backing={} physical_used={} owner_charge={}", rx.len(), backing, budget.physical_used(), owners);
        assert_eq!(budget.used, 0, "physical metadata is not logical TCP payload");
        rx.clear();
        assert_eq!(budget.physical_used(), 0);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn rx_descriptor_growth_rolls_back_at_global_port_and_peer_limits() {
        for level in [Level::Global, Level::Port, Level::Peer] {
            let high = if level == Level::Global { 4096 } else { 8192 };
            let global = GlobalBudget::new(high);
            let mut budget = Budget::new(global.clone());
            budget.set_limits(if level == Level::Port { 4096 } else { high }, 4096, 8);
            let mut rx = RxQueue::default();
            for _ in 0..4 {
                assert!(rx.push_charged(&[3; 64], &mut budget, PeerId(1)));
            }
            assert_eq!(rx.q.capacity(), 4);
            let next_index = 8 * std::mem::size_of::<Bytes>() as u64 * 2;
            let blocker = budget.try_allocate(PeerId(1), 4096 - budget.physical_used() - 128 - next_index + 1).unwrap();
            let before = global.reserved();
            assert!(!rx.push_charged(&[4; 64], &mut budget, PeerId(1)));
            assert_eq!(rx.len(), 256);
            assert_eq!(rx.q.capacity(), 4);
            assert_eq!(rx.charged_tail.as_ref().unwrap().data[0], 3);
            assert_eq!(global.reserved(), before, "failed growth retained a provisional lease");
            let index = match level {
                Level::Global => 0,
                Level::Port => 1,
                Level::Peer => 2,
            };
            assert_eq!(budget.stats().failures_by_level()[index], 1);
            drop(blocker);
            assert!(rx.push_charged(&[4; 64], &mut budget, PeerId(1)));
            let mut got = Vec::new();
            while let Some(chunk) = rx.read_chunk(usize::MAX) {
                got.extend_from_slice(&chunk);
            }
            assert_eq!(got, [vec![3; 256], vec![4; 64]].concat());
            rx.clear();
            assert_eq!(global.reserved(), 0);
        }
    }

    #[test]
    fn rx_descriptor_allocator_failure_preserves_payload_tail_and_capacity() {
        for len in [65, RX_JUMBO_CHUNK + 65, 2 * RX_JUMBO_CHUNK + 65] {
            let (global, mut budget) = budget(1 << 20);
            let mut rx = RxQueue::default();
            for _ in 0..3 {
                assert!(rx.push_charged(&[8; 64], &mut budget, PeerId(1)));
            }
            assert!(rx.push_charged(&[9; 63], &mut budget, PeerId(1)));
            let before = global.reserved();
            let payload = vec![5; len];
            FAIL_RX_INDEX_ALLOCATION.with(|fail| fail.set(true));
            assert!(!rx.push_charged(&payload, &mut budget, PeerId(1)));
            assert_eq!(rx.len(), 255);
            assert_eq!(rx.q.capacity(), 4);
            assert_eq!(rx.charged_tail.as_ref().unwrap().data.len(), 63);
            assert_eq!(global.reserved(), before);
            assert!(rx.push_charged(&payload, &mut budget, PeerId(1)));
            let mut got = Vec::new();
            while let Some(chunk) = rx.read_chunk(usize::MAX) {
                got.extend_from_slice(&chunk);
            }
            assert_eq!(got, [vec![8; 192], vec![9; 63], payload].concat());
            rx.clear();
            assert_eq!(global.reserved(), 0);
        }
    }

    #[test]
    fn rx_descriptor_ooo_admission_is_transactional_and_duplicates_allocate_nothing() {
        let (global, mut budget) = budget(1 << 20);
        let mut rx = RxQueue::default();
        let mut ooo = OooQueue::default();
        FAIL_RX_INDEX_ALLOCATION.with(|fail| fail.set(true));
        assert_eq!(ooo.insert_charged_for_rx(64, &[2; 64], &mut budget, PeerId(1), &mut rx), None);
        assert_eq!((ooo.bytes(), ooo.segments(), rx.ooo_slots, rx.q.capacity()), (0, 0, 0, 0));
        assert_eq!(global.reserved(), 0);
        assert_eq!(ooo.insert_charged_for_rx(64, &[2; 64], &mut budget, PeerId(1), &mut rx), Some(64));
        let before = global.reserved();
        assert_eq!(ooo.insert_charged_for_rx(64, &[9; 64], &mut budget, PeerId(1), &mut rx), Some(0));
        assert_eq!(global.reserved(), before);
        assert_eq!(rx.ooo_slots, 1);
        assert_eq!(ooo.pop_contiguous_reserved(64, &mut rx), (128, 0));
        assert_eq!(rx.ooo_slots, 0);
        let owner = rx.read_chunk(1).unwrap();
        rx.clear();
        assert_eq!(global.reserved(), 64 + OOO_DESCRIPTOR_BYTES, "OOO payload lease follows the exported slice");
        assert_eq!(&owner[..], &[2]);
        drop(owner);
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn rx_descriptor_ooo_payload_failure_rolls_back_prepared_backing() {
        // Derive the exact failure phase from descriptor and owner charges.
        let (global, mut budget) = budget(rx_ooo_payload_failure_budget(64));
        let mut rx = RxQueue::default();
        let mut ooo = OooQueue::default();
        assert_eq!(ooo.insert_charged_for_rx(64, &[2; 64], &mut budget, PeerId(1), &mut rx), None);
        assert_eq!((ooo.bytes(), ooo.segments(), rx.ooo_slots, rx.q.capacity()), (0, 0, 0, 0));
        assert_eq!(budget.stats().failures().ooo, 1, "OOO payload must be the failed reservation");
        assert_eq!(budget.stats().failures().rx_chunk, 0, "future RX index must have fitted");
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn rx_descriptor_ooo_transfer_and_overlap_need_no_space_after_admission() {
        for overlap in [false, true] {
            let (global, mut budget) = budget(8192);
            let mut rx = RxQueue::default();
            let mut ooo = OooQueue::default();
            assert!(rx.push_charged(&[1; 63], &mut budget, PeerId(1)));
            let off = if overlap { 63 } else { 64 };
            assert_eq!(ooo.insert_charged_for_rx(off, &[2], &mut budget, PeerId(1), &mut rx), Some(1));
            let capacity = rx.q.capacity();
            let blocker = budget.try_allocate(PeerId(1), 8192 - global.reserved()).unwrap();
            assert!(rx.push_charged(&[3], &mut budget, PeerId(1)), "existing tail should fill with no allocation");
            let end = if overlap { (64, 1) } else { (65, 0) };
            assert_eq!(ooo.pop_contiguous_reserved(64, &mut rx), end);
            assert_eq!(rx.q.capacity(), capacity);
            assert_eq!(rx.ooo_slots, 0);
            assert_eq!(budget.stats().failures().rx_chunk, 0);
            let mut got = Vec::new();
            while let Some(chunk) = rx.read_chunk(usize::MAX) {
                got.extend_from_slice(&chunk);
            }
            let mut expected = vec![1; 63];
            expected.push(3);
            if !overlap {
                expected.push(2);
            }
            assert_eq!(got, expected);
            drop(blocker);
            rx.clear();
            assert_eq!(global.reserved(), 0);
        }
    }

    #[test]
    fn rx_descriptor_drain_preserves_ooo_promises_and_max_packet_headroom() {
        let (global, mut budget) = budget(128 << 10);
        let mut rx = RxQueue::default();
        let mut ooo = OooQueue::default();
        assert!(rx.push_charged(&[7; 64], &mut budget, PeerId(1)));
        assert_eq!(ooo.insert_charged_for_rx(65599, &[2], &mut budget, PeerId(1), &mut rx), Some(1));
        drop(rx.read_chunk(64).unwrap());
        assert!(rx.is_empty());
        assert_eq!(rx.ooo_slots, 1);
        assert!(rx.q.capacity() >= 1 + RX_GAP_SLOTS);
        let capacity = rx.q.capacity();
        // Keep only the payload capacity available, no descriptor growth room.
        let payload_charge = 8 * (8192 + CHUNK_METADATA_BYTES);
        let available = (128 << 10) - global.reserved() - payload_charge;
        let blocker = budget.try_allocate(PeerId(1), available).unwrap();
        assert!(rx.push_charged(&vec![3; 65535], &mut budget, PeerId(1)));
        assert_eq!(ooo.pop_contiguous_reserved(65599, &mut rx), (65600, 0));
        assert_eq!(rx.q.capacity(), capacity);
        assert_eq!(rx.ooo_slots, 0);
        let mut got = Vec::new();
        while let Some(chunk) = rx.read_chunk(usize::MAX) {
            got.extend_from_slice(&chunk);
        }
        assert_eq!(got, [vec![3; 65535], vec![2]].concat());
        drop(blocker);
        rx.clear();
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn rx_descriptor_multi_gap_ooo_admission_counts_each_owner_and_partial_overlap() {
        let (global, mut budget) = budget(32768);
        let mut rx = RxQueue::default();
        let mut ooo = OooQueue::default();
        assert_eq!(ooo.insert_charged_for_rx(100, &[1; 10], &mut budget, PeerId(1), &mut rx), Some(10));
        assert_eq!(ooo.insert_charged_for_rx(120, &[2; 10], &mut budget, PeerId(1), &mut rx), Some(10));
        assert_eq!(ooo.insert_charged_for_rx(90, &[3; 50], &mut budget, PeerId(1), &mut rx), Some(30));
        assert_eq!((ooo.segments(), rx.ooo_slots), (5, 5));
        let before = global.reserved();
        assert_eq!(ooo.insert_charged_for_rx(90, &[9; 50], &mut budget, PeerId(1), &mut rx), Some(0));
        assert_eq!((ooo.segments(), rx.ooo_slots), (5, 5));
        assert_eq!(global.reserved(), before);
        // First owner loses a prefix but must transfer its remaining suffix.
        assert_eq!(ooo.pop_contiguous_reserved(95, &mut rx), (140, 5));
        assert_eq!(rx.ooo_slots, 0);
        assert!(ooo.is_empty());
        let mut got = Vec::new();
        while let Some(chunk) = rx.read_chunk(usize::MAX) {
            got.extend_from_slice(&chunk);
        }
        assert_eq!(got, [vec![3; 5], vec![1; 10], vec![3; 10], vec![2; 10], vec![3; 10]].concat());
        rx.clear();
        assert_eq!(global.reserved(), 0);
    }

    #[test]
    fn rx_descriptor_admission_respects_each_debt_without_claiming_tx_drain() {
        for level in [Level::Global, Level::Port, Level::Peer] {
            let (global, mut budget) = budget(1 << 20);
            let peer = PeerId(1);
            let debt = budget.admit_debt(peer, level, 4096);
            let mut rx = RxQueue::default();
            for _ in 0..128 {
                assert!(rx.push_charged(&[1; 64], &mut budget, peer));
            }
            let owed = budget.admission_debt();
            assert_eq!(owed.iter().sum::<u64>(), 4096);
            rx.clear();
            assert_eq!(debt.released(), 0, "RX descriptor/payload releases are not ACK-releasable TX drain");
            assert_eq!(global.reserved(), 0);
            drop(debt);
            assert_eq!(budget.admission_debt(), [0; 3]);
        }
    }

    #[test]
    fn rx_descriptor_legacy_mutation_is_rejected_before_removing_ooo_data() {
        let (_, mut budget) = budget(1 << 20);
        let mut rx = RxQueue::default();
        assert!(rx.push_charged(&[1], &mut budget, PeerId(1)));
        let mut ooo = OooQueue::default();
        ooo.insert(1, Bytes::from_static(b"x"));
        let before = (rx.len(), ooo.bytes());
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| ooo.pop_contiguous(1, &mut rx))).is_err());
        assert_eq!((rx.len(), ooo.bytes()), before);
        assert!(std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| rx.push(Bytes::from_static(b"x")))).is_err());
        assert_eq!(rx.len(), before.0);
    }
}
