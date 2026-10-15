//! Real std synchronization tests; the separate qa-models crate describes the
//! much narrower model-only evidence. No synchronization type is changed here.
use super::*;

#[test]
fn c13_waiter_burst_one_zero_burst_ledger_and_capacity() {
    const BURST: usize = 2048;
    let global = GlobalBudget::new(512);
    let mut budget = Budget::new(global.clone());
    budget.set_limits(512, 512, 8);
    let memory = budget.memory_handle(PeerId(1));
    for round in 0..3 {
        let held = memory.try_allocate(512).unwrap();
        let counter = Arc::new(CountWake(AtomicUsize::new(0)));
        let waker = Waker::from(counter.clone());
        let observed = global.release_epoch();
        let mut ids = Vec::new();
        for _ in 0..BURST {
            let physical = global.new_waiter_id();
            let logical = global.new_waiter_id();
            global.register_physical_waiter(physical, observed, &waker, &memory, 512, AllocationKind::Other);
            global.register_waiter(logical, observed, &waker);
            ids.push((physical, logical));
        }
        assert_eq!(global.physical_waiting.load(Ordering::SeqCst), BURST);
        assert_eq!(global.waiting.load(Ordering::Acquire), BURST);
        let peak_physical = global.physical_waiters.lock().unwrap().capacity();
        let peak_logical = global.waiters.lock().unwrap().capacity();
        for &(physical, logical) in &ids[..BURST - 1] {
            global.remove_waiter(physical);
            global.remove_waiter(logical);
        }
        assert_eq!(global.reserved(), 512);
        assert_eq!(global.physical_waiting.load(Ordering::SeqCst), 1);
        assert_eq!(global.waiting.load(Ordering::Acquire), 1);
        let sparse_physical = global.physical_waiters.lock().unwrap().capacity();
        let sparse_logical = global.waiters.lock().unwrap().capacity();
        // Remaining capacity is an observation, not a fabricated requirement
        // that the production implementation shrink while one waiter remains.
        eprintln!("C13 round={round} peak_waiters={BURST} sparse_waiters=1 physical_capacity={peak_physical}/{sparse_physical} logical_capacity={peak_logical}/{sparse_logical}");
        assert_eq!(counter.0.load(Ordering::Relaxed), 0);
        drop(held); // the only release; exactly the two remaining waiters run
        assert_eq!(counter.0.load(Ordering::Relaxed), 2);
        assert_eq!(global.physical_waiting.load(Ordering::SeqCst), 0);
        assert_eq!(global.waiting.load(Ordering::Acquire), 0);
        assert_eq!(global.physical_waiters.lock().unwrap().capacity(), 0);
        assert_eq!(global.waiters.lock().unwrap().capacity(), 0);
        assert_eq!(global.reserved(), 0);
        assert_eq!(memory.peer_physical_used(), 0);
        assert_eq!(Arc::strong_count(&counter), 2, "canceled entries must release waker owners");
    }
}

fn concurrent_budget(workers: usize) {
    const ROUNDS: usize = 64;
    let global = GlobalBudget::new(workers as u64 * 512);
    let barrier = Arc::new(std::sync::Barrier::new(workers));
    let (tx, rx) = std::sync::mpsc::channel();
    let mut threads = Vec::new();
    for _ in 0..workers {
        let global = global.clone();
        let barrier = barrier.clone();
        let tx = tx.clone();
        threads.push(std::thread::spawn(move || {
            let mut budget = Budget::new(global.clone());
            budget.set_limits(512, 256, 8);
            // Same IDs across independent shards must not alias port/peer state.
            let a = budget.memory_handle(PeerId(7));
            let b = budget.memory_handle(PeerId(8));
            for _ in 0..ROUNDS {
                let first = a.try_allocate(256).unwrap();
                let second = b.try_allocate(256).unwrap();
                assert!(global.reserved() <= global.high());
                barrier.wait();
                assert_eq!(global.reserved(), global.high());
                assert_eq!(budget.physical_used(), 512);
                // Repeated failed hierarchical reservations must roll back;
                // no phantom release/credit may become visible to a neighbor.
                let epoch = global.release_epoch();
                assert!(a.try_allocate(1).is_none());
                assert!(b.try_allocate(257).is_none());
                assert_eq!(global.release_epoch(), epoch);
                barrier.wait();
                drop(first);
                barrier.wait();
                assert_eq!(global.reserved(), workers as u64 * 256);
                assert_eq!(a.peer_physical_used(), 0);
                assert_eq!(b.peer_physical_used(), 256);
                barrier.wait();
                let replacement = a.try_allocate(256).unwrap();
                barrier.wait();
                assert_eq!(global.reserved(), global.high());
                barrier.wait();
                drop((replacement, second));
                barrier.wait();
                assert_eq!(global.reserved(), 0);
                barrier.wait();
            }
            tx.send(()).unwrap();
        }));
    }
    drop(tx);
    for _ in 0..workers {
        rx.recv_timeout(std::time::Duration::from_secs(10)).expect("native reserve/release watchdog expired");
    }
    for thread in threads {
        thread.join().unwrap();
    }
    assert_eq!(global.reserved(), 0);
    assert_eq!(global.connection_counts(), (0, 0));
}

#[test]
fn a1_shared_global_two_threads_reserve_rollback_release() {
    concurrent_budget(2);
}
#[test]
fn a1_shared_global_four_threads_reserve_rollback_release() {
    concurrent_budget(4);
}
