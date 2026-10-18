//! Public ownership/budget contract, in its own allocator-instrumented binary.
//! A single test runs cases serially, so only one live allocation is tracked.

use bytes::Bytes;
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::task::{Wake, Waker};
use std::time::{Duration, Instant};
use zfstack::budget::{AllocationKind, Budget, GlobalBudget, MemoryHandle};
use zfstack::pktpool::{PacketBuf, PacketPool};
use zfstack::PeerId;

const BUFFER_BYTES: usize = 4096;
static TRACKED_POINTER: AtomicUsize = AtomicUsize::new(0);
static TRACKED_SIZE: AtomicUsize = AtomicUsize::new(0);
static TRACKED_ALIGN: AtomicUsize = AtomicUsize::new(0);
static TRACKED_GENERATION: AtomicUsize = AtomicUsize::new(0);
static NEXT_GENERATION: AtomicUsize = AtomicUsize::new(1);
static DEALLOC_GENERATION: AtomicUsize = AtomicUsize::new(0);

fn backing_freed() -> bool {
    let generation = TRACKED_GENERATION.load(Ordering::SeqCst);
    generation != 0 && DEALLOC_GENERATION.load(Ordering::SeqCst) == generation
}

struct ObservedSystem;

// SAFETY: every allocation operation delegates unchanged to System. The
// deallocation observer uses atomics only: no allocation, locking or panic.
unsafe impl GlobalAlloc for ObservedSystem {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        unsafe { System.alloc(layout) }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        unsafe { System.alloc_zeroed(layout) }
    }

    unsafe fn realloc(&self, pointer: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        unsafe { System.realloc(pointer, layout, new_size) }
    }

    unsafe fn dealloc(&self, pointer: *mut u8, layout: Layout) {
        let generation = TRACKED_GENERATION.load(Ordering::SeqCst);
        let tracked = generation != 0
            && TRACKED_POINTER.load(Ordering::SeqCst) == pointer as usize
            && TRACKED_SIZE.load(Ordering::SeqCst) == layout.size()
            && TRACKED_ALIGN.load(Ordering::SeqCst) == layout.align()
            && TRACKED_GENERATION.load(Ordering::SeqCst) == generation;
        unsafe { System.dealloc(pointer, layout) };
        if tracked {
            // A delayed deallocation of an address reused between rows must
            // neither mark the new row freed nor overwrite its newer result.
            DEALLOC_GENERATION.fetch_max(generation, Ordering::SeqCst);
        }
    }
}

#[global_allocator]
static ALLOCATOR: ObservedSystem = ObservedSystem;

struct TrackAllocation;

impl TrackAllocation {
    fn new(buffer: &mut PacketBuf) -> Self {
        assert_eq!(TRACKED_POINTER.load(Ordering::Acquire), 0);
        assert_eq!(buffer.len(), 0);
        let pointer = buffer.spare_capacity_mut().as_mut_ptr().cast::<u8>() as usize;
        TRACKED_SIZE.store(buffer.capacity(), Ordering::SeqCst);
        TRACKED_ALIGN.store(std::mem::align_of::<u8>(), Ordering::SeqCst);
        TRACKED_POINTER.store(pointer, Ordering::SeqCst);
        TRACKED_GENERATION.store(NEXT_GENERATION.fetch_add(1, Ordering::Relaxed), Ordering::SeqCst);
        Self
    }
}

impl Drop for TrackAllocation {
    fn drop(&mut self) {
        TRACKED_GENERATION.store(0, Ordering::SeqCst);
        TRACKED_POINTER.store(0, Ordering::SeqCst);
    }
}

enum Reenter {
    SamePool(PacketPool),
    // Retaining a pool here would invalidate the last-owner scenario.
    NewPool(MemoryHandle),
}

struct ReleaseWake {
    target: Reenter,
    get_count: usize,
    calls: AtomicUsize,
    early_wakes: AtomicUsize,
    callback_error: AtomicBool,
    acquired: Mutex<[Option<PacketBuf>; 2]>,
}

impl Wake for ReleaseWake {
    fn wake(self: Arc<Self>) {
        self.wake_by_ref();
    }

