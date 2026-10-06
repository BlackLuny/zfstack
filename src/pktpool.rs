//! Budget-charged ingress packet buffers for zero-copy receive.
//!
//! A host that reads packets from a local device (a TUN) can read straight
//! into a [`PacketBuf`] from a [`PacketPool`] and hand it to
//! `Shard::ingress_buf`. Every buffer's full capacity is charged to the
//! stack's budget for as long as it exists, cached ones included, so the
//! stack may keep a large in-order payload as a slice of the buffer instead
//! of copying it (docs/design/0004 §2: owned ingress needs a verifiable
//! owner). The buffer returns to the pool when the last slice is dropped —
//! for a relay, when the bytes have been written upstream.
//!
//! Small payloads are still copied into compact chunks and their buffer goes
//! straight back, so a buffer is never pinned by a few bytes.

use crate::budget::{AllocationKind, MemoryHandle, MemoryLease};
use std::mem::MaybeUninit;
use std::ops::Deref;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

struct Inner {
    buf_size: usize,
    max_cached: usize,
    memory: MemoryHandle,
    free: Mutex<Vec<(Vec<u8>, MemoryLease)>>,
    in_use: AtomicUsize,
}

/// A bounded pool of equally sized, charged packet buffers. Cheap to clone;
/// usable from any thread (e.g. a dedicated TUN reader).
#[derive(Clone)]
pub struct PacketPool {
    inner: Arc<Inner>,
}

impl PacketPool {
    /// `memory` is the budget the buffers are charged to (`Shard::memory_handle`
    /// of the peer the packets belong to); `buf_size` must hold the largest
    /// packet read (the device MTU plus any device header); at most
    /// `max_cached` idle buffers stay allocated (and charged).
    pub fn new(memory: MemoryHandle, buf_size: usize, max_cached: usize) -> Self {
        PacketPool { inner: Arc::new(Inner { buf_size, max_cached, memory, free: Mutex::new(Vec::new()), in_use: AtomicUsize::new(0) }) }
    }

    pub fn buf_size(&self) -> usize {
        self.inner.buf_size
    }

    /// Idle buffers currently cached.
    pub fn cached(&self) -> usize {
        self.inner.free.lock().unwrap().len()
    }

    /// Buffers handed out and not yet back: being filled, queued for the
    /// stack, or holding payload the application has not consumed.
    pub fn in_use(&self) -> usize {
        self.inner.in_use.load(Ordering::Relaxed)
    }

    /// An empty buffer, or `None` when the budget has no room for another or
    /// the allocation failed (the caller should stop reading the device until
    /// memory is released). A buffer's capacity never exceeds its charge.
    pub fn get(&self) -> Option<PacketBuf> {
        let cached = self.inner.free.lock().unwrap().pop();
        let (data, lease) = match cached {
            Some((mut data, lease)) => {
                data.clear();
                (data, lease)
            }
            None => {
                let lease = self.inner.memory.try_allocate_kind(self.inner.buf_size as u64, AllocationKind::RxChunk)?;
                let mut data = Vec::new();
                data.try_reserve_exact(self.inner.buf_size).ok()?;
                // The lease covers `buf_size`; an allocation that came back
                // larger would be uncharged backing (as for the other
                // charged containers, refuse rather than under-count).
                if data.capacity() > self.inner.buf_size {
                    return None;
                }
                (data, lease)
            }
        };
        self.inner.in_use.fetch_add(1, Ordering::Relaxed);
        Some(PacketBuf { data, lease: Some(lease), pool: Some(self.inner.clone()) })
    }
}

/// One packet buffer: `len()` filled bytes of `capacity()`.
pub struct PacketBuf {
    data: Vec<u8>,
    lease: Option<MemoryLease>,
    pool: Option<Arc<Inner>>,
}

impl PacketBuf {
    pub fn capacity(&self) -> usize {
        self.data.capacity()
    }

    /// The unfilled tail to read into; then call [`Self::set_len`].
    pub fn spare_capacity_mut(&mut self) -> &mut [MaybeUninit<u8>] {
        self.data.spare_capacity_mut()
    }

    /// # Safety
    /// The first `len` bytes must have been initialized, e.g. by a `read`
    /// into [`Self::spare_capacity_mut`] that returned `len`.
    pub unsafe fn set_len(&mut self, len: usize) {
        debug_assert!(len <= self.data.capacity());
        self.data.set_len(len);
    }

    pub fn clear(&mut self) {
        self.data.clear();
    }
}

impl Deref for PacketBuf {
    type Target = [u8];
    fn deref(&self) -> &[u8] {
        &self.data
    }
}

impl AsRef<[u8]> for PacketBuf {
    fn as_ref(&self) -> &[u8] {
        &self.data
    }
}

impl Drop for PacketBuf {
    fn drop(&mut self) {
        let (Some(pool), Some(lease)) = (self.pool.take(), self.lease.take()) else { return };
        pool.in_use.fetch_sub(1, Ordering::Relaxed);
        let mut free = pool.free.lock().unwrap();
        if free.len() < pool.max_cached {
            free.push((std::mem::take(&mut self.data), lease));
        }
        // Otherwise the buffer and its lease are released here.
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::{Budget, GlobalBudget};
    use crate::PeerId;

    #[test]
    fn buffers_stay_charged_while_cached_and_release_beyond_the_cap() {
        let global = GlobalBudget::new(1 << 20);
        let mut budget = Budget::new(global.clone());
        let pool = PacketPool::new(budget.memory_handle(PeerId(1)), 4096, 2);
        let mut bufs: Vec<_> = (0..3).map(|_| pool.get().unwrap()).collect();
        assert_eq!(global.reserved(), 3 * 4096);
        let b = &mut bufs[2];
        b.spare_capacity_mut()[0].write(7);
        unsafe { b.set_len(1) };
        assert_eq!(&b[..], &[7]);
        let owner = bytes::Bytes::from_owner(bufs.pop().unwrap());
        // (An empty slice would not reference the owner.)
        let slice = owner.slice(0..1);
        drop(owner);
        drop(bufs);
        assert_eq!(pool.cached(), 2);
        assert_eq!(pool.in_use(), 1);
        assert_eq!(global.reserved(), 3 * 4096, "the slice still holds one buffer");
        drop(slice);
        assert_eq!(pool.cached(), 2);
        assert_eq!(global.reserved(), 2 * 4096, "beyond the cap a buffer is released");
        let again = pool.get().unwrap();
        assert_eq!(again.len(), 0);
        assert_eq!(pool.cached(), 1);
        drop(again);
        drop(pool);
        assert_eq!(global.reserved(), 0);
        let _ = &mut budget;
    }
}
