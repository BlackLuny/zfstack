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
use std::time::{Duration, Instant};

#[derive(Default)]
struct FreeCache {
    buffers: Vec<(Vec<u8>, MemoryLease)>,
    idle: Option<IdleWindow>,
}

struct IdleWindow {
    since: Instant,
    // The LIFO prefix never borrowed during this observation window.
    untouched: usize,
}

impl FreeCache {
    fn note_tail_removal(&mut self) {
        if let Some(idle) = &mut self.idle {
            idle.untouched = idle.untouched.min(self.buffers.len());
        }
    }
}

struct Inner {
    buf_size: usize,
    max_cached: usize,
    memory: MemoryHandle,
    free: Mutex<FreeCache>,
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
        PacketPool { inner: Arc::new(Inner { buf_size, max_cached, memory, free: Mutex::new(FreeCache::default()), in_use: AtomicUsize::new(0) }) }
    }

    pub fn buf_size(&self) -> usize {
        self.inner.buf_size
    }

    /// Idle buffers currently cached.
    pub fn cached(&self) -> usize {
        self.inner.free.lock().unwrap().buffers.len()
    }

    /// Buffers handed out and not yet back: being filled, queued for the
    /// stack, or holding payload the application has not consumed.
    pub fn in_use(&self) -> usize {
        self.inner.in_use.load(Ordering::Relaxed)
    }

    /// Release only free packet buffers; payloads and leases drop outside the lock.
    /// Concurrent returns may refill the cache. Nonzero targets retain Vec capacity.
    /// Zero also frees free-list backing, which has no lease in the existing model.
    pub fn trim_cached_to(&self, target: usize) -> usize {
        if target == 0 {
            let retired = {
                let mut free = self.inner.free.lock().unwrap();
                let retired = std::mem::take(&mut free.buffers);
                free.note_tail_removal();
                retired
            };
            let removed = retired.len();
            drop(retired);
            return removed;
        }
        let mut remaining = self.cached().saturating_sub(target);
        let mut removed = 0;
        while remaining != 0 {
            let mut retired: [Option<(Vec<u8>, MemoryLease)>; 32] = std::array::from_fn(|_| None);
            let count = {
                let mut free = self.inner.free.lock().unwrap();
                let count = remaining.min(free.buffers.len().saturating_sub(target)).min(retired.len());
                for slot in &mut retired[..count] {
                    *slot = free.buffers.pop();
                }
                free.note_tail_removal();
                count
            };
            if count == 0 {
                break;
            }
            remaining -= count;
            removed += count;
            drop(retired);
        }
        removed
    }

    /// Release historical free buffers that were never borrowed during an
    /// observation window of at least `idle`. Keep at least `target` free
    /// buffers and all free buffers reused within the window. With a zero
    /// target, low-rate traffic still retains its recently reused free buffers.
    ///
    /// The first call opts this shared pool into tracking and starts a window.
    /// A single host maintenance loop should call this periodically, including
    /// with no traffic, using a monotonic clock and stable settings. No task or
    /// timer is started here. Late returns age through the next full window;
    /// reclamation can therefore take two windows plus scheduling delay.
    /// `idle == Duration::ZERO` observes the interval between calls.
    ///
    /// Tracking adds a minimum update under the existing get mutex, but no
    /// per-packet clocks, atomics or additional locks. Destruction and budget
    /// wakeups occur outside the mutex, in batches of at most 32 buffers.
    /// Concurrent explicit trims and returns are safe; borrowed buffers are
    /// never reclaimed. Empty free-list backing is also released.
    pub fn trim_idle(&self, now: Instant, idle: Duration, target: usize) -> usize {
        let mut removed = 0;
        loop {
            let mut retired: [Option<(Vec<u8>, MemoryLease)>; 32] = std::array::from_fn(|_| None);
            let (done, backing) = {
                let mut free = self.inner.free.lock().unwrap();
                let len = free.buffers.len();
                let Some(window) = free.idle.as_mut() else {
                    free.idle = Some(IdleWindow { since: now, untouched: len });
                    return removed;
                };
                if now.saturating_duration_since(window.since) < idle {
                    return removed;
                }
                let count = window.untouched.min(len.saturating_sub(target)).min(retired.len());
                for (slot, buffer) in retired.iter_mut().zip(free.buffers.drain(..count)) {
                    *slot = Some(buffer);
                }
                let remaining = free.buffers.len();
                let window = free.idle.as_mut().unwrap();
                window.untouched -= count;
                let done = window.untouched == 0 || remaining <= target;
                if done {
                    *window = IdleWindow { since: now, untouched: remaining };
                }
                let backing = (remaining == 0).then(|| std::mem::take(&mut free.buffers));
                removed += count;
                (done, backing)
            };
            drop(retired);
            drop(backing);
            if done {
                return removed;
            }
        }
    }

    /// An empty buffer, or `None` when the budget has no room for another or
    /// the allocation failed (the caller should stop reading the device until
    /// memory is released). A buffer's capacity never exceeds its charge.
    pub fn get(&self) -> Option<PacketBuf> {
        let cached = {
            let mut free = self.inner.free.lock().unwrap();
            let cached = free.buffers.pop();
            free.note_tail_removal();
            cached
        };
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
        if free.buffers.len() < pool.max_cached {
            free.buffers.push((std::mem::take(&mut self.data), lease));
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

#[cfg(test)]
mod idle_trim_tests {
    use super::*;
    use crate::{
        budget::{Budget, GlobalBudget},
        PeerId,
    };

    #[test]
    fn low_rate_reclaims_history_but_preserves_hot_tail_and_budget() {
        for target in [0, 2] {
            let g = GlobalBudget::new(1 << 20);
            let mut b = Budget::new(g.clone());
            b.set_limits(1 << 20, 1 << 20, u32::MAX);
            let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 128);
            drop((0..128).map(|_| p.get().unwrap()).collect::<Vec<_>>());
            let start = Instant::now();
            let idle = Duration::from_secs(10);
            assert_eq!(p.trim_idle(start, idle, target), 0);
            // The same hot buffer must survive, even with a zero target.
            let hot = p.get().unwrap();
            let address = hot.data.as_ptr();
            drop(hot);
            for second in 1..=30 {
                let hot = p.get().unwrap();
                assert_eq!(hot.data.as_ptr(), address);
                drop(hot);
                p.trim_idle(start + Duration::from_secs(second), idle, target);
                assert_eq!(p.cached(), if second < 10 { 128 } else { target.max(1) });
                assert_eq!(g.reserved(), p.cached() as u64 * 4096);
            }
            assert_eq!(p.in_use(), 0);
            p.trim_cached_to(0);
            assert_eq!(g.reserved(), 0);
            assert_eq!(b.physical_used(), 0);
        }
    }

    #[test]
    fn full_reuse_above_floor_does_not_churn_and_late_returns_age() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 8);
        drop((0..8).map(|_| p.get().unwrap()).collect::<Vec<_>>());
        let start = Instant::now();
        let idle = Duration::from_secs(10);
        p.trim_idle(start, idle, 2);
        for cycle in 1..=3 {
            drop((0..8).map(|_| p.get().unwrap()).collect::<Vec<_>>());
            assert_eq!(p.trim_idle(start + idle * cycle, idle, 2), 0);
            assert_eq!(p.cached(), 8);
        }
        let held = (0..8).map(|_| p.get().unwrap()).collect::<Vec<_>>();
        assert_eq!(p.trim_idle(start + idle * 4, idle, 0), 0);
        assert_eq!(p.in_use(), 8);
        assert_eq!(g.reserved(), 8 * 4096);
        drop(held);
        assert_eq!(p.trim_idle(start + idle * 5, idle, 0), 0, "late returns need a full window");
        assert_eq!(p.trim_idle(start + idle * 6, idle, 0), 8);
        assert_eq!(p.inner.free.lock().unwrap().buffers.capacity(), 0);
        assert_eq!(g.reserved(), 0);
    }

    #[test]
    fn explicit_tail_trim_does_not_age_new_returns_or_disable_tracking() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 8);
        drop((0..8).map(|_| p.get().unwrap()).collect::<Vec<_>>());
        assert!(p.inner.free.lock().unwrap().idle.is_none(), "tracking is opt-in");
        let start = Instant::now();
        let idle = Duration::from_secs(10);
        p.trim_idle(start, idle, 0);
        assert_eq!(p.trim_cached_to(2), 6);
        let held = (0..4).map(|_| p.get().unwrap()).collect::<Vec<_>>();
        drop(held);
        assert_eq!(p.trim_idle(start + idle, idle, 0), 0);
        assert_eq!(p.trim_cached_to(0), 4);
        drop(p.get().unwrap());
        assert_eq!(p.trim_idle(start + idle * 2, idle, 0), 0);
        assert_eq!(p.trim_idle(start + idle * 3, idle, 0), 1);
        assert_eq!(g.reserved(), 0);
    }

    #[test]
    fn zero_window_observes_between_ticks_and_target_is_a_floor() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 4);
        drop((0..4).map(|_| p.get().unwrap()).collect::<Vec<_>>());
        let now = Instant::now();
        assert_eq!(p.trim_idle(now, Duration::ZERO, 0), 0);
        drop(p.get().unwrap());
        assert_eq!(p.trim_idle(now, Duration::ZERO, 0), 3);
        assert_eq!(p.trim_idle(now, Duration::ZERO, usize::MAX), 0);
        assert_eq!(p.trim_idle(now, Duration::ZERO, 0), 1);
        assert_eq!(g.reserved(), 0);
    }

    #[test]
    fn idle_trim_wakes_outside_mutex_across_retirement_batches() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        b.set_limits(1 << 20, 1 << 20, u32::MAX);
        let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 128);
        drop((0..128).map(|_| p.get().unwrap()).collect::<Vec<_>>());
        let now = Instant::now();
        p.trim_idle(now, Duration::from_secs(1), 0);
        let wake = Arc::new(UnlockWake { pool: p.clone(), calls: AtomicUsize::new(0) });
        let w = std::task::Waker::from(wake.clone());
        let waiter = g.new_waiter_id();
        g.register_waiter(waiter, g.release_epoch(), &w);
        assert_eq!(p.trim_idle(now + Duration::from_secs(1), Duration::from_secs(1), 0), 128);
        assert_eq!(wake.calls.load(Ordering::Relaxed), 1);
        g.remove_waiter(waiter);
        assert_eq!(g.reserved(), 0);
    }
    #[test]
    fn free_only_trim_and_empty_backing() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 5);
        let mut v: Vec<_> = (0..6).map(|_| p.get().unwrap()).collect();
        let held = v.pop().unwrap();
        drop(v);
        assert_eq!((p.cached(), p.in_use()), (5, 1));
        assert_eq!(p.trim_cached_to(2), 3);
        assert_eq!((p.cached(), p.in_use()), (2, 1));
        assert_eq!(g.reserved(), 3 * 4096);
        assert_eq!(p.trim_cached_to(usize::MAX), 0);
        let a = p.get().unwrap();
        assert_eq!(a.len(), 0);
        assert_eq!(p.trim_cached_to(0), 1);
        assert_eq!(p.in_use(), 2);
        assert_eq!(g.reserved(), 8192);
        drop(a);
        drop(held);
        assert_eq!(p.trim_cached_to(0), 2);
        assert_eq!(p.trim_cached_to(0), 0);
        assert_eq!(p.inner.free.lock().unwrap().buffers.capacity(), 0);
        assert_eq!(g.reserved(), 0);
        assert_eq!(b.physical_used(), 0);
    }
    #[test]
    fn empty_retained_vec_and_live_owner_are_distinct() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 2);
        drop(p.get().unwrap());
        let held = p.get().unwrap();
        assert!(p.inner.free.lock().unwrap().buffers.capacity() > 0);
        assert_eq!(p.trim_cached_to(0), 0);
        assert_eq!(p.inner.free.lock().unwrap().buffers.capacity(), 0);
        assert_eq!(g.reserved(), 4096);
        drop(held);
        p.trim_cached_to(0);
        assert_eq!(g.reserved(), 0);
    }
    #[test]
    fn clones_and_late_slices_survive_trim() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 3);
        let observer = p.clone();
        let mut held = p.get().unwrap();
        for (i, s) in held.spare_capacity_mut()[..8].iter_mut().enumerate() {
            s.write(i as u8);
        }
        // SAFETY: the eight bytes above were initialized.
        unsafe { held.set_len(8) };
        let idle: Vec<_> = (0..2).map(|_| p.get().unwrap()).collect();
        drop(idle);
        let owner = bytes::Bytes::from_owner(held);
        let clone = owner.clone();
        let a = owner.slice(1..7);
        let c = clone.slice(2..5);
        drop(owner);
        drop(clone);
        drop(p);
        let t = observer.clone();
        assert_eq!(std::thread::spawn(move || t.trim_cached_to(0)).join().unwrap(), 2);
        assert_eq!(observer.in_use(), 1);
        assert_eq!(g.reserved(), 4096);
        assert_eq!(&a[..], &[1, 2, 3, 4, 5, 6]);
        assert_eq!(&c[..], &[2, 3, 4]);
        drop(a);
        assert_eq!(observer.in_use(), 1);
        drop(c);
        assert_eq!((observer.cached(), observer.in_use()), (1, 0));
        assert_eq!(observer.trim_cached_to(0), 1);
        assert_eq!(g.reserved(), 0);
        let mut held = observer.get().unwrap();
        held.spare_capacity_mut()[0].write(9);
        // SAFETY: the first byte was initialized.
        unsafe { held.set_len(1) };
        let owner = bytes::Bytes::from_owner(held);
        let slice = owner.slice(..);
        drop(owner);
        drop(observer);
        assert_eq!(g.reserved(), 4096);
        assert_eq!(&slice[..], &[9]);
        drop(slice);
        assert_eq!(g.reserved(), 0);
        assert_eq!(b.physical_used(), 0);
    }
    struct UnlockWake {
        pool: PacketPool,
        calls: AtomicUsize,
    }
    impl std::task::Wake for UnlockWake {
        fn wake(self: Arc<Self>) {
            self.check();
        }
        fn wake_by_ref(self: &Arc<Self>) {
            self.check();
        }
    }
    impl UnlockWake {
        fn check(&self) {
            assert!(self.pool.inner.free.try_lock().is_ok(), "wake under pool mutex");
            self.calls.fetch_add(1, Ordering::Relaxed);
        }
    }
    #[test]
    fn lease_wakeup_is_outside_pool_lock() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        let m = b.memory_handle(PeerId(1));
        let p = PacketPool::new(m.clone(), 4096, 4);
        let v: Vec<_> = (0..4).map(|_| p.get().unwrap()).collect();
        drop(v);
        let wake = Arc::new(UnlockWake { pool: p.clone(), calls: AtomicUsize::new(0) });
        let w = std::task::Waker::from(wake.clone());
        let a = g.new_waiter_id();
        g.register_waiter(a, g.release_epoch(), &w);
        assert_eq!(p.trim_cached_to(2), 2);
        assert_eq!(wake.calls.load(Ordering::Relaxed), 1);
        let c = g.new_waiter_id();
        g.register_waiter(c, g.release_epoch(), &w);
        assert_eq!(p.trim_cached_to(0), 2);
        assert_eq!(wake.calls.load(Ordering::Relaxed), 2);
        drop(m.try_allocate_kind(4096, AllocationKind::RxChunk).unwrap());
        g.remove_waiter(a);
        g.remove_waiter(c);
        assert_eq!(g.reserved(), 0);
    }
    #[test]
    fn concurrent_trim_get_drop_and_bytes_validation() {
        let g = GlobalBudget::new(1 << 20);
        let mut b = Budget::new(g.clone());
        let p = PacketPool::new(b.memory_handle(PeerId(1)), 4096, 8);
        let barrier = Arc::new(std::sync::Barrier::new(7));
        let mut threads = Vec::new();
        for worker in 0..4usize {
            let p = p.clone();
            let barrier = barrier.clone();
            threads.push(std::thread::spawn(move || {
                barrier.wait();
                for i in 0..256usize {
                    let v = (worker as u8).wrapping_add(i as u8);
                    let mut held = p.get().unwrap();
                    for s in &mut held.spare_capacity_mut()[..64] {
                        s.write(v);
                    }
                    // SAFETY: all 64 bytes were initialized.
                    unsafe { held.set_len(64) };
                    let owner = bytes::Bytes::from_owner(held);
                    let c = owner.clone();
                    let s = owner.slice(1..63);
                    drop(owner);
                    assert!(s.iter().all(|&b| b == v));
                    drop(c);
                    assert!(s.iter().all(|&b| b == v));
                    drop(s);
                }
            }));
        }
        for n in 0..2 {
            let p = p.clone();
            let barrier = barrier.clone();
            threads.push(std::thread::spawn(move || {
                barrier.wait();
                for i in 0..512 {
                    if n == 0 {
                        p.trim_idle(Instant::now(), Duration::ZERO, i % 4);
                    } else {
                        p.trim_cached_to((i + n) % 4);
                    }
                }
            }));
        }
        barrier.wait();
        for t in threads {
            t.join().unwrap();
        }
        assert_eq!(p.in_use(), 0);
        assert!(p.cached() <= 8);
        assert_eq!(g.reserved(), p.cached() as u64 * 4096);
        assert_eq!(b.physical_used(), g.reserved());
        p.trim_cached_to(0);
        assert_eq!(p.inner.free.lock().unwrap().buffers.capacity(), 0);
        assert_eq!(g.reserved(), 0);
    }
}