    fn wake_by_ref(self: &Arc<Self>) {
        // Snapshot before any replacement allocation can reuse the pointer.
        let freed_before_wake = backing_freed();
        self.calls.fetch_add(1, Ordering::Relaxed);
        if !freed_before_wake {
            self.early_wakes.fetch_add(1, Ordering::Relaxed);
        }
        let pool = match &self.target {
            Reenter::SamePool(pool) => pool.clone(),
            Reenter::NewPool(memory) => PacketPool::new(memory.clone(), BUFFER_BYTES, 0),
        };
        if let Ok(mut acquired) = self.acquired.lock() {
            for slot in acquired.iter_mut().take(self.get_count) {
                *slot = pool.get();
            }
        } else {
            self.callback_error.store(true, Ordering::Relaxed);
        }
        // Assertions belong to the test after the original Drop completes.
        // Keep replacements alive there instead of releasing recursively.
    }
}

#[derive(Clone, Copy, Debug)]
enum Lifecycle {
    NoCache,
    FullCache,
    ClonedSlice,
    ExplicitTrim,
    IdleTrim,
    LastOwner { max_cached: usize },
}

#[derive(Debug)]
struct Outcome {
    lifecycle: Lifecycle,
    calls: usize,
    early_wakes: usize,
    callback_error: bool,
    acquired: usize,
    expected_acquired: usize,
    backing_freed: bool,
}

fn assert_still_owned(global: &GlobalBudget, wake: &ReleaseWake, expected_bytes: u64) {
    assert_eq!(global.reserved(), expected_bytes);
    assert_eq!(wake.calls.load(Ordering::Relaxed), 0, "retaining an owner/cache must not release its budget");
    assert!(!backing_freed(), "retained packet backing was freed");
}

fn exercise(lifecycle: Lifecycle) -> Outcome {
    let max_cached = match lifecycle {
        Lifecycle::NoCache | Lifecycle::ClonedSlice => 0,
        Lifecycle::LastOwner { max_cached } => max_cached,
        _ => 1,
    };
    let buffers = if matches!(lifecycle, Lifecycle::FullCache) { 2 } else { 1 };
    let ceiling = (buffers * BUFFER_BYTES) as u64;
    let global = GlobalBudget::new(ceiling);
    let mut budget = Budget::new(global.clone());
    budget.set_limits(ceiling, ceiling, 1);
    let memory = budget.memory_handle(PeerId(23));
    let pool = PacketPool::new(memory.clone(), BUFFER_BYTES, max_cached);
    let mut packet = pool.get().unwrap();
    assert_eq!(packet.capacity(), BUFFER_BYTES);
    let _tracking = TrackAllocation::new(&mut packet);
    for (i, byte) in packet.spare_capacity_mut()[..16].iter_mut().enumerate() {
        byte.write(i as u8);
    }
    // SAFETY: the first sixteen bytes were initialized immediately above.
    unsafe { packet.set_len(16) };
    if matches!(lifecycle, Lifecycle::FullCache) {
        drop(pool.get().unwrap());
        assert_eq!(pool.cached(), 1);
    }
    assert_eq!(global.reserved(), ceiling);
    assert_eq!(budget.physical_used(), ceiling);
    assert_eq!(memory.peer_physical_used(), ceiling);
    assert!(memory.try_allocate_kind(BUFFER_BYTES as u64, AllocationKind::RxChunk).is_none(), "fixture must exhaust the physical quota");

    let target = if matches!(lifecycle, Lifecycle::LastOwner { .. }) { Reenter::NewPool(memory.clone()) } else { Reenter::SamePool(pool.clone()) };
    // With a full cache, the first get borrows the cached buffer; the second
    // must allocate using the released quota, while re-entering the same pool.
    let expected_acquired = buffers;
    let wake = Arc::new(ReleaseWake {
        target,
        get_count: expected_acquired,
        calls: AtomicUsize::new(0),
        early_wakes: AtomicUsize::new(0),
        callback_error: AtomicBool::new(false),
        acquired: Mutex::new([None, None]),
    });
    let waiter = global.new_waiter_id();
    global.register_physical_waiter(waiter, global.release_epoch(), &Waker::from(wake.clone()), &memory, BUFFER_BYTES as u64, AllocationKind::RxChunk);
    assert_eq!(budget.physical_waiters().rx_chunk, 1);
    assert_still_owned(&global, &wake, ceiling);
    let mut public_pool = Some(pool);
    match lifecycle {
        Lifecycle::NoCache | Lifecycle::FullCache => drop(packet),
        Lifecycle::ClonedSlice | Lifecycle::LastOwner { .. } => {
            let owner = Bytes::from_owner(packet);
            let clone = owner.clone();
            let first = owner.slice(1..9);
            let last = clone.slice(4..12);
            drop((owner, clone, first));
            if matches!(lifecycle, Lifecycle::LastOwner { .. }) {
                let second_public_handle = public_pool.as_ref().unwrap().clone();
                drop((public_pool.take(), second_public_handle));
            }
            assert_still_owned(&global, &wake, ceiling);
            assert_eq!(&last[..], &[4, 5, 6, 7, 8, 9, 10, 11]);
            // An external packet owner can release on a different thread.
            std::thread::spawn(move || drop(last)).join().unwrap();
        }
        Lifecycle::ExplicitTrim | Lifecycle::IdleTrim => {
            drop(packet);
            let pool = public_pool.as_ref().unwrap();
            assert_eq!((pool.cached(), pool.in_use()), (1, 0));
            assert_still_owned(&global, &wake, ceiling);
            if matches!(lifecycle, Lifecycle::ExplicitTrim) {
                assert_eq!(pool.trim_cached_to(0), 1);
            } else {
                let now = Instant::now();
                let idle = Duration::from_secs(5);
                assert_eq!(pool.trim_idle(now, idle, 0), 0);
                assert_eq!(pool.trim_idle(now + idle - Duration::from_nanos(1), idle, 0), 0);
                assert_still_owned(&global, &wake, ceiling);
                assert_eq!(pool.trim_idle(now + idle, idle, 0), 1);
            }
        }
    }

    let acquired = std::mem::replace(&mut *wake.acquired.lock().unwrap(), [None, None]);
    let outcome = Outcome {
        lifecycle,
        calls: wake.calls.load(Ordering::Relaxed),
        early_wakes: wake.early_wakes.load(Ordering::Relaxed),
        callback_error: wake.callback_error.load(Ordering::Relaxed),
        acquired: acquired.iter().flatten().count(),
        expected_acquired,
        backing_freed: backing_freed(),
    };
    global.remove_waiter(waiter);
    drop(acquired);
    if let Some(pool) = &public_pool {
        assert_eq!(pool.in_use(), 0);
        pool.trim_cached_to(0);
        assert_eq!(pool.cached(), 0);
    }
    drop((wake, public_pool));
    assert_eq!(global.reserved(), 0, "{lifecycle:?}: global charge leaked");
    assert_eq!(budget.physical_used(), 0, "{lifecycle:?}: port charge leaked");
    assert_eq!(memory.peer_physical_used(), 0, "{lifecycle:?}: peer charge leaked");
    assert_eq!(budget.physical_waiters().rx_chunk, 0);
    outcome
}

#[test]
fn packet_backing_is_freed_before_budget_wakes_across_owner_lifecycles() {
    // Bound regressions that accidentally put synchronous pool.get callbacks
    // under the pool mutex, without relying on the blocked thread's progress.
    let (send, receive) = std::sync::mpsc::sync_channel(1);
    let worker = std::thread::spawn(move || {
        let outcomes = [
            Lifecycle::NoCache,
            Lifecycle::FullCache,
            Lifecycle::ClonedSlice,
            Lifecycle::ExplicitTrim,
            Lifecycle::IdleTrim,
            Lifecycle::LastOwner { max_cached: 0 },
            Lifecycle::LastOwner { max_cached: 1 },
        ]
        .map(exercise);
        send.send(outcomes).unwrap();
    });
    let outcomes = receive.recv_timeout(Duration::from_secs(10)).expect("packet-pool callback deadlocked or scenario failed");
    worker.join().unwrap();
    for outcome in &outcomes {
        assert_eq!(outcome.calls, 1, "{outcome:?}");
        assert!(!outcome.callback_error, "{outcome:?}");
        assert_eq!(outcome.acquired, outcome.expected_acquired, "{outcome:?}: synchronous pool.get did not recover capacity");
        assert!(outcome.backing_freed, "{outcome:?}");
        assert_eq!(outcome.early_wakes, 0, "{:?}: budget wake preceded backing deallocation; all outcomes: {outcomes:?}", outcome.lifecycle);
    }
}
